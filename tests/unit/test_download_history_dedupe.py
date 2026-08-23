"""Download history keeps one row per game, not one per attempt.

Retrying an install left a row per attempt, so a game tried twice rendered
as two identical cards under "Failed" — visually indistinguishable from two
different problems. Reported from the field with Crash Bandicoot 4, which
had a ``failed`` and a ``cancelled`` row for the same id.
"""

from __future__ import annotations

from unifideck.services.download.models import DownloadItem
from unifideck.services.download.service import _newest_per_id
from unifideck.services.download.worker import _WorkerMixin


def _item(game_id: str, status: str, store: str = "battlenet") -> DownloadItem:
    return DownloadItem(
        store=store,
        game_id=game_id,
        install_path="/tmp/games",
        title=game_id,
        status=status,
    )


class _Host(_WorkerMixin):
    """Just enough host for ``_cleanup_running``."""

    def __init__(self) -> None:
        self._running: dict[str, DownloadItem] = {}
        self._finished: list[DownloadItem] = []


def test_retrying_replaces_the_previous_row_rather_than_stacking():
    host = _Host()
    for status in ("failed", "cancelled", "complete"):
        item = _item("wlby", status)
        host._running[f"{item.store}:{item.game_id}"] = item
        host._cleanup_running(item)

    assert len(host._finished) == 1
    assert host._finished[0].status == "complete"


def test_other_games_are_untouched():
    host = _Host()
    for item in (_item("wlby", "failed"), _item("s2", "complete"), _item("wlby", "complete")):
        host._cleanup_running(item)

    assert [(i.game_id, i.status) for i in host._finished] == [
        ("s2", "complete"),
        ("wlby", "complete"),
    ]


def test_same_game_id_in_two_stores_is_not_collapsed():
    """The id is store-qualified; two stores can share a game_id."""
    host = _Host()
    host._cleanup_running(_item("wlby", "failed", store="battlenet"))
    host._cleanup_running(_item("wlby", "complete", store="microsoft"))
    assert len(host._finished) == 2


def test_history_written_before_the_fix_is_collapsed_on_load():
    """A file already holding a row per attempt must not keep rendering twice."""
    legacy = [
        _item("wlby", "failed"),
        _item("s2", "complete"),
        _item("wlby", "cancelled"),
    ]
    collapsed = _newest_per_id(legacy)
    assert [(i.game_id, i.status) for i in collapsed] == [
        ("s2", "complete"),
        ("wlby", "cancelled"),
    ]


def test_newest_per_id_is_a_noop_on_already_clean_history():
    clean = [_item("a", "complete"), _item("b", "failed")]
    assert _newest_per_id(clean) == clean
