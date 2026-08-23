"""A Battle.net install queued behind the Agent's own update is not stuck.

The reported bug: every Battle.net install sat at a 0% bar reading "Queued"
with a resume arrow and never moved, so the user cancelled and retried, three
times, getting the same thing each time.

Nothing was ever wrong with the game download. Blizzard's Agent runs exactly
one exclusive operation at a time, and on a fresh prefix it makes its own
self-update that operation, so the game waits. On the reported machine that
wait was 28 minutes; the game itself then downloaded in 86 seconds. Two
separate defects turned a correct wait into a permanent one:

* nothing said what was being waited for, so cancelling looked reasonable;
* cancelling deletes the prefix, which throws away the agent update, so the
  next attempt started from zero and looked identical.

Every log line quoted below is real, lifted from the reported prefix
(``Agent.9700/Logs`` on 2026-08-22). The tag strings matter as much as the
operations: the same agent build (``d049a9f9…``) costs 2 seconds under
``Volatile Windows US?`` and 45 minutes under ``KR? acct-IND? geoip-IN?``,
because only the first is already in the local content store.
"""

from __future__ import annotations

import shutil
import time
from pathlib import Path

from unifideck.launcher.wrapper_client_cache import (
    capture_client_cache,
    read_generation,
)
from unifideck.launcher.wrapper_session_specs import SessionSpec, spec_for
from unifideck.stores.battlenet import agent_status

AGENT_LOGS = "pfx/drive_c/ProgramData/Battle.net/Agent/Agent.9700/Logs"
CASC = "pfx/drive_c/ProgramData/Battle.net/Agent/data"

KR_TAGS = "Volatile Windows KR? acct-IND? geoip-IN?"
US_TAGS = "Volatile Windows US?"

# Verbatim, from the prefix where the install appeared to hang.
QUEUED_BEHIND_AGENT = """\
[I 2026-08-22 12:39:16.0843] Queue operation - OP_UPDATE for 'agent'
[I 2026-08-22 12:39:16.0844] Active operation nullptr replaced by OP_UPDATE for 'agent'
[I 2026-08-22 12:39:19.0761] Reservation Canceled for battle.net:OP_VERSION
[I 2026-08-22 12:39:19.0764] Concurrent operation OP_VERSION for 'battle.net' completed
[I 2026-08-22 12:40:10.0661] Reservation Created for d1:OP_UPDATE
[I 2026-08-22 12:40:10.0666] Queue operation - OP_UPDATE for 'd1'
[I 2026-08-22 12:40:34.0582] Concurrent operation OP_UPDATE for 'battle.net' completed
"""

# The same prefix 27 minutes later: the agent finished, d1 took the slot.
AGENT_DONE_GAME_RUNNING = QUEUED_BEHIND_AGENT + """\
[I 2026-08-22 13:05:43.0000] OP_UPDATE for 'agent' completed
[I 2026-08-22 13:05:44.0036] Active operation nullptr replaced by OP_UPDATE for 'd1'
"""

ALL_DONE = AGENT_DONE_GAME_RUNNING + """\
[I 2026-08-22 13:07:10.0745] OP_UPDATE for 'd1' completed
"""

# Note the ``[I <date> <time>]`` stamp: its space-separated fields are ``[I``,
# the date, then the time. That is why a field-offset regex silently read no
# progress at all off a real log.
AGENT_PROGRESS = """\
[I 2026-08-22 12:39:19.0916] agent Update Progress - 0.0069 (0.0069)
[I 2026-08-22 12:39:46.0718] bna Update Progress - 0.0092 (0.0092)
[I 2026-08-22 12:49:58.0099] agent Update Progress - 0.7543 (0.7543)
"""

NGDP_KR = (
    "[I 2026-08-22 12:39:17.0028] Start Update of agent w/ tags "
    f"({KR_TAGS})\n"
)
NGDP_US = (
    "[I 2026-08-20 22:04:18.0115] Start Update of agent w/ tags "
    f"({US_TAGS})\n"
)


def _prefix(
    tmp_path: Path,
    *,
    operations: str = ALL_DONE,
    progress: str = AGENT_PROGRESS,
    ngdp: str = NGDP_KR,
    name: str = "D1",
    stale: bool = False,
) -> Path:
    """A prefix carrying the Agent logs, optionally back-dated as inherited."""
    prefix = tmp_path / name
    logs = prefix / AGENT_LOGS
    logs.mkdir(parents=True, exist_ok=True)
    written = {
        "Operations-20260822T123916.log": operations,
        "AgentUpdate-20260822T123916.log": progress,
        "AgentNGDP-20260822T123916.log": ngdp,
    }
    for filename, body in written.items():
        path = logs / filename
        path.write_text(body, encoding="utf-8")
        if stale:
            # rsync -a preserves mtimes, so a clone arrives carrying the
            # template's logs at their original timestamps.
            old = time.time() - 86_400
            import os

            os.utime(path, (old, old))
    return prefix


def _drive_c(prefix: Path) -> Path:
    return prefix / "pfx" / "drive_c"


