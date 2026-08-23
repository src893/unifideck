"""Where a launch's umu / Proton / game output goes, and how to read it back.

py_modules/unifideck/launcher/proton/infrastructure/game_log.py

Proton, Wine and the game itself write to stdout+stderr, which the Python
logging archive does **not** capture. Without this, a run that died left no
trace at all and had to be reproduced by hand — and one that did exactly
that cost a full round trip with a tester: the Battle.net client started,
aborted ~20 s later, and its output had gone to ``DEVNULL``, so a
five-minute failure had to be reasoned about from four other logs instead
of read off disk.

Split out of ``umu_runtime`` when :func:`game_log_path` was added and the
module crossed its size cap. The grouping is natural rather than
convenient: these three functions are the whole answer to "where does this
launch's output live", and both the writer (``run_umu_with_retry``,
``prefix_init``, the Battle.net client start) and the reader
(``battlenet_wsi``, which tells one crash from another by its signature)
need it.

Stdlib-only; runs under the SYSTEM python (3.10-3.14).
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def launches_dir() -> Path:
    """The per-launch log archive.

    Resolved per call, not at import — the same trap as
    ``wrapper_session.prefix_index_path``: a module-level constant is
    computed before pytest's fixtures redirect ``HOME``, so it keeps
    pointing at the developer's real data directory for a whole run.
    """
    return Path(
        os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local" / "share")),
    ) / "unifideck" / "launches"


def game_log_path() -> Path | None:
    """This launch's game-output log, or None when it cannot be resolved.

    Exposed so a caller can *read back* what a run printed. The Battle.net
    handler uses it to tell one specific crash (an ANGLE abort inside the
    gamescope WSI layer) apart from every other way a client can fail to
    start — the difference between a workaround measured on the host that
    needs it and one applied to every host.
    """
    from unifideck.launcher.diagnostics.correlation import get_launch_id
    try:
        return launches_dir() / f"{get_launch_id()}.game.log"
    except Exception:
        logger.debug("[launcher.game_log] path unresolved", exc_info=True)
        return None


def open_game_log() -> Any:
    """Open this launch's game-output log for umu stdout+stderr.

    Returns ``None`` on any error, in which case the caller inherits
    stdout/stderr as before — a launch must never fail because its log
    could not be opened.
    """
    path = game_log_path()
    if path is None:
        return None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        return path.open("a", encoding="utf-8", errors="replace")
    except OSError as e:
        logger.debug("[launcher.game_log] open failed: %s", e)
        return None
