"""Regression: the Ubisoft auth shortcut's ``Exe`` field must use the
casing Steam's shortcuts.vdf actually uses.

``stores/ubisoft/auth/shortcut.py`` read and wrote the lowercase key
``"exe"`` everywhere, but a real Steam-authored shortcuts.vdf entry
uses ``"Exe"`` (confirmed by parsing a live shortcuts.vdf on a test
device). Effects of the mismatch:

* A brand-new auth shortcut (``_add_canonical_if_missing``,
  ``add_shortcut_to_vdf``) was written with NO real ``Exe`` field at
  all -- Steam has nothing to execute, so clicking "Sign in to
  Ubisoft Connect" does nothing. This is the most likely explanation
  for the field reports of the sign-in button not launching UPC.
* The self-heal repair (``_fix_shortcut_fields``) always read an
  empty string back (wrong key), so it believed the exe was
  "outdated" on every single check and rewrote a harmless-but-wrong
  ``"exe"`` key that Steam never reads -- masking real staleness
  with a no-op "fix" that always claims success.
* The orphan-shortcut prune (``_prune_orphan_shortcuts``) always saw
  an empty exe too, making its "exe is empty" condition vacuous.

Existing correctly-cased entries (created before this bug, or by
other code paths) must keep working -- fixed reads check both cases.
"""
from __future__ import annotations

from typing import Any

from unifideck.stores.ubisoft.auth.shortcut import (
    _AuthShortcut,
    _prune_orphan_shortcuts,
)


class _Config:
    auth_shortcut_store_id = "ubisoft:upc-auth"
    auth_prefix_name = ".upc-auth"


class _Parent:
    _plugin_dir = "/home/deck/homebrew/plugins/Unifideck"
    _config = _Config()


def _auth_shortcut() -> _AuthShortcut:
    return _AuthShortcut(_Parent())


# ── _prune_orphan_shortcuts ────────────────────────────────────────────

def test_prune_orphan_ignores_entry_with_real_exe() -> None:
    """A real, working shortcut must never be pruned as an orphan."""
    shortcuts: dict[str, Any] = {
        "0": {
            "AppName": "Ubisoft Connect",
            "Exe": '"/home/deck/homebrew/plugins/Unifideck/bin/unifideck-launcher"',
            "LaunchOptions": "",
        },
    }

    removed = _prune_orphan_shortcuts(shortcuts)

    assert removed == []
    assert "0" in shortcuts


def test_prune_orphan_removes_truly_bare_entry() -> None:
    shortcuts: dict[str, Any] = {
        "0": {"AppName": "upc.exe", "Exe": "", "LaunchOptions": "", "appid": 77},
    }

    removed = _prune_orphan_shortcuts(shortcuts)

    # The appid is returned, not just a count: a bare row has no Exe, so
    # the write guard reads it as the user's and would block the delete
    # unless the caller declares it. See ``_dropped_appids``.
    assert removed == [77]
    assert "0" not in shortcuts


# ── _AuthShortcut._fix_shortcut_fields ─────────────────────────────────

def test_fix_shortcut_fields_no_change_when_already_correct() -> None:
    auth = _auth_shortcut()
    launcher_path = "/home/deck/homebrew/plugins/Unifideck/bin/unifideck-launcher"
    entry: dict[str, Any] = {
        "Exe": f'"{launcher_path}"',
        "LaunchOptions": "ubisoft:upc-auth UNIFIDECK_UBISOFT_ACTION=auth",
        "appid": 123,
    }

    changed = auth._fix_shortcut_fields(
        entry, launcher_path, entry["LaunchOptions"], 123,
    )

    assert changed is False
    assert entry["Exe"] == f'"{launcher_path}"'
    assert "exe" not in entry


def test_fix_shortcut_fields_updates_stale_exe_and_cleans_phantom_key() -> None:
    """A phantom lowercase 'exe' left by the old bug must be removed."""
    auth = _auth_shortcut()
    old_path = "/home/deck/old-build/bin/unifideck-launcher"
    new_path = "/home/deck/homebrew/plugins/Unifideck/bin/unifideck-launcher"
    entry: dict[str, Any] = {
        "Exe": f'"{old_path}"',
        "exe": f'"{old_path}"',  # phantom key from the pre-fix code
        "LaunchOptions": "ubisoft:upc-auth UNIFIDECK_UBISOFT_ACTION=auth",
        "appid": 123,
    }

    changed = auth._fix_shortcut_fields(
        entry, new_path, entry["LaunchOptions"], 123,
    )

    assert changed is True
    assert entry["Exe"] == f'"{new_path}"'
    assert "exe" not in entry


def test_fix_shortcut_fields_never_writes_lowercase_key() -> None:
    auth = _auth_shortcut()
    launcher_path = "/home/deck/homebrew/plugins/Unifideck/bin/unifideck-launcher"
    entry: dict[str, Any] = {"LaunchOptions": "", "appid": 0}

    auth._fix_shortcut_fields(entry, launcher_path, "expected-options", 123)

    assert entry["Exe"] == f'"{launcher_path}"'
    assert "exe" not in entry


# ── _AuthShortcut._add_canonical_if_missing / add_shortcut_to_vdf ──────

def test_add_canonical_writes_capital_exe_key() -> None:
    """A freshly-created shortcut must have a real, Steam-readable Exe."""
    auth = _auth_shortcut()
    shortcuts: dict[str, Any] = {}
    launcher_path = "/home/deck/homebrew/plugins/Unifideck/bin/unifideck-launcher"

    added = auth._add_canonical_if_missing(shortcuts, launcher_path, 123, 123)

    assert added is True
    entry = next(iter(shortcuts.values()))
    assert entry["Exe"] == f'"{launcher_path}"'
    assert "exe" not in entry
