from __future__ import annotations

import asyncio
from collections import defaultdict
from decimal import Decimal


class MetricsRegistry:
    def __init__(self) -> None:
        self._counters: dict[str, int] = defaultdict(int)
        self._gauges: dict[str, Decimal] = {}
        self._lock = asyncio.Lock()

    async def increment(self, name: str, amount: int = 1) -> None:
        async with self._lock:
            self._counters[name] += amount

    async def gauge(self, name: str, value: int | float | Decimal) -> None:
        async with self._lock:
            self._gauges[name] = Decimal(str(value))

    async def render_prometheus(self) -> str:
        async with self._lock:
            lines = [
                f"options_sentinel_{name} {value}" for name, value in sorted(self._counters.items())
            ]
            lines.extend(
                f"options_sentinel_{name} {value}" for name, value in sorted(self._gauges.items())
            )
        return "\n".join(lines) + "\n"
