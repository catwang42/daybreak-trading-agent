"""Load provider-agnostic prompt templates from ``prompts/*.md``.

Prompts are plain markdown with ``{placeholder}`` fields so they port to any
provider unchanged. Formatting is strict: a missing placeholder raises rather
than silently shipping a literal ``{brace}`` to the model.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

PROMPTS_DIR = Path(__file__).parent / "prompts"


class PromptError(KeyError):
    """A prompt file is missing, or a placeholder was not supplied."""


@lru_cache(maxsize=32)
def load_prompt(name: str) -> str:
    path = PROMPTS_DIR / f"{name}.md"
    if not path.exists():
        raise PromptError(f"prompt '{name}' not found at {path}")
    return path.read_text()


def render(prompt_name: str, /, **values: object) -> str:
    """Render a template. Positional-only so ``name=`` can be a placeholder."""
    template = load_prompt(prompt_name)
    try:
        return template.format(**values)
    except KeyError as exc:
        raise PromptError(f"prompt '{prompt_name}' needs placeholder {exc}") from exc
