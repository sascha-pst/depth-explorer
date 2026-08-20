"""Run a connector end to end and persist the corpus."""

from __future__ import annotations

import csv
import os
from typing import Any, Dict, Iterable, List, Optional

from .core.chunking import ChunkingConfig
from .core.jsonl import upsert_jsonl
from .sources.base import SourceConnector

RECORDS_FILE = "records.jsonl"
CHUNKS_FILE = "chunks.jsonl"


def species_from_csv(path: str, column: str = "scientific_name") -> List[str]:
    """Read species names from a CSV column.

    Falls back to joining ``genus`` and ``species`` columns when the named
    column is absent, which is how the repository's seed dataset is shaped.
    """
    names: List[str] = []
    with open(path, newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        headers = {h.lower(): h for h in (reader.fieldnames or [])}
        target = headers.get(column.lower())
        genus_col, species_col = headers.get("genus"), headers.get("species")
        if target is None and not (genus_col and species_col):
            raise ValueError(
                f"{path} has no {column!r} column and no genus/species pair "
                f"(found: {', '.join(reader.fieldnames or []) or 'nothing'})"
            )
        for row in reader:
            name = (row.get(target) or "").strip() if target else ""
            if not name and genus_col and species_col:
                name = f"{(row.get(genus_col) or '').strip()} {(row.get(species_col) or '').strip()}".strip()
            if name and name not in names:
                names.append(name)
    return names


def run_ingest(
    connector: SourceConnector,
    species: Iterable[str],
    out_dir: str = "data",
    config: Optional[ChunkingConfig] = None,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """Ingest ``species`` and upsert the results into ``out_dir``.

    Writing is an upsert keyed on the stable record and chunk ids, so
    re-running over an unchanged source leaves the corpus byte-identical.
    """
    names = list(species)
    result = connector.ingest(names, config)
    summary: Dict[str, Any] = {
        "source": connector.name,
        "requested": len(names),
        **result.summary(),
    }

    client = getattr(connector, "client", None)
    if client is not None and getattr(client, "stats", None):
        summary["http"] = dict(client.stats)
    if getattr(connector, "ambiguous", None):
        summary["ambiguous_names"] = connector.ambiguous

    if dry_run:
        summary["written"] = False
        return summary

    os.makedirs(out_dir, exist_ok=True)
    summary["records_written"] = upsert_jsonl(
        os.path.join(out_dir, RECORDS_FILE),
        (record.to_dict() for record in result.records),
        id_key="record_id",
    )
    summary["chunks_written"] = upsert_jsonl(
        os.path.join(out_dir, CHUNKS_FILE),
        (chunk.to_dict() for chunk in result.chunks),
        id_key="chunk_id",
    )
    summary["written"] = True
    summary["out_dir"] = out_dir
    return summary
