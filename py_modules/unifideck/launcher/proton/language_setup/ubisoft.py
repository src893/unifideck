from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import TYPE_CHECKING

from .registry_io import (
    _atomic_write_text,
    _resolve_prefix,
)
from .resolver import UBISOFT_LANG_MAP, get_unifideck_language

if TYPE_CHECKING:
    from unifideck.config import ConfigManager
logger = logging.getLogger(__name__)
def _load_ubisoft_install_id(space_id: str) -> str | None:
    """Load UBISOFT install ID."""
    id_map_path = str(Path("~/.local/share/unifideck/ubisoft_id_map.json").expanduser())
    if not Path(id_map_path).is_file():
        return None
    try:
        with Path(id_map_path).open(encoding="utf-8") as fh:
            id_map = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return None
    entry = id_map.get(space_id, {})
    install_id = entry.get("install_id")
    return install_id if isinstance(install_id, str) else None
def _patch_upc_language_section(
    content: str, install_id: str, ubi_lang: str,
) -> str:
    """Patch UPC language section."""
    section = (
        f"[Software\\\\WOW6432Node\\\\Ubisoft\\\\Launcher\\\\"
        f"Installs\\\\{install_id}]"
    )
    if section not in content:
        return content + f'\n{section}\n"Language"="{ubi_lang}"\n'
    sec_start = content.index(section)
    body_start = sec_start + len(section)
    next_sec = re.search(r"\n\[", content[body_start:])
    sec_end = (
        body_start + next_sec.start()
        if next_sec else len(content)
    )
    sec_body = content[body_start:sec_end]
    pattern = r'^"Language"="[^"]*"'
    replacement = f'"Language"="{ubi_lang}"'
    new_body, count = re.subn(
        pattern, replacement, sec_body, flags=re.MULTILINE,
    )
    if count > 0:
        sec_body = new_body
    else:
        sec_body = (
            sec_body.rstrip("\n") + f'\n"Language"="{ubi_lang}"\n'
        )
    return content[:body_start] + sec_body + content[sec_end:]

def _apply_ubisoft_upc_language(
    prefix_path: str, space_id: str, language: str,
) -> bool:

    """Apply UBISOFT UPC language."""
    install_id = _load_ubisoft_install_id(space_id)
    if not install_id:
        logger.info(
            "[language_setup.ubisoft] no install_id for space_id=%s, "
            "skipping UPC language", space_id,
        )
        return False
    system_reg = str(Path(prefix_path) / "system.reg")
    if not Path(system_reg).is_file():
        logger.info(
            "[language_setup.ubisoft] system.reg missing at %s, "
            "skipping UPC language", system_reg,
        )
        return False
    with Path(system_reg).open(encoding="utf-8", errors="replace") as fh:
        content = fh.read()
    ubi_lang = UBISOFT_LANG_MAP.get(
        language, language.split("-", maxsplit=1)[0],
    )
    content = _patch_upc_language_section(content, install_id, ubi_lang)
    _atomic_write_text(system_reg, content)
    logger.info(
        "[language_setup.ubisoft] UPC Language=%s written "
        "(install_id=%s)", ubi_lang, install_id,
    )
    return True
def apply_ubisoft_language(
    prefix_path: str, space_id: str = "",
    config: ConfigManager | None = None,
) -> bool:
    """Set UPC's own ``Language`` for this install.

    The prefix's Windows locale is **not** written here any more: it is
    store-agnostic and applied once from ``proton.dispatch`` for every
    launch, so doing it here as well was a duplicate write of identical
    values. What remains is genuinely Ubisoft's — the UPC launcher reads its
    language from its own registry key, in its own two-letter spelling, under
    an install id only this store knows how to resolve.
    """
    language = get_unifideck_language(config)
    logger.info(
        "[language_setup.ubisoft] applying %s to prefix=%s space_id=%s",
        language, prefix_path, space_id,
    )
    if not space_id:
        return False
    resolved_prefix = _resolve_prefix(prefix_path)
    return _apply_ubisoft_upc_language(resolved_prefix, space_id, language)
