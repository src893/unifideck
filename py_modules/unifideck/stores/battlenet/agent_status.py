"""Read Blizzard's Agent state out of the logs it writes inside the prefix.

py_modules/unifideck/stores/battlenet/agent_status.py

**The Agent runs exactly one exclusive operation at a time.** That single fact
is why this module exists, and it was measured the expensive way: a user's
installs all sat at a 0% bar labelled "Queued" with a resume arrow, looking
paused. Nothing was paused. The Agent had made its own self-update the active
operation and the game was behind it in the queue::

    Operations-20260822T123916.log
      12:39:16  Queue operation - OP_UPDATE for 'agent'
      12:39:16  Active operation nullptr replaced by OP_UPDATE for 'agent'
      12:40:10  Queue operation - OP_UPDATE for 'd1'      <- waits here
      13:05:44  Active operation nullptr replaced by OP_UPDATE for 'd1'
      13:07:10  OP_UPDATE for 'd1' completed              <- 86 seconds

Twenty-eight minutes of "stuck", then the game itself downloaded in under two.
Every second of the wait was correct and none of it was legible, so the user
cancelled. Cancelling deletes the prefix and throws the agent update away, making
the next attempt identically "stuck". Naming the wait is what breaks that loop.

Why the logs and not the Agent's REST API. The Agent does serve one, on
``127.0.0.1`` at the port in ``ProgramData/Battle.net/Agent/Agent.dat``, and it
is reachable from the host, but every endpoint answers 401 and the
``--session=`` value it is launched with is not the basic-auth credential. The
logs cost nothing, need no handshake, and survive the client exiting.

**Inherited logs are the trap here.** A game prefix is an rsync clone of
``.template``, so it arrives carrying the *previous* runs' Agent logs. Reading
"newest file" without qualification will happily report a completion that
happened days ago in a different prefix under a different region tag. Every
entry point therefore takes ``since`` and ignores any log not written after it.

Backend-side only (bundled Python 3.11); nothing here is imported by the
launcher. Best-effort throughout: a parse failure returns "I don't know", never
an exception, because every caller is either a progress message or a bounded
wait and neither may fail an install.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

#: The Agent's logs live under a build-versioned directory, e.g.
#: ``Agent.9700/Logs``. The build changes, so it is globbed, not named.
_LOGS_GLOB = "ProgramData/Battle.net/Agent/Agent.*/Logs"

#: Uids that are the Agent updating *itself* or the Battle.net client, as
#: opposed to a game. Both block a game's operation and both are worth naming.
SELF_UIDS = frozenset({"agent", "bna", "battle.net"})

#: How much of a log to read. Operations logs are ~1 KB and AgentUpdate tops
#: out around 20 KB even after a 45-minute update, so this only ever guards
#: against a pathological file.
_MAX_TAIL_BYTES = 128 * 1024

# ``Active operation nullptr replaced by OP_UPDATE for 'agent'``
_ACTIVE_RE = re.compile(r"Active operation .* replaced by (.+?)\s*$")
# ``OP_UPDATE for 'agent'``: the tail of the line above, and of a completion.
_OP_RE = re.compile(r"OP_UPDATE for '([^']+)'")
# ``OP_UPDATE for 'agent' completed`` (with or without a ``Concurrent`` prefix)
_DONE_RE = re.compile(r"OP_UPDATE for '([^']+)' completed")
# ``Queue operation - OP_UPDATE for 'd1'``
_QUEUED_RE = re.compile(r"Queue operation - OP_UPDATE for '([^']+)'")
# ``Start Update of agent w/ tags (Volatile Windows KR? acct-IND? geoip-IN?)``
_TAGS_RE = re.compile(r"Start Update of agent w/ tags \(([^)]*)\)")
# ``[I 2026-08-22 12:49:58.0099] agent Update Progress - 0.7543 (0.7543)``
# Anchored on the phrase, not on a field offset: the line begins with a
# bracketed ``[I <date> <time>]`` stamp whose *space-separated* field count is
# not what it looks like (``[I``, the date, then ``12:49:58.0099]``), so a
# leading ``^\S+\s+`` captured the date and this quietly reported no progress
# at all against a real log.
_PROGRESS_RE = re.compile(r"(\S+) Update Progress - ([0-9.]+)")


@dataclass(frozen=True, slots=True)
class AgentState:
    """What the Agent's own logs say it is doing right now.

    ``active`` is the uid holding the single exclusive slot, or ``None`` when
    nothing does. ``queued`` is every uid that asked and has not been given it.
    ``progress`` is 0..1 for ``active``, when that operation reports any.
    """

    active: str | None = None
    queued: frozenset[str] = frozenset()
    completed: frozenset[str] = frozenset()
    progress: float | None = None

    @property
    def blocked_by_self_update(self) -> bool:
        """Whether a game is waiting on the Agent or client updating itself."""
        return self.active in SELF_UIDS

    def is_queued(self, uid: str) -> bool:
        """Whether ``uid`` asked for the slot and has not been given it."""
        return uid in self.queued and self.active != uid

    def finished(self, uid: str) -> bool:
        """Whether ``uid``'s update ran to completion in this log."""
        return uid in self.completed


def _tail(path: Path) -> list[str]:
    """The last :data:`_MAX_TAIL_BYTES` of ``path`` as lines, or empty."""
    try:
        size = path.stat().st_size
        with path.open("rb") as handle:
            if size > _MAX_TAIL_BYTES:
                handle.seek(size - _MAX_TAIL_BYTES)
            return handle.read().decode("utf-8", "replace").splitlines()
    except OSError:
        return []


