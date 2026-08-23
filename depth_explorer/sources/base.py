"""Contract every source connector implements.

The pipeline is always ``fetch -> normalize -> chunk``. Only ``fetch`` and
``normalize`` are source-specific; chunking is shared
(:mod:`depth_explorer.core.chunking`), so a new source is a new
:class:`SourceConnector` and nothing else.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Dict, Iterable, List, Optional

from ..core.chunking import ChunkingConfig, record_to_chunks
from ..core.models import Chunk, SpeciesRecord


class SourceConnector(ABC):
    """Base class for an upstream data source."""

    #: Short stable slug used in record ids and chunk metadata.
    name: str = "unknown"

    @abstractmethod
    def fetch(self, scientific_name: str) -> Optional[Dict[str, Any]]:
        """Return the raw upstream payload for one species, or ``None``.

        Implementations should cache raw responses to disk before parsing, so a
        parser change never forces a re-download.
        """

    @abstractmethod
    def normalize(self, payload: Dict[str, Any]) -> SpeciesRecord:
        """Map a raw payload onto the canonical schema."""

    def to_chunks(
        self, record: SpeciesRecord, config: Optional[ChunkingConfig] = None
    ) -> List[Chunk]:
        """Render a canonical record into chunks. Shared by all sources."""
        return record_to_chunks(record, config)

    def ingest(
        self, scientific_names: Iterable[str], config: Optional[ChunkingConfig] = None
    ) -> "IngestResult":
        """Run the full pipeline over a list of species names."""
        result = IngestResult()
        for name in scientific_names:
            try:
                payload = self.fetch(name)
            except Exception as exc:  # noqa: BLE001 - one bad species must not end the run
                result.failed.append((name, f"{type(exc).__name__}: {exc}"))
                continue
            if payload is None:
                result.not_found.append(name)
                continue
            record = self.normalize(payload)
            result.records.append(record)
            result.chunks.extend(self.to_chunks(record, config))
        return result


class IngestResult:
    """Outcome of an ingestion run, including per-species failures."""

    def __init__(self) -> None:
        self.records: List[SpeciesRecord] = []
        self.chunks: List[Chunk] = []
        self.not_found: List[str] = []
        self.failed: List[tuple] = []

    def summary(self) -> Dict[str, Any]:
        sections: Dict[str, int] = {}
        for chunk in self.chunks:
            sections[chunk.section] = sections.get(chunk.section, 0) + 1
        tokens = sum(chunk.est_tokens for chunk in self.chunks)
        return {
            "records": len(self.records),
            "chunks": len(self.chunks),
            "chunks_by_section": dict(sorted(sections.items())),
            "est_tokens": tokens,
            "not_found": list(self.not_found),
            "failed": [{"species": n, "error": e} for n, e in self.failed],
        }
