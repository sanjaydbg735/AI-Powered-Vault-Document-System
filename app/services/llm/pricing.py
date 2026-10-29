import logging

logger = logging.getLogger(__name__)

# USD per 1M tokens: (input, output). Prefix match handles dated model ids.
PRICING: dict[str, tuple[float, float]] = {
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-4o": (2.50, 10.00),
    "gpt-4.1-nano": (0.10, 0.40),
    "gpt-4.1-mini": (0.40, 1.60),
    "gpt-4.1": (2.00, 8.00),
    "gpt-4-turbo": (10.00, 30.00),
    "text-embedding-3-small": (0.02, 0.0),
    "text-embedding-3-large": (0.13, 0.0),
    "fake": (0.0, 0.0),
}


def cost_usd(model: str, prompt_tokens: int, completion_tokens: int = 0) -> float:
    price = PRICING.get(model)
    if price is None:
        # longest prefix wins so "gpt-4o-mini-2024-07-18" doesn't match "gpt-4o"
        for name in sorted(PRICING, key=len, reverse=True):
            if model.startswith(name):
                price = PRICING[name]
                break
    if price is None:
        logger.warning("no pricing for model", extra={"model": model})
        return 0.0
    return round((prompt_tokens * price[0] + completion_tokens * price[1]) / 1_000_000, 6)
