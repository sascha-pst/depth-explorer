"""Canonical, source-agnostic schema for the species corpus.

Every connector (FishBase, WoRMS, OBIS, GBIF, SeaLifeBase, IUCN) normalizes its
native payload into :class:`SpeciesRecord`. Downstream chunking, embedding and
retrieval only ever see this schema, so adding a source never changes anything
below the connector layer.

Design notes
------------
* Every field is optional. Sources are patchy; a missing value is ``None`` and
  never a sentinel, so "absent" and "zero" stay distinguishable.
* ``identifiers`` is the cross-source join surface. FishBase contributes
  ``fishbase_speccode``; WoRMS will contribute ``worms_aphia_id``, which the
  project treats as the eventual primary key (see docs/fishbase.md).
* ``TextBlock`` carries the unstructured half of the corpus with enough
  provenance to cite it after retrieval.
"""

from __future__ import annotations

from dataclasses import dataclass, field, fields, is_dataclass
from typing import Any, Dict, List, Optional


def _prune(value: Any) -> Any:
    """Recursively convert dataclasses to dicts, dropping empty values.

    ``None``, ``""``, ``[]`` and ``{}`` are dropped so serialized records stay
    readable and small. ``0`` and ``False`` are meaningful and are kept.
    """
    if is_dataclass(value) and not isinstance(value, type):
        out: Dict[str, Any] = {}
        for f in fields(value):
            if not f.metadata.get("serialize", True):
                continue
            key = f.metadata.get("name", f.name)
            pruned = _prune(getattr(value, f.name))
            if pruned is None or pruned == "" or pruned == [] or pruned == {}:
                continue
            out[key] = pruned
        return out
    if isinstance(value, dict):
        return {k: _prune(v) for k, v in value.items() if _prune(v) not in (None, "", [], {})}
    if isinstance(value, (list, tuple)):
        return [_prune(v) for v in value if _prune(v) not in (None, "", [], {})]
    return value


class _Serializable:
    def to_dict(self) -> Dict[str, Any]:
        return _prune(self)


@dataclass
class Provenance(_Serializable):
    """Where a record or text block came from, and how to cite it."""

    source: str
    retrieved_at: Optional[str] = None
    source_url: Optional[str] = None
    api_url: Optional[str] = None
    tables: List[str] = field(default_factory=list)
    source_fields: List[str] = field(default_factory=list, metadata={"name": "fields"})
    citation: Optional[str] = None
    #: FishBase-style literature reference ids mentioned in the source text.
    reference_ids: List[str] = field(default_factory=list)


@dataclass
class Taxon(_Serializable):
    scientific_name: Optional[str] = None
    genus: Optional[str] = None
    species: Optional[str] = None
    author: Optional[str] = None
    subfamily: Optional[str] = None
    family: Optional[str] = None
    order: Optional[str] = None
    taxon_class: Optional[str] = field(default=None, metadata={"name": "class"})
    superclass: Optional[str] = None

    def rank_path(self) -> List[str]:
        """Higher taxonomy from coarsest to finest, skipping gaps."""
        ordered = [self.superclass, self.taxon_class, self.order, self.family, self.subfamily]
        return [r for r in ordered if r]


@dataclass
class CommonName(_Serializable):
    name: str
    language: Optional[str] = None
    is_preferred: bool = False


@dataclass
class DepthRange(_Serializable):
    """Vertical distribution in metres below the surface."""

    shallow_m: Optional[float] = None
    deep_m: Optional[float] = None
    common_shallow_m: Optional[float] = None
    common_deep_m: Optional[float] = None

    def has_range(self) -> bool:
        return self.shallow_m is not None or self.deep_m is not None

    def has_common_range(self) -> bool:
        return self.common_shallow_m is not None or self.common_deep_m is not None


