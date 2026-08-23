"""JSONL read/write with id-keyed upsert.

The corpus is stored as newline-delimited JSON so ingestion runs can append and
so downstream loaders can stream it without holding the corpus in memory.
"""

from __future__ import annotations

import json
import os
import tempfile
from typing import Any, Dict, Iterable, Iterator, List


def write_jsonl(path: str, rows: Iterable[Dict[str, Any]]) -> int:
    """Write ``rows`` to ``path`` atomically. Returns the number of rows."""
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    count = 0
    fd, tmp = tempfile.mkstemp(dir=directory, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            for row in rows:
                handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True))
                handle.write("\n")
                count += 1
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
    return count


def read_jsonl(path: str) -> Iterator[Dict[str, Any]]:
    """Yield rows from a JSONL file; a missing file yields nothing."""
    if not os.path.exists(path):
        return
    with open(path, "r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                yield json.loads(line)


def upsert_jsonl(path: str, rows: Iterable[Dict[str, Any]], id_key: str) -> Dict[str, int]:
    """Merge ``rows`` into ``path`` keyed on ``id_key``.

    Existing rows keep their position in the file, so a re-ingest of unchanged
    data rewrites an identical file. Returns counts of added/updated/unchanged.
    """
    existing: List[Dict[str, Any]] = list(read_jsonl(path))
    index = {row.get(id_key): position for position, row in enumerate(existing)}
    stats = {"added": 0, "updated": 0, "unchanged": 0}

    for row in rows:
        key = row.get(id_key)
        if key in index:
            position = index[key]
            if existing[position] == row:
                stats["unchanged"] += 1
            else:
                existing[position] = row
                stats["updated"] += 1
        else:
            index[key] = len(existing)
            existing.append(row)
            stats["added"] += 1

    write_jsonl(path, existing)
    stats["total"] = len(existing)
    return stats