# ── what the user is told ───────────────────────────────────────────────


def test_a_game_queued_behind_the_agent_says_so_with_a_percentage(
    tmp_path: Path,
) -> None:
    """The whole point: name the wait instead of showing a dead bar."""
    prefix = _prefix(tmp_path, operations=QUEUED_BEHIND_AGENT)

    message = agent_status.describe_wait(_drive_c(prefix), 0.0, "d1")

    assert message is not None
    assert "updating its downloader" in message
    # 0.7543 off the real progress log. A field-offset regex read None here.
    assert "(75%)" in message
    assert "Don't cancel" in message


def test_the_wait_is_silent_once_the_game_holds_the_slot(tmp_path: Path) -> None:
    """No message means the caller keeps its own byte-count tick.

    Once d1 is the active operation the download really is progressing, and
    an "explained wait" then would be a lie that overwrote a real number.
    """
    prefix = _prefix(tmp_path, operations=AGENT_DONE_GAME_RUNNING)

    assert agent_status.describe_wait(_drive_c(prefix), 0.0, "d1") is None


def test_a_finished_install_explains_nothing(tmp_path: Path) -> None:
    prefix = _prefix(tmp_path, operations=ALL_DONE)

    assert agent_status.describe_wait(_drive_c(prefix), 0.0, "d1") is None


def test_state_is_unknown_rather_than_empty_when_no_log_is_fresh(
    tmp_path: Path,
) -> None:
    """A clone's inherited logs must never be read as this run's.

    ``None`` (cannot tell) and an empty state (nothing running) are different
    answers, and the capture path depends on the difference. Trusting a
    day-old inherited log would report a completion from another prefix,
    under another region tag, as if it had just happened here.
    """
    prefix = _prefix(tmp_path, stale=True)

    assert agent_status.read_state(_drive_c(prefix), time.time()) is None
    assert agent_status.self_update_finished(_drive_c(prefix), time.time()) is False
    assert agent_status.describe_wait(_drive_c(prefix), time.time(), "d1") is None


def test_a_prefix_with_no_agent_logs_at_all_is_not_an_error(
    tmp_path: Path,
) -> None:
    empty = tmp_path / "fresh"
    (empty / "pfx" / "drive_c").mkdir(parents=True)

    assert agent_status.read_state(_drive_c(empty), 0.0) is None
    assert agent_status.describe_wait(_drive_c(empty), 0.0, "d1") is None
    assert agent_status.update_generation(_drive_c(empty), 0.0) is None


# ── the generation, which is what makes a captured store reusable ───────


def test_the_generation_is_the_tag_query_not_the_size(tmp_path: Path) -> None:
    """Size cannot identify a content store; the Agent's tag query can.

    Measured: after the KR update the store was 5.4 MB against the stale US
    template's 6.9 MB, because the Agent compacts away content the new tags
    do not select. The smaller store is the correct one.
    """
    assert agent_status.update_generation(
        _drive_c(_prefix(tmp_path, ngdp=NGDP_KR)), 0.0,
    ) == KR_TAGS
    assert agent_status.update_generation(
        _drive_c(_prefix(tmp_path, ngdp=NGDP_US, name="auth")), 0.0,
    ) == US_TAGS


def test_self_update_finished_only_once_the_agent_actually_completed(
    tmp_path: Path,
) -> None:
    mid = _prefix(tmp_path, operations=QUEUED_BEHIND_AGENT, name="mid")
    done = _prefix(tmp_path, operations=ALL_DONE, name="done")

    assert agent_status.self_update_finished(_drive_c(mid), 0.0) is False
    assert agent_status.self_update_finished(_drive_c(done), 0.0) is True


# ── carrying the store back to the template ─────────────────────────────


def _casc(prefix: Path, marker: str) -> Path:
    """A stand-in content store: an ``indices/`` + ``data/`` pair."""
    store = prefix / CASC
    (store / "indices").mkdir(parents=True, exist_ok=True)
    (store / "data").mkdir(parents=True, exist_ok=True)
    (store / "indices" / "0000.idx").write_text(marker, encoding="utf-8")
    (store / "data" / "0000.data").write_text(marker, encoding="utf-8")
    return store


def _spec() -> SessionSpec:
    spec = spec_for("battlenet")
    assert spec is not None
    return spec


def test_the_store_is_swapped_whole_never_merged(tmp_path: Path) -> None:
    """An index describing archives that are not there is a broken store.

    These caches are an ``indices/`` directory describing archives under
    ``data/``, so blending two generations is not a conservative choice, it is
    a corrupt one. After a capture the template must hold the source's store
    and nothing of its own.
    """
    source = tmp_path / "game"
    template = tmp_path / "template"
    _casc(source, "kr")
    store = _casc(template, "us")
    (store / "indices" / "stale-us-only.idx").write_text("us", encoding="utf-8")

    assert capture_client_cache(_spec(), source, template, KR_TAGS, complete=True) == 1

    assert (store / "indices" / "0000.idx").read_text() == "kr"
    assert not (store / "indices" / "stale-us-only.idx").exists()
    assert read_generation(template) == KR_TAGS


