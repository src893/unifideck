"""Keeping a wrapper store's vendor client in the plugin's UI language.

py_modules/unifideck/launcher/wrapper_locale.py

Split out of ``wrapper_prefs``, which was at 503 lines against a 550 cap and
holds a different concern: that module carries whatever the *user* changed
between prefixes, this one decides what the *plugin* should put there in the
first place. The two meet at exactly one point — ``wrapper_session.inject``
seeds with this module and then merges with that one.

**What the language actually controls.** Measured on-device 2026-08-23 by
hand-editing ``<install-hash>.Client.Language`` to ``deDE`` in the auth prefix
and changing nothing else:

* the client UI came up in German;
* the value reached the template prefix and then a game prefix on its own,
  through ``wrapper_prefs.merge`` (auth 00:22 → template 00:41 → game 00:43);
* a *subsequent* game install came out German end to end — the Agent's
  ``/register`` call carried ``"primary_locale_hint": "deDE"`` where every
  previous install in the logs had said ``enUS``.

So this one key drives the client, the Agent's locale hint, and the text and
speech data a new install downloads. That is why it is worth being careful
about, and why the seed is deliberately *not* a per-prefix operation: writing
auth is enough, and writing more would fight the merge.

**Why the marker holds a tag rather than a bit.** The first version stamped an
empty ``.unifideck_battlenet_locale_seeded.v1`` and never looked again. On this
Deck it was stamped at 23:40:52.989 in the same millisecond the launcher
resolved ``en-US``; the user set German at 23:54, fourteen minutes later, and
the seed had already retired itself. The next launch could not even resolve a
locale — ``ensure_locale_seeded`` returned on its first line, so the log has no
``[locale] resolved`` line at all, which is what made the bug look like the
resolver rather than the gate.

A bit cannot distinguish "already seeded German" from "already seeded English",
so the marker now holds the BCP-47 tag it last wrote. Re-seeding happens when
that tag changes and at no other time, which keeps the property the bit was
there for: a language picked inside the vendor client survives, right up until
the user changes the plugin's own language and plainly means it to change.

Stdlib-only. Runs under the SYSTEM python (3.10-3.14), not Decky's bundled
3.11, and must never raise — a language preference is not worth failing a
game launch over.
"""

from __future__ import annotations

import contextlib
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from unifideck.launcher.wrapper_prefs import config_path, load_config, write_config

if TYPE_CHECKING:
    from unifideck.launcher.wrapper_session_specs import SessionSpec

logger = logging.getLogger(__name__)

__all__ = [
    "bootstrapper_locale",
    "ensure_locale_seeded",
    "plugin_locale",
]

# BCP-47 tags to Battle.net's own locale codes.
#
# Not guessed. Read out of the client payload on 2026-08-23 with
# ``strings battle.net.dll | grep -E '^[a-z]{2}[A-Z]{2}$'``, which yields
# exactly the 22 the client's own log reports loading from ``languages.xml``:
#
#   arSA deDE enCN enGB enSA enSG enTW enUS esES esMX frFR itIT jaJP koKR
#   plPL ptBR ptPT ruRU thTH trTR zhCN zhTW
#
# (``thVW``, ``trRQ`` and ``trSV`` also appear in the binary and are excluded —
# they are not among the 22 and are not language codes.)
#
# Mapped here are the plugin catalogue tags the client can actually serve.
# ``nl-NL`` and ``uk-UA`` are in the catalogue and absent from the client, so
# they stay unmapped: an unrecognised tag is left alone rather than
# approximated. See ``bootstrapper_locale`` for why guessing is worse than
# English. ``es-MX`` is the reverse case — not a catalogue tag, but reachable
# through the legacy ``ui.language`` key, so it costs one line to honour.
_BNET_UI_LOCALES: dict[str, str] = {
    "ar-SA": "arSA",
    "de-DE": "deDE",
    "en-US": "enUS",
    "es-ES": "esES",
    "es-MX": "esMX",
    "fr-FR": "frFR",
    "it-IT": "itIT",
    "ja-JP": "jaJP",
    "ko-KR": "koKR",
    "pl-PL": "plPL",
    "pt-BR": "ptBR",
    "ru-RU": "ruRU",
    "tr-TR": "trTR",
    "zh-CN": "zhCN",
    "zh-TW": "zhTW",
}

