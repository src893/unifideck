"""launcher/proton/infrastructure/wineserver_reap.py — reap orphaned wineservers.

Wine names each prefix's server runtime dir
``$XDG_RUNTIME_DIR-less fallback → /tmp/.wine-<uid>/server-<st_dev>-<st_ino_hex>``
keyed by the WINEPREFIX directory's device+inode. The wineserver holds a
``lock`` file there and, once started, **detaches** from the umu-run that
spawned it — so killing the umu-run process group (see
``umu_runtime._kill_process_group``) leaves the wineserver alive holding
that lock.

That is exactly what wedges install warmup: each timed-out compat step
leaves an orphaned wineserver on ``server-<dev>-<ino>/lock``; the retry
(same prefix → same server dir) spawns a new wineserver that blocks
forever waiting for the orphan to release the lock. Retries stack more
stuck wineservers on the same lock. Confirmed live: a prefix at
dev=66312 ino=0x3e07ce had four wineservers piled on
``/tmp/.wine-1000/server-10308-3e07ce/lock``.

This module reaps the wineserver(s) bound to a given prefix (by matching
the server dir in their open fds), and sweeps server dirs whose owning
prefix no longer exists.

**The scope is one prefix, not one process tree. Every process in that
prefix dies — live or orphaned, whoever started it.** A different prefix
means a different server dir and is never touched, but that is the only
thing this is surgical about. Callers must own the prefix.

An earlier version of this docstring promised that "a wineserver for a
game the user is actually running is never touched", reasoning that a
running game lives in its own prefix. That holds for every store whose
prefix hosts exactly one umu run — and is false for Battle.net, where a
long-lived client and its short-lived ``--exec`` commands share one
prefix. The launcher's phase-C exec is cancelled on a 60 s timeout,
which reached here and SIGKILLed the client the *user's own launch* had
started: the Battle.net Agent died mid-download, and every Diablo II
install stalled inside a minute. See ``handlers/battlenet.py`` and
``umu_runtime.run_umu_with_retry``'s ``reap_wineserver`` opt-out.
"""
from __future__ import annotations

import contextlib
import logging
import os
import signal
from pathlib import Path

logger = logging.getLogger(__name__)

# Wine's own fixed, uid-scoped server-dir root — not our choice and not a
# temp file we create; we only read/reap under it. (noqa S108.)
_WINE_TMP = Path(f"/tmp/.wine-{os.getuid()}")  # noqa: S108


def server_dir_for_prefix(prefix: Path) -> str | None:
    """Wine server dir *name* for ``prefix`` (``server-<dev_hex>-<ino_hex>``).

    Returns ``None`` if the prefix can't be stat'd. Wine keys the server
    dir off the WINEPREFIX dir's device+inode; the umu prefix layout puts
    the Wine tree under ``<prefix>`` (its ``pfx`` symlink resolves to the
    same inode), so ``prefix`` is the right thing to stat.

    **Both** the device and inode are formatted as lowercase hex — Wine's
    ``server_dir`` uses ``%x`` for each (verified on-device: a prefix at
    dev=66312 shows up as ``server-10308-…`` = hex(66312), NOT decimal).
    An earlier decimal-device build silently matched nothing, so the reap
    was a no-op and orphaned wineservers kept stacking.
    """
    try:
        st = os.stat(prefix)
    except OSError:
        return None
    return f"server-{st.st_dev:x}-{st.st_ino:x}"


def _pids_holding_server_dir(server_name: str) -> list[int]:
    """PIDs with an open fd under ``/tmp/.wine-<uid>/<server_name>/``.

    Scans ``/proc/<pid>/fd`` for a symlink into the server dir (the
    wineserver holds ``.../lock``; its Wine children hold the socket).
    Best-effort — unreadable procs are skipped.
    """
    target = str(_WINE_TMP / server_name)
    pids: list[int] = []
    try:
        proc_entries = os.listdir("/proc")
    except OSError:
        return pids
    for entry in proc_entries:
        if not entry.isdigit():
            continue
        pid = int(entry)
        fd_dir = f"/proc/{entry}/fd"
        try:
            fds = os.listdir(fd_dir)
        except OSError:
            continue  # proc gone or not ours
        for fd in fds:
            try:
                dest = os.readlink(f"{fd_dir}/{fd}")
            except OSError:
                continue
            if dest.startswith(target):
                pids.append(pid)
                break
    return pids


