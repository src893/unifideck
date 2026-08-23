"""Battle.net prefix lifecycle: auth prefix, template, per-game clones.

py_modules/unifideck/stores/battlenet/prefix/manager.py

Three tiers, because Unifideck does not share prefixes between games::

    .bnet-auth      the user signs into the client here, once
    .template       an rsync clone OF THE AUTH PREFIX — never a fresh install
    <uid>           one per game, cloned from .template

**The template is derived from the auth prefix.** This module previously
built it as a standalone pristine install, deliberately keeping the user's
session out of it. The result was a client that demanded a fresh sign-in for
every single game, and the obvious repair — copying the session across
afterwards — was measured on-device and *does not work*:

    attempt 1: copy the token vault, ``CachedData.db``, ``BrowserCaches/``
               and the config's ``SavedAccountNames``/``AutoLogin``
               -> client logged ``browser state changed: LoginCredential``,
                  i.e. it still put up a password form.

    attempt 2: additionally copy ``Client.GaClientId``
               -> no ``LoginCredential`` at all, signed straight in.

(``UnifiedAuth``, ``EncryptionKey`` and ``Identity`` are **Wine registry
keys**, under ``Software\\Blizzard Entertainment\\Battle.net\\`` in
``user.reg`` — not files. Confirmed 2026-08-11 on client build 17651, after a
files-only session copy produced ``ERROR_TOKEN_NOT_FOUND (49)`` and the
client's own log said ``DeleteToken(): Deleting registry token``. That is also
why the whole-prefix rsync below works where a curated file list does not: it
carries ``user.reg``. ``launcher/wrapper_session_specs`` holds the verified
layout.)

The token is bound to the client instance that minted it, so a session only
transplants when *every* piece of identity material agrees. Deriving the
whole template from auth makes that true by construction rather than by a
hand-maintained list of files staying correct as Blizzard changes the
client.

Derivation alone is not enough, and that was the second half of the same
bug. Blizzard rotates the token on every client run, so a template derived
once is a snapshot that goes server-stale — measured here as ``.bnet-auth``
and ``.template`` byte-identical and frozen at 08:57 while the game prefix's
client had rewritten every session file at 21:15. The template's session is
therefore *refreshed* from auth whenever auth is newer, which is also what
makes re-signing-in heal every prefix instead of only the auth one.

This is not a new idea — it is Ubisoft's ``shared-identity invariant``,
stated in ``stores/ubisoft/prefix/helpers.py``: the template "is always an
rsync clone of ``.upc-auth`` — never a standalone fresh install… so the
credential vault decrypts everywhere". Battle.net copied Ubisoft's three
tiers but not this step, and that omission is the whole defect.

Keeping the middle tier (rather than cloning each game straight from auth)
buys one thing that matters: the template is *quiesced*, so a game can be
installed while the user has the Battle.net window open. Cloning the live
auth prefix cannot — its databases are mid-write and a torn ``CachedData.db``
reads as signed out.

It also removes the second client install: the template used to be
bootstrapped separately, which is why the first install of any game spent
three minutes re-downloading a client that was already on disk.

The self-update concern the old "warmed" marker guarded is now answered for
free. A freshly installed client self-updated from 2.52.3.17554 to
2.52.8.17651 within five minutes of first launch and then raised *"You need
to restart the application to finish installing a required update."* —
unclickable in Gaming Mode. The auth prefix is the one prefix the user has
definitely launched interactively, so anything derived from it is already
past that.

Ownership of any prefix is proven by its in-directory marker, never by its
path. Deleting a prefix here destroys the game inside it.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
from dataclasses import dataclass
from pathlib import Path

from unifideck.launcher import wrapper_session
from unifideck.stores.battlenet import paths
from unifideck.stores.shared.prefix_clone import (
    clone_template,
    ensure_pfx_symlink,
    is_owned_by,
    read_marker,
    repair_from_template,
)

logger = logging.getLogger(__name__)

STORE_ID = "battlenet"
MARKER_FILENAME = paths.PREFIX_MARKER

# Written into the template once it has been derived from the auth prefix.
# A template without this came from a standalone install, carries its own
# identity, and produces clones that demand a fresh sign-in — so it is
# re-derived rather than trusted. Replaces the old "warmed" marker, whose
# question (has the client self-updated?) the auth prefix answers by having
# been launched interactively.
DERIVED_MARKER = ".unifideck_battlenet_from_auth"


@dataclass(frozen=True, slots=True)
class PrefixStatus:
    """What we know about one prefix on disk."""

    path: Path
    exists: bool
    has_client: bool
    is_ours: bool
    derived: bool = False

    @property
    def usable(self) -> bool:
        return self.exists and self.has_client


def inspect_prefix(prefix: Path) -> PrefixStatus:
    """Describe a prefix without modifying it."""
    path = Path(prefix)
    return PrefixStatus(
        path=path,
        exists=path.is_dir(),
        has_client=paths.client_installed(path),
        is_ours=is_owned_by(path, MARKER_FILENAME, STORE_ID),
        derived=(path / DERIVED_MARKER).exists(),
    )


class BattlenetPrefixManager:
    """Creates and repairs the three prefix tiers."""

    def __init__(self, prefixes_dir: Path) -> None:
        self._root = Path(prefixes_dir)

    @property
    def root(self) -> Path:
        return self._root

    @property
    def auth_prefix(self) -> Path:
        return paths.auth_prefix(self._root)

    @property
    def template_prefix(self) -> Path:
        return paths.template_prefix(self._root)

    def game_prefix(self, uid: str) -> Path:
        """Default location for a new game prefix.

        Only for creation. An existing prefix is looked up in the id map,
        never rebuilt from the uid — a reconstructed path once stamped a
        marker into a directory no launch opened, wedging the prefix in a
        permanent reset loop.
        """
        return paths.game_prefix(self._root, uid)

    # -- the template, derived from auth -----------------------------------

    def auth_ready(self) -> bool:
        """True when the auth prefix holds a client worth cloning."""
        return inspect_prefix(self.auth_prefix).usable

    def auth_is_busy(self) -> bool:
        """Whether a client is live in the auth prefix.

        Public because writing *into* the auth prefix needs the same guard
        that copying out of it does, and the caller doing the writing is the
        installer rather than this class. See :meth:`_auth_is_busy`.
        """
        return self._auth_is_busy()

    def template_status(self) -> PrefixStatus:
        return inspect_prefix(self.template_prefix)

    def template_ready(self) -> bool:
        """True when the template is a usable, auth-derived snapshot.

        The derived marker is load-bearing, not bookkeeping: a template that
        came from a standalone install carries its own identity, so the
        session copied into a clone of it is rejected and the user is asked
        to sign in for every game. Templates predating this check are
        re-derived rather than trusted.
        """
        return self.template_status().usable and (
            self.template_prefix / DERIVED_MARKER
        ).exists()

    def _refresh_template_session(self) -> bool:
        """Bring the template's session up to date with the auth prefix.

        Cheap: only session material moves, so this is a handful of small
        files rather than the 12 s / 1.6 GB the full derivation costs. A
        failure is not fatal — the existing template is still usable, just
        older, and failing the install instead would be strictly worse.
        """
        spec = wrapper_session.spec_for(STORE_ID)
        if spec is None:
            return False
        try:
            return wrapper_session.inject(
                spec, self.auth_prefix, self.template_prefix,
            )
        except Exception:
            logger.warning(
                "[Battlenet] could not refresh the template's session — "
                "keeping the existing one",
            )
            return False

    async def ensure_template(self) -> bool:
        """Make ``.template`` an rsync clone of the signed-in auth prefix.

        Never a standalone install — that is the whole point. Ubisoft states
        the same invariant in ``prefix/helpers.py``: the template is always
        a clone of the auth prefix "so the credential vault decrypts
        everywhere". Battle.net inherited the three-tier shape but not this
        step, which is why every game prefix opened signed out.

        A template rather than cloning each game straight from auth, because
        the template is *quiesced*: it can be copied while the user has the
        Battle.net window open, which the live auth prefix cannot.

        A ready template is **refreshed, not rebuilt**. This used to return
        here unconditionally, which froze the template's session at whatever
        it was on the day of the first install: re-signing-in fixed only
        ``.bnet-auth``, every game prefix kept the dead token, and each
        Install stamped that dead token back over any prefix whose client had
        since refreshed itself.
        """
        if self.template_ready():
            if self.auth_ready() and not self._auth_is_busy():
                await asyncio.to_thread(self._refresh_template_session)
            return True
        if not self.auth_ready():
            logger.error(
                "[Battlenet] cannot derive a template: no client in %s "
                "— sign in first so there is a session to inherit",
                self.auth_prefix,
            )
            return False
        if self._auth_is_busy():
            logger.error(
                "[Battlenet] refusing to derive the template while the client "
                "is running — its databases are mid-write and the copy would "
                "be torn (close the Battle.net window and retry)",
            )
            return False

        target = self.template_prefix
        if target.exists():
            logger.info(
                "[Battlenet] replacing a template that was not derived from auth",
            )
            try:
                shutil.rmtree(target)
            except OSError as exc:
                logger.warning("[Battlenet] cannot replace stale template: %s", exc)
                return False

        if not await clone_template(
            self.auth_prefix,
            target,
            store=STORE_ID,
            marker_filename=MARKER_FILENAME,
            client_build=self._auth_build(),
        ):
            return False
        ensure_pfx_symlink(target)
        try:
            (target / DERIVED_MARKER).write_text("", encoding="utf-8")
        except OSError as exc:
            logger.warning("[Battlenet] cannot mark template as auth-derived: %s", exc)
            return False
        logger.info(
            "[Battlenet] template derived from the auth prefix — shared identity "
            "established, game prefixes will open signed in",
        )
        return True

    def _auth_is_busy(self) -> bool:
        """True while a client is live in the auth prefix.

        Copying a prefix out from under a running client captures
        ``CachedData.db`` and the CEF caches mid-write, and a torn session
        database reads as "signed out" — the exact failure this exists to
        remove. Only the one-off derivation pays this cost; per-game clones
        come from the quiesced template and are unaffected.

        A probe that cannot run (launcher subset absent) reports "not busy":
        refusing every derivation because we could not look would be worse
        than the race it guards against.
        """
        try:
            from unifideck.launcher.proton.handlers.battlenet_watch import (
                client_running,
            )

            return client_running(self.auth_prefix)
        except Exception:
            logger.debug("[Battlenet] could not probe the auth prefix for a live client")
            return False

    # -- per-game prefixes -------------------------------------------------

    async def create_game_prefix(self, uid: str, destination: Path | None = None) -> Path | None:
        """Clone the auth-derived template into a new per-game prefix.

        ``destination`` is the user's picked storage location, resolved by
        ``stores/shared/prefix_placement``. The game installs inside this
        prefix, so this call is what puts it on the chosen disk.
        """
        if not await self.ensure_template():
            return None
        target = Path(destination) if destination else self.game_prefix(uid)
        # Only a *usable* existing prefix is reused. A half-written one — an
        # interrupted clone to removable media — would otherwise be returned
        # as-is and fail at launch on a missing client exe; falling through
        # lets the additive (no ``--delete``) rsync finish the job.
        if inspect_prefix(target).usable:
            logger.info("[Battlenet] prefix already exists for %s: %s", uid, target)
            return target

        if not await clone_template(
            self.template_prefix,
            target,
            store=STORE_ID,
            marker_filename=MARKER_FILENAME,
            client_build=self._source_build(),
        ):
            return None
        ensure_pfx_symlink(target)
        logger.info(
            "[Battlenet] created prefix for %s at %s (session inherited)", uid, target,
        )
        return target

    async def repair_game_prefix(self, prefix: Path) -> bool:
        """Refresh a game prefix's identity, keeping its installed game.

        Additive only. The game lives inside this prefix, so the games
        directory is excluded and nothing is deleted.
        """
        status = inspect_prefix(prefix)
        if not status.exists:
            return False
        if not status.is_ours:
            logger.warning(
                "[Battlenet] refusing to repair unmarked prefix %s "
                "(not provably ours)", prefix,
            )
            return False
        ok = await repair_from_template(self.template_prefix, Path(prefix))
        if ok:
            ensure_pfx_symlink(Path(prefix))
        return ok

    def remove_game_prefix(self, prefix: Path) -> bool:
        """Delete a prefix we created. Refuses anything unmarked.

        This deletes the game too — the install lives inside. Callers must
        have explicit user intent, and the marker check is the backstop.
        """
        path = Path(prefix)
        # The marker cannot tell a game clone from the shared tiers: the
        # template carries one too (``clone_template`` stamps its
        # destination). Harmless while every caller read its path back from
        # the id map, but placement now *computes* paths, so name them.
        if path in (self.auth_prefix, self.template_prefix):
            logger.error(
                "[Battlenet] refusing to delete the shared %s prefix at %s",
                "auth" if path == self.auth_prefix else "template", path,
            )
            return False
        if not path.is_dir():
            return True
        if not is_owned_by(path, MARKER_FILENAME, STORE_ID):
            logger.error(
                "[Battlenet] refusing to delete unmarked prefix %s — "
                "no proof we created it", path,
            )
            return False
        try:
            shutil.rmtree(path)
        except OSError as exc:
            logger.warning("[Battlenet] cannot remove %s: %s", path, exc)
            return False
        return True

    # -- internals ---------------------------------------------------------

    def _source_build(self) -> str | None:
        return self._build_of(self.template_prefix)

    def _auth_build(self) -> str | None:
        return self._build_of(self.auth_prefix)

    @staticmethod
    def _build_of(prefix: Path) -> str | None:
        marker = read_marker(prefix, MARKER_FILENAME)
        if marker and marker.client_build:
            return marker.client_build
        versions = paths.client_version_dirs(prefix)
        return versions[-1].name.rsplit(".", 1)[-1] if versions else None
