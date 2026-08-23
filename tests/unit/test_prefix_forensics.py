"""Salvaging a vendor client's logs before its prefix is deleted.

For a wrapper store the prefix *is* the install, so a failed install
deletes it — and with it the only first-hand account of why the vendor
client would not start. Reported from the field: a Battle.net install
failed with "the client did not become ready", the prefix was removed
30 seconds later, and by the time a bundle was collected every Blizzard
log had gone with it.

The rules that matter here are all failure-path rules: never raise, never
block the cleanup that follows, and never leave the caller guessing
whether anything was captured.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from unifideck.stores.shared import prefix_forensics as forensics


def _prefix(root: Path, *, logs: dict[str, str] | None = None) -> Path:
    """A prefix with Blizzard's log layout inside it."""
    prefix = root / "w3"
    drive_c = prefix / "pfx" / "drive_c"
    drive_c.mkdir(parents=True)
    for rel, body in (logs or {}).items():
        path = drive_c / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
    return prefix


def _salvage(prefix: Path, destination: Path, store: str = "battlenet") -> int:
    return asyncio.run(forensics.preserve_vendor_logs(store, prefix, destination))


def test_the_client_logs_survive_the_prefix(tmp_path: Path) -> None:
    """The whole point: read the reason after the evidence is gone."""
    prefix = _prefix(tmp_path, logs={
        "ProgramData/Battle.net/Setup/bootstrapper.log": "STATE_UPDATE_BOOTSTRAPPER",
        "ProgramData/Battle.net/Agent/Agent.log": "agent said this",
    })
    out = tmp_path / "launches" / "battlenet-w3.vendor.txt"

    assert _salvage(prefix, out) == 2

    text = out.read_text(encoding="utf-8")
    assert "STATE_UPDATE_BOOTSTRAPPER" in text
    assert "agent said this" in text
    # Each excerpt is labelled with where it came from, or a reader cannot
    # tell the bootstrapper's account from the Agent's.
    assert "bootstrapper.log" in text
    assert "Agent.log" in text


def test_the_agents_own_logs_are_salvaged_from_their_versioned_directory(
    tmp_path: Path,
) -> None:
    """The Agent's real logs are a directory deeper than first assumed.

    ``Agent/Logs/*.log`` matched, but only a one-line ``Switcher`` log. The
    Agent writes everything that matters under a build-versioned
    ``Agent.<build>/Logs``, and a whole investigation into installs stuck at
    "Queued" ran on files this salvage had not collected: the answer was in
    ``Operations-*`` (the game's operation sitting behind the Agent's own
    self-update), with the cause in ``AgentNGDP-*`` (a region-tag change).
    """
    prefix = _prefix(tmp_path, logs={
        "ProgramData/Battle.net/Agent/Logs/Switcher-20260822T123736.log":
            "switcher argument[0]: '--session=1032'",
        "ProgramData/Battle.net/Agent/Agent.9700/Logs/Operations-20260822.log":
            "Active operation nullptr replaced by OP_UPDATE for 'agent'",
        "ProgramData/Battle.net/Agent/Agent.9700/Logs/AgentNGDP-20260822.log":
            "Start Update of agent w/ tags (Volatile Windows KR? geoip-IN?)",
        "ProgramData/Battle.net/Agent/Agent.9700/Logs/AgentUpdate-20260822.log":
            "agent Update Progress - 0.7543 (0.7543)",
    })
    out = tmp_path / "launches" / "battlenet-d1.vendor.txt"

    assert _salvage(prefix, out) == 4

    text = out.read_text(encoding="utf-8")
    assert "OP_UPDATE for 'agent'" in text
    assert "Volatile Windows KR? geoip-IN?" in text
    assert "0.7543" in text


def test_a_prefix_with_no_logs_writes_nothing(tmp_path: Path) -> None:
    """No file beats an empty file that reads as 'we looked and found none'."""
    out = tmp_path / "launches" / "battlenet-w3.vendor.txt"
    assert _salvage(_prefix(tmp_path), out) == 0
    assert not out.exists()


def test_a_missing_prefix_is_not_an_error(tmp_path: Path) -> None:
    """Runs on the failure path — a prefix that was never created is normal."""
    out = tmp_path / "launches" / "battlenet-nope.vendor.txt"
    assert _salvage(tmp_path / "does-not-exist", out) == 0
    assert not out.exists()


def test_an_unknown_store_is_a_no_op(tmp_path: Path) -> None:
    """Only stores with a known log layout are salvaged; the rest cost nothing."""
    prefix = _prefix(tmp_path, logs={"ProgramData/Battle.net/Agent/Agent.log": "x"})
    out = tmp_path / "launches" / "epic-x.vendor.txt"
    assert _salvage(prefix, out, store="epic") == 0


def test_a_huge_log_is_tailed_not_copied(tmp_path: Path) -> None:
    """Blizzard's Agent log reaches tens of MB and the end is the useful part."""
    body = "x" * (forensics.MAX_BYTES_PER_FILE * 2) + "THE-INTERESTING-PART"
    prefix = _prefix(tmp_path, logs={"ProgramData/Battle.net/Agent/Agent.log": body})
    out = tmp_path / "launches" / "battlenet-w3.vendor.txt"

    assert _salvage(prefix, out) == 1

    text = out.read_text(encoding="utf-8")
    assert "THE-INTERESTING-PART" in text
    assert len(text) < forensics.MAX_BYTES_PER_FILE + 4096


def test_an_unreadable_log_is_noted_not_raised(tmp_path: Path) -> None:
    """One bad file must not cost the salvage the other files."""
    prefix = _prefix(tmp_path, logs={
        "ProgramData/Battle.net/Agent/Agent.log": "readable",
        "ProgramData/Battle.net/Setup/setup.log": "also readable",
    })
    drive_c = prefix / "pfx" / "drive_c"
    (drive_c / "ProgramData/Battle.net/Agent/Agent.log").chmod(0o000)
    out = tmp_path / "launches" / "battlenet-w3.vendor.txt"
    try:
        assert _salvage(prefix, out) == 2
        assert "also readable" in out.read_text(encoding="utf-8")
    finally:
        (drive_c / "ProgramData/Battle.net/Agent/Agent.log").chmod(0o644)


@pytest.mark.parametrize(
    ("game_id", "expected"),
    [("w3", "battlenet-w3.vendor.txt"), ("../evil", "battlenet-___evil.vendor.txt")],
)
def test_the_destination_name_cannot_escape_the_launches_dir(
    tmp_path: Path, game_id: str, expected: str,
) -> None:
    """Game ids come from a store's catalogue, not from us."""
    assert forensics.salvage_path("battlenet", game_id, into=tmp_path).name == expected
    assert forensics.salvage_path("battlenet", game_id, into=tmp_path).parent == tmp_path


def test_the_launches_dir_is_resolved_per_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A module-level constant would outlive a redirected HOME."""
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "elsewhere"))
    assert forensics.launches_dir() == tmp_path / "elsewhere" / "unifideck" / "launches"
