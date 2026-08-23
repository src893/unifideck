"""support_bundle/probe_conflicts.py — Live state and interference.

The artifacts past investigations actually needed and had no way to
get. Each block maps to a concrete failure class:

* **Other Decky plugins.** Third-party interference is a recurring root
  cause, not an edge case: an external launcher-management plugin whose
  scanner rewrites ``shortcuts.vdf`` is the whole "synced but zero
  games" cluster. Until now a bug report never said what else was
  installed.
* **Scheduled writers.** That rewrite has a *schedule*. Seeing the
  timer armed is the answer; without it we are guessing at a race.
* **Toolchain processes and Wine locks.** A stranded ``wineserver`` or
  ``upc.exe`` is a documented cause of installs hanging during
  first-run setup.
* **Session-variable recoverability.** The install-time hang came down
  to the headless backend lacking four session variables, borrowed back
  from the live Steam process. If that borrow fails on a user's device,
  this is the only way to see it.

Everything is read-only: processes are named, never signalled; lock
files are stat-ed, never removed.
"""
from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any

from . import procscan

logger = logging.getLogger(__name__)

# The four variables the install-time prefix warmup needs. Reported as
# present/absent only; values never leave the device.
SESSION_VARS = (
    "DISPLAY", "WAYLAND_DISPLAY", "XDG_RUNTIME_DIR",
    "DBUS_SESSION_BUS_ADDRESS",
)
# Plugins known to write shortcuts.vdf, so a conflict is nameable
# rather than merely suspected.
SHORTCUT_WRITERS = ("nonsteamlaunchers", "steamgriddb", "shortcuts")
_NSL_GLOBS = ("NonSteamLaunchers*", ".config/systemd/user/*onsteam*")
_UNIT_DIR = ".config/systemd/user"


def _entry_state(path: Path) -> dict[str, Any]:
    """Existence, size and mtime for one path."""
    try:
        info = path.stat()
    except OSError:
        return {"path": str(path), "present": False}
    return {
        "path": str(path),
        "present": True,
        "size": info.st_size,
        "modified": time.strftime(
            "%Y-%m-%d %H:%M:%S", time.localtime(info.st_mtime),
        ),
        "mtime": info.st_mtime,
    }


def plugins_block() -> dict[str, Any]:
    """Every other Decky plugin installed, with its version."""
    root = Path.home() / "homebrew" / "plugins"
    if not root.is_dir():
        return {"available": False, "plugins": []}
    found: list[dict[str, Any]] = []
    try:
        children = sorted(root.iterdir())
    except OSError as err:
        return {"available": False, "error": repr(err), "plugins": []}
    for child in children:
        if child.is_dir():
            found.append(_plugin_summary(child))
    return {
        "available": True,
        "plugins": found,
        "known_shortcut_writers": [
            item["name"] for item in found
            if any(w in item["name"].lower() for w in SHORTCUT_WRITERS)
        ],
    }


def _plugin_summary(path: Path) -> dict[str, Any]:
    """Name, version and last-modified for one installed plugin."""
    version = "unknown"
    manifest = path / "package.json"
    if manifest.is_file():
        version = _json_version(manifest)
    state = _entry_state(path)
    return {
        "name": path.name,
        "version": version,
        "modified": state.get("modified", ""),
    }


def _json_version(path: Path) -> str:
    """Read ``version`` out of a package.json."""
    import json

    try:
        parsed = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError):
        return "unreadable"
    return str(parsed.get("version", "unknown")) if isinstance(parsed, dict) else "unknown"


def scheduled_writers_block() -> dict[str, Any]:
    """User systemd units belonging to third-party shortcut writers.

    Enablement is derived from a ``*.wants/`` symlink rather than by
    shelling out to ``systemctl``, which needs a session bus the
    backend may not have.
    """
    home = Path.home()
    units: list[dict[str, Any]] = []
    for pattern in _NSL_GLOBS:
        units.extend(_glob_states(home, pattern))
    return {
        "candidates": units,
        "enabled_units": _enabled_units(home / _UNIT_DIR),
    }


def _glob_states(base: Path, pattern: str) -> list[dict[str, Any]]:
    """Stat every path matching ``pattern`` under ``base``."""
    try:
        return [_entry_state(match) for match in sorted(base.glob(pattern))]
    except OSError as err:
        logger.debug("[support_bundle] glob %s failed: %s", pattern, err)
        return []


def _enabled_units(unit_dir: Path) -> list[str]:
    """Unit names symlinked into any ``*.wants`` directory."""
    if not unit_dir.is_dir():
        return []
    enabled: list[str] = []
    try:
        for wants in unit_dir.glob("*.wants"):
            enabled.extend(sorted(link.name for link in wants.iterdir()))
    except OSError:
        return enabled
    return enabled


def processes_block() -> dict[str, Any]:
    """Snapshot of allowlisted toolchain processes."""
    processes = procscan.iter_processes()
    return {
        "count": len(processes),
        "processes": [
            {
                "pid": item.pid,
                "name": item.name,
                "uid": item.uid,
                "started_at": item.started_at,
                "started": _stamp(item.started_at),
                "cmdline": item.cmdline,
            }
            for item in processes
        ],
    }


def _stamp(epoch: float | None) -> str:
    """Format an epoch as local time, "" when unknown."""
    if epoch is None:
        return ""
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(epoch))


