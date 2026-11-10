"""Model tiering: cheap, fast models for wording; stronger models for SQL and judging."""
from functools import lru_cache

import config


@lru_cache(maxsize=None)
def pick_llm(level: str):
    """Return a chat model for a tier: 'low', 'medium' or 'high'. Models are set in .env."""
    from langchain_anthropic import ChatAnthropic  # imported lazily so tests run without an API key

    models = {"low": config.MODEL_LOW, "medium": config.MODEL_MEDIUM, "high": config.MODEL_HIGH}
    try:
        model = models[level.lower()]
    except KeyError:
        raise ValueError(f"Unsupported level: {level!r}. Use low, medium or high.") from None
    return ChatAnthropic(model_name=model, temperature=0, max_retries=2, timeout=60)
