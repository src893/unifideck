"""Amazon, Ubisoft and Battle.net must launch in the user's language too.

The Epic path has its own suite (``test_epic_launch_language.py``). These
cover the other stores whose handlers build a ``ConfigManager`` of
their own inside the launcher process, because the same two defects hit
all of them:

* the bundled ``config.json`` was looked up at ``<plugin>/defaults/`` only,
  which does not exist on a Decky CLI install (the CLI flattens
  ``defaults/`` to the plugin root), so nothing but the hardcoded
  ``_FALLBACK`` was merged;
* no ``user_path`` was passed, so the language the user picked was never
  read whatever the layout.

Both tests drive the *real* ``ConfigManager`` over a fixture laid out the
way an installed plugin is, with the machine pinned to a language the
user did not choose — so a fallback can never masquerade as success.
"""
from __future__ import annotations

import json
import types
from pathlib import Path
from typing import Any

import pytest

from unifideck.launcher.proton.infrastructure.core import ProtonLaunchPlan
from unifideck.utils.locale import get_unifideck_locale


def _installed_plugin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, user_locale: str | None,
) -> Path:
    """Lay down the packaged layout + user config; return the plugin dir."""
    plugin_dir = tmp_path / "plugin"
    plugin_dir.mkdir(parents=True)
    repo_defaults = Path(__file__).resolve().parents[2] / "defaults/config.json"
    # The Decky CLI layout: defaults/config.json flattened to the install
    # root. This is what a plugin installed from a CLI-built zip looks
    # like, and the hardcoded nested path found nothing in it.
    (plugin_dir / "config.json").write_text(
        repo_defaults.read_text(encoding="utf-8"), encoding="utf-8",
    )
    user_cfg = tmp_path / "user_config.json"
    if user_locale is not None:
        user_cfg.write_text(
            json.dumps({"ui": {"locale": user_locale}}), encoding="utf-8",
        )
    monkeypatch.setenv("UNIFIDECK_USER_CONFIG", str(user_cfg))
    monkeypatch.setenv("HOME", str(tmp_path))       # no registry.vdf here
    # Pin what the resolver sees as the machine's locale. Patching
    # ``getlocale`` rather than ``LANG``: Python reads the process locale
    # set at startup, so setting the env mid-test proves nothing.
    import unifideck.utils.locale as locale_module
    monkeypatch.setattr(
        locale_module._locale, "getlocale", lambda: ("en_US", "UTF-8"),
    )
    return plugin_dir


def _plan(plugin_dir: Path, work_dir: Path, store: str) -> ProtonLaunchPlan:
    """A minimal launch plan (same shape as test_epic_launch_language)."""
    return ProtonLaunchPlan(
        context=types.SimpleNamespace(
            game_id="abc123", store=store,
            exe_path=work_dir / "game.exe",
            work_dir=work_dir,
            plugin_dir=plugin_dir,
        ),
        state=types.SimpleNamespace(wrappers=[], game_args=[], umu_id=None),
        python_bin=Path("/usr/bin/python3"),
        umu_wrapper=plugin_dir / "bin/umu/umu/umu-run",
        prefix_path=work_dir / "prefix",
        env={},
        on_process_start=None,
    )


def _capture_config(
    monkeypatch: pytest.MonkeyPatch, func_name: str,
) -> list[Any]:
    """Record the ConfigManager each handler hands to language setup.

    The handlers swallow every exception around language setup — a launch
    must never be blocked by it — so assertions have to happen outside the
    captured call, not inside it.
    """
    seen: list[Any] = []
    import unifideck.launcher.proton.language_setup as ls

    def _record(*_args: Any, config: Any = None, **_kw: Any) -> bool:
        seen.append(config)
        return True

    monkeypatch.setattr(ls, func_name, _record)
    return seen


# ── Ubisoft ────────────────────────────────────────────────────────────
def test_ubisoft_honours_the_language_picked_in_the_unifideck_ui(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from unifideck.launcher.proton.handlers.ubisoft import _apply_language_setup

    plugin_dir = _installed_plugin(tmp_path, monkeypatch, "es-ES")
    seen = _capture_config(monkeypatch, "apply_ubisoft_language")

    _apply_language_setup(_plan(plugin_dir, tmp_path, "ubisoft"))

    assert len(seen) == 1
    assert get_unifideck_locale(seen[0]) == "es-ES"


def test_ubisoft_falls_back_to_the_machine_when_nothing_was_picked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from unifideck.launcher.proton.handlers.ubisoft import _apply_language_setup

    plugin_dir = _installed_plugin(tmp_path, monkeypatch, None)
    seen = _capture_config(monkeypatch, "apply_ubisoft_language")

    _apply_language_setup(_plan(plugin_dir, tmp_path, "ubisoft"))

    assert get_unifideck_locale(seen[0]) == "en-US"


# ── the prefix locale: one path, every store ───────────────────────────
#
# This used to be three per-store wrappers and is now a single call in
# ``proton.dispatch``. The tests below drive that call rather than any
# handler, and are parametrised over stores precisely because the store is
# no longer supposed to matter.


def _dispatch_plan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, store: str,
    user_locale: str | None,
) -> ProtonLaunchPlan:
    plugin_dir = _installed_plugin(tmp_path, monkeypatch, user_locale)
    return _plan(plugin_dir, tmp_path, store)


