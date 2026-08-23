"""Disabling gamescope's WSI layer is measured per host, never assumed.

The Battle.net client's ANGLE renderer aborts inside
``VK_LAYER_FROG_gamescope_wsi`` on some GPUs and not others. Measured
2026-08-19 on two machines running the *same* SteamOS 3.8.25 (build
20260807.2), kernel 6.18.42-valve2 and gamescope 3.16.23.5:

* Steam Deck, Van Gogh ``1002:163f`` — the client works.
* ROG Ally X, Phoenix ``1002:15BF`` — the client aborts.

Turning the layer off costs the XWayland-bypass path (direct scanout, HDR)
and the *game* inherits it too, because the game is started by the client.
So the tests that matter most here are the ones proving a healthy host
never pays: no marker, no environment variable, no retry.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from unifideck.launcher.proton.handlers import battlenet_wsi as wsi

# The real tail of the ROG Ally X game log, trimmed.
_CRASH_LOG = """\
[Gamescope WSI] Application info:
  pApplicationName: Battle.net.exe
  applicationVersion: 1
  pEngineName: ANGLE
  engineVersion: 1
[Gamescope WSI] Forcing on VK_EXT_swapchain_maintenance1.
Fossilize INFO: Overriding serialization path: "/home/deck/.local/share/Steam/x".
../subprojects/vkroots/vkroots.h:129: const DispatchType* vkroots::tables::\
VkDispatchTableMap<Object, DispatchType, DispatchPtr>::insert(Object, DispatchPtr) \
[with Object = VkQueue_T*]: Assertion `obj' failed.
"""

# The tail of the Ubisoft log from the SAME device and session: identical
# layer lines, engine vkd3d, no abort. This one must NOT match.
_HEALTHY_LOG = """\
[Gamescope WSI] Application info:
  pApplicationName: UplayWebCore.exe
  applicationVersion: 0
  pEngineName: vkd3d
  engineVersion: 12587008
[Gamescope WSI] Forcing on VK_EXT_swapchain_maintenance1.
Fossilize INFO: Overriding serialization path: "/home/deck/.local/share/Steam/y".
"""


@pytest.fixture(autouse=True)
def _isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Never read or write the developer's real marker."""
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "share"))


def _log(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "launch.game.log"
    path.write_text(body, encoding="utf-8")
    return path


# --------------------------------------------------------------------------
# recognising the one crash this works around
# --------------------------------------------------------------------------


def test_the_angle_abort_is_recognised(tmp_path: Path) -> None:
    assert wsi.crashed_in_gamescope_wsi(_log(tmp_path, _CRASH_LOG)) is True


def test_a_healthy_wsi_launch_is_not_mistaken_for_it(tmp_path: Path) -> None:
    """Ubisoft's CEF child clears the identical layer lines on the same host.

    Keying on the WSI banner alone would disable the layer for everyone.
    """
    assert wsi.crashed_in_gamescope_wsi(_log(tmp_path, _HEALTHY_LOG)) is False


def test_an_unrelated_crash_is_not_blamed_on_the_layer(tmp_path: Path) -> None:
    body = "wine: Unhandled page fault\nAssertion `obj' failed.\n"
    assert wsi.crashed_in_gamescope_wsi(_log(tmp_path, body)) is False


def test_a_missing_or_empty_log_is_not_a_match(tmp_path: Path) -> None:
    assert wsi.crashed_in_gamescope_wsi(None) is False
    assert wsi.crashed_in_gamescope_wsi(tmp_path / "nope.log") is False
    assert wsi.crashed_in_gamescope_wsi(_log(tmp_path, "")) is False


def test_the_signature_is_found_after_a_long_session(tmp_path: Path) -> None:
    """A client that ran for a while writes megabytes before it dies."""
    body = ("chatter\n" * 200_000) + _CRASH_LOG
    assert wsi.crashed_in_gamescope_wsi(_log(tmp_path, body)) is True


# --------------------------------------------------------------------------
# a healthy host pays nothing
# --------------------------------------------------------------------------


def test_a_host_with_no_marker_gets_no_environment_change() -> None:
    env: dict[str, str] = {}
    assert wsi.apply_if_recorded(env) is False
    assert env == {}


def test_the_marker_makes_it_stick() -> None:
    """One slow launch per host, not one per launch."""
    assert wsi.workaround_recorded() is False
    wsi.record_workaround("test")
    assert wsi.workaround_recorded() is True

    env: dict[str, str] = {}
    assert wsi.apply_if_recorded(env) is True
    assert env[wsi.DISABLE_VAR] == "1"


def test_the_marker_is_host_scoped_not_per_prefix() -> None:
    """It is a property of the graphics stack, so every game inherits it."""
    wsi.record_workaround("test")
    assert wsi.marker_path().parent.name == "unifideck"
    assert "prefixes" not in str(wsi.marker_path())


def test_a_corrupt_marker_reads_as_absent(tmp_path: Path) -> None:
    """Failing towards "no workaround" keeps the healthy path the default."""
    path = wsi.marker_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")
    assert wsi.workaround_recorded() is False


def test_a_marker_saying_false_is_honoured(tmp_path: Path) -> None:
    path = wsi.marker_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"disable_gamescope_wsi": False}), encoding="utf-8")
    assert wsi.workaround_recorded() is False


def test_recording_never_raises_into_the_launch(monkeypatch: pytest.MonkeyPatch) -> None:
    """A failed write costs a repeated slow launch, never a failed one."""
    def _boom(*_a: object, **_k: object) -> None:
        raise OSError("read-only")

    monkeypatch.setattr(Path, "write_text", _boom)
    wsi.record_workaround("test")  # must not raise
    assert wsi.workaround_recorded() is False


def test_the_disable_var_is_the_layers_documented_switch() -> None:
    """Not ENABLE_GAMESCOPE_WSI=0.

    The layer manifest declares ``disable_environment:
    {"DISABLE_GAMESCOPE_WSI": "1"}`` and the loader tests for that key's
    presence; the gamescope session sets the enable key itself.
    """
    assert wsi.DISABLE_VAR == "DISABLE_GAMESCOPE_WSI"
