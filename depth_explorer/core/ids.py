"""Deterministic identifiers.

Ingestion must be idempotent: re-running the pipeline over an unchanged source
has to produce byte-identical ids so a vector store can upsert instead of
duplicating. Ids are therefore derived from position (source, record, section,
ordinal) and never from content or wall-clock time. ``content_hash`` is carried
alongside so a store can still tell whether the *text* at a stable id changed.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def slugify(value: str) -> str:
    """Lowercase ASCII slug, e.g. ``"Thunnus thynnus" -> "thunnus-thynnus"``."""
    decomposed = unicodedata.normalize("NFKD", value)
    ascii_only = decomposed.encode("ascii", "ignore").decode("ascii")
    return _SLUG_RE.sub("-", ascii_only.lower()).strip("-")


def record_id(source: str, native_id: str) -> str:
    """Stable id for one species as seen by one source, e.g. ``fishbase:143``."""
    return f"{source}:{native_id}"


def chunk_id(record_id_: str, section: str, ordinal: int) -> str:
    """Stable id for one chunk, e.g. ``fishbase:143#habitat_depth:0``."""
    return f"{record_id_}#{section}:{ordinal}"


def content_hash(text: str, length: int = 16) -> str:
    """Truncated SHA-256 of chunk text, for change detection at a stable id."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:length]


def cache_key(*parts: str, length: int = 32) -> str:
    """Filesystem-safe key for a cached HTTP response."""
    joined = "\x1f".join(parts)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()[:length]
