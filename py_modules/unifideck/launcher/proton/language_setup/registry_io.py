from __future__ import annotations

import contextlib
import logging
import os
import re
import tempfile
from pathlib import Path

from .matchers import smart_match_locale
from .resolver import _DEFAULT_LANGUAGE, LOCALE_MAP

logger = logging.getLogger(__name__)
def _resolve_prefix(prefix_path: str) -> str:
    """Resolve prefix."""
    from unifideck.launcher.proton.infrastructure.prefix_layout import (
        resolve_registry_prefix,
    )
    return str(resolve_registry_prefix(prefix_path))
def _atomic_write_text(path: str, content: str) -> None:
    """Atomic write text."""
    target_dir = str(Path(path).parent) or "."
    fd, tmp_path = tempfile.mkstemp(
        prefix=".reg.", suffix=".tmp", dir=target_dir,
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(content)
            fh.flush()
            os.fsync(fh.fileno())
        Path(tmp_path).replace(path)
    except Exception:
        with contextlib.suppress(OSError):
            Path(tmp_path).unlink()
        raise

def _update_user_reg(
    prefix_path: str,
    lcid: str, slanguage: str, locale_name: str, scountry: str,
) -> bool:

    """Update user reg."""
    user_reg = str(Path(prefix_path) / "user.reg")
    if not Path(user_reg).exists():
        logger.warning(
            "[language_setup] user.reg missing at %s — prefix not "
            "initialised yet", user_reg,
        )
        return False
    with Path(user_reg).open(encoding="utf-8", errors="replace") as fh:
        content = fh.read()
    section_header = "[Control Panel\\\\International]"
    new_values = {
        "Locale": lcid,
        "LocaleName": locale_name,
        "sLanguage": slanguage,
        "sCountry": scountry,
    }
    if section_header in content:
        section_start = content.index(section_header)
        body_start = section_start + len(section_header)
        next_section = re.search(r"\n\[", content[body_start:])
        section_end = (
            body_start + next_section.start()
            if next_section else len(content)
        )
        section_body = content[body_start:section_end]
        for key, value in new_values.items():
            pattern = rf'^"{re.escape(key)}"="[^"]*"'
            replacement = f'"{key}"="{value}"'
            new_body, count = re.subn(
                pattern, replacement, section_body, flags=re.MULTILINE,
            )
            if count > 0:
                section_body = new_body
            else:
                section_body = (
                    section_body.rstrip("\n") + f'\n"{key}"="{value}"\n'
                )
        content = (
            content[:body_start] + section_body + content[section_end:]
        )
    else:
        section = f"\n{section_header}\n"
        for key, value in new_values.items():
            section += f'"{key}"="{value}"\n'
        content += section
    _atomic_write_text(user_reg, content)
    return _confirm_written(user_reg, new_values)


def _confirm_written(user_reg: str, expected: dict[str, str]) -> bool:
    """Read the file back and report what the prefix will actually see.

    Not ceremony, and the same guard ``prefix/tweaks.py`` documents for
    ``Battle.net.config``. Measured on-device 2026-08-23: this function's
    caller logged ``wrote locale=fr-FR`` for a prefix whose ``user.reg``
    still read ``en-US`` half an hour later, because a live wineserver owned
    the registry and flushed its own copy over ours on exit. The write
    reported success at every layer. A read-back is one open and settles it.
    """
    try:
        with Path(user_reg).open(encoding="utf-8", errors="replace") as fh:
            content = fh.read()
    except OSError as exc:
        logger.warning("[language_setup] cannot read back %s: %s", user_reg, exc)
        return False
    missing = {
        key for key, value in expected.items()
        if f'"{key}"="{value}"' not in content
    }
    if missing:
        logger.warning(
            "[language_setup] %s did not keep %s — something rewrote the "
            "registry underneath us", user_reg, sorted(missing),
        )
        return False
    logger.info(
        "[language_setup] locale in force: %s (%s)",
        expected["LocaleName"], user_reg,
    )
    return True


def _apply_windows_locale(prefix_path: str, language: str) -> bool:
    """Write ``language`` into the prefix's ``Control Panel\\International``.

    Refuses while any Wine process is alive in the prefix. A live wineserver
    holds the registry in memory and rewrites the file when it exits, so a
    write underneath one is discarded without error — the worst kind of
    failure, because every log line says success. That is not hypothetical:
    it is what this function did on 2026-08-23 before the check existed.

    A refusal is an INFO line and a ``False``, never an exception. The next
    launch on a quiet prefix applies it, which is the same "eventually, but
    never wrongly" shape ``battlenet_bootstrap.ensure_tweaks`` uses.
    """
    resolved_prefix = _resolve_prefix(prefix_path)
    if not _registry_is_writable(prefix_path):
        return False
    locale = smart_match_locale(language)
    if locale is None:
        logger.info(
            "[language_setup] no locale mapping for %s, using %s",
            language, _DEFAULT_LANGUAGE,
        )
        locale = LOCALE_MAP[_DEFAULT_LANGUAGE]
    return _update_user_reg(resolved_prefix, *locale)


def _registry_is_writable(prefix_path: str) -> bool:
    """Whether nothing in the prefix currently owns the registry.

    Imported lazily so this module stays importable in isolation, matching
    how the rest of ``language_setup`` reaches out of itself.
    """
    try:
        from unifideck.launcher.proton.infrastructure.wineserver_reap import (
            prefix_wine_pids,
        )
        from unifideck.launcher.wine_registry import registry_is_writable
    except ImportError:  # pragma: no cover - defensive
        logger.debug("[language_setup] liveness check unavailable", exc_info=True)
        return True
    return registry_is_writable(prefix_path, len(prefix_wine_pids(prefix_path)))
