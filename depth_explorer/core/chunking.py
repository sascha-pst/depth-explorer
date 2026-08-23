"""Turn canonical records into retrievable chunks.

This module is deliberately source-agnostic: it consumes :class:`SpeciesRecord`
and knows nothing about FishBase. Every connector added later inherits this
chunking behaviour for free.

Three rules drive the design:

1. **Every chunk is self-contained.** A retrieved chunk reading "Depth range:
   0-985 m" is useless if the reader cannot tell which species it describes, so
   each chunk opens with a header naming the species and the section.
2. **Structured facts are rendered as prose, not dumped as JSON.** Embedding
   models index language; ``{"DepthRangeDeep": 985}`` retrieves poorly for
   "how deep do bluefin tuna dive". The same facts are also copied into
   ``facets`` for exact metadata filtering.
3. **Free text is split on sentence boundaries with overlap**, so a fact that
   straddles a window boundary survives in at least one chunk intact.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, replace
from typing import Any, Dict, List, Optional

from .ids import chunk_id, content_hash
from .models import Chunk, SpeciesRecord
from .vocab import THREATENED_CATEGORIES, strata_for_depth

# Periods that end an abbreviation rather than a sentence. FishBase prose is
# dense with literature markers like "(Ref. 12345)", which a naive splitter
# would tear in half.
_ABBREVIATIONS = [
    "Ref", "Refs", "spp", "ssp", "sp", "subsp", "var", "cf", "approx", "ca",
    "vs", "Fig", "Figs", "no", "St", "Dr", "Mr", "Ms", "Prof", "et al",
    "pers", "comm", "i.e", "e.g", "incl", "min", "max", "temp", "cm", "mm",
]
_ABBREV_RE = re.compile(
    r"\b(?:" + "|".join(re.escape(a) for a in _ABBREVIATIONS) + r")\.", re.IGNORECASE
)
_SENTENCE_BOUNDARY = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\"'(\[])")
_PARAGRAPH_BOUNDARY = re.compile(r"\n\s*\n")
_WHITESPACE = re.compile(r"\s+")
_PERIOD_PLACEHOLDER = "\x00"


@dataclass
class ChunkingConfig:
    """Tuning for text splitting.

    Budgets are in characters rather than tokens so the pipeline stays free of a
    tokenizer dependency. ``chars_per_token`` converts to an estimate for
    reporting; ~4 characters per token is the usual ratio for English prose.
    """

    max_chars: int = 1200
    overlap_chars: int = 150
    #: Trailing fragments shorter than this are folded into the previous chunk.
    min_chars: int = 100
    chars_per_token: float = 4.0

    def estimate_tokens(self, text: str) -> int:
        return int(math.ceil(len(text) / self.chars_per_token))


def split_sentences(text: str) -> List[str]:
    """Split prose into sentences, protecting abbreviation periods."""
    sentences: List[str] = []
    for paragraph in _PARAGRAPH_BOUNDARY.split(text or ""):
        normalized = _WHITESPACE.sub(" ", paragraph).strip()
        if not normalized:
            continue
        protected = _ABBREV_RE.sub(
            lambda m: m.group(0).replace(".", _PERIOD_PLACEHOLDER), normalized
        )
        for piece in _SENTENCE_BOUNDARY.split(protected):
            restored = piece.replace(_PERIOD_PLACEHOLDER, ".").strip()
            if restored:
                sentences.append(restored)
    return sentences


def _hard_split(sentence: str, max_chars: int) -> List[str]:
    """Break a single oversized sentence on word boundaries."""
    words = sentence.split(" ")
    pieces: List[str] = []
    current: List[str] = []
    length = 0
    for word in words:
        if len(word) > max_chars:
            # A single token wider than the budget -- a url, an identifier, or
            # malformed source data. Splitting mid-token is ugly but bounded;
            # emitting it whole would overflow the caller's embedding window.
            if current:
                pieces.append(" ".join(current))
                current, length = [], 0
            pieces.extend(word[i:i + max_chars] for i in range(0, len(word), max_chars))
            continue
        addition = len(word) + (1 if current else 0)
        if current and length + addition > max_chars:
            pieces.append(" ".join(current))
            current, length = [], 0
            addition = len(word)
        current.append(word)
        length += addition
    if current:
        pieces.append(" ".join(current))
    return pieces


def split_text(text: str, config: Optional[ChunkingConfig] = None) -> List[str]:
    """Split ``text`` into overlapping, sentence-aligned windows."""
    config = config or ChunkingConfig()
    sentences = split_sentences(text)
    if not sentences:
        return []

    # Oversized sentences are pre-split so packing never emits an over-budget window.
    units: List[str] = []
    for sentence in sentences:
        if len(sentence) > config.max_chars:
            units.extend(_hard_split(sentence, config.max_chars))
        else:
            units.append(sentence)

    chunks: List[str] = []
    current: List[str] = []
    length = 0

    for unit in units:
        addition = len(unit) + (1 if current else 0)
        if current and length + addition > config.max_chars:
            chunks.append(" ".join(current))
            current = _overlap_tail(current, config.overlap_chars)
            length = sum(len(part) + 1 for part in current) - 1 if current else 0
            # Repeated overlap is context, not content: shed the oldest of it
            # rather than let overlap plus the next sentence overrun the budget.
            while current and length + len(unit) + 1 > config.max_chars:
                current.pop(0)
                length = sum(len(part) + 1 for part in current) - 1 if current else 0
            addition = len(unit) + (1 if current else 0)
        current.append(unit)
        length += addition

    if current:
        chunks.append(" ".join(current))

    # Fold a runt tail back into its predecessor rather than embedding a stub,
    # but never past the budget -- an over-length chunk is worse than a short one.
    if len(chunks) > 1 and len(chunks[-1]) < config.min_chars:
        merged = f"{chunks[-2]} {chunks[-1]}"
        if len(merged) <= config.max_chars:
            chunks.pop()
            chunks[-1] = merged

    return chunks


def _overlap_tail(sentences: List[str], overlap_chars: int) -> List[str]:
    """Trailing sentences of the emitted window to repeat in the next one.

    Always a strict suffix: repeating the whole window would make packing loop
    forever on the next oversized unit.
    """
    if overlap_chars <= 0 or len(sentences) < 2:
        return []
    tail: List[str] = []
    length = 0
    for sentence in reversed(sentences[1:]):
        if tail and length + len(sentence) + 1 > overlap_chars:
            break
        tail.insert(0, sentence)
        length += len(sentence) + 1
        if length >= overlap_chars:
            break
    return tail


# --------------------------------------------------------------------------
# Section renderers: canonical record -> prose
# --------------------------------------------------------------------------


def _fmt_number(value: Optional[float], unit: str = "") -> Optional[str]:
    if value is None:
        return None
    text = f"{value:g}"
    return f"{text} {unit}".strip()


def _join_list(items: List[str]) -> str:
    """Join for prose: ``["a", "b", "c"] -> "a, b and c"``."""
    if len(items) <= 1:
        return items[0] if items else ""
    return f"{', '.join(items[:-1])} and {items[-1]}"


def _fmt_latitude(value: Optional[float]) -> Optional[str]:
    """Signed degrees as a compass bearing, e.g. ``-40 -> "40°S"``."""
    if value is None:
        return None
    return f"{abs(value):g}°{'N' if value >= 0 else 'S'}"


def _fmt_mass(grams: Optional[float]) -> Optional[str]:
    """Grams, expressed at a scale a reader would use."""
    if grams is None:
        return None
    return f"{grams / 1000:g} kg" if grams >= 1000 else f"{grams:g} g"


def _fmt_range(low: Optional[float], high: Optional[float], unit: str) -> Optional[str]:
    if low is None and high is None:
        return None
    if low is not None and high is not None:
        return f"{low:g}-{high:g} {unit}"
    if high is not None:
        return f"to {high:g} {unit}"
    return f"from {low:g} {unit}"


def render_taxonomy(record: SpeciesRecord) -> Optional[str]:
    taxon = record.taxon
    if not taxon.scientific_name:
        return None
    authority = f" {taxon.author}" if taxon.author else ""
    lines = [f"Scientific name: {taxon.scientific_name}{authority}."]
    labels = [
        ("Superclass", taxon.superclass),
        ("Class", taxon.taxon_class),
        ("Order", taxon.order),
        ("Family", taxon.family),
        ("Subfamily", taxon.subfamily),
        ("Genus", taxon.genus),
    ]
    classification = [f"{label}: {value}" for label, value in labels if value]
    if classification:
        lines.append("Classification — " + "; ".join(classification) + ".")
    preferred = record.preferred_common_name()
    if preferred:
        lines.append(f"Preferred common name: {preferred}.")
    others = [n.name for n in record.common_names if not n.is_preferred][:12]
    if others:
        lines.append("Other common names: " + ", ".join(others) + ".")
    if record.identifiers:
        ids = "; ".join(f"{k}={v}" for k, v in sorted(record.identifiers.items()))
        lines.append(f"Source identifiers — {ids}.")
    return "\n".join(lines)


def render_habitat_depth(record: SpeciesRecord) -> Optional[str]:
    habitat, depth = record.habitat, record.depth
    lines: List[str] = []
    if habitat.zone:
        lines.append(f"Habitat zone: {habitat.zone}.")
    if habitat.environments:
        lines.append("Water bodies: " + ", ".join(habitat.environments) + ".")
    if habitat.climate_zone:
        lines.append(f"Climate zone: {habitat.climate_zone}.")

    depth_range = _fmt_range(depth.shallow_m, depth.deep_m, "m")
    if depth_range:
        sentence = f"Depth range: {depth_range}"
        common = _fmt_range(depth.common_shallow_m, depth.common_deep_m, "m")
        if common:
            sentence += f", usually {common}"
        lines.append(sentence + ".")
    elif depth.has_common_range():
        lines.append(f"Usual depth range: {_fmt_range(depth.common_shallow_m, depth.common_deep_m, 'm')}.")

    strata = strata_for_depth(depth.shallow_m, depth.deep_m)
    if strata:
        noun = "zone" if len(strata) == 1 else "zones"
        lines.append(f"Spans the {_join_list(strata)} {noun} by depth.")
    if habitat.pelagic_zones:
        lines.append("Recorded from the water column: " + _join_list(habitat.pelagic_zones) + ".")
    if habitat.associations:
        lines.append("Habitat associations: " + _join_list(habitat.associations) + ".")

    temperature = _fmt_range(habitat.temp_min_c, habitat.temp_max_c, "°C")
    if temperature:
        lines.append(f"Recorded temperature range: {temperature}.")
    north = _fmt_latitude(habitat.northernmost_deg)
    south = _fmt_latitude(habitat.southernmost_deg)
    if north or south:
        bounds = f"{north} to {south}" if north and south else (north or south)
        lines.append(f"Latitudinal extent: {bounds}.")
    if habitat.range_description:
        lines.append(f"Range: {habitat.range_description}")
    return "\n".join(lines) if lines else None


def render_conservation(record: SpeciesRecord) -> Optional[str]:
    conservation = record.conservation
    lines: List[str] = []
    if conservation.iucn_code or conservation.iucn_category:
        label = conservation.iucn_category or conservation.iucn_code
        code = f" ({conservation.iucn_code})" if conservation.iucn_code else ""
        sentence = f"IUCN Red List status: {label}{code}"
        if conservation.iucn_date_assessed:
            sentence += f", assessed {conservation.iucn_date_assessed}"
        lines.append(sentence + ".")
    assessment = conservation.iucn_assessment
    if assessment:
        # FishBase often stores the assessment as "Least Concern (LC)", which
        # only restates the status line above. Keep the line when something
        # survives removing the category and code -- criteria, a trend, a note.
        residue = assessment.replace(conservation.iucn_category or "\x00", "")
        residue = residue.replace(f"({conservation.iucn_code})", "")
        if residue.strip(" ()[]-–—.,;:"):
            lines.append(f"Assessment detail: {assessment}.")
    if conservation.vulnerability_index is not None:
        band = f" ({conservation.vulnerability_band})" if conservation.vulnerability_band else ""
        lines.append(f"Intrinsic vulnerability index: {conservation.vulnerability_index:g} of 100{band}.")
    if conservation.resilience:
        lines.append(f"Population resilience: {conservation.resilience}.")
    if conservation.resilience_remark:
        lines.append(conservation.resilience_remark)
    if conservation.protected:
        lines.append("Listed as protected in the source database.")
    if conservation.threat_to_humans:
        lines.append(f"Threat to humans: {conservation.threat_to_humans}.")
    return "\n".join(lines) if lines else None


def render_biology(record: SpeciesRecord) -> Optional[str]:
    bio, trophic = record.biometrics, record.trophic
    lines: List[str] = []
    if bio.max_length_cm is not None:
        length_type = f" ({bio.length_type})" if bio.length_type else ""
        lines.append(f"Maximum recorded length: {bio.max_length_cm:g} cm{length_type}.")
    if bio.common_length_cm is not None:
        lines.append(f"Common length: {bio.common_length_cm:g} cm.")
    mass = _fmt_mass(bio.max_weight_g)
    if mass:
        lines.append(f"Maximum recorded weight: {mass}.")
    if bio.max_age_years is not None:
        lines.append(f"Maximum reported age: {bio.max_age_years:g} years.")
    if trophic.feeding_type:
        lines.append(f"Feeding type: {trophic.feeding_type}.")
    for label, level, error in (
        ("diet composition", trophic.diet_trophic_level, trophic.diet_trophic_se),
        ("food items", trophic.food_trophic_level, trophic.food_trophic_se),
    ):
        if level is not None:
            se = f" (s.e. {error:g})" if error is not None else ""
            lines.append(f"Trophic level from {label}: {level:g}{se}.")
    if trophic.food_items:
        lines.append("Recorded prey groups: " + _join_list(trophic.food_items[:15]) + ".")
    return "\n".join(lines) if lines else None


#: Structured sections in chunk order. Each renderer returns prose or ``None``.
SECTION_RENDERERS: List[tuple] = [
    ("taxonomy", "Taxonomy", render_taxonomy),
    ("habitat_depth", "Habitat and depth", render_habitat_depth),
    ("conservation", "Conservation status", render_conservation),
    ("biology", "Biology and trophic ecology", render_biology),
]


def build_facets(record: SpeciesRecord) -> Dict[str, Any]:
    """Filterable fields copied onto every chunk of a record.

    Retrieval over species data is often a hybrid query — "mesopelagic species
    listed as Endangered" is a metadata filter, not a semantic one.
    """
    depth, habitat, conservation = record.depth, record.habitat, record.conservation
    facets: Dict[str, Any] = {}
    if depth.shallow_m is not None:
        facets["depth_shallow_m"] = depth.shallow_m
    if depth.deep_m is not None:
        facets["depth_deep_m"] = depth.deep_m
    strata = strata_for_depth(depth.shallow_m, depth.deep_m)
    if strata:
        facets["depth_strata"] = strata
    if habitat.zone:
        facets["habitat_zone"] = habitat.zone
    if habitat.environments:
        facets["environments"] = habitat.environments
    if habitat.pelagic_zones:
        facets["pelagic_zones"] = habitat.pelagic_zones
    if habitat.climate_zone:
        facets["climate_zone"] = habitat.climate_zone
    if conservation.iucn_code:
        facets["iucn_code"] = conservation.iucn_code
        facets["is_threatened"] = conservation.iucn_code in THREATENED_CATEGORIES
    if conservation.vulnerability_index is not None:
        facets["vulnerability_index"] = conservation.vulnerability_index
    level = record.trophic.diet_trophic_level or record.trophic.food_trophic_level
    if level is not None:
        facets["trophic_level"] = level
    if record.biometrics.max_length_cm is not None:
        facets["max_length_cm"] = record.biometrics.max_length_cm
    return facets


def _budget_for(config: ChunkingConfig, prefix: str) -> ChunkingConfig:
    """Shrink the text budget by the length of the mandatory chunk header.

    Without this, every chunk overshoots the configured budget by the width of
    its "Species (Common name) — Section" header, which matters when the budget
    was chosen to fit an embedding model's input window. A header longer than
    the whole budget cannot be compensated for; the floor keeps the split
    sensible rather than degenerate.
    """
    effective = max(config.max_chars - len(prefix), max(80, config.max_chars // 4))
    return replace(
        config,
        max_chars=effective,
        overlap_chars=min(config.overlap_chars, effective // 2),
        min_chars=min(config.min_chars, effective // 2),
    )


def record_to_chunks(
    record: SpeciesRecord, config: Optional[ChunkingConfig] = None
) -> List[Chunk]:
    """Render one canonical record into its retrievable chunks."""
    config = config or ChunkingConfig()
    header = record.display_name
    facets = build_facets(record)
    taxonomy = {
        key: value
        for key, value in (
            ("class", record.taxon.taxon_class),
            ("order", record.taxon.order),
            ("family", record.taxon.family),
            ("genus", record.taxon.genus),
        )
        if value
    }
    chunks: List[Chunk] = []
    # Collected first, emitted second: ordinals are assigned per section across
    # both structured and free-text content, so a section fed by a renderer *and*
    # by source prose (biology, for one) cannot mint duplicate chunk ids.
    pending: List[Dict[str, Any]] = []

    for section, title, renderer in SECTION_RENDERERS:
        body = renderer(record)
        if not body:
            continue
        budget = _budget_for(config, f"{header} — {title}\n")
        for window in split_text(body, budget) or [body]:
            pending.append({
                "section": section,
                "title": title,
                "body": window,
                "content_type": "structured_summary",
                "provenance": record.provenance,
            })

    for block in record.text_blocks:
        title = block.title or block.section.replace("_", " ").capitalize()
        budget = _budget_for(config, f"{header} — {title}\n")
        for window in split_text(block.text, budget):
            pending.append({
                "section": block.section,
                "title": title,
                "body": window,
                "content_type": "free_text",
                "provenance": block.provenance,
            })

    totals: Dict[str, int] = {}
    for item in pending:
        totals[item["section"]] = totals.get(item["section"], 0) + 1

    ordinals: Dict[str, int] = {}
    for item in pending:
        section = item["section"]
        ordinal = ordinals.get(section, 0)
        ordinals[section] = ordinal + 1
        text = f"{header} — {item['title']}\n{item['body']}".strip()
        chunks.append(
            Chunk(
                chunk_id=chunk_id(record.record_id, section, ordinal),
                record_id=record.record_id,
                source=record.source,
                section=section,
                content_type=item["content_type"],
                text=text,
                ordinal=ordinal,
                n_chunks_in_section=totals[section],
                content_hash=content_hash(text),
                char_len=len(text),
                est_tokens=config.estimate_tokens(text),
                scientific_name=record.taxon.scientific_name,
                common_name=record.preferred_common_name(),
                taxonomy=dict(taxonomy),
                identifiers=dict(record.identifiers),
                facets=dict(facets),
                provenance=item["provenance"],
            )
        )

    return chunks
