"""support_bundle/inventory.py — What exists that we did not collect.

The deliberate counterpart to the collector. Every artifact we exclude
by policy — Wine prefixes, the browser profile, save data, installed
games — still gets *enumerated*: name, size, mtime, mode. Never a byte
of content.

The reasoning is that "we did not ship this" and "this is not there"
are completely different answers to a support question, and the audit
only distinguishes them for paths someone thought to register. This
module closes the rest of the gap: it walks the excluded areas so an
engineer can see a save backup exists, a prefix was built, or a game
directory is empty, without any of it leaving the device.

Two hard rules:

* **Names and metadata only.** Nothing here opens a file. Contents of
  prefixes and browser profiles stay on the device.
* **Bounded.** Each root has its own depth and entry cap, because a
  Wine prefix holds tens of thousands of files. Whenever a cap trims
  the walk it is stated in the output rather than silently applied.
"""
from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any, NamedTuple

logger = logging.getLogger(__name__)

# Per-root entry cap. Deep enough to be useful, bounded enough that a
# prefix's drive_c cannot flood the report.
_ENTRY_CAP = 400

# Known state inside a Ubisoft prefix. Checked by name rather than
# found by walking, because their *absence* is the diagnostic: a
# prefix with upc.exe but no ownership file has Ubisoft Connect
# installed and no entitlement data, which is a specific failure.
_UPC_PROBES = (
    "pfx/drive_c/Program Files (x86)/Ubisoft/Ubisoft Game Launcher/upc.exe",
    "pfx/drive_c/ProgramData/Ubisoft/Ubisoft Game Launcher/settings.yml",
    "pfx/drive_c/ProgramData/Ubisoft/Ubisoft Game Launcher/ownership",
    "pfx/drive_c/ProgramData/Ubisoft/Ubisoft Game Launcher/logs",
    "pfx/drive_c/users/steamuser/AppData/Local/Ubisoft Game Launcher",
    "pfx/drive_c/users/steamuser/AppData/Local/Ubisoft Game Launcher/cache/http2",
    "config_info",
    "version",
    "tracked_files",
)

# Same idea for Battle.net, whose failures are also absence-shaped. The
# client exe missing means the prefix never got bootstrapped; CachedData.db
# missing means the user never completed sign-in, so the library is empty
# for a reason that is not a bug; and the warmed marker missing on
# ``.template`` is exactly why an install refuses to clone.
# A glob probe can match many directories; enough to see the self-update
# pair (a new build lands beside the old one) without listing a tree.
_GLOB_MATCH_CAP = 8

_BNET_PROBES = (
    "pfx/drive_c/Program Files (x86)/Battle.net/Battle.net.exe",
    "pfx/drive_c/Program Files (x86)/Battle.net/Battle.net Launcher.exe",
    # The client payload. ``Battle.net.exe`` above is a ~1 MB shim written
    # early in the install; this DLL is the client it loads, and an
    # interrupted install leaves the first without the second. That prefix
    # passes as "has a client" to the naked eye and cannot start one.
    # A DLL, not an exe — the versioned dir holds no ``Battle.net.exe``.
    "pfx/drive_c/Program Files (x86)/Battle.net/Battle.net.*/battle.net.dll",
    "pfx/drive_c/ProgramData/Battle.net/Agent/product.db",
    "pfx/drive_c/ProgramData/Battle.net/Agent/data/cache",
    "pfx/drive_c/users/steamuser/AppData/Roaming/Battle.net/Battle.net.config",
    ".unifideck_battlenet",
    ".unifideck_battlenet_tweaks.v1",
    "config_info",
    "version",
)

# (prefix subdirectory, section tag, client name, probes) per wrapper store.
# Both namespace their prefixes a level deeper than every other store.
#
# The tag is spelled out rather than derived from the store id: triage notes
# and saved greps refer to ``ubisoft_upc_state`` by name, so renaming it to
# match a new convention would silently break searching old bundles.
_WRAPPER_PREFIX_PROBES: tuple[tuple[str, str, str, tuple[str, ...]], ...] = (
    ("ubisoft", "ubisoft_upc_state", "Ubisoft Connect", _UPC_PROBES),
    ("battlenet", "battlenet_client_state", "Battle.net", _BNET_PROBES),
)