def wine_locks_block(data_dir: str | None) -> dict[str, Any]:
    """Wine server sockets and per-prefix lock timestamps.

    The other half of the stuck-install picture: a socket directory
    left behind by a dead wineserver, or a prefix whose update
    timestamp says setup started and never finished.
    """
    # /tmp is where wineserver puts its socket dirs; we only stat
    # them, never create anything there.
    sockets = _glob_states(Path("/tmp"), ".wine-*")  # noqa: S108
    prefixes: list[dict[str, Any]] = []
    if data_dir:
        prefixes = _prefix_locks(Path(data_dir) / "prefixes")
    return {"wineserver_sockets": sockets, "prefix_locks": prefixes}


def _prefix_locks(root: Path) -> list[dict[str, Any]]:
    """Per-prefix lock and setup state, without reading the prefix.

    Handles both layouts. Most stores put a prefix directly under
    ``prefixes/<id>``, but Ubisoft namespaces its own one level deeper
    (``prefixes/ubisoft/{.template, .upc-auth, <game-uuid>}``). Scanning
    only the top level meant Ubisoft — the store most prone to installs
    that hang during first-run setup — contributed no lock state at all,
    because ``prefixes/ubisoft/pfx`` does not exist.
    """
    if not root.is_dir():
        return []
    found: list[dict[str, Any]] = []
    for child in _dirs(root):
        if (child / "pfx").is_dir() or (child / "drive_c").is_dir():
            found.append(_prefix_state(child.name, child))
            continue
        # A namespace directory: report each prefix underneath it.
        for nested in _dirs(child):
            found.append(_prefix_state(f"{child.name}/{nested.name}", nested))
    return found


def _dirs(root: Path) -> list[Path]:
    """Immediate subdirectories of ``root``, tolerating errors."""
    try:
        return sorted(child for child in root.iterdir() if child.is_dir())
    except OSError:
        return []


def _prefix_state(label: str, path: Path) -> dict[str, Any]:
    """Setup markers for one prefix. Never reads prefix contents.

    ``.update-timestamp`` says Wine finished writing the prefix;
    ``pfx.lock`` left behind points at an interrupted setup; the
    bootstrap marker records that our own first-run step completed.
    """
    stamp = path / "pfx" / ".update-timestamp"
    if not stamp.is_file():
        stamp = path / ".update-timestamp"
    markers = [
        item.name for item in path.glob("*.marker") if item.is_file()
    ]
    return {
        "prefix": label,
        "update_timestamp": _entry_state(stamp),
        "pfx_lock_present": (path / "pfx.lock").exists(),
        "markers": markers,
    }


def session_env_block() -> dict[str, Any]:
    """Whether the session variables can be borrowed from Steam.

    An empty ``variables`` map with ``readable`` false means we could
    not read Steam's environment at all, which is a different problem
    from Steam having started without those variables set.
    """
    processes = procscan.iter_processes()
    steam = next((p for p in processes if p.name == "steam"), None)
    if steam is None:
        return {"steam_running": False, "readable": False, "variables": {}}
    variables = procscan.env_of(steam.pid, SESSION_VARS)
    return {
        "steam_running": True,
        "steam_pid": steam.pid,
        "readable": bool(variables),
        "variables": variables,
        "all_present": bool(variables) and all(variables.values()),
    }


def shortcuts_race_block(shortcuts_path: str | None) -> dict[str, Any]:
    """Compare the shortcut file's write time to Steam's start time.

    Steam reads ``shortcuts.vdf`` only at startup. If we wrote it after
    Steam started, a missing shortcut is a pending restart rather than
    a bug, and that distinction currently costs several messages to
    establish.
    """
    if not shortcuts_path:
        return {"resolved": False}
    state = _entry_state(Path(shortcuts_path))
    processes = procscan.iter_processes()
    steam_started = procscan.newest_start(processes, "steam")
    written = state.get("mtime")
    stale = None
    if isinstance(written, float) and steam_started is not None:
        stale = written > steam_started
    return {
        "resolved": True,
        "shortcuts_vdf": state,
        "steam_started": _stamp(steam_started),
        "written_after_steam_start": stale,
        "siblings": _glob_states(Path(shortcuts_path).parent, "shortcuts.vdf.*"),
    }


def shortcuts_census_block(
    shortcuts_path: str | None, data_dir: str | None, launcher_path: str | None,
) -> dict[str, Any]:
    """Split shortcuts.vdf into ours vs the user's, and count backups.

    The one measurement that separates "this user has no non-Steam games"
    from "ours are all that survived" — the question every report of
    vanished shortcuts turns on, and the one a bundle could not answer.
    Parses the file directly rather than reading the service's cache,
    since the bundle is collected out-of-band from any sync.
    """
    if not shortcuts_path:
        return {"resolved": False}

    from unifideck.services.shortcut.vdf_backup import backup_dir
    from unifideck.services.shortcut.vdf_read import VdfStatus, read_vdf_sync
    from unifideck.services.shortcut.write_guard import census

    result = read_vdf_sync(shortcuts_path)
    if result.status is VdfStatus.UNREADABLE:
        return {
            "resolved": True, "readable": False, "reason": result.reason,
            "raw_entry_count": result.raw_count,
        }

    counts = census(
        result.data.get("shortcuts", {}) or {}, launcher_path or "",
    )
    backups = (
        _glob_states(backup_dir(data_dir), "shortcuts.vdf.bak.*")
        if data_dir else []
    )
    return {
        "resolved": True,
        "readable": True,
        "total": counts.total,
        "ours": counts.ours,
        "foreign": counts.foreign,
        "backup_count": len(backups),
        "backup_newest": backups[0].get("modified") if backups else None,
        "backups": backups,
    }