def _newest_since(logs_dir: Path, pattern: str, since: float) -> Path | None:
    """Newest ``pattern`` log in ``logs_dir`` written after ``since``.

    The ``since`` filter is the whole point. See the module docstring on
    inherited logs. A clone's logs predate the run asking about them, and
    trusting one reports a stale completion as a live one.
    """
    newest: Path | None = None
    newest_mtime = since
    try:
        candidates = list(logs_dir.glob(pattern))
    except OSError:
        return None
    for path in candidates:
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        if mtime > newest_mtime:
            newest, newest_mtime = path, mtime
    return newest


def logs_dir(drive_c: Path) -> Path | None:
    """The Agent's build-versioned log directory inside ``drive_c``."""
    try:
        found = sorted(drive_c.glob(_LOGS_GLOB))
    except OSError:
        return None
    return found[-1] if found else None


def _scan_operations(lines: list[str]) -> tuple[str | None, set[str], set[str]]:
    """``(active, queued, completed)`` from an ``Operations-*.log``."""
    active: str | None = None
    queued: set[str] = set()
    completed: set[str] = set()
    for line in lines:
        switch = _ACTIVE_RE.search(line)
        if switch:
            op = _OP_RE.search(switch.group(1))
            active = op.group(1) if op else None
            # Taking the slot is leaving the queue. Without this an operation
            # stays "queued" for the rest of the log after it starts running.
            queued.discard(active or "")
            continue
        done = _DONE_RE.search(line)
        if done:
            completed.add(done.group(1))
            queued.discard(done.group(1))
            if active == done.group(1):
                active = None
            continue
        asked = _QUEUED_RE.search(line)
        if asked:
            queued.add(asked.group(1))
    return active, queued, completed


def _progress_for(lines: list[str], uid: str) -> float | None:
    """The last reported fraction for ``uid`` in an ``AgentUpdate-*.log``."""
    for line in reversed(lines):
        match = _PROGRESS_RE.search(line)
        if match and match.group(1) == uid:
            try:
                return float(match.group(2))
            except ValueError:
                return None
    return None


def read_state(drive_c: Path, since: float) -> AgentState | None:
    """The Agent's current operation state, or ``None`` when unknowable.

    ``None`` and an empty :class:`AgentState` mean different things and callers
    depend on the difference: ``None`` is "the Agent has not written anything
    since ``since``" (it may not have started yet), while an empty state is
    "it has, and nothing holds the slot".
    """
    directory = logs_dir(Path(drive_c))
    if directory is None:
        return None
    operations = _newest_since(directory, "Operations-*.log", since)
    if operations is None:
        return None
    active, queued, completed = _scan_operations(_tail(operations))
    progress = None
    if active is not None:
        updates = _newest_since(directory, "AgentUpdate-*.log", since)
        if updates is not None:
            progress = _progress_for(_tail(updates), active)
    return AgentState(
        active=active,
        queued=frozenset(queued),
        completed=frozenset(completed),
        progress=progress,
    )


def describe_wait(drive_c: Path, since: float, uid: str) -> str | None:
    """A sentence explaining why ``uid`` is not downloading yet, or ``None``.

    ``None`` means "nothing worth saying": the game holds the slot, or the
    Agent has not written anything yet. The caller keeps its generic
    tick. Only a genuinely explicable wait produces a sentence.

    The "Don't cancel" clause is not padding. Cancelling deletes the prefix,
    which discards the very agent update being waited on, so the next attempt
    starts from zero. Users who cancelled here did so three times in a row.
    """
    state = read_state(Path(drive_c), since)
    if state is None or not state.is_queued(uid):
        return None
    percent = (
        f" ({state.progress * 100:.0f}%)" if state.progress is not None else ""
    )
    if not state.blocked_by_self_update:
        return (
            f"Queued in Battle.net behind '{state.active}'{percent}. Your "
            "download starts when that finishes. Don't cancel."
        )
    # 'agent' is the downloader component, 'bna'/'battle.net' the client
    # itself. Worth distinguishing: the agent update is the slow one, and a
    # user told "Battle.net is updating itself" while the visible client sits
    # idle has been given a sentence that contradicts what they can see.
    what = (
        "Battle.net is updating its downloader"
        if state.active == "agent"
        else "Battle.net is updating itself"
    )
    return (
        f"{what}{percent}. Your download starts when it finishes. "
        "Don't cancel."
    )


def update_generation(drive_c: Path, since: float) -> str | None:
    """The TACT tag query the Agent last updated *itself* for, or ``None``.

    This is what makes one copy of the Agent's content store interchangeable
    with another, and it is not derivable from anything on disk. The store's
    size actively misleads (a completed update *shrinks* it, because the Agent
    compacts away content the new tags do not select). The Agent states it
    outright, once per update::

        Start Update of agent w/ tags (Volatile Windows KR? acct-IND? geoip-IN?)

    Two prefixes whose stores were built for the same string hold equivalent
    content; two built for different strings do not, however similar the
    byte counts look. Handed to ``wrapper_client_cache`` as an opaque token.
    """
    directory = logs_dir(Path(drive_c))
    if directory is None:
        return None
    ngdp = _newest_since(directory, "AgentNGDP-*.log", since)
    if ngdp is None:
        return None
    for line in reversed(_tail(ngdp)):
        match = _TAGS_RE.search(line)
        if match:
            return match.group(1).strip() or None
    return None


def self_update_finished(drive_c: Path, since: float) -> bool:
    """Whether the Agent has finished updating *itself* since ``since``.

    False while it is still running and false when we cannot tell, so a caller
    waiting on this always has to carry its own timeout. That is deliberate:
    the alternative, treating "no logs yet" as done, would return instantly
    on a client that has not started.
    """
    state = read_state(Path(drive_c), since)
    if state is None:
        return False
    return "agent" in state.completed and not state.blocked_by_self_update