# Holds the BCP-47 tag last seeded into the auth prefix.
_LOCALE_MARKER = ".unifideck_battlenet_locale.v2"

# The v1 marker: empty, and stamped even when the seed did nothing. Retired on
# sight so a Deck carrying one gets a single corrective seed rather than being
# frozen at whatever locale happened to be resolving the day it was written.
_LEGACY_MARKER = ".unifideck_battlenet_locale_seeded.v1"

# What the bootstrapper falls back to. Its own fallback, per the strings in
# the installer binary, is "Fallback region not found in config, us".
_DEFAULT_BNET_LOCALE = "enUS"


#: Resolved once per launcher process. The launcher is short-lived (one game
#: launch) so a single config load is the whole cost, but ``plugin_locale`` is
#: called from more than one place in a run. Note that ``client_install``
#: imports ``bootstrapper_locale`` into the long-lived backend too; nothing
#: there calls it today, and a future caller would want ``_reset_cache``.
_RESOLVED_LOCALE: str | None = None
_RESOLVE_ATTEMPTED = False


def _detect_locale() -> str | None:
    """Ask the plugin's own locale resolver, the way everything else does.

    ``utils.locale.get_unifideck_locale`` is the single source of truth: it
    walks the explicit ``ui.locale`` preference, then Steam's UI language,
    then the POSIX locale, then the configured source tag. The launcher can
    reach it because it builds a standalone ``ConfigManager`` from disk for
    exactly this kind of question.

    Never raises.
    """
    try:
        from unifideck.launcher.bootstrap import _load_standalone_config
        from unifideck.utils.locale import get_unifideck_locale

        return get_unifideck_locale(_load_standalone_config())
    except Exception:
        logger.debug("[wrapper_locale] locale resolver unavailable", exc_info=True)
        return None


def plugin_locale() -> str | None:
    """The plugin's BCP-47 UI locale, from the one resolver everything uses.

    There is deliberately no second source. Copying the answer into
    ``wrapper_prefixes.json`` and reading it back was tried and removed twice
    over: on 2026-08-22 the file was simply *absent* on a working install, so
    every caller silently got ``None`` and every locale-dependent behaviour
    fell back to English; and it was written once per backend start, so a
    language changed afterwards could not reach the launcher at all.

    This wrapper exists only to hold the cache and the never-raises contract.
    """
    global _RESOLVED_LOCALE, _RESOLVE_ATTEMPTED
    if not _RESOLVE_ATTEMPTED:
        _RESOLVE_ATTEMPTED = True
        _RESOLVED_LOCALE = _detect_locale()
    return _RESOLVED_LOCALE


def bootstrapper_locale(store: str) -> str:
    """The locale to hand the vendor bootstrapper for ``store``.

    Distinct from the seeding path below in one way that matters: an
    unrecognised tag falls back to :data:`_DEFAULT_BNET_LOCALE` rather than
    being left alone. Seeding a preference is optional and a no-op is fine;
    this value is a **command-line argument the installer blocks on**. Launched
    without a usable locale the Battle.net bootstrapper stops on
    ``STATE_SELECT_LANGUAGE``, and in Gaming Mode that wizard has no gamescope
    session to render into, so it waits behind everything for the full
    30-minute timeout while the user looks at a Sign In button that did
    nothing. Passing a locale the client does not ship risks exactly that, so
    only codes known to :data:`_BNET_UI_LOCALES` are ever passed through.

    Why this is worth doing at all, given it was hardcoded ``enUS`` and worked:
    the bootstrapper derives its *region* from the locale (``Configuration:
    locale=enUS region=US`` in its own log) and warms the Agent's content store
    for that region before anyone has logged in. A non-US account then
    invalidates the whole warm-up on first login, which is what
    ``launcher/wrapper_client_cache`` exists to stop being re-paid.

    ``store`` is accepted for symmetry with the rest of the wrapper-store API;
    Battle.net is the only store with a bootstrapper today.
    """
    del store
    tag = plugin_locale()
    return _BNET_UI_LOCALES.get(tag or "", _DEFAULT_BNET_LOCALE)


# ── locale seeding ─────────────────────────────────────────────────────────


