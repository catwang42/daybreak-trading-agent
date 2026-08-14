import pytest
from pydantic import BaseModel

from tradingagent.llm import LLMGateway, SchemaViolation, TokenLedger, _parse_schema


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
