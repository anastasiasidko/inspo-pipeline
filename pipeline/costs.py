"""Per-model LLM cost, from the prices in config.yaml (per 1k tokens)."""
import logging

logger = logging.getLogger(__name__)
FALLBACK_PRICE = {"input": 0.001, "output": 0.005}


def llm_cost_usd(config: dict, model: str, input_tokens: int, output_tokens: int) -> float:
    price = (config.get("model_prices") or {}).get(model)
    if not price:
        logger.warning(f"No price configured for model {model!r} in config.yaml model_prices — using Haiku rates")
        price = FALLBACK_PRICE
    return input_tokens / 1000 * price["input"] + output_tokens / 1000 * price["output"]
