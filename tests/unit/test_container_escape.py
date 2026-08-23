"""Tests for launcher.proton.infrastructure.container_escape.

Field bug: setting Steam's own Properties > Compatibility "Force the use of
a specific Steam Play compatibility tool" on a Unifideck shortcut makes Steam
wrap ``bin/unifideck-launcher`` in ITS OWN pressure-vessel container. Proton's
``python3`` cannot resolve ``libz.so.1`` in there, so umu exits 127 —
*whichever* Proton the user picks, which is why trying different builds to
work around it never helped.

Reproduced deterministically on-device by entering the container exactly the
way Steam does and running the identical umu command (fails, container python
``May 5 2026``), then escaping it (succeeds, host python ``Jun 21 2025``,
rc=0). ``UMU_NO_RUNTIME=1`` was also verified NOT to help.

Second field bug (2026-08-12 bundle, SteamOS desktop): escaping alone was not
enough. ``argv[0]`` is the interpreter ``selector.find_python_3_10_plus``
picked by probing THIS process's filesystem — the container's — but the
escaped command runs on the host. Where the two differ (container python3.13,
host 3.14.6) every Force-Compat launch died in 0.0s with rc=127 and a game log
of nothing but ``env: '/usr/bin/python3.13': No such file or directory``. The
interpreter is now re-resolved on the far side at exec time.

These tests pin the escape's decision logic and argv shape.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from unifideck.launcher.proton.infrastructure import container_escape as ce

_ARGV = ["/usr/bin/python3.13", "/plugin/bin/umu/umu/umu-run", "/games/G.exe"]
# What ``_ARGV`` becomes once the interpreter is deferred to the host.
_SHIMMED_TAIL = _ARGV[1:]


def _command_of(out: list[str]) -> list[str]:
    """The command portion of an escaped argv (everything past the env pairs)."""
    return out[out.index("sh"):]


@pytest.fixture
def _containerised(monkeypatch: pytest.MonkeyPatch):
    """Pretend we're inside pressure-vessel with the escape client present."""
    monkeypatch.setattr(ce, "in_pressure_vessel", lambda: True)
    monkeypatch.setattr(
        ce.shutil, "which", lambda _n: "/usr/bin/steam-runtime-launch-client",
    )


def test_noop_when_not_containerised(monkeypatch):
    """The normal (unwrapped) launch must be left completely alone."""
    monkeypatch.setattr(ce, "in_pressure_vessel", lambda: False)
    assert ce.escape_argv(_ARGV, {"GAMEID": "umu-0"}, None) == _ARGV


def test_noop_when_escape_client_missing(monkeypatch):
    """No client → run as before rather than breaking outright."""
    monkeypatch.setattr(ce, "in_pressure_vessel", lambda: True)
    monkeypatch.setattr(ce.shutil, "which", lambda _n: None)
    assert ce.escape_argv(_ARGV, {"GAMEID": "umu-0"}, None) == _ARGV


def test_wraps_with_alongside_steam(_containerised):
    out = ce.escape_argv(_ARGV, {"GAMEID": "umu-0"}, None)
    assert out[0] == "/usr/bin/steam-runtime-launch-client"
    assert out[1] == "--alongside-steam"
    assert "--" in out and "env" in out
    # The umu command survives intact at the end; only the interpreter in
    # front of it is swapped for the host-resolving shim.
    assert out[-len(_SHIMMED_TAIL):] == _SHIMMED_TAIL


def test_passes_cwd_as_directory(_containerised):
    out = ce.escape_argv(_ARGV, {}, Path("/games/The Gap"))
    assert "--directory=/games/The Gap" in out


def test_inherited_container_vars_dropped_ours_forwarded(
    _containerised, monkeypatch,
):
    """Only what this launch deliberately set crosses the boundary.

    ``PATH`` is the load-bearing case: it is inherited from the container
    and must NOT be forwarded, or the escaped process would resolve
    binaries against container paths instead of the clean host ones.
    """
    monkeypatch.setattr(
        ce.os, "environ", {"PATH": "/container/bin", "HOME": "/home/deck"},
    )
    env = {
        "PATH": "/container/bin",      # inherited, unchanged → dropped
        "HOME": "/home/deck",          # inherited, unchanged → dropped
        "GAMEID": "umu-0",             # ours → forwarded
        "PROTONPATH": "/ge",           # ours → forwarded
        "WINEDLLOVERRIDES": "x=n,b",   # ours → forwarded
    }
    out = ce.escape_argv(_ARGV, env, None)
    pairs = out[out.index("env") + 1: out.index("sh")]
    assert "PATH=/container/bin" not in pairs
    assert "HOME=/home/deck" not in pairs
    assert "GAMEID=umu-0" in pairs
    assert "PROTONPATH=/ge" in pairs
    assert "WINEDLLOVERRIDES=x=n,b" in pairs