class Root(NamedTuple):
    """One directory tree to enumerate."""

    label: str
    path: str
    depth: int
    note: str = ""


def _stat_line(path: Path, indent: str) -> str:
    """One ``kind name size mtime mode`` row."""
    try:
        info = path.stat()
    except OSError as err:
        return f"{indent}{path.name}  <unreadable: {err.strerror}>"
    kind = "dir " if path.is_dir() else "file"
    stamp = time.strftime("%Y-%m-%d %H:%M", time.localtime(info.st_mtime))
    return (
        f"{indent}{kind} {path.name:<44} {info.st_size:>12}  {stamp}  "
        f"{oct(info.st_mode & 0o7777)}"
    )


def _walk(root: Path, depth: int, budget: list[int], indent: str = "  ") -> list[str]:
    """Enumerate ``root`` to ``depth``, spending from ``budget``."""
    rows: list[str] = []
    try:
        children = sorted(root.iterdir())
    except OSError as err:
        return [f"{indent}<unreadable: {err.strerror}>"]
    for child in children:
        if budget[0] <= 0:
            rows.append(f"{indent}... (entry cap reached, {_ENTRY_CAP} shown)")
            return rows
        budget[0] -= 1
        rows.append(_stat_line(child, indent))
        if child.is_dir() and depth > 1:
            rows.extend(_walk(child, depth - 1, budget, indent + "  "))
    return rows


def _roots(ctx: Any, install_dirs: list[str]) -> list[Root]:
    """Every tree worth enumerating, with its own depth."""
    data = ctx.root("data")
    home = ctx.root("home") or str(Path.home())
    roots = [
        Root("data", data or "", 2),
        Root("config", ctx.root("config") or "", 1),
        Root("decky_logs", ctx.root("decky_logs") or "", 1),
        Root("launches", ctx.root("launches") or "", 1),
    ]
    if data:
        roots.extend([
            # Depth 2 lists every prefix and, for the namespaced
            # Ubisoft layout, every prefix inside it. Depth 3 descended
            # into drive_c and burned the whole entry budget on one
            # prefix, truncating the list it was meant to show.
            Root("prefixes", f"{data}/prefixes", 2, "excluded from the archive"),
            Root("edge_auth_profile", f"{data}/edge-auth", 1,
                 "browser profile, excluded (holds cookies)"),
            Root("saves", f"{data}/saves", 2, "excluded from the archive"),
            Root("save_backups", f"{data}/save_backups", 2,
                 "excluded from the archive"),
            Root("ubisoft_installer_cache", f"{data}/ubisoft_installer_cache", 1),
        ])
    steam = ctx.root("steam")
    if steam:
        # ~186 MB, nearly all CEF/webhelper noise, so only the launch,
        # compat and install logs are collected. Enumerating the whole
        # directory means an engineer can still ask for a specific one.
        roots.append(
            Root("steam_logs", f"{steam}/logs", 1,
                 "only compat/console/content/gameprocess/cloud/shader collected"),
        )
    roots.extend([
        Root("legendary_config", f"{home}/.config/legendary", 1),
        Root("nile_config", f"{home}/.config/nile", 1),
        Root("umu_runtime", f"{home}/.local/share/umu", 1,
             "runtime payload excluded; completeness is checked instead"),
    ])
    # Depth 1: the complete list of installed games. Descending into
    # them spent the budget on one game's file tree and cut the list
    # off partway, losing the very thing it was there to show.
    roots.extend(
        Root(f"install_location[{index}]", raw, 1, "installed games")
        for index, raw in enumerate(install_dirs)
    )
    return roots


