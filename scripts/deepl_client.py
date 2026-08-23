"""scripts/deepl_client.py — stdlib-only DeepL translate client.

Isolated from ``translate_at_build.py`` so the i18n pipeline
(diff / cache / orchestration) and the network layer (HTTP POST /
response parsing) live in separate modules. Two benefits:

  1. ``translate_at_build.py`` fits under the default 550 LOC cap
     without the network code bloating it.
  2. Tests can stub one side without touching the other —
     e.g. unit-test the diff pipeline with a fake ``translate_batch``,
     or unit-test the parser with a canned response body.

Public surface:
  - ``endpoint_for(api_key)`` — free vs pro URL selection
  - ``translate_batch(...)`` — POST a list of texts to DeepL and
    return the translated strings in input order

Stdlib-only (``urllib``) so we don't pull in ``requests`` or the
official ``deepl`` package as a runtime dependency — the script
runs in CI where every extra dep is extra supply-chain surface.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request


# Free-tier keys end with ":fx"; pro keys don't.
FREE_URL = "https://api-free.deepl.com/v2/translate"
PRO_URL = "https://api.deepl.com/v2/translate"

# 30s is generous — DeepL typically responds under 3s even for
# 25-text batches. Set conservatively to survive transient
# network slowdowns without hanging CI.
REQUEST_TIMEOUT_SEC = 30


def endpoint_for(api_key: str) -> str:
    """Return the correct DeepL endpoint URL for ``api_key``."""
    return FREE_URL if api_key.endswith(":fx") else PRO_URL


def translate_batch(
    texts: list[str],
    target_lang: str,
    source_lang: str,
    api_key: str,
) -> list[str]:
    """Translate ``texts`` into ``target_lang`` via DeepL.

    Returns translated strings in input order. Raises
    ``RuntimeError`` on any API or transport error — the caller
    aborts the run (there is no partial-success mode for i18n
    builds; a half-translated JSON would silently ship).
    """
    body = _post(texts, target_lang, source_lang, api_key)
    return _parse_response(body, target_lang, expected=len(texts))


def _post(
    texts: list[str],
    target_lang: str,
    source_lang: str,
    api_key: str,
) -> str:
    """POST the translate request and return the raw response body.

    The key travels in the ``Authorization`` header, not the form
    body: DeepL removed form-body auth in November 2025 and now
    answers ``403 Legacy authentication method 'form body' is no
    longer supported`` to any request carrying ``auth_key`` there.
    See https://developers.deepl.com/docs/getting-started/auth
    """
    params: list[tuple[str, str]] = [
        ("target_lang", target_lang),
        ("source_lang", source_lang),
        # preserve_formatting keeps {placeholders} intact so
        # interpolations like "Hello, {name}" survive translation.
        ("preserve_formatting", "1"),
        # tag_handling=xml tells DeepL to treat <...> markers as
        # non-translatable. We don't use XML but this prevents
        # it from translating HTML-like content.
        ("tag_handling", "xml"),
    ]
    params.extend(("text", t) for t in texts)

    data = urllib.parse.urlencode(params).encode("utf-8")
    req = urllib.request.Request(
        endpoint_for(api_key),
        data=data,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Authorization": f"DeepL-Auth-Key {api_key}",
        },
    )
    try:
        with urllib.request.urlopen(  # noqa: S310 — trusted host
            req, timeout=REQUEST_TIMEOUT_SEC,
        ) as resp:
            return resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(
            f"DeepL API error {e.code} for {target_lang}: {err_body}",
        ) from e
    except urllib.error.URLError as e:
        raise RuntimeError(
            f"DeepL API unreachable for {target_lang}: {e}",
        ) from e


def _parse_response(
    body: str, target_lang: str, *, expected: int,
) -> list[str]:
    """Decode the JSON body + validate the translation count.

    DeepL guarantees ``translations`` mirrors the input ``text``
    order, so a count mismatch means the response is corrupt
    and we abort rather than silently misalign keys.
    """
    try:
        parsed = json.loads(body)
    except json.JSONDecodeError as e:
        raise RuntimeError(
            f"DeepL returned invalid JSON for {target_lang}: "
            f"{body[:200]}",
        ) from e
    translations = parsed.get("translations", [])
    if len(translations) != expected:
        raise RuntimeError(
            f"DeepL returned {len(translations)} translations for "
            f"{expected} inputs (target: {target_lang})",
        )
    return [t["text"] for t in translations]
