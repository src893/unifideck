"""scripts/translate_at_build.py — DeepL build-time translator.

Invoked from the GitHub Actions ``tests.yml`` workflow before the
frontend build step. Reads the canonical list of locales from
``defaults/config.json`` (section ``i18n.locales``) and translates
new or changed keys from the source locale JSON into every target
locale file, with a per-key hash cache so unchanged keys are
never re-sent.

Single source of truth for the language list: ``defaults/config.json``.
Adding a language = adding an entry there; this script and the
frontend catalog (``src/i18n/locales.generated.ts``) pick it up
automatically.

Flow: load + validate config → load source JSON → load cache →
diff (changed, deleted) → translate changed keys via DeepL →
write target files → update cache.

Security: reads ``DEEPL_API_KEY`` from env, never from disk.
With no key set (the manual-translation workflow, and fork PRs)
this is a complete no-op: it reports what would have been
translated and touches nothing on disk. Locale files are then
maintained by hand — ``scripts/check_orphan_keys.py`` reports the
completeness gaps.

Exit codes:
    0 — success (done, nothing to do, or no API key)
    2 — DeepL API or I/O failure
    3 — config / argument / missing-file error

Reference: Technical Document v1.0 — Section 10 (i18n),
Figure 85. Operational Plan — OP-TRANS.

Layout: constants → cache helpers → flatten/unflatten → diff
helpers → target-file I/O → pipeline stages → ``main``. The
DeepL HTTP layer lives in ``scripts/deepl_client.py``.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path

from deepl_client import translate_batch
from locale_config import Locale, LocaleConfig, LocaleConfigError, load_from_path


# ══════════════════════════════════════════════════════════════
# Constants
# ══════════════════════════════════════════════════════════════

# DeepL accepts up to 50 texts per call; 25 stays well within
# limits and gives clearer error messages if one key fails.
BATCH_SIZE = 25

# Exit codes — mirrored in the module docstring.
EXIT_OK = 0
EXIT_IO_OR_API_ERROR = 2
EXIT_CONFIG_ERROR = 3


# ══════════════════════════════════════════════════════════════
# Cache helpers — per-key MD5 of the source value
# ══════════════════════════════════════════════════════════════


def md5_of(value: str) -> str:
    """Return the hex MD5 of a UTF-8 string.

    Used as the signature of a translation entry in the cache.
    ``usedforsecurity=False`` tells hashlib this is purely for
    cache-key dedup (non-cryptographic), which silences
    FIPS-mode warnings on locked-down environments and quiets
    S324 on static analyzers.
    """
    return hashlib.md5(
        value.encode("utf-8"), usedforsecurity=False,
    ).hexdigest()


def load_cache(path: Path) -> dict[str, str]:
    """Load the per-key MD5 cache, or return ``{}`` if missing.

    A corrupt cache is treated as "missing" — start fresh
    rather than crashing. Worst case is a full re-translate
    on this run.
    """
    if not path.is_file():
        return {}
    try:
        with path.open() as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        print(
            f"[translate] warning: cache at {path} corrupt, ignoring",
            file=sys.stderr,
        )
        return {}


def save_cache(path: Path, cache: dict[str, str]) -> None:
    """Write the cache atomically (tmp + rename).

    Prevents a crash mid-write from leaving a half-written file
    that breaks the next run.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with tmp.open("w") as f:
        json.dump(cache, f, indent=2, sort_keys=True, ensure_ascii=False)
        f.write("\n")
    tmp.replace(path)


# ══════════════════════════════════════════════════════════════
# JSON flatten / unflatten (lossless, dotted keys ↔ nested dicts)
# ══════════════════════════════════════════════════════════════


def flatten_json(obj: object, prefix: str = "") -> dict[str, str]:
    """Flatten a nested translation dict into dotted-key form.

    Input:  ``{"library": {"title": "Library", "empty": "No games"}}``
    Output: ``{"library.title": "Library", "library.empty": "No games"}``

    Only string leaves are included; integers, booleans, and
    lists are skipped (they shouldn't appear in i18n files
    anyway). The flat form makes per-key cache comparison
    trivially simple.
    """
    flat: dict[str, str] = {}
    if not isinstance(obj, dict):
        return flat
    for key, value in obj.items():
        composed = f"{prefix}.{key}" if prefix else key
        if isinstance(value, dict):
            flat.update(flatten_json(value, composed))
        elif isinstance(value, str):
            flat[composed] = value
        # else: non-string leaf silently skipped
    return flat


def unflatten_dict(flat: dict[str, str]) -> dict[str, object]:
    """Rebuild a nested dict from a flat dotted-key dict.

    Inverse of ``flatten_json``. Used when writing locale files
    so they stay human-readable with nested sections.
    """
    out: dict[str, object] = {}
    for key, value in flat.items():
        parts = key.split(".")
        cursor = out
        for part in parts[:-1]:
            next_cursor = cursor.setdefault(part, {})
            # Defensive: if a caller mixes "a.b" as both a leaf
            # and an intermediate node, the first wins and the
            # second is skipped rather than clobbering the tree.
            if not isinstance(next_cursor, dict):
                break
            cursor = next_cursor
        else:
            cursor[parts[-1]] = value
    return out


