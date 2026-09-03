from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.clock.base import VirtualClock
from app.replay import ReplayEngine, ReplayEvent, ReplayMode

START = datetime(2026, 9, 2, 13, 0, tzinfo=UTC)


def event(event_id: str, minute: int, value: int) -> ReplayEvent:
    at = START + timedelta(minutes=minute)
    return ReplayEvent(
        event_id=event_id,
        kind="quote",
        effective_at=at,
        available_at=at,
        payload={"value": value},
        sequence=minute,
    )


@pytest.mark.asyncio
async def test_handler_cannot_see_future_observations() -> None:
    visible: list[tuple[str, ...]] = []

    def handler(current: ReplayEvent, context: object) -> dict[str, object]:
        ids = tuple(item.event_id for item in context.events("quote"))  # type: ignore[attr-defined]
        visible.append(ids)
        return {"current": current.event_id, "visible": ids}

    engine = ReplayEngine(
        VirtualClock(START),
        mode=ReplayMode.DETERMINISTIC_REGRESSION,
        strategy_version="strategy-v1",
        config_version="config-v1",
        handlers={"quote": handler},
    )
    await engine.run([event("later", 2, 2), event("first", 1, 1)])

    assert visible == [("first",), ("first", "later")]


@pytest.mark.asyncio
async def test_identical_seeded_inputs_have_identical_replay_hashes() -> None:
    async def run_once() -> str:
        engine = ReplayEngine(
            VirtualClock(START),
            strategy_version="strategy-v1",
            config_version="config-v1",
            handlers={"quote": lambda item, _: {"decision": item.payload["value"] > 1}},
        )
        return (await engine.run([event("one", 1, 1), event("two", 2, 2)])).replay_hash

    assert await run_once() == await run_once()


def test_event_cannot_be_available_before_it_is_effective() -> None:
    with pytest.raises(ValueError, match="available before"):
        ReplayEvent(
            event_id="impossible",
            kind="quote",
            effective_at=START + timedelta(minutes=1),
            available_at=START,
            payload={},
        )
