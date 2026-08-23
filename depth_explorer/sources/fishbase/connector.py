"""FishBase connector: resolve a name, fetch its tables, normalize, chunk."""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

from ..base import SourceConnector
from ...core.models import SpeciesRecord
from .client import FishBaseClient
from .fields import Row, rows_from
from .normalize import normalize_species
from .tables import ALL_TABLES, DEFAULT_TABLES


def split_binomial(scientific_name: str) -> Optional[Dict[str, str]]:
    """Split ``"Thunnus thynnus Linnaeus, 1758"`` into genus and epithet.

    Authorities and subspecies are dropped: FishBase keys the species table on
    genus plus epithet alone.
    """
    parts = (scientific_name or "").replace(",", " ").split()
    words = [p for p in parts if p and p[0].isalpha()]
    if len(words) < 2:
        return None
    genus, epithet = words[0], words[1]
    if not epithet[:1].islower():
        return None
    return {"Genus": genus.capitalize(), "Species": epithet.lower()}


class FishBaseConnector(SourceConnector):
    """Ingest one species at a time from FishBase or SeaLifeBase."""

    def __init__(
        self,
        client: Optional[FishBaseClient] = None,
        tables: Optional[Sequence[str]] = None,
        source_name: Optional[str] = None,
    ) -> None:
        self.client = client or FishBaseClient()
        self.name = source_name or self.client.server
        requested = list(tables) if tables is not None else list(DEFAULT_TABLES)
        unknown = [t for t in requested if t not in ALL_TABLES]
        if unknown:
            raise ValueError(f"unknown FishBase table(s): {', '.join(unknown)}")
        # `species` carries the SpecCode every other lookup needs.
        self.tables = ["species"] + [t for t in requested if t != "species"]
        #: Names that resolved to more than one species row, for the run report.
        self.ambiguous: List[Dict[str, Any]] = []

    # -- fetch ------------------------------------------------------------

    def fetch(self, scientific_name: str) -> Optional[Dict[str, Any]]:
        """Fetch every configured table for one species.

        Returns ``None`` when the name does not resolve, so the caller can
        report it as not-found rather than treating it as an error.
        """
        query = split_binomial(scientific_name)
        if query is None:
            raise ValueError(f"not a binomial name: {scientific_name!r}")

        species_rows, species_envelope = self.client.fetch_table("species", query)
        if not species_rows:
            return None
        if len(species_rows) > 1:
            self.ambiguous.append(
                {"query": scientific_name, "matches": len(species_rows)}
            )

        species_row = Row(species_rows[0])
        speccode = species_row.integer("SpecCode")

        payload: Dict[str, Any] = {
            "source": self.name,
            "server": self.client.server,
            "query_name": scientific_name,
            "speccode": speccode,
            "citation": self.client.citation,
            "source_url": self.client.species_page_url(speccode) if speccode else None,
            "retrieved_at": species_envelope.get("retrieved_at"),
            "tables": {
                "species": {
                    "rows": [species_rows[0]],
                    "url": species_envelope.get("url"),
                    "retrieved_at": species_envelope.get("retrieved_at"),
                }
            },
        }

        if speccode is None:
            # Without a SpecCode the dependent tables cannot be keyed; the
            # species row alone still normalizes into a usable record.
            return payload

        for table in self.tables:
            if table == "species":
                continue
            rows, envelope = self.client.fetch_table(table, {"SpecCode": speccode})
            payload["tables"][table] = {
                "rows": rows,
                "url": envelope.get("url"),
                "retrieved_at": envelope.get("retrieved_at"),
            }

        return payload

    # -- normalize --------------------------------------------------------

    def normalize(self, payload: Dict[str, Any]) -> SpeciesRecord:
        return normalize_species(payload)

    # -- schema verification ----------------------------------------------

    def verify_schema(self, sample_species: str = "Thunnus thynnus") -> Dict[str, Any]:
        """Compare a live response against the fields the normalizer reads.

        FishBase mirrors differ in column casing and occasionally rename
        columns. Running this against a live endpoint turns "the output is
        mysteriously empty" into an explicit list of missing columns.
        """
        payload = self.fetch(sample_species)
        report: Dict[str, Any] = {
            "sample_species": sample_species,
            "server": self.client.server,
            "base_url": self.client.base_url,
            "resolved": payload is not None,
            "tables": {},
        }
        if payload is None:
            return report

        for table in self.tables:
            entry = payload["tables"].get(table) or {}
            rows = rows_from(entry.get("rows"))
            spec = ALL_TABLES[table]
            present = set()
            for row in rows:
                present |= {field.lower() for field in row.present_fields()}
            missing = [f for f in spec.expected_fields if f.lower() not in present]
            report["tables"][table] = {
                "url": entry.get("url"),
                "rows": len(rows),
                "expected_fields": len(spec.expected_fields),
                "missing_fields": missing,
                "ok": bool(rows) and not missing,
            }

        report["ok"] = all(t["ok"] for t in report["tables"].values())
        return report
