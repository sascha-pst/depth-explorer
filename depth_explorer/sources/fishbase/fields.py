"""Reading FishBase rows defensively.

FishBase descends from a Microsoft Access database and its REST mirrors expose
that history:

* **Column casing is not stable.** The same column appears as ``SpecCode``,
  ``speccode`` or ``SPECCODE`` depending on mirror and table, so every lookup is
  case-insensitive.
* **Some column names are misspelled upstream** — ``Southermost`` for the
  southern range limit is the best-known example. Call sites pass the variants
  they accept as aliases rather than hard-coding one spelling.
* **Booleans are Access booleans**: ``-1`` is true and ``0`` is false.
* **Free text carries HTML** (``<i>``, ``&nbsp;``) and inline literature markers
  such as ``(Ref. 12345)``, which are worth keeping as citations.
"""

from __future__ import annotations

import html
import re
from typing import Any, Dict, Iterable, List, Optional, Set

_TAG_RE = re.compile(r"<[^>]+>")
_WHITESPACE_RE = re.compile(r"[ \t ]+")
_NEWLINES_RE = re.compile(r"(\r\n|\r|\n){3,}")
_REFERENCE_RE = re.compile(r"\bRefs?\.\s*([\d,\s&and]+)", re.IGNORECASE)
_REF_NUMBER_RE = re.compile(r"\d{2,7}")

#: Strings FishBase uses where a value is simply absent.
_NULL_STRINGS = {"", "na", "n/a", "null", "none", "unknown", "-"}


def clean_text(value: Any) -> Optional[str]:
    """Strip HTML, unescape entities and normalize whitespace.

    Returns ``None`` for values that are empty or a FishBase null placeholder,
    so callers can distinguish "no text" from "empty string".
    """
    if value is None:
        return None
    text = str(value)
    text = _TAG_RE.sub(" ", text)
    text = html.unescape(text)
    text = text.replace(" ", " ")
    text = _WHITESPACE_RE.sub(" ", text)
    text = _NEWLINES_RE.sub("\n\n", text)
    text = "\n".join(line.strip() for line in text.splitlines())
    text = text.strip()
    if text.lower() in _NULL_STRINGS:
        return None
    return text or None


def extract_reference_ids(text: Optional[str]) -> List[str]:
    """Pull FishBase literature reference ids out of prose.

    ``"...spawns offshore (Ref. 5288, 30874)"`` yields ``["5288", "30874"]``.
    These are the handles for the papers behind a statement, which the RAG layer
    can surface as citations alongside a retrieved chunk.
    """
    if not text:
        return []
    found: List[str] = []
    for match in _REFERENCE_RE.finditer(text):
        for number in _REF_NUMBER_RE.findall(match.group(1)):
            if number not in found:
                found.append(number)
    return found


class Row:
    """Case-insensitive, type-coercing view over one FishBase result row."""

    def __init__(self, data: Optional[Dict[str, Any]] = None) -> None:
        self.data: Dict[str, Any] = dict(data or {})
        self._index: Dict[str, str] = {}
        for key in self.data:
            # First spelling wins, so an exact-case duplicate cannot shadow it.
            self._index.setdefault(str(key).strip().lower(), key)

    def __bool__(self) -> bool:
        return bool(self.data)

    def __contains__(self, alias: str) -> bool:
        return str(alias).strip().lower() in self._index

    def present_fields(self) -> Set[str]:
        """Original-cased field names present on this row."""
        return set(self.data)

    def raw(self, *aliases: str, default: Any = None) -> Any:
        """First alias present on the row, regardless of casing."""
        for alias in aliases:
            key = self._index.get(str(alias).strip().lower())
            if key is not None:
                return self.data[key]
        return default

    def text(self, *aliases: str) -> Optional[str]:
        """Cleaned free text for the first matching alias."""
        return clean_text(self.raw(*aliases))

    def number(self, *aliases: str) -> Optional[float]:
        """Float value, or ``None`` when absent or unparseable."""
        value = self.raw(*aliases)
        if value is None or isinstance(value, bool):
            return None
        if isinstance(value, (int, float)):
            return float(value)
        text = str(value).strip().replace(",", "")
        if not text or text.lower() in _NULL_STRINGS:
            return None
        match = re.search(r"-?\d+(?:\.\d+)?", text)
        return float(match.group(0)) if match else None

    def integer(self, *aliases: str) -> Optional[int]:
        value = self.number(*aliases)
        return int(value) if value is not None else None

    def flag(self, *aliases: str) -> Optional[bool]:
        """Access-style boolean: ``-1``/``1`` true, ``0`` false, absent ``None``."""
        value = self.raw(*aliases)
        if value is None:
            return None
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return value != 0
        text = str(value).strip().lower()
        if text in _NULL_STRINGS:
            return None
        if text in {"-1", "1", "true", "yes", "y"}:
            return True
        if text in {"0", "false", "no", "n"}:
            return False
        return None


def rows_from(payload: Optional[Iterable[Dict[str, Any]]]) -> List[Row]:
    """Wrap an iterable of raw dicts as :class:`Row` objects."""
    return [Row(item) for item in (payload or []) if isinstance(item, dict)]
