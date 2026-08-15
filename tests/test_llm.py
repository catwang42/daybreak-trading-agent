import json
from typing import Literal, get_args, get_origin

import pytest
from pydantic import BaseModel, Field

from tradingagent.discovery.shortlist import QuickTake
from tradingagent.llm import (
    _CHARS_PER_TOKEN,
    _REASONING_ALLOWANCE,
    EmptyCompletion,
    LLMError,
    LLMGateway,
    SchemaViolation,
    TokenLedger,
    _parse_schema,
    token_budget,
)
from tradingagent.pipeline.schemas import (
    AnalystReport,
    DebateTurn,
    PortfolioDecision,
    ResearchPlan,
    RiskTake,
    TraderProposal,
)


class Reply(BaseModel):
    rating: str
    score: int


class FakeSettings:
    fast_model = "provider/fast"
    smart_model = "provider/smart"
    deep_model = "provider/deep"
    llm_max_retries = 2
    llm_timeout = 10


def gateway(replies):
    """Gateway whose transport returns `replies` in order.

    A reply that is an exception is raised rather than returned, so a provider
    that answers with nothing can be scripted alongside one that answers badly.
    """
    gw = LLMGateway.__new__(LLMGateway)
    gw.settings = FakeSettings()
    gw.ledger = TokenLedger()
    gw.calls = []

    def fake_call(prompt, *, tier, system, max_tokens, temperature):
        gw.calls.append(prompt)
        reply = replies.pop(0)
        if isinstance(reply, Exception):
            gw.ledger.record_failure(tier)
            raise reply
        return reply

    gw._call = fake_call
    return gw


def test_tier_selection_uses_env_model_strings():
    gw = gateway([])
    assert gw.model_for("fast") == "provider/fast"
    assert gw.model_for("smart") == "provider/smart"
    assert gw.model_for("deep") == "provider/deep"


@pytest.mark.parametrize(
    "raw",
    [
        '{"rating":"Buy","score":7}',
        '```json\n{"rating":"Buy","score":7}\n```',
        'Sure! {"rating":"Buy","score":7} Hope that helps.',
    ],
)
def test_parse_schema_tolerates_fences_and_prose(raw):
    parsed = _parse_schema(raw, Reply)
    assert parsed.rating == "Buy" and parsed.score == 7


def test_schema_violation_triggers_exactly_one_reprompt():
    gw = gateway(["not json at all", '{"rating":"Hold","score":3}'])
    result = gw.complete("go", tier="fast", schema=Reply)
    assert result.rating == "Hold"
    assert len(gw.calls) == 2
    assert "was rejected" in gw.calls[1]


def test_an_empty_reply_buys_the_same_one_reprompt_as_a_malformed_one():
    """An empty completion is malformed output, not a dead provider.

    The request was accepted and billed; CLAUDE.md's rule for output that does
    not match the schema is one re-prompt before DEGRADED, and a reply with
    nothing in it fails the schema as surely as a truncated one. Seen live: the
    options strategist lost a ticker to a single empty reply.
    """
    gw = gateway([EmptyCompletion("empty"), '{"rating":"Buy","score":9}'])
    result = gw.complete("go", tier="smart", schema=Reply)
    assert result.score == 9
    assert len(gw.calls) == 2
    assert "previous reply was empty" in gw.calls[1]


def test_two_empty_replies_surface_to_the_caller_rather_than_looping():
    gw = gateway([EmptyCompletion("empty"), EmptyCompletion("empty again")])
    with pytest.raises(LLMError):
        gw.complete("go", tier="smart", schema=Reply)
    assert len(gw.calls) == 2


def test_a_transport_failure_is_not_re_prompted_as_if_it_were_bad_output():
    """A dead provider must not be billed twice for the same call."""
    gw = gateway([LLMError("provider is down")])
    with pytest.raises(LLMError):
        gw.complete("go", tier="smart", schema=Reply)
    assert len(gw.calls) == 1


def test_second_violation_raises_rather_than_looping():
    gw = gateway(["nope", "still nope"])
    with pytest.raises(SchemaViolation):
        gw.complete("go", tier="fast", schema=Reply)
    assert len(gw.calls) == 2
    assert gw.ledger.by_tier["fast"].failures == 1


ROLE_SCHEMAS = [
    QuickTake,
    AnalystReport,
    DebateTurn,
    ResearchPlan,
    TraderProposal,
    RiskTake,
    PortfolioDecision,
]