def test_always_forward_survives_identical_container_value(
    _containerised, monkeypatch,
):
    """Critical umu vars are forwarded even if the container already had
    the same value — the escaped process starts from Steam's env, not ours."""
    monkeypatch.setattr(ce.os, "environ", {"WINEPREFIX": "/pfx"})
    out = ce.escape_argv(_ARGV, {"WINEPREFIX": "/pfx"}, None)
    assert "WINEPREFIX=/pfx" in out


def test_container_interpreter_never_exec_target(_containerised):
    """The regression itself: the container's python must not be the exec target.

    Shipping ``/usr/bin/python3.13`` across the boundary is what produced
    ``env: '/usr/bin/python3.13': No such file or directory`` and rc=127 on a
    host that only had 3.14.
    """
    out = ce.escape_argv(_ARGV, {}, None)
    command = _command_of(out)
    assert command[0] == "sh"
    assert command[1] == "-c"
    # The container path may appear inside the candidate list (it is a
    # legitimate candidate), but never as the thing being executed.
    assert "/usr/bin/python3.13" not in command[3:]


def test_shim_tries_every_candidate_in_selector_order(_containerised):
    """The shim walks the SAME list as the selector, in the same order."""
    from unifideck.launcher.proton.infrastructure.selector import (
        PYTHON_CANDIDATES,
    )
    import shlex
    script = _command_of(ce.escape_argv(_ARGV, {}, None))[2]
    # Parse the loop's word list rather than substring-searching: the last
    # candidate (/usr/bin/python3) is a prefix of every other one.
    loop_line = next(ln for ln in script.splitlines() if ln.startswith("for p in"))
    listed = shlex.split(loop_line.removeprefix("for p in").rstrip("; do"))
    assert listed == PYTHON_CANDIDATES
    assert "-x" in script and "exec" in script


def test_shim_preserves_arguments_with_spaces(_containerised):
    """``/home/deck/Games/Cyberpunk 2077/...`` must stay ONE argument."""
    argv = [
        "/usr/bin/python3.13",
        "/plugin/bin/umu/umu/umu-run",
        "/home/deck/Games/Cyberpunk 2077/REDprelauncher.exe",
    ]
    command = _command_of(ce.escape_argv(argv, {}, None))
    assert command[-1] == "/home/deck/Games/Cyberpunk 2077/REDprelauncher.exe"
    # $0 is filled so the real arguments start at $1.
    assert command[3] == "sh"
    assert command[4:] == argv[1:]


def test_shim_actually_resolves_a_working_interpreter(_containerised):
    """End-to-end: a nonexistent interpreter still lands on a real one."""
    import subprocess
    argv = [
        "/usr/bin/python3.99",  # exists in no container and on no host
        "-c",
        "import sys; print(sys.argv[1])",
        "/games/The Gap/g.exe",
    ]
    command = _command_of(ce.escape_argv(argv, {}, None))
    proc = subprocess.run(command, capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "/games/The Gap/g.exe"


def test_non_python_command_passed_through(_containerised):
    """A command that isn't an interpreter is escaped but not rewritten."""
    argv = ["/usr/bin/some-tool", "--flag"]
    assert ce.escape_argv(argv, {}, None)[-len(argv):] == argv


def test_no_shim_when_not_containerised(monkeypatch):
    """Outside a container the interpreter was resolved correctly already."""
    monkeypatch.setattr(ce, "in_pressure_vessel", lambda: False)
    assert ce.escape_argv(_ARGV, {}, None) == _ARGV


def test_detection_reads_lowercase_container_var(monkeypatch):
    """pressure-vessel sets lowercase ``container`` (OCI convention)."""
    monkeypatch.setattr(ce.os, "environ", {"container": "pressure-vessel"})
    monkeypatch.setattr(ce.Path, "is_dir", lambda _self: False)
    assert ce.in_pressure_vessel() is True

    monkeypatch.setattr(ce.os, "environ", {})
    assert ce.in_pressure_vessel() is False