def build_inventory(ctx: Any, install_dirs: list[str]) -> str:
    """Render the existence inventory."""
    lines = [
        "EXISTENCE INVENTORY",
        "=" * 60,
        "",
        "Names, sizes and modes only - no file contents are read here.",
        "This covers what is on the device but deliberately NOT collected",
        "(Wine prefixes, the browser profile, save data, installed games),",
        "so 'we did not ship it' can be told apart from 'it is not there'.",
        "",
    ]
    for root in _roots(ctx, install_dirs):
        lines.extend(_render_root(root))
    lines.extend(_render_upc(ctx))
    return "\n".join(lines) + "\n"


def _render_root(root: Root) -> list[str]:
    """Header plus enumeration for one root."""
    suffix = f"  ({root.note})" if root.note else ""
    if not root.path:
        return [f"[{root.label}] <unresolved>{suffix}", ""]
    path = Path(root.path)
    if not path.exists():
        return [f"[{root.label}] {root.path}{suffix}", "  <does not exist>", ""]
    if not path.is_dir():
        return [f"[{root.label}] {root.path}{suffix}", _stat_line(path, "  "), ""]
    budget = [_ENTRY_CAP]
    return [f"[{root.label}] {root.path}{suffix}", *_walk(path, root.depth, budget), ""]


def _render_upc(ctx: Any) -> list[str]:
    """Existence of known vendor-client state, per wrapper-store prefix.

    Probed by name because absence is the signal. The wrapper stores are
    the ones whose prefixes are namespaced a level deeper, and whose
    install and sign-in failures usually come down to which of these exist.
    """
    data = ctx.root("data")
    if not data:
        return []
    lines: list[str] = []
    for store, tag, label, probes in _WRAPPER_PREFIX_PROBES:
        lines.extend(
            _render_wrapper_prefixes(Path(data), store, tag, label, probes),
        )
    return lines


def _render_wrapper_prefixes(
    data: Path, store: str, section: str, label: str, probes: tuple[str, ...],
) -> list[str]:
    """The per-prefix existence block for one wrapper store."""
    tag = f"[{section}]"
    base = data / "prefixes" / store
    if not base.is_dir():
        return [f"{tag} no {label} prefixes on this device", ""]
    lines = [f"{tag} per-prefix {label} state (existence only)"]
    try:
        prefixes = sorted(child for child in base.iterdir() if child.is_dir())
    except OSError as err:
        return [*lines, f"  <unreadable: {err.strerror}>", ""]
    for prefix in prefixes:
        lines.append(f"  {prefix.name}")
        lines.extend(f"    {row}" for row in _upc_rows(prefix, probes))
    lines.append("")
    return lines


def _upc_rows(prefix: Path, probes: tuple[str, ...] = _UPC_PROBES) -> list[str]:
    """One existence row per known vendor path inside ``prefix``.

    A probe containing ``*`` is expanded, because some of what matters is
    named after a version. Battle.net's client payload lives in
    ``Battle.net.<build>/`` and its *absence* is the whole diagnosis for an
    interrupted client install — a bundle that could not report it cost a
    field investigation the one fact that would have ended it in a line.
    """
    rows: list[str] = []
    for relative in probes:
        rows.extend(
            _glob_rows(prefix, relative) if "*" in relative
            else [_stat_row(prefix / relative, relative)],
        )
    return rows


def _stat_row(target: Path, label: str) -> str:
    """``EXISTS``/``absent`` for one concrete path."""
    try:
        info = target.stat()
    except OSError:
        return f"absent   {label}"
    size = "dir" if target.is_dir() else f"{info.st_size} bytes"
    return f"EXISTS   {label}  ({size})"


def _glob_rows(prefix: Path, pattern: str) -> list[str]:
    """One row per match, or a single ``absent`` row when nothing matches."""
    try:
        matches = sorted(prefix.glob(pattern))
    except OSError as err:
        return [f"absent   {pattern}  <unreadable: {err.strerror}>"]
    if not matches:
        return [f"absent   {pattern}  (no matches)"]
    return [
        _stat_row(match, str(match.relative_to(prefix)))
        for match in matches[:_GLOB_MATCH_CAP]
    ]
