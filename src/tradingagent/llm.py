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
from typing import Any, Literal, TypeVar

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
        max_tokens: int = 1024,
        temperature: float = 0.2,
        schema: type[ModelT] | None = None,
    ) -> str | ModelT:
        """Run one completion.

        With ``schema``, the reply is parsed as JSON and validated against the
        pydantic model; on violation the model is re-prompted exactly once with
        the validation error before :class:`SchemaViolation` is raised.
        """
        if schema is None:
            return self._call(prompt, tier=tier, system=system, max_tokens=max_tokens, temperature=temperature)

        json_prompt = f"{prompt}\n\n{_schema_instruction(schema)}"
        raw = self._call(json_prompt, tier=tier, system=system, max_tokens=max_tokens, temperature=temperature)
        try:
            return _parse_schema(raw, schema)
        except (ValidationError, ValueError) as first_error:
            log.warning("Schema violation on tier=%s (%s); re-prompting once.", tier, schema.__name__)
            repair = (
                f"{json_prompt}\n\nYour previous reply was rejected:\n{first_error}\n"
                "Reply again with ONLY the corrected JSON object."
            )
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
            raise LLMError(f"Empty completion from tier={tier} model={model}")
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


def _parse_schema(raw: str, schema: type[ModelT]) -> ModelT:
    """Validate ``raw`` against ``schema``, tolerating a markdown fence."""
    text = raw.strip()
    fence = _FENCE_RE.search(text)
    if fence:
        text = fence.group(1).strip()
    if not text.startswith("{"):
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end <= start:
            raise ValueError(f"No JSON object found in model output: {raw[:200]!r}")
        text = text[start : end + 1]
    return schema.model_validate_json(text)


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