# ══════════════════════════════════════════════════════════════
# Translation orchestration — delegates HTTP to deepl_client
# ══════════════════════════════════════════════════════════════


def translate_for_locale(
    keys_to_translate: dict[str, str],
    target_deepl_code: str,
    source_deepl_code: str,
    api_key: str,
) -> dict[str, str]:
    """Translate ``{key: source_value}`` into one target locale.

    Batches to stay under DeepL per-request limits. Returns a
    new dict with the same keys and translated values.
    """
    result: dict[str, str] = {}
    items = list(keys_to_translate.items())
    for i in range(0, len(items), BATCH_SIZE):
        batch = items[i:i + BATCH_SIZE]
        keys = [k for k, _ in batch]
        texts = [v for _, v in batch]
        translated = translate_batch(
            texts, target_deepl_code, source_deepl_code, api_key,
        )
        for key, translation in zip(keys, translated):
            result[key] = translation
    return result


# ══════════════════════════════════════════════════════════════
# Diff helpers
# ══════════════════════════════════════════════════════════════


def compute_changed_keys(
    source_flat: dict[str, str], cache: dict[str, str],
) -> dict[str, str]:
    """Return keys whose source value has a different MD5 vs cache.

    Includes both "new" keys (absent from cache) and "modified"
    keys (present with stale MD5). The caller decides whether to
    translate them — we only report the diff.
    """
    return {
        key: value
        for key, value in source_flat.items()
        if cache.get(key) != md5_of(value)
    }


def compute_deleted_keys(
    source_flat: dict[str, str], cache: dict[str, str],
) -> set[str]:
    """Return keys present in cache but absent from source."""
    return set(cache.keys()) - set(source_flat.keys())


def build_new_cache(
    source_flat: dict[str, str],
    old_cache: dict[str, str],
    changed: dict[str, str],
    *,
    translations_applied: bool,
) -> dict[str, str]:
    """Return the cache dict that should be persisted.

    Changed keys get their fresh MD5 only when translations were
    actually applied (API key available). Unchanged keys inherit
    their previous MD5. Deleted keys are implicitly dropped by
    iterating over ``source_flat``.
    """
    new_cache: dict[str, str] = {}
    for key, value in source_flat.items():
        if key in changed and translations_applied:
            new_cache[key] = md5_of(value)
        elif key in old_cache:
            new_cache[key] = old_cache[key]
    return new_cache


# ══════════════════════════════════════════════════════════════
# Target-file I/O
# ══════════════════════════════════════════════════════════════


def load_target_flat(target_path: Path) -> dict[str, str]:
    """Load a target locale file and return its flat form.

    Missing file → empty dict (first time we translate into this
    locale). The pipeline later merges translations in.
    """
    if not target_path.is_file():
        return {}
    with target_path.open() as f:
        return flatten_json(json.load(f))


def write_target_file(target_path: Path, flat: dict[str, str]) -> None:
    """Write ``flat`` as a nested JSON file at ``target_path``."""
    target_path.parent.mkdir(parents=True, exist_ok=True)
    nested = unflatten_dict(flat)
    with target_path.open("w") as f:
        json.dump(nested, f, indent=2, sort_keys=True, ensure_ascii=False)
        f.write("\n")


def update_one_target(
    target: Locale,
    locales_dir: Path,
    changed: dict[str, str],
    deleted: set[str],
    source_deepl_code: str,
    api_key: str,
) -> None:
    """Apply changes to one target locale's JSON file.

    Removes deleted keys, translates changed keys (if we have an
    API key), merges the result into the existing file, and
    writes it back. Pure side-effect function — its contract is
    "after I return, ``{target.tag}.json`` is up to date".
    """
    assert target.deepl_code is not None, (
        "target.deepl_code must be set — guaranteed by LocaleConfig"
    )
    target_path = locales_dir / f"{target.tag}.json"
    target_flat = load_target_flat(target_path)

    for key in deleted:
        target_flat.pop(key, None)

    if changed and api_key:
        print(
            f"[translate] → {target.tag} ({target.deepl_code}): "
            f"{len(changed)} keys",
        )
        translated = translate_for_locale(
            changed, target.deepl_code, source_deepl_code, api_key,
        )
        target_flat.update(translated)

    write_target_file(target_path, target_flat)


# ══════════════════════════════════════════════════════════════
# Pipeline stages
# ══════════════════════════════════════════════════════════════


