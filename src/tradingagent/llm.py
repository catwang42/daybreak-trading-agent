"""Single LLM gateway for the whole app (LiteLLM). THE ONLY FILE THAT CALLS AN LLM.

Design rules (CLAUDE.md):
- Provider-agnostic. Model identifiers are opaque strings from the environment
  (``LLM_FAST_MODEL`` / ``LLM_SMART_MODEL`` / ``LLM_DEEP_MODEL``). Nothing in
  this module branches on a vendor name; swapping ``vertex_ai/...`` for
  ``anthropic/...``, ``gemini/...`` or ``ollama/...`` needs no code change.
- Three cost tiers: ``fast`` (analysts + summaries), ``smart`` (manager, judge)
  and ``deep`` (reserved for the M2 portfolio manager).
- Retries via tenacity, per-run token accounting, and one automatic re-prompt
  when structured output violates its schema (then the caller marks DEGRADED).

Auth is whatever the provider expects from the ambient environment. For Vertex
AI that is Application Default Credentials plus ``VERTEXAI_PROJECT`` /
``VERTEXAI_LOCATION``; no key material is ever read or logged here.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Literal, TypeVar, get_args, get_origin

from pydantic import BaseModel, ValidationError
from tenacity import (
    RetryError,
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from .config import Settings, get_settings

log = logging.getLogger(__name__)

Tier = Literal["fast", "smart", "deep"]
ModelT = TypeVar("ModelT", bound=BaseModel)

# Silence LiteLLM's chatty startup/debug output; it also prevents any chance of
# request payloads landing in logs.
os.environ.setdefault("LITELLM_LOG", "ERROR")


class LLMError(RuntimeError):
    """A completion could not be obtained after retries."""


class SchemaViolation(LLMError):
    """Model output did not match the requested schema, twice."""


class EmptyCompletion(LLMError):
    """The provider answered, but with nothing in it.

    Separate from :class:`LLMError` because it is a malformed *reply*, not a
    failed call: the request was accepted, billed and accounted for. CLAUDE.md's
    rule for malformed output is one re-prompt before DEGRADED, so the schema
    path treats this like a validation error rather than a dead provider.
    """


@dataclass
class Usage:
    """Token counters for one tier."""

    calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cached_tokens: int = 0
    cost_usd: float = 0.0
    failures: int = 0

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


@dataclass
class TokenLedger:
    """Per-run token accounting, rendered into the report footer."""

    by_tier: dict[str, Usage] = field(default_factory=dict)
    by_model: dict[str, str] = field(default_factory=dict)
    started_at: float = field(default_factory=time.monotonic)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def record(
        self,
        tier: str,
        model: str,
        prompt_tokens: int,
        completion_tokens: int,
        cached_tokens: int = 0,
        cost_usd: float = 0.0,
    ) -> None:
        with self._lock:
            usage = self.by_tier.setdefault(tier, Usage())
            usage.calls += 1
            usage.prompt_tokens += prompt_tokens
            usage.completion_tokens += completion_tokens
            usage.cached_tokens += cached_tokens
            usage.cost_usd += cost_usd
            self.by_model[tier] = model

    def record_failure(self, tier: str) -> None:
        with self._lock:
            self.by_tier.setdefault(tier, Usage()).failures += 1

    @property
    def total_tokens(self) -> int:
        return sum(u.total_tokens for u in self.by_tier.values())

    @property
    def total_calls(self) -> int:
        return sum(u.calls for u in self.by_tier.values())

    @property
    def total_cost_usd(self) -> float:
        return sum(u.cost_usd for u in self.by_tier.values())

    def elapsed_seconds(self) -> float:
        return time.monotonic() - self.started_at


_LEDGER = TokenLedger()


def ledger() -> TokenLedger:
    """The process-wide ledger for the current run."""
    return _LEDGER


def reset_ledger() -> TokenLedger:
    global _LEDGER
    _LEDGER = TokenLedger()
    return _LEDGER


class LLMGateway:
    """Thin, retrying, accounting wrapper around ``litellm.completion``."""

    def __init__(self, settings: Settings | None = None, token_ledger: TokenLedger | None = None):
        self.settings = settings or get_settings()
        self.ledger = token_ledger or ledger()

    def model_for(self, tier: Tier) -> str:
        model = {
            "fast": self.settings.fast_model,
            "smart": self.settings.smart_model,
            "deep": self.settings.deep_model,
        }[tier]
        if not model:
            raise LLMError(
                f"No model configured for tier '{tier}'. Set LLM_{tier.upper()}_MODEL in config/.env."
            )
        return model

    # -- core ------------------------------------------------------------
    def complete(
        self,
        prompt: str,
        *,
        tier: Tier = "fast",
        system: str | None = None,
        max_tokens: int | None = None,
        temperature: float = 0.2,
        schema: type[ModelT] | None = None,
    ) -> str | ModelT:
        """Run one completion.

        With ``schema``, the reply is parsed as JSON and validated against the
        pydantic model; on violation the model is re-prompted exactly once with
        the validation error before :class:`SchemaViolation` is raised.

        ``max_tokens`` defaults to :func:`token_budget` of the schema, so a
        caller cannot ask for output the budget cannot hold.
        """
        if schema is None:
            return self._call(
                prompt, tier=tier, system=system, max_tokens=max_tokens or 1024, temperature=temperature
            )
        max_tokens = max_tokens or token_budget(schema)

        json_prompt = f"{prompt}\n\n{_schema_instruction(schema)}"
        try:
            raw = self._call(
                json_prompt, tier=tier, system=system, max_tokens=max_tokens, temperature=temperature
            )
            return _parse_schema(raw, schema)
        except (ValidationError, ValueError, EmptyCompletion) as first_error:
            log.warning(
                "Schema violation on tier=%s (%s): %s; re-prompting once.",
                tier,
                schema.__name__,
                _violation_digest(first_error),
            )
            rejected = (
                "Your previous reply was empty."
                if isinstance(first_error, EmptyCompletion)
                else f"Your previous reply was rejected:\n{first_error}"
            )
            repair = f"{json_prompt}\n\n{rejected}\nReply again with ONLY the corrected JSON object."
            raw2 = self._call(repair, tier=tier, system=system, max_tokens=max_tokens, temperature=temperature)
            try:
                return _parse_schema(raw2, schema)
            except (ValidationError, ValueError) as second_error:
                self.ledger.record_failure(tier)
                raise SchemaViolation(
                    f"{schema.__name__} not satisfied after one re-prompt: {second_error}"
                ) from second_error

    def _call(
        self,
        prompt: str,
        *,
        tier: Tier,
        system: str | None,
        max_tokens: int,
        temperature: float,
    ) -> str:
        import litellm  # imported lazily: ~1s import cost, keeps `--help` snappy

        model = self.model_for(tier)
        messages: list[dict[str, str]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        transient = (
            litellm.exceptions.RateLimitError,
            litellm.exceptions.ServiceUnavailableError,
            litellm.exceptions.InternalServerError,
            litellm.exceptions.APIConnectionError,
            litellm.exceptions.Timeout,
        )

        @retry(
            retry=retry_if_exception_type(transient),
            stop=stop_after_attempt(self.settings.llm_max_retries),
            wait=wait_exponential(multiplier=2, min=2, max=30),
            reraise=False,
        )
        def _attempt() -> Any:
            return litellm.completion(
                model=model,
                messages=messages,
                max_tokens=max_tokens,
                temperature=temperature,
                timeout=self.settings.llm_timeout,
                num_retries=0,  # tenacity owns retry policy
                drop_params=True,  # tolerate params a given provider lacks
            )

        try:
            response = _attempt()
        except RetryError as exc:  # pragma: no cover - network dependent
            self.ledger.record_failure(tier)
            raise LLMError(f"LLM call failed after retries (tier={tier}, model={model})") from exc
        except Exception as exc:  # non-transient provider error
            self.ledger.record_failure(tier)
            raise LLMError(f"LLM call failed (tier={tier}, model={model}): {exc}") from exc

        self._account(response, tier=tier, model=model)
        content = _extract_text(response)
        if not content.strip():
            self.ledger.record_failure(tier)
            raise EmptyCompletion(f"Empty completion from tier={tier} model={model}")
        return content

    def _account(self, response: Any, *, tier: str, model: str) -> None:
        usage = getattr(response, "usage", None)
        prompt_tokens = int(getattr(usage, "prompt_tokens", 0) or 0)
        completion_tokens = int(getattr(usage, "completion_tokens", 0) or 0)
        cached = 0
        details = getattr(usage, "prompt_tokens_details", None)
        if details is not None:
            cached = int(getattr(details, "cached_tokens", 0) or 0)

        cost = 0.0
        try:  # cost tables are best-effort and may not know a private model id
            import litellm

            cost = float(litellm.completion_cost(completion_response=response) or 0.0)
        except Exception:  # noqa: BLE001 - never fail a run over a price lookup
            cost = 0.0

        self.ledger.record(tier, model, prompt_tokens, completion_tokens, cached, cost)


# -- helpers -------------------------------------------------------------


def _extract_text(response: Any) -> str:
    try:
        return response.choices[0].message.content or ""
    except (AttributeError, IndexError, TypeError):
        return ""


def _schema_instruction(schema: type[BaseModel]) -> str:
    return (
        "Reply with ONLY a single JSON object; no prose, no markdown fence. "
        "It must validate against this JSON Schema:\n"
        f"{json.dumps(schema.model_json_schema(), separators=(',', ':'))}"
    )


_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)


# -- completion budgets ---------------------------------------------------
# A schema's ``max_length`` caps and the call's ``max_tokens`` are the same
# constraint measured in two units, and they have to move together. Raising the
# caps alone lets the model write a longer reply that the budget then truncates
# mid-string: the failure arrives as `json_invalid` rather than
# `string_too_long`, costs a re-prompt, and can lose the role entirely — which
# is what happened to the portfolio manager the first time the caps went up.
# Deriving the budget from the schema removes the chance to update one and
# forget the other.
_CHARS_PER_TOKEN = 3.0  # conservative: JSON escaping and prose both run short of 4
_UNCAPPED_STR_CHARS = 400
_LIST_ITEM_CHARS = 250
# Headroom exists because the caps are advisory to the model and binding to us:
# it overruns a stated character limit routinely (the research manager did so
# twice in three tickers at 1.25), and an overrun that lands inside the budget
# costs one `string_too_long` re-prompt with a usable error, while an overrun
# that hits the ceiling costs a truncated reply and a `json_invalid` guess.
_BUDGET_HEADROOM = 1.6
_BUDGET_FLOOR = 600
# A reasoning model spends tokens before it writes the answer, and the provider
# charges that thinking against the same `max_tokens` ceiling. Measured on the
# options strategist against Sonnet-class on Vertex: the same prompt returned
# `finish_reason=length` with **no content at all** at a 1,907-token ceiling and
# completed in 3,057 tokens at 4,000 — roughly 2,400 tokens of thinking before
# the first brace. A ceiling sized to the JSON alone therefore buys a silent
# empty reply, which is the most expensive failure available: a wasted call, a
# re-prompt, and often a DEGRADED ticker. Unused ceiling is not billed.
_REASONING_ALLOWANCE = 2500


def _max_length(info: Any) -> int | None:
    return next((m.max_length for m in getattr(info, "metadata", ()) if hasattr(m, "max_length")), None)


def token_budget(schema: type[BaseModel]) -> int:
    """Completion tokens enough to hold a maximal valid instance of ``schema``.

    Every string field is assumed to run to its cap, every list to its item
    limit, plus the JSON scaffolding and :data:`_REASONING_ALLOWANCE` for the
    thinking the model does before the first brace. The estimate errs high on
    purpose: unused budget is not billed — providers charge generated tokens —
    so the only cost of a generous ceiling is that a runaway reply runs longer
    before it is cut off.
    """
    chars = 0
    for name, info in schema.model_fields.items():
        chars += len(name) + 8  # `"name": "...",`
        annotation = info.annotation
        if get_origin(annotation) is list:
            chars += (_max_length(info) or 3) * _LIST_ITEM_CHARS
        elif annotation is str or str in get_args(annotation):  # str, or `str | None`
            chars += _max_length(info) or _UNCAPPED_STR_CHARS
        else:
            chars += 24  # numbers, enums, short literals
    prose = max(_BUDGET_FLOOR, int(chars / _CHARS_PER_TOKEN * _BUDGET_HEADROOM))
    return prose + _REASONING_ALLOWANCE


def _violation_digest(error: Exception) -> str:
    """Which fields failed and why, in one line — the re-prompt costs a call each time.

    Pydantic's own ``str()`` runs to several lines and quotes the whole rejected
    value back, which would put model prose (and its length) into the logs.
    """
    if not isinstance(error, ValidationError):
        return str(error).splitlines()[0][:160]
    parts = []
    for err in error.errors()[:4]:
        field = ".".join(str(loc) for loc in err["loc"]) or "<root>"
        detail = ""
        if err["type"] == "json_invalid":
            # "truncated mid-string" and "unescaped newline" are both json_invalid
            # and want opposite fixes — a bigger budget vs. a lenient parse.
            detail = f" ({str(err.get('ctx', {}).get('error', '')).split(' at line')[0]})"
        parts.append(f"{field}: {err['type']}{detail}")
    return ", ".join(parts)


def _parse_schema(raw: str, schema: type[ModelT]) -> ModelT:
    """Validate ``raw`` against ``schema``, tolerating a markdown fence.

    Also tolerates raw newlines and tabs inside JSON string values. The models
    do this often when a prose field runs to several paragraphs; pydantic's
    parser is strict and rejects it, but the reply is otherwise perfectly good,
    and re-prompting to fix an escaping detail costs a whole call.
    """
    text = raw.strip()
    fence = _FENCE_RE.search(text)
    if fence:
        text = fence.group(1).strip()
    if not text.startswith("{"):
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end <= start:
            raise ValueError(f"No JSON object found in model output: {raw[:200]!r}")
        text = text[start : end + 1]
    try:
        return schema.model_validate_json(text)
    except ValidationError as exc:
        if not _is_json_error(exc):
            raise
        return schema.model_validate(json.loads(text, strict=False))  # strict=False: allow \n in strings


def _is_json_error(error: ValidationError) -> bool:
    return any(err["type"] == "json_invalid" for err in error.errors())


def smoke_test(tier: Tier = "fast", settings: Settings | None = None) -> dict[str, Any]:
    """Minimal end-to-end liveness check for the configured provider."""
    gateway = LLMGateway(settings)
    started = time.monotonic()
    text = gateway.complete("Reply with the single word: ok", tier=tier, max_tokens=8, temperature=0.0)
    return {
        "tier": tier,
        "model": gateway.model_for(tier),
        "reply": text.strip(),
        "seconds": round(time.monotonic() - started, 2),
    }
