"""Whether a failed sign-in run is worth relaunching.

py_modules/unifideck/launcher/proton/handlers/battlenet_auth_retry.py

One decision, split out of ``battlenet.py`` when that file hit its size cap.

The sign-in client runs through ``run_umu_with_retry``, which decides on
exit code and duration alone::

    _RECOVERABLE_CODES = {2, 74, 127}
    _RECOVERABLE_MAX_RUNTIME_SECONDS = 120

That is the right test for a game and the wrong one for a window a human
closes. Closing the sign-in client inside two minutes is completely ordinary,
and it looked identical to the ANGLE/gamescope renderer abort the retry exists
for, so the client reopened itself. Reported as "still launching the sign in
launcher when I close it". Codes 2 and 74 also wipe the shared umu runtime
cache on the way through, which every other game then re-downloads.

Deleting the retry is not an option: the startup abort is real and a retry
does recover it. This module holds the extra evidence that separates them.
"""

from __future__ import annotations

import logging

from unifideck.launcher.proton.handlers import battlenet_login_state as login_state
from unifideck.launcher.proton.handlers import battlenet_watch as watch
from unifideck.launcher.proton.infrastructure.core import ProtonLaunchPlan

logger = logging.getLogger(__name__)


def auth_retry_worthwhile(
    plan: ProtonLaunchPlan, ready: watch.ReadinessLatch,
) -> bool:
    """Whether reopening the sign-in client could still achieve anything.

    Two independent reasons not to, either of which is sufficient:

    * **The window was seen.** Whatever the exit code says, the client got far
      enough to render, so this was a close and not the startup abort.
    * **The prefix is already signed in.** Then the sign-in succeeded and there
      is nothing left to reopen for, however the client happened to exit.

    Unknown reads as "retry": this only ever *suppresses* the existing
    behaviour, so a probe that cannot answer must not be the thing that
    suppresses a recovery from a real crash.
    """
    if ready.seen:
        logger.info(
            "[battlenet] sign-in client was seen running, so its exit was a "
            "close and not a crash: not reopening it",
        )
        return False
    try:
        if login_state.read_login_state(plan.prefix_path) is (
            login_state.LoginState.SIGNED_IN
        ):
            logger.info("[battlenet] already signed in: not reopening the client")
            return False
    except Exception:
        logger.debug("[battlenet] could not read login state", exc_info=True)
    return True