@dataclass
class Habitat(_Serializable):
    #: Canonical habitat zone, e.g. "pelagic-oceanic", "demersal" (see vocab.py).
    zone: Optional[str] = None
    #: Raw source term, kept so normalization is auditable.
    zone_raw: Optional[str] = None
    #: Water bodies occupied: marine / brackish / freshwater.
    environments: List[str] = field(default_factory=list)
    #: Vertical pelagic strata: epipelagic / mesopelagic / bathypelagic / ...
    pelagic_zones: List[str] = field(default_factory=list)
    #: Other habitat associations: reef, estuaries, mangroves, ...
    associations: List[str] = field(default_factory=list)
    climate_zone: Optional[str] = None
    temp_min_c: Optional[float] = None
    temp_max_c: Optional[float] = None
    #: Latitudinal extent, degrees; positive north, negative south.
    northernmost_deg: Optional[float] = None
    southernmost_deg: Optional[float] = None
    #: Free-text stock/range definition from the source.
    range_description: Optional[str] = None


@dataclass
class Conservation(_Serializable):
    iucn_code: Optional[str] = None
    iucn_category: Optional[str] = None
    iucn_assessment: Optional[str] = None
    iucn_date_assessed: Optional[str] = None
    #: FishBase intrinsic vulnerability index, 0-100.
    vulnerability_index: Optional[float] = None
    vulnerability_band: Optional[str] = None
    resilience: Optional[str] = None
    resilience_remark: Optional[str] = None
    protected: Optional[bool] = None
    threat_to_humans: Optional[str] = None


@dataclass
class Biometrics(_Serializable):
    max_length_cm: Optional[float] = None
    length_type: Optional[str] = None
    common_length_cm: Optional[float] = None
    max_weight_g: Optional[float] = None
    max_age_years: Optional[float] = None


@dataclass
class Trophic(_Serializable):
    #: Trophic level estimated from diet composition.
    diet_trophic_level: Optional[float] = None
    diet_trophic_se: Optional[float] = None
    #: Trophic level estimated from food items.
    food_trophic_level: Optional[float] = None
    food_trophic_se: Optional[float] = None
    feeding_type: Optional[str] = None
    #: Distinct prey groups recorded for the species.
    food_items: List[str] = field(default_factory=list)


@dataclass
class TextBlock(_Serializable):
    """One span of unstructured prose from a source.

    ``section`` drives chunk grouping and metadata filtering at retrieval time
    (e.g. restrict a query to ``distribution`` blocks).
    """

    section: str
    text: str
    title: Optional[str] = None
    provenance: Optional[Provenance] = None


@dataclass
class SpeciesRecord(_Serializable):
    """One species as seen by one source."""

    record_id: str
    source: str
    identifiers: Dict[str, str] = field(default_factory=dict)
    taxon: Taxon = field(default_factory=Taxon)
    common_names: List[CommonName] = field(default_factory=list)
    depth: DepthRange = field(default_factory=DepthRange)
    habitat: Habitat = field(default_factory=Habitat)
    conservation: Conservation = field(default_factory=Conservation)
    biometrics: Biometrics = field(default_factory=Biometrics)
    trophic: Trophic = field(default_factory=Trophic)
    text_blocks: List[TextBlock] = field(default_factory=list)
    provenance: Optional[Provenance] = None

    @property
    def display_name(self) -> str:
        """Human label used as the self-contained header on every chunk."""
        sci = self.taxon.scientific_name or self.record_id
        preferred = self.preferred_common_name()
        return f"{sci} ({preferred})" if preferred else sci

    def preferred_common_name(self) -> Optional[str]:
        for name in self.common_names:
            if name.is_preferred:
                return name.name
        return self.common_names[0].name if self.common_names else None


@dataclass
class Chunk(_Serializable):
    """A retrievable unit: self-contained text plus filterable metadata."""

    chunk_id: str
    record_id: str
    source: str
    section: str
    #: "structured_summary" for rendered facts, "free_text" for source prose.
    content_type: str
    text: str
    ordinal: int = 0
    n_chunks_in_section: int = 1
    content_hash: Optional[str] = None
    char_len: int = 0
    est_tokens: int = 0
    scientific_name: Optional[str] = None
    common_name: Optional[str] = None
    taxonomy: Dict[str, str] = field(default_factory=dict)
    identifiers: Dict[str, str] = field(default_factory=dict)
    #: Filterable numeric facets copied down for hybrid retrieval.
    facets: Dict[str, Any] = field(default_factory=dict)
    provenance: Optional[Provenance] = None
