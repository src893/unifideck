"""Trustworthy reads of Steam's ``shortcuts.vdf``.

``persistence.read_vdf`` historically swallowed *every* exception and
returned ``{"shortcuts": {}}`` — indistinguishable from "the file does
not exist yet". That conflation is a data-loss funnel: a file we failed
to parse reads as an empty library, reconcile then rebuilds our entries
from scratch, ``merge_foreign_shortcuts`` re-reads with the same parser
and also gets nothing to merge back, and the write that follows replaces
the user's whole non-Steam library with only our own shortcuts. The
reconcile tally still says ``removed=0``, because from reconcile's point
of view nothing *was* removed.

This module makes the failure legible instead. A read has three
outcomes and the caller must decide what to do about each:

* :attr:`VdfStatus.MISSING`  — no file. An empty library is the truth.
* :attr:`VdfStatus.PARSED`   — entries are trustworthy.
* :attr:`VdfStatus.UNREADABLE` — the bytes exist but we cannot vouch for
  our view of them. Callers must refuse to write.

The ``UNREADABLE`` verdict covers two distinct failures. The obvious one
is a raised exception (our vendored ``vdf`` defaults to
``raise_on_remaining=True``, so a third-party writer leaving trailing
bytes fails the *whole* parse rather than truncating it). The subtle one
is a parse that succeeds while seeing fewer entries than the file
actually holds — so we cross-check every parse against a
parser-independent byte scan and distrust any shortfall. Never trust a
derived count over the source bytes.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

import vdf

logger = logging.getLogger(__name__)

# Every shortcuts.vdf entry opens with a 32-bit ``appid`` field, so
# counting this marker in the raw bytes yields an entry count that does
# not depend on the parser agreeing with us. Deliberately duplicated
# from ``support_bundle/counts.py`` rather than imported: one 12-byte
# constant is cheaper than a dependency between two unrelated services.
_VDF_APPID_KEY = b"\x02appid\x00"


class VdfStatus(Enum):
    """How much the caller may trust a :class:`VdfRead`."""

    MISSING = "missing"
    PARSED = "parsed"
    UNREADABLE = "unreadable"


@dataclass(frozen=True)
class VdfRead:
    """One read of ``shortcuts.vdf`` plus the confidence we have in it."""

    status: VdfStatus
    data: dict[str, Any]
    #: Entries the parser produced. 0 when unreadable.
    parsed_count: int = 0
    #: Entries the raw byte scan found. Diverges from ``parsed_count``
    #: only when the parser is losing content.
    raw_count: int = 0
    #: Populated on UNREADABLE — why, for the log line.
    reason: str = ""

    @property
    def trustworthy(self) -> bool:
        """True when the caller may write back based on this read."""
        return self.status is not VdfStatus.UNREADABLE


def count_entries_in_bytes(raw: bytes) -> int:
    """Count shortcut entries by scanning raw bytes, ignoring the parser."""
    return raw.count(_VDF_APPID_KEY)


def entries_of(data: dict[str, Any]) -> dict[str, Any]:
    """Return the inner ``shortcuts`` sub-dict of a loaded vdf (or ``{}``).

    ``shortcuts.vdf`` wraps entries under a top-level ``"shortcuts"``
    key; a third party can leave the file in a shape without it.
    """
    inner = data.get("shortcuts") if isinstance(data, dict) else None
    return inner if isinstance(inner, dict) else {}


def read_vdf_sync(shortcuts_path: str) -> VdfRead:
    """Blocking read. Prefer :func:`read_vdf_checked`; this exists for
    sync callers such as the support-bundle probes.
    """
    path = Path(shortcuts_path)
    if not path.is_file():
        return VdfRead(VdfStatus.MISSING, {"shortcuts": {}})

    try:
        raw = path.read_bytes()
    except OSError as e:
        return _unreadable(f"could not read bytes: {e}")

    raw_count = count_entries_in_bytes(raw)

    try:
        data = vdf.binary_loads(raw)  # type: ignore[no-untyped-call]
    except Exception as e:  # any parser failure at all is a distrust signal
        return _unreadable(f"parse failed: {e}", raw_count=raw_count)

    if not isinstance(data, dict):
        return _unreadable(
            f"parsed to {type(data).__name__}, not a dict", raw_count=raw_count,
        )
    inner = data.get("shortcuts")
    if inner is not None and not isinstance(inner, dict):
        # A non-dict ``shortcuts`` root used to be silently replaced with
        # an empty one, which discards the whole library on the next write.
        return _unreadable(
            f"'shortcuts' root is {type(inner).__name__}, not a dict",
            raw_count=raw_count,
        )

    parsed_count = len(entries_of(data))
    if parsed_count < raw_count:
        return _unreadable(
            f"parser saw {parsed_count} entries but the bytes hold "
            f"{raw_count} — refusing to treat the parse as complete",
            raw_count=raw_count,
        )

    return VdfRead(
        VdfStatus.PARSED, data,
        parsed_count=parsed_count, raw_count=raw_count,
    )


def _unreadable(reason: str, *, raw_count: int = 0) -> VdfRead:
    """Build the UNREADABLE verdict with an empty, never-writable payload."""
    return VdfRead(
        VdfStatus.UNREADABLE, {"shortcuts": {}},
        parsed_count=0, raw_count=raw_count, reason=reason,
    )


async def read_vdf_checked(shortcuts_path: str) -> VdfRead:
    """Read ``shortcuts.vdf`` and report how far it can be trusted.

    Offloaded via ``to_thread`` since the vdf library is sync. Logs the
    reason on an UNREADABLE verdict — that line is the only warning a
    user gets before we decline to touch their file, so it names the
    counts that disagreed.
    """
    result = await asyncio.to_thread(read_vdf_sync, shortcuts_path)
    if result.status is VdfStatus.UNREADABLE:
        logger.error(
            "[ShortcutPersistence] shortcuts.vdf is UNREADABLE (%s) — "
            "treating it as untouchable so no write can drop the user's "
            "own non-Steam shortcuts",
            result.reason,
        )
    return result
