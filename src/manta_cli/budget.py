from __future__ import annotations

from pathlib import Path

from .schemas import CostRecord, ModelPrice


DEFAULT_PRICES = {
    "openai:gpt-5-nano": ModelPrice(input_per_million=0.05, output_per_million=0.40, context_window=128000),
    "openai:gpt-5-mini": ModelPrice(input_per_million=0.25, output_per_million=2.00, context_window=128000),
    "openai:gpt-builder": ModelPrice(input_per_million=1.25, output_per_million=10.00, context_window=200000),
    "anthropic:claude-opus": ModelPrice(input_per_million=15.00, output_per_million=75.00, context_window=200000),
    "google:gemini-pro": ModelPrice(input_per_million=1.25, output_per_million=10.00, context_window=1000000),
}


class BudgetExceeded(RuntimeError):
    pass


class PriceTable:
    def __init__(self, prices: dict[str, ModelPrice] | None = None):
        self.prices = prices or DEFAULT_PRICES

    def estimate(self, model: str, input_tokens: int, output_tokens: int) -> float:
        price = self.prices.get(model)
        if price is None:
            # Unknown model: fail safe by returning a noticeable non-zero estimate.
            return ((input_tokens + output_tokens) / 1_000_000) * 10.0
        return (
            (input_tokens / 1_000_000) * price.input_per_million
            + (output_tokens / 1_000_000) * price.output_per_million
        )


class BudgetLedger:
    def __init__(self, session_id: str, max_usd: float, path: Path, price_table: PriceTable | None = None):
        self.session_id = session_id
        self.max_usd = max_usd
        self.path = path
        self.price_table = price_table or PriceTable()
        self.used_usd = 0.0
        self.path.parent.mkdir(parents=True, exist_ok=True)

    @property
    def remaining_usd(self) -> float:
        return max(0.0, self.max_usd - self.used_usd)

    def can_spend(self, amount_usd: float) -> bool:
        return self.used_usd + amount_usd <= self.max_usd

    def estimate(self, model: str, input_tokens: int, output_tokens: int) -> float:
        return self.price_table.estimate(model, input_tokens, output_tokens)

    def record(self, role: str, model: str, input_tokens: int, output_tokens: int, route: str, reason: str = "") -> CostRecord:
        cost = self.estimate(model, input_tokens, output_tokens)
        if not self.can_spend(cost):
            raise BudgetExceeded(
                f"Model call would exceed budget: cost=${cost:.4f}, remaining=${self.remaining_usd:.4f}"
            )
        self.used_usd += cost
        record = CostRecord(
            session_id=self.session_id,
            role=role,
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            estimated_cost_usd=cost,
            route=route,
            reason=reason,
        )
        with self.path.open("a", encoding="utf-8") as f:
            f.write(record.model_dump_json() + "\n")
        return record

    def summary(self) -> dict[str, float]:
        return {"max_usd": self.max_usd, "used_usd": self.used_usd, "remaining_usd": self.remaining_usd}