def parse_cli_args() -> argparse.Namespace:
    """Parse command-line arguments.

    Defaults match the repository layout so a bare
    ``python translate_at_build.py`` run works from the project
    root without any flag.
    """
    parser = argparse.ArgumentParser(
        description=(
            "Translate the source locale to all target locales "
            "via DeepL, using the i18n section of config.json "
            "as the single source of truth for the language list."
        ),
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("defaults/config.json"),
        help="Path to defaults/config.json",
    )
    parser.add_argument(
        "--locales-dir",
        type=Path,
        default=Path("src/i18n/locales"),
        help="Directory containing the {tag}.json locale files",
    )
    parser.add_argument(
        "--cache-file",
        type=Path,
        default=Path("src/i18n/.translation-cache.json"),
        help="Path to the per-key MD5 cache file",
    )
    return parser.parse_args()


def load_config_or_exit(config_path: Path) -> LocaleConfig:
    """Load + validate the i18n config or call ``sys.exit(3)``.

    ``LocaleConfigError`` is the expected failure mode — any
    other exception propagates up to the top-level guard in
    ``__main__`` which emits exit code 2.
    """
    try:
        return load_from_path(config_path)
    except LocaleConfigError as e:
        print(f"[translate] config error: {e}", file=sys.stderr)
        sys.exit(EXIT_CONFIG_ERROR)


def load_source_or_exit(source_path: Path) -> dict[str, str]:
    """Load the source locale JSON flat-form or exit 3."""
    if not source_path.is_file():
        print(
            f"[translate] error: source file not found: {source_path}",
            file=sys.stderr,
        )
        sys.exit(EXIT_CONFIG_ERROR)
    with source_path.open() as f:
        return flatten_json(json.load(f))


def source_deepl_lang(source: Locale) -> str:
    """Extract the DeepL source-language code from a ``Locale``.

    DeepL takes ``source_lang`` as a language only (never a
    regional variant), so ``en-US`` → ``EN``, ``fr-FR`` → ``FR``.
    """
    return source.tag.split("-")[0].upper()


def warn_no_api_key(changed: dict[str, str], deleted: set[str]) -> None:
    """Report the pending diff when no ``DEEPL_API_KEY`` is set.

    Machine translation is opt-in: locale JSONs are maintained by
    hand, so a missing key is the normal case, not an error. The
    caller returns without touching disk — this only says what a
    keyed run would have done, so the gap is visible in the log.
    """
    print(
        f"[translate] no DEEPL_API_KEY — skipping machine translation "
        f"({len(changed)} keys would be translated, "
        f"{len(deleted)} removed). Locale files are maintained "
        f"manually; run scripts/check_orphan_keys.py for the "
        f"completeness report.",
        file=sys.stderr,
    )


def apply_translations(
    config: LocaleConfig,
    args: argparse.Namespace,
    changed: dict[str, str],
    deleted: set[str],
    api_key: str,
) -> None:
    """Walk every target locale and update its JSON file."""
    source_deepl = source_deepl_lang(config.source)
    for target in config.targets:
        update_one_target(
            target, args.locales_dir,
            changed, deleted, source_deepl, api_key,
        )


# ══════════════════════════════════════════════════════════════
# Entry point
# ══════════════════════════════════════════════════════════════


def main() -> int:
    """Top-level orchestrator — each step is its own helper.

    Returns the process exit code. The ``__main__`` guard maps
    unexpected exceptions to ``EXIT_IO_OR_API_ERROR`` (2) and
    prints a diagnostic.
    """
    args = parse_cli_args()
    config = load_config_or_exit(args.config)

    print(f"[translate] source locale: {config.source.tag}")
    print(
        f"[translate] target locales: {len(config.targets)} "
        f"({', '.join(t.tag for t in config.targets)})",
    )

    source_path = args.locales_dir / f"{config.source.tag}.json"
    source_flat = load_source_or_exit(source_path)
    print(f"[translate] loaded {len(source_flat)} keys from {source_path}")

    cache = load_cache(args.cache_file)
    changed = compute_changed_keys(source_flat, cache)
    deleted = compute_deleted_keys(source_flat, cache)

    if not changed and not deleted:
        print("[translate] cache is up to date, nothing to do")
        return EXIT_OK

    print(
        f"[translate] {len(changed)} keys changed, "
        f"{len(deleted)} keys deleted",
    )

    api_key = os.environ.get("DEEPL_API_KEY", "").strip()
    if not api_key:
        # Return BEFORE apply_translations: with no key it would
        # still rewrite all 15 target files (re-sorted, deletions
        # applied), churning hand-maintained translations for no
        # benefit. No key means touch nothing.
        warn_no_api_key(changed, deleted)
        return EXIT_OK

    apply_translations(config, args, changed, deleted, api_key)

    new_cache = build_new_cache(
        source_flat, cache, changed,
        translations_applied=bool(api_key),
    )
    save_cache(args.cache_file, new_cache)
    print(f"[translate] cache updated with {len(new_cache)} entries")
    return EXIT_OK


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001 — top-level guard
        print(
            f"[translate] fatal: {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        sys.exit(EXIT_IO_OR_API_ERROR)
