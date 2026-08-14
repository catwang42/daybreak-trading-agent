import json
from typing import Literal, get_args, get_origin

import pytest
from pydantic import BaseModel, Field

from tradingagent.discovery.shortlist import QuickTake
from tradingagent.llm import (
    _CHARS_PER_TOKEN,
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
    """Gateway whose transport returns `replies` in order."""
    gw = LLMGateway.__new__(LLMGateway)
    gw.settings = FakeSettings()
    gw.ledger = TokenLedger()
    gw.calls = []

    def fake_call(prompt, *, tier, system, max_tokens, temperature):
        gw.calls.append(prompt)
        return replies.pop(0)

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
    assert token_budget(schema) * _CHARS_PER_TOKEN >= longest, (
        f"{schema.__name__}: budget holds "
        f"{int(token_budget(schema) * _CHARS_PER_TOKEN)} chars, maximal reply is {longest}"
    )


def test_token_budget_tracks_a_cap_change():
    """The budget is derived, not copied — no call site to update by hand."""

    class Narrow(BaseModel):
        text: str = Field(max_length=200)

    class Wide(BaseModel):
        text: str = Field(max_length=8000)

    assert token_budget(Wide) > token_budget(Narrow)
    assert token_budget(Narrow) == 600  # floor: schemas this small still need room


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