def test_capturing_the_same_generation_twice_is_a_no_op(tmp_path: Path) -> None:
    source, template = tmp_path / "game", tmp_path / "template"
    _casc(source, "kr")
    _casc(template, "us")

    assert capture_client_cache(_spec(), source, template, KR_TAGS, complete=True) == 1
    assert capture_client_cache(_spec(), source, template, KR_TAGS, complete=True) == 0


def test_a_cancelled_download_is_kept_so_the_retry_resumes(tmp_path: Path) -> None:
    """The loop this exists to break.

    Users cancel exactly when the wait looks broken, which is mid-update. If
    only finished downloads were kept, the one path that actually repeats
    would keep nothing and restart from zero, which is what made three
    consecutive attempts look identically stuck.
    """
    source, template = tmp_path / "game", tmp_path / "template"
    _casc(source, "kr-partial")
    store = _casc(template, "us")

    assert capture_client_cache(_spec(), source, template, KR_TAGS, complete=False) == 1

    assert (store / "data" / "0000.data").read_text() == "kr-partial"
    assert read_generation(template) == "partial:" + KR_TAGS


def test_a_finished_download_supersedes_a_kept_partial(tmp_path: Path) -> None:
    source, template = tmp_path / "game", tmp_path / "template"
    _casc(source, "partial")
    _casc(template, "us")
    capture_client_cache(_spec(), source, template, KR_TAGS, complete=False)

    _casc(source, "complete")
    assert capture_client_cache(_spec(), source, template, KR_TAGS, complete=True) == 1

    assert read_generation(template) == KR_TAGS
    assert (
        template / CASC / "data" / "0000.data"
    ).read_text() == "complete"


def test_a_partial_never_overwrites_a_finished_capture(tmp_path: Path) -> None:
    source, template = tmp_path / "game", tmp_path / "template"
    _casc(source, "complete")
    _casc(template, "us")
    capture_client_cache(_spec(), source, template, KR_TAGS, complete=True)

    _casc(source, "partial")
    assert capture_client_cache(_spec(), source, template, KR_TAGS, complete=False) == 0

    assert (template / CASC / "data" / "0000.data").read_text() == "complete"


def test_a_store_for_a_region_the_account_left_is_superseded(
    tmp_path: Path,
) -> None:
    """Finished is not the same as useful.

    A completed US-tagged store is worthless to an account Blizzard now routes
    to KR, however finished it is, so a *partial* KR store still wins.
    """
    source, template = tmp_path / "game", tmp_path / "template"
    _casc(source, "us")
    _casc(template, "old")
    capture_client_cache(_spec(), source, template, US_TAGS, complete=True)

    _casc(source, "kr")
    assert capture_client_cache(_spec(), source, template, KR_TAGS, complete=False) == 1
    assert read_generation(template) == "partial:" + KR_TAGS


def test_a_failed_capture_leaves_the_template_intact(tmp_path: Path) -> None:
    """A half-written store in the template would be cloned into every prefix."""
    source, template = tmp_path / "game", tmp_path / "template"
    store = _casc(template, "us")
    # Source drive_c exists but the declared cache does not.
    (source / "pfx" / "drive_c").mkdir(parents=True)

    assert capture_client_cache(_spec(), source, template, KR_TAGS, complete=True) == 0

    assert (store / "data" / "0000.data").read_text() == "us"
    assert read_generation(template) is None


def test_a_store_declaring_no_cache_captures_nothing(tmp_path: Path) -> None:
    source, template = tmp_path / "game", tmp_path / "template"
    _casc(source, "kr")
    bare = SessionSpec(store="ubisoft", files=())

    assert capture_client_cache(bare, source, template, KR_TAGS, complete=True) == 0


def test_a_capture_survives_the_template_being_rebuilt(tmp_path: Path) -> None:
    """The template is a derived artifact, so a capture must outlive it.

    Measured on 2026-08-22, after the first version of this shipped. A capture
    landed in ``.template`` at 21:14; the user signed out at 22:22; at 22:45
    ``ensure_template`` re-derived the template from ``.bnet-auth``, which
    discards everything the template had learned. The next install found the
    store back at its 6.9 MB bootstrap size and paid the agent update again,
    which is exactly the loop the capture exists to break.

    Capturing into the auth prefix as well fixes it, because the rebuild is
    an rsync clone *of* the auth prefix.
    """
    game, template, auth = tmp_path / "g", tmp_path / "tpl", tmp_path / "auth"
    _casc(game, "kr")
    _casc(template, "us")
    _casc(auth, "us")

    for destination in (template, auth):
        capture_client_cache(_spec(), game, destination, KR_TAGS, complete=True)

    # ``ensure_template`` rebuilds the template as a clone of the auth prefix.
    shutil.rmtree(template)
    shutil.copytree(auth, template)

    assert read_generation(template) == KR_TAGS, (
        "the rebuilt template lost the captured store"
    )
    assert (template / CASC / "data" / "0000.data").read_text() == "kr"