def _install_section_key(config: dict[str, Any]) -> str | None:
    """The top-level key that names the client's install section, or None.

    The measured layout stores launcher settings under a hash of the client's
    install path (``5a61123b37cafce1``). Discovered rather than hardcoded: a
    future client build could change the hash, and ``Client`` / ``Games`` are
    the only two fixed section names.
    """
    for key, value in config.items():
        if key not in ("Client", "Games") and isinstance(value, dict) and "Client" in value:
            return key
    return None


def _marker_path(auth: Path) -> Path:
    return Path(auth) / _LOCALE_MARKER


def _seeded_tag(auth: Path) -> str | None:
    """The BCP-47 tag last seeded into ``auth``, or None if never seeded.

    A v1 marker is retired here rather than read: it records only *that* a
    seed happened, which is the ambiguity this version exists to remove.
    """
    legacy = Path(auth) / _LEGACY_MARKER
    if legacy.exists():
        with contextlib.suppress(OSError):
            legacy.unlink()
        logger.info(
            "[wrapper_locale] retired the v1 seed marker in %s — "
            "re-seeding from the current plugin locale", Path(auth).name,
        )
        return None
    try:
        return _marker_path(auth).read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def _write_marker(auth: Path, tag: str) -> None:
    """Record the tag now in force. A failure here is a warning, not an error.

    The cost of losing this write is one redundant seed on the next launch,
    which is idempotent — so it must not be allowed to fail a launch.
    """
    try:
        _marker_path(auth).write_text(tag, encoding="utf-8")
    except OSError as exc:
        logger.warning("[wrapper_locale] cannot record the seeded locale: %s", exc)


def ensure_locale_seeded(spec: SessionSpec, auth_prefix: Path) -> bool:
    """Put the plugin's locale into ``auth_prefix`` whenever it has changed.

    Battle.net's factory default is ``enUS`` and the plugin already knows what
    language the user reads, so leaving the client in English is a setting the
    user has to go and find. Writing auth is the whole job: ``wrapper_prefs.
    merge`` carries the value to the template and to every game prefix, which
    was measured end to end (see the module docstring).

    Re-seeds only when the plugin's locale differs from the tag last written.
    That is what makes a language change in the plugin reach the client, while
    a language change made *inside* the client still survives every launch
    until the plugin's own setting moves again.

    Never raises. A fresh auth prefix whose Wine has not yet run has no config
    file to seed, and that is a no-op rather than a failure — the next launch
    that reaches this function will have one.
    """
    if spec.prefs is None:
        return False
    auth = Path(auth_prefix)
    tag = plugin_locale()
    if tag is None:
        # Nothing resolved. Do not record anything: the next launch should try
        # again rather than inherit this failure as a decision.
        return False
    if _seeded_tag(auth) == tag:
        return False
    bnet_locale = _BNET_UI_LOCALES.get(tag)
    if bnet_locale is None:
        # The client ships no such locale. Record the tag anyway so this is
        # one read per launch rather than a repeated config parse.
        _write_marker(auth, tag)
        logger.info(
            "[wrapper_locale] %s: no client locale for %s — leaving the "
            "language alone", spec.store, tag,
        )
        return False
    return _apply_seed(spec, auth, tag, bnet_locale)


def _apply_seed(
    spec: SessionSpec, auth: Path, tag: str, bnet_locale: str,
) -> bool:
    """Write ``bnet_locale`` into auth's config and record ``tag``.

    Split from the guards above to stay inside the locals cap.
    """
    path = config_path(spec, auth)
    if path is None:
        return False
    config = load_config(path)
    if not config:
        return False
    section = _install_section_key(config)
    if section is None:
        return False
    section_client = config[section].get("Client")
    if not isinstance(section_client, dict):
        return False
    if section_client.get("Language") == bnet_locale:
        # Already right — the user picked it in the client, or a capture
        # carried it back. Record the tag so this is a read next time.
        _write_marker(auth, tag)
        return False
    # Assigned rather than set-if-absent on purpose: a missing key used to be
    # read as "the user chose this" and skipped, which meant a config that had
    # never carried the key was never seeded at all.
    section_client["Language"] = bnet_locale
    if not write_config(path, config):
        return False
    _write_marker(auth, tag)
    logger.info(
        "[wrapper_locale] %s: seeded launcher language %s (%s) into %s. "
        "A game already installed keeps the locale data it downloaded; "
        "changing that is a download the vendor client has to schedule.",
        spec.store, bnet_locale, tag, auth.name,
    )
    return True