def _maximal(schema):
    """The longest instance ``schema`` would still accept."""

    payload = {}
    for name, info in schema.model_fields.items():
        annotation = info.annotation
        cap = next(
            (m.max_length for m in info.metadata if hasattr(m, "max_length")), None
        )
        floor = next((m.ge for m in info.metadata if hasattr(m, "ge")), None)
        if get_origin(annotation) is list:
            payload[name] = ["y" * 250] * (cap or 3)
        elif get_origin(annotation) is Literal:
            payload[name] = get_args(annotation)[0]
        elif annotation is str or str in get_args(annotation):
            payload[name] = "y" * (cap or 400)
        else:
            payload[name] = floor if floor is not None else 1
    return payload


@pytest.mark.parametrize("schema", ROLE_SCHEMAS, ids=lambda s: s.__name__)
def test_token_budget_covers_a_maximal_valid_instance(schema):
    """Caps and completion budget are one constraint; they must move together.

    Raising the character caps without raising ``max_tokens`` truncates the
    reply mid-string, which surfaces as `json_invalid` — a re-prompt, and
    sometimes a lost role — for output the schema would have accepted.
    """
    payload = _maximal(schema)
    schema.model_validate(payload)  # the longest reply is a legal reply
    longest = len(json.dumps(payload))
    # The reasoning allowance is for thinking, not prose — the prose share alone
    # has to hold the reply, or the allowance is quietly covering a cap overrun.
    prose = (token_budget(schema) - _REASONING_ALLOWANCE) * _CHARS_PER_TOKEN
    assert prose >= longest, (
        f"{schema.__name__}: budget holds {int(prose)} chars, maximal reply is {longest}"
    )


def test_token_budget_tracks_a_cap_change():
    """The budget is derived, not copied — no call site to update by hand."""

    class Narrow(BaseModel):
        text: str = Field(max_length=200)

    class Wide(BaseModel):
        text: str = Field(max_length=8000)

    assert token_budget(Wide) > token_budget(Narrow)
    # floor: schemas this small still need room, plus the thinking allowance
    assert token_budget(Narrow) == 600 + _REASONING_ALLOWANCE


def test_every_budget_leaves_room_to_think_before_the_first_brace():
    """A ceiling sized to the JSON alone returns an empty reply, not a short one.

    Measured against Sonnet-class on Vertex: the options strategist spent ~2,400
    tokens reasoning before it wrote a brace, and at a JSON-sized ceiling the
    provider returned `finish_reason=length` with no content at all.
    """
    for schema in ROLE_SCHEMAS:
        assert token_budget(schema) >= _REASONING_ALLOWANCE + 600


def test_schema_call_defaults_its_budget_from_the_schema():
    gw = gateway([])
    seen = {}

    def fake_call(prompt, *, tier, system, max_tokens, temperature):
        seen["max_tokens"] = max_tokens
        return json.dumps(_maximal(PortfolioDecision))

    gw._call = fake_call
    gw.complete("go", tier="deep", schema=PortfolioDecision)
    assert seen["max_tokens"] == token_budget(PortfolioDecision)


def test_ledger_totals_across_tiers():
    ledger = TokenLedger()
    ledger.record("fast", "m1", 100, 50, cost_usd=0.001)
    ledger.record("fast", "m1", 10, 5, cost_usd=0.0002)
    ledger.record("smart", "m2", 200, 100, cost_usd=0.02)
    assert ledger.by_tier["fast"].calls == 2
    assert ledger.by_tier["fast"].total_tokens == 165
    assert ledger.total_tokens == 465
    assert ledger.total_calls == 3
    assert round(ledger.total_cost_usd, 4) == 0.0212


def test_unescaped_newlines_in_prose_parse_without_a_reprompt():
    """The models write multi-paragraph prose into a JSON string field often
    enough that re-prompting over the escaping alone is a real surcharge."""
    body = '{"rating": "Buy", "score": 7}'.replace("Buy", "Buy\nand hold\there")
    gw = gateway([body])
    result = gw.complete("go", tier="fast", schema=Reply)
    assert result.rating == "Buy\nand hold\there"
    assert len(gw.calls) == 1  # no repair round trip


def test_a_genuinely_broken_reply_still_gets_its_one_reprompt():
    gw = gateway(['{"rating": "Buy", "score":', '{"rating":"Hold","score":3}'])
    assert gw.complete("go", tier="fast", schema=Reply).rating == "Hold"
    assert len(gw.calls) == 2


def test_violation_digest_separates_truncation_from_bad_escaping():
    from tradingagent.llm import _violation_digest

    def digest(text):
        try:
            Reply.model_validate_json(text)
        except Exception as exc:
            return _violation_digest(exc)
        raise AssertionError("expected a validation error")

    assert "EOF while parsing" in digest('{"rating": "Buy", "score":')
    assert "control character" in digest('{"rating": "Bu\ny", "score": 7}')