def _quiet_prefix(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A prefix with a US ``user.reg`` and nothing running in it.

    The registry body is the one measured on this Deck's Battle.net prefixes,
    including the ``pfx -> .`` self-symlink umu creates — the earlier version
    of this test omitted ``pfx`` entirely and so never exercised the layout
    that actually failed.
    """
    prefix = tmp_path / "prefix"
    prefix.mkdir(exist_ok=True)
    (prefix / "pfx").symlink_to(".")
    (prefix / "user.reg").write_text(
        "WINE REGISTRY Version 2\n\n"
        "[Control Panel\\\\International] 1785947765\n"
        '"Locale"="00000409"\n'
        '"LocaleName"="en-US"\n'
        '"sLanguage"="ENU"\n'
        '"sCountry"="United States"\n',
        encoding="utf-8",
    )
    _set_wine_pids(monkeypatch, [])
    return prefix


def _set_wine_pids(monkeypatch: pytest.MonkeyPatch, pids: list[int]) -> None:
    """Pin how many Wine processes the prefix looks like it has.

    Never left to the real scanner: it reads ``/proc``, so an unpinned test
    would pass or fail according to whatever the developer's Deck happens to
    be running. That is not hypothetical — three tests in
    ``test_battlenet_launch`` do exactly that and fail whenever a Battle.net
    client is up.
    """
    import unifideck.launcher.proton.infrastructure.wineserver_reap as reap
    monkeypatch.setattr(reap, "prefix_wine_pids", lambda _prefix: pids)


@pytest.mark.parametrize("store", ["battlenet", "epic", "gog", "amazon"])
def test_every_store_gets_the_prefix_locale(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, store: str,
) -> None:
    """The point of moving it: Epic and GOG never got one before."""
    from unifideck.launcher.proton import _apply_prefix_language

    prefix = _quiet_prefix(tmp_path, monkeypatch)
    plan = _dispatch_plan(tmp_path, monkeypatch, store, "de-DE")

    _apply_prefix_language(plan)

    written = (prefix / "user.reg").read_text(encoding="utf-8")
    assert '"LocaleName"="de-DE"' in written
    assert '"sLanguage"="DEU"' in written
    assert '"sCountry"="Germany"' in written
    assert '"Locale"="00000407"' in written


def test_the_prefix_locale_falls_back_to_the_machine(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    prefix = _quiet_prefix(tmp_path, monkeypatch)
    from unifideck.launcher.proton import _apply_prefix_language

    _apply_prefix_language(_dispatch_plan(tmp_path, monkeypatch, "gog", None))

    assert '"LocaleName"="en-US"' in (prefix / "user.reg").read_text(encoding="utf-8")


def test_a_busy_prefix_is_refused_not_silently_discarded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The defect this guard exists for, stated as a test.

    A live wineserver holds the registry in memory and rewrites the file when
    it exits, so a write underneath one vanishes while every log line claims
    success. Measured on-device 2026-08-23: the launcher logged ``wrote
    locale=fr-FR`` for a prefix that still read ``en-US`` half an hour later.
    """
    from unifideck.launcher.proton import _apply_prefix_language

    prefix = _quiet_prefix(tmp_path, monkeypatch)
    _set_wine_pids(monkeypatch, [32802])
    before = (prefix / "user.reg").read_text(encoding="utf-8")

    _apply_prefix_language(_dispatch_plan(tmp_path, monkeypatch, "battlenet", "de-DE"))

    assert (prefix / "user.reg").read_text(encoding="utf-8") == before
    assert '"LocaleName"="en-US"' in before


def test_a_write_that_does_not_survive_is_reported_as_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The read-back, which is what turns a silent loss into a warning."""
    import unifideck.launcher.proton.language_setup.registry_io as rio

    prefix = _quiet_prefix(tmp_path, monkeypatch)
    original = (prefix / "user.reg").read_text(encoding="utf-8")

    def _write_then_lose_it(path: str, _content: str) -> None:
        Path(path).write_text(original, encoding="utf-8")

    monkeypatch.setattr(rio, "_atomic_write_text", _write_then_lose_it)

    assert rio._apply_windows_locale(str(prefix), "de-DE") is False


def test_the_prefix_locale_never_fails_a_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A game in the wrong language beats a game that will not start."""
    import unifideck.launcher.proton.language_setup as ls
    from unifideck.launcher.proton import _apply_prefix_language

    def _boom(*_args: Any, **_kw: Any) -> bool:
        raise OSError("user.reg is not writable")

    monkeypatch.setattr(ls, "apply_prefix_language", _boom)

    _apply_prefix_language(_dispatch_plan(tmp_path, monkeypatch, "epic", "de-DE"))
