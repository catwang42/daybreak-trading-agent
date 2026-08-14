"""Single LLM gateway for the whole app (LiteLLM). THE ONLY FILE THAT CALLS AN LLM.
- complete(prompt, tier="fast"|"smart", schema: pydantic model | None)
- models from env LLM_FAST_MODEL / LLM_SMART_MODEL; provider-agnostic by design
- retries (tenacity), token accounting per run, one re-prompt on schema violation
"""
# TODO(M1): implement.
