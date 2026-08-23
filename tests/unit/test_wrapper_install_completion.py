"""A manual install that never completes must not be reported as installed.

The watch loop used to return the install directory whenever one had ever
appeared, so exhausting the completion polls read as success and a
part-written game got a Play button. Field case: Crash Bandicoot 4, whose
Battle.net Agent had no network (WSAENETUNREACH), left a 1.2 GB prefix
containing an empty game directory.
"""

from __future__ import annotations

import asyncio

import pytest

from unifideck.stores.shared.wrapper_install import watch as watch_mod
from unifideck.stores.shared.wrapper_install.watch import watch_manual_install


class _Probe:
    """Minimal InstallProbe. ``complete`` drives ``is_complete``."""

    store = "battlenet"
    client_label = "Battle.net"
    # Non-zero: the loop derives its poll count as timeout/poll. Sleeping is
    # stubbed out, so these only set how many iterations run.
    poll_interval_s = 0.01
    timeout_s = 0.05
    never_started_grace_s = 1.0
    client_gone_grace_s = 1.0

    def __init__(self, complete: bool | None, install_dir: str | None = "/tmp/game"):
        self._complete = complete
        self._dir = install_dir
        self.size = 5

    def snapshot(self) -> None:
        return None

    def detect(self, baseline):
        del baseline
        return self._dir

    def measure(self, install_dir: str) -> int:
        del install_dir
        return self.size

    def is_complete(self, install_dir: str) -> bool | None:
        del install_dir
        return self._complete


@pytest.fixture(autouse=True)
def _fast(monkeypatch):
    """No sleeping, and the client always reads as alive."""

    async def _no_sleep(_):
        return None

    monkeypatch.setattr(watch_mod.asyncio, "sleep", _no_sleep)
    monkeypatch.setattr(watch_mod, "install_alive", lambda *a, **k: True)
    monkeypatch.setattr(watch_mod, "STABILITY_MAX_POLLS", 5)


def _run(probe):
    return asyncio.run(watch_manual_install(probe=probe, prefix="/tmp/pfx"))


def test_store_verdict_true_completes():
    assert _run(_Probe(complete=True)) == "/tmp/game"


def test_never_completing_install_is_a_failure_not_a_success():
    """The regression: polls ran out, so nothing ever said 'complete'."""
    assert _run(_Probe(complete=False)) is None


def test_a_false_verdict_never_falls_back_to_the_size_heuristic():
    """A paused download holds a steady size; that must not end the install."""
    probe = _Probe(complete=False)
    probe.size = 999  # unchanging, which the heuristic would call stable
    assert _run(probe) is None


def test_probe_that_cannot_answer_still_uses_stability():
    """``None`` means 'no authoritative signal' — the heuristic still applies."""
    assert _run(_Probe(complete=None)) == "/tmp/game"


def test_game_that_never_appears_is_a_failure():
    assert _run(_Probe(complete=True, install_dir=None)) is None
