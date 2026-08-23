"""Unit tests for compat/prefix_init — proton-change reset + first init.

Covers the deterministic logic: Proton family classification, the
reset-vs-notify decision on a Proton change, the destructive reset
(wipe-but-preserve + user-data backup), and the marker bookkeeping.
The umu ``createprefix`` subprocess path is exercised only for the
fast "already initialised → skip" case (no subprocess).
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from unifideck.launcher.proton.compat import prefix_init as pi


@pytest.fixture
def toast_spy(monkeypatch):
    """Capture launcher_toast(i18n_key, **kw) calls."""
    spy = MagicMock()
    monkeypatch.setattr(pi, "launcher_toast", spy)
    return spy


def _plan(prefix_root: Path, tool: str, store: str = "gog"):
    """Minimal ProtonLaunchPlan stand-in for the pure-logic helpers."""
    return SimpleNamespace(
        prefix_path=prefix_root,
        state=SimpleNamespace(proton_tool_id=tool),
        context=SimpleNamespace(game_key=f"{store}:123", store=store),
    )


def _make_root_prefix(root: Path, *, proton_marker: str | None = None) -> None:
    """Build a root-layout Wine prefix with a save + setup markers."""
    (root / "drive_c" / "users" / "steamuser").mkdir(parents=True)
    (root / "drive_c" / "users" / "steamuser" / "save.dat").write_text("savegame")
    (root / "system.reg").write_text("reg")
    (root / "user.reg").write_text("reg")
    (root / "version").write_text("GE-Proton10-10")
    (root / "unifideck_winetricks_complete.marker").write_text("complete")
    (root / ".unifideck_prereqs_x.done").write_text("done")
    if proton_marker is not None:
        (root / pi._MARKER_NAME).write_text(proton_marker)


# ── _proton_family ────────────────────────────────────────────────

@pytest.mark.parametrize(
    ("tool", "family"),
    [
        ("proton_experimental", "experimental"),
        ("GE-Proton10-34", "ge-proton"),
        ("GE-Proton9-26", "ge-proton"),
        ("UMU-Proton-9.0-4e", "umu-proton"),
        ("Proton 9.0 (Beta)", "proton9"),
        ("Proton 10.0", "proton10"),
        ("something-weird", "other"),
        # Steam's compatibility-tool DISPLAY names, as the user picks them in
        # Properties > Compatibility. These used to collapse to "other".
        ("Proton-GE Latest", "ge-proton"),
        ("GE-Proton11-5-x86_64", "ge-proton"),
        ("Proton - Experimental", "experimental"),
        ("Proton-CachyOS Latest", "cachyos"),
        ("cachyos-11.0-20260703-slr", "cachyos"),
        ("CachyOS-11.0-100", "cachyos"),
    ],
)
def test_proton_family(tool, family):
    assert pi._proton_family(tool) == family


# Regression: a GOG prefix was reset six times in two days because the
# display-name and directory-name spellings of GE-Proton were read as
# different families, while CachyOS and GE both collapsed to "other" and
# read as the same one. A false family change WIPES the prefix, so both
# directions matter.
@pytest.mark.parametrize(
    ("previous", "current", "same_family"),
    [
        # Same family — must NOT reset (these three were the spurious wipes).
        ("Proton-GE Latest", "GE-Proton11-3", True),
        ("GE-Proton11-3", "Proton-GE Latest", True),
        ("GE-Proton11-3", "GE-Proton11-5", True),
        # Genuinely different families — must reset. The first was missed.
        ("Proton-CachyOS Latest", "Proton-GE Latest", False),
        ("GE-Proton11-5", "Proton-CachyOS Latest", False),
        ("proton_experimental", "GE-Proton11-3", False),
    ],
)
def test_proton_family_transitions_from_the_field(previous, current, same_family):
    assert (
        pi._proton_family(previous) == pi._proton_family(current)
    ) is same_family


# ── _handle_proton_change ─────────────────────────────────────────

def test_first_launch_records_marker_no_toast_no_reset(tmp_path, toast_spy):
    root = tmp_path / "prefix"
    _make_root_prefix(root, proton_marker=None)  # no marker → fresh baseline

    pi._handle_proton_change(_plan(root, "GE-Proton10-34"), root, "GE-Proton10-34")

    toast_spy.assert_not_called()
    assert (root / pi._MARKER_NAME).read_text() == "GE-Proton10-34"
    # Prefix untouched.
    assert (root / "system.reg").is_file()
    assert (root / "unifideck_winetricks_complete.marker").is_file()


def test_minor_change_notifies_but_keeps_prefix(tmp_path, toast_spy):
    root = tmp_path / "prefix"
    _make_root_prefix(root, proton_marker="GE-Proton10-10")

    pi._handle_proton_change(_plan(root, "GE-Proton10-34"), root, "GE-Proton10-34")

    key = toast_spy.call_args.args[0]
    assert key == "toasts.launcher.protonSwitchedTo"
    # Same family → prefix + setup markers preserved.
    assert (root / "system.reg").is_file()
    assert (root / "unifideck_winetricks_complete.marker").is_file()
    assert (root / pi._MARKER_NAME).read_text() == "GE-Proton10-34"


def test_major_change_resets_prefix_and_backs_up(tmp_path, toast_spy):
    root = tmp_path / "prefix"
    _make_root_prefix(root, proton_marker="proton_experimental")

    pi._handle_proton_change(_plan(root, "GE-Proton10-34"), root, "GE-Proton10-34")

    assert toast_spy.call_args.args[0] == "toasts.launcher.resettingPrefix"
    # Wine state + setup markers wiped...
    assert not (root / "system.reg").exists()
    assert not (root / "drive_c").exists()
    assert not (root / "unifideck_winetricks_complete.marker").exists()
    assert not (root / ".unifideck_prereqs_x.done").exists()
    # ...the proton marker is updated and the save is backed up.
    assert (root / pi._MARKER_NAME).read_text() == "GE-Proton10-34"
    backup = root / ".save_backup" / "steamuser" / "save.dat"
    assert backup.is_file()
    assert backup.read_text() == "savegame"


# ── prefixes that own their game install (Ubisoft) ────────────────

def test_ubisoft_family_change_keeps_the_installed_game(tmp_path, toast_spy):
    """A family change must NOT wipe a prefix the game is installed inside.

    Ubisoft titles live in ``drive_c/Program Files (x86)/Ubisoft/Ubisoft Game
    Launcher/games/``, so the reset that is merely expensive for every other
    store is permanent data loss here. Observed live 2026-08-01: launching
    Rayman Origins resolved ``proton_experimental``, ``prefix_setup`` borrowed
    managed GE for umu's winetricks verb, and this deleted the install.
    """
    root = tmp_path / "prefix"
    _make_root_prefix(root, proton_marker="Proton - Experimental")
    game = (
        root / "drive_c" / "Program Files (x86)" / "Ubisoft"
        / "Ubisoft Game Launcher" / "games" / "Rayman Origins"
    )
    game.mkdir(parents=True)
    (game / "Rayman Origins.exe").write_text("game")

    pi._handle_proton_change(
        _plan(root, "GE-Proton11-3", store="ubisoft"), root, "GE-Proton11-3",
    )

    # The install survived, and so did the rest of the prefix.
    assert (game / "Rayman Origins.exe").read_text() == "game"
    assert (root / "system.reg").is_file()
    # Reported as a switch, not a reset — Proton upgrades the prefix in place.
    assert toast_spy.call_args.args[0] == "toasts.launcher.protonSwitchedTo"
    # The marker still moves forward, so this is announced once, not every launch.
    assert (root / pi._MARKER_NAME).read_text() == "GE-Proton11-3"


def test_non_ubisoft_family_change_still_resets(tmp_path, toast_spy):
    """The guard is scoped — every other store keeps the reset behaviour."""
    root = tmp_path / "prefix"
    _make_root_prefix(root, proton_marker="Proton - Experimental")

    pi._handle_proton_change(
        _plan(root, "GE-Proton11-3", store="epic"), root, "GE-Proton11-3",
    )

    assert toast_spy.call_args.args[0] == "toasts.launcher.resettingPrefix"
    assert not (root / "system.reg").exists()


@pytest.mark.parametrize(
    ("store", "owns"),
    [("ubisoft", True), ("gog", False), ("epic", False), ("amazon", False)],
)
def test_prefix_owns_game_install(tmp_path, store, owns):
    assert pi._prefix_owns_game_install(_plan(tmp_path, "x", store=store)) is owns


def test_prefix_owns_game_install_without_a_store_attribute(tmp_path):
    """Missing ``store`` must read as 'does not own' — never as Ubisoft."""
    plan = SimpleNamespace(context=SimpleNamespace(game_key="gog:123"))
    assert pi._prefix_owns_game_install(plan) is False


def test_reset_preserves_marker_and_backup_dirs(tmp_path):
    root = tmp_path / "prefix"
    _make_root_prefix(root, proton_marker="proton_experimental")
    (root / ".save_backup").mkdir()
    (root / ".save_backup" / "old").write_text("x")

    pi._reset_prefix(root)

    # A pre-existing backup is refreshed (old content gone) but the
    # backup + proton marker dirs/files themselves are never deleted.
    assert (root / pi._MARKER_NAME).is_file()
    assert (root / ".save_backup").is_dir()


# ── _ensure_created (fast path) ───────────────────────────────────

async def test_ensure_created_skips_when_system_reg_present(tmp_path, toast_spy):
    root = tmp_path / "prefix"
    root.mkdir()
    (root / "system.reg").write_text("reg")

    # Should return immediately without toasting or spawning umu.
    await pi._ensure_created(_plan(root, "GE-Proton10-34"), root)
    toast_spy.assert_not_called()


async def test_ensure_created_no_migration_when_already_initialised(
    tmp_path, monkeypatch,
):
    """An already-initialised prefix never triggers a legacy migration."""
    root = tmp_path / "prefix"
    root.mkdir()
    (root / "system.reg").write_text("reg")
    spy = MagicMock()
    monkeypatch.setattr(pi, "restore_or_migrate_saves", spy)

    await pi._ensure_created(_plan(root, "GE-Proton10-34"), root)

    spy.assert_not_called()


async def test_ensure_created_skips_when_system_reg_under_pfx(tmp_path, toast_spy):
    """Regression: umu/Proton nest the real registry under ``pfx/``.

    WINEPREFIX is the prefix root, but umu-run creates the actual Wine
    tree at ``<root>/pfx/``. Checking ``root/system.reg`` directly never
    finds it, so a fully-initialised prefix looked "missing" on every
    single first launch — 3 pointless createprefix retries (each wiping
    the shared Steam Runtime cache) + a "Network Error" toast + a
    wineboot fallback, all failing the same way, before the game
    launched anyway.
    """
    root = tmp_path / "prefix"
    (root / "pfx").mkdir(parents=True)
    (root / "pfx" / "system.reg").write_text("reg")
    (root / "pfx" / "user.reg").write_text("reg")

    await pi._ensure_created(_plan(root, "GE-Proton10-34"), root)

    toast_spy.assert_not_called()


async def test_run_createprefix_with_retry_detects_success_under_pfx(
    tmp_path, toast_spy, monkeypatch,
):
    """A real createprefix success (registry lands under pfx/) must not retry."""
    root = tmp_path / "prefix"
    root.mkdir()

    async def _fake_run_umu(plan, env, *args):
        (root / "pfx").mkdir(parents=True, exist_ok=True)
        (root / "pfx" / "system.reg").write_text("reg")

    monkeypatch.setattr(pi, "_run_umu", _fake_run_umu)
    # The between-attempts hook is now the SURGICAL repair, not the
    # whole-cache nuke (which destroyed healthy variants and wedged every
    # store — see test_prefix_init_no_runtime_nuke.py). Either way, a
    # first-try success must not touch the runtime at all.
    cleanup = MagicMock()
    monkeypatch.setattr(pi, "repair_incomplete_umu_runtime", cleanup)

    ok = await pi._run_createprefix_with_retry(
        _plan(root, "GE-Proton10-34"), {}, root,
    )

    assert ok is True
    cleanup.assert_not_called()  # no retry needed → runtime never touched
    assert not any(
        c.args[0] == "toasts.launcher.retryingUmu" for c in toast_spy.call_args_list
    )


# ── _run_umu: bounded + process-group kill on hang ─────────────────
#
# Regression: a hung Proton/Wine boot (confirmed live -- a broken
# Proton-Experimental build spun wineserver forever inside createprefix)
# was never killed. ``_run_umu`` had no timeout at all, and the only
# thing that ever gave up (DownloadWorker's outer 600s watchdog) merely
# cancelled the awaiting Python coroutine without touching the actual
# subprocess -- start_new_session=True detaches it into its own
# session/process group, so a plain kill of the parent never reaches it.
# Multiple such trees were found still running, one having burned ~30%
# CPU for nearly an hour.

async def test_run_umu_kills_hung_process_on_timeout(tmp_path, monkeypatch):
    monkeypatch.setattr(pi, "_UMU_STEP_TIMEOUT_SECONDS", 0.2)
    pid_file = tmp_path / "pid"
    plan = SimpleNamespace(python_bin="/bin/bash", umu_wrapper="-c")

    await pi._run_umu(plan, {}, f"echo $$ > {pid_file}; exec sleep 100")

    pid = int(pid_file.read_text().strip())
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


async def test_run_umu_completes_normally_within_timeout(caplog):
    plan = SimpleNamespace(python_bin="/bin/true", umu_wrapper="")

    await pi._run_umu(plan, {})

    assert "killing process group" not in caplog.text


def test_kill_process_group_kills_real_process():
    proc = subprocess.Popen(["sleep", "100"], start_new_session=True)
    fake_proc = SimpleNamespace(pid=proc.pid)

    pi._kill_process_group(fake_proc)

    proc.wait(timeout=2)
    assert proc.returncode is not None


def test_kill_process_group_swallows_already_dead_process():
    proc = subprocess.Popen(["true"], start_new_session=True)
    proc.wait()
    fake_proc = SimpleNamespace(pid=proc.pid)

    pi._kill_process_group(fake_proc)  # must not raise