def prefix_wine_pids(prefix: Path | str) -> list[int]:
    """PIDs of every Wine process bound to ``prefix``, without killing any.

    The read-only half of :func:`reap_prefix_wineserver`, split out because
    "is anything alive in this prefix" is a question worth asking on its own —
    a live wineserver owns the prefix registry and rewrites it from memory
    when it exits, so a write underneath one is discarded in silence. See
    ``wine_registry.registry_is_writable``, which takes this count.

    Deliberately here rather than reusing ``handlers/battlenet_watch.scan``:
    that module is Blizzard-specific (its ``EXCLUDED_IMAGES`` and
    ``_client_pids`` name Battle.net images), and this is asked from
    store-agnostic dispatch.

    Empty for a prefix that cannot be stat'd, which is the same answer a
    prefix with nothing running gives — both mean "nothing owns the
    registry", and neither is an error.
    """
    server_name = server_dir_for_prefix(Path(prefix))
    if server_name is None:
        return []
    return _pids_holding_server_dir(server_name)


def reap_prefix_wineserver(prefix: Path) -> int:
    """SIGKILL any wineserver/Wine process bound to ``prefix``'s server dir.

    Returns the number of processes signalled. Call on a umu-step timeout
    *before* retrying the same prefix, so the retry doesn't deadlock on the
    orphan this step left behind. No-op (returns 0) if the prefix can't be
    resolved or nothing holds its server dir.
    """
    server_name = server_dir_for_prefix(prefix)
    if server_name is None:
        return 0
    pids = _pids_holding_server_dir(server_name)
    killed = 0
    for pid in pids:
        try:
            os.kill(pid, signal.SIGKILL)
            killed += 1
        except ProcessLookupError:
            pass
        except OSError as e:
            logger.warning("[wineserver_reap] kill %s failed: %s", pid, e)
    if killed:
        logger.warning(
            "[wineserver_reap] reaped %d orphaned wine process(es) on %s "
            "(prefix=%s)", killed, server_name, prefix,
        )
    return killed


def sweep_orphaned_server_dirs(live_prefixes: list[Path]) -> int:
    """Reap wineservers whose prefix is among ``live_prefixes`` but orphaned.

    Pre-warmup hygiene: before an install's prefix setup, clear any
    wineserver still holding the server dir of a unifideck prefix (left by
    a prior hung/timed-out run) so the fresh setup starts clean. Only the
    server dirs of the given prefixes are considered — never a wineserver
    for an unrelated prefix (e.g. a running game). Returns processes reaped.

    Also removes stale ``server-*`` dirs with no live holder, so Wine
    doesn't trip over an abandoned lock file.
    """
    reaped = 0
    wanted = {
        name
        for p in live_prefixes
        if (name := server_dir_for_prefix(p)) is not None
    }
    for server_name in wanted:
        reaped += reap_prefix_wineserver_by_name(server_name)
    # Remove abandoned lock dirs (no holder left) for our prefixes.
    for server_name in wanted:
        d = _WINE_TMP / server_name
        if d.is_dir() and not _pids_holding_server_dir(server_name):
            with contextlib.suppress(OSError):
                for f in d.iterdir():
                    f.unlink()
                d.rmdir()
    return reaped


def reap_prefix_wineserver_by_name(server_name: str) -> int:
    """Reap by a precomputed server-dir name (see :func:`reap_prefix_wineserver`)."""
    pids = _pids_holding_server_dir(server_name)
    killed = 0
    for pid in pids:
        try:
            os.kill(pid, signal.SIGKILL)
            killed += 1
        except ProcessLookupError:
            pass
        except OSError as e:
            logger.warning("[wineserver_reap] kill %s failed: %s", pid, e)
    if killed:
        logger.warning(
            "[wineserver_reap] swept %d orphaned wine process(es) on %s",
            killed, server_name,
        )
    return killed
