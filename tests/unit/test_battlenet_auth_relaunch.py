"""Closing the Battle.net sign-in window must not reopen it.

The reported bug: "still launching the sign in launcher when I close it, even
after the connection status is signed in."

``battlenet_auth_launch`` calls ``run_umu_with_retry`` without ``max_attempts``,
so it takes the default of 2. The launcher log shows it plainly, ``run attempt
1/2`` for the sign-in client against ``run attempt 1/1`` for a game launch,
which opts out explicitly. Whether a retry fires is then decided by rc and
duration alone::

    _RECOVERABLE_CODES = {2, 74, 127}
    _RECOVERABLE_MAX_RUNTIME_SECONDS = 120

Two minutes is far longer than anyone takes to close a window they did not
want, so a deliberate close looked exactly like the ANGLE/gamescope abort the
retry exists for, and the client came back by itself. For rc 2 and 74 the
retry also wipes the shared umu runtime cache every other game depends on.

The retry cannot simply be deleted: the startup abort is real. What separates
the cases is whether the client's renderer was ever seen.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from unifideck.launcher.proton.handlers import battlenet_watch as watch
from unifideck.launcher.proton.infrastructure import umu_runtime


def _run(**kwargs: Any) -> int:
    return asyncio.run(umu_runtime.run_umu_with_retry(["/bin/true"], **kwargs))


@pytest.fixture
def attempts(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Count attempts, and make every run exit 2 after a moment."""
    seen: list[int] = []

    async def _once(*_a: Any, **_kw: Any) -> int:
        seen.append(1)
        return 2

    monkeypatch.setattr(umu_runtime, "_run_umu_once", _once)
    monkeypatch.setattr(umu_runtime, "open_game_log", lambda: None)

    async def _no_backoff(*_a: Any, **_kw: Any) -> None:
        return None

    monkeypatch.setattr(umu_runtime, "_prepare_retry", _no_backoff)
    return seen


def test_a_recoverable_crash_still_retries(attempts: list[int]) -> None:
    """The startup abort the retry exists for is untouched."""
    assert _run() == 2
    assert len(attempts) == 2


def test_a_veto_stops_the_relaunch(attempts: list[int]) -> None:
    """The reported bug: a close inside 120s must not reopen the window."""
    assert _run(should_retry=lambda: False) == 2
    assert len(attempts) == 1, "the client was reopened after the user closed it"


def test_the_veto_only_applies_to_recoverable_codes(
    monkeypatch: pytest.MonkeyPatch, attempts: list[int],
) -> None:
    """An unrecoverable code returns without ever consulting the veto."""
    asked = []

    async def _once(*_a: Any, **_kw: Any) -> int:
        attempts.append(1)
        return 9

    monkeypatch.setattr(umu_runtime, "_run_umu_once", _once)

    assert _run(should_retry=lambda: asked.append(1) or True) == 9
    assert asked == [], "the veto was consulted for a code that never retries"


def test_a_successful_run_never_consults_the_veto(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _once(*_a: Any, **_kw: Any) -> int:
        return 0

    monkeypatch.setattr(umu_runtime, "_run_umu_once", _once)
    monkeypatch.setattr(umu_runtime, "open_game_log", lambda: None)
    asked: list[int] = []

    assert _run(should_retry=lambda: asked.append(1) or True) == 0
    assert asked == []


# ── the latch that decides ──────────────────────────────────────


def test_the_latch_remembers_a_client_that_has_since_exited(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``client_ready`` answers about now; the veto needs "was it ever up".

    Latching also survives the renderer disappearing during a self-update,
    which a live reading at exit time would misreport as never started.
    """
    live = {"ready": True}
    monkeypatch.setattr(watch, "client_ready", lambda _p: live["ready"])

    async def _exercise() -> bool:
        async with watch.watch_readiness("/pfx", interval=0.001) as latch:
            await asyncio.sleep(0.02)
            live["ready"] = False
            await asyncio.sleep(0.02)
            return latch.seen

    assert asyncio.run(_exercise()) is True


def test_a_client_that_never_rendered_leaves_the_latch_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Which is what keeps a genuine startup crash retryable."""
    monkeypatch.setattr(watch, "client_ready", lambda _p: False)

    async def _exercise() -> bool:
        async with watch.watch_readiness("/pfx", interval=0.001) as latch:
            await asyncio.sleep(0.02)
            return latch.seen

    assert asyncio.run(_exercise()) is False
