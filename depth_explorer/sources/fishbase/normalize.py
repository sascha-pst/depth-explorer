"""Map raw FishBase tables onto the canonical :class:`SpeciesRecord`.

Everything FishBase-specific ends here. Downstream code sees only the canonical
schema, which is what lets WoRMS, OBIS, GBIF and IUCN slot in later without
touching the chunker or the store.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

from ...core.ids import record_id as make_record_id
from ...core.models import (
    Biometrics,
    CommonName,
    Conservation,
    DepthRange,
    Habitat,
    Provenance,
    SpeciesRecord,
    Taxon,
    TextBlock,
    Trophic,
)
from ...core.vocab import (
    iucn_category_label,
    normalize_habitat_zone,
    normalize_iucn_code,
    vulnerability_band,
)
from .fields import Row, extract_reference_ids, rows_from
from .tables import (
    HABITAT_ASSOCIATION_FIELDS,
    PELAGIC_ZONE_FIELDS,
    TEXT_FIELDS,
)

#: FishBase environment flags mapped to canonical water bodies.
_ENVIRONMENT_FIELDS = [
    ("Saltwater", "marine"),
    ("Brack", "brackish"),
    ("Fresh", "freshwater"),
]

#: Stock rows describing the species as a whole, preferred over regional stocks.
_SPECIES_LEVEL = "species level"


def _table_rows(payload: Dict[str, Any], table: str) -> List[Row]:
    entry = (payload.get("tables") or {}).get(table) or {}
    return rows_from(entry.get("rows"))


def _table_meta(payload: Dict[str, Any], table: str) -> Dict[str, Any]:
    return (payload.get("tables") or {}).get(table) or {}


def _first(rows: Sequence[Row]) -> Row:
    return rows[0] if rows else Row()


def _pick_stock_row(rows: Sequence[Row]) -> Row:
    """Prefer the species-level stock; fall back to the first regional one."""
    for row in rows:
        level = (row.text("Level") or "").lower()
        if level == _SPECIES_LEVEL:
            return row
    return _first(rows)


def _signed_latitude(row: Row, value_aliases: Sequence[str], hemisphere_aliases: Sequence[str]) -> Optional[float]:
    """Apply FishBase's separate hemisphere column to a latitude magnitude."""
    value = row.number(*value_aliases)
    if value is None:
        return None
    hemisphere = (row.text(*hemisphere_aliases) or "").strip().upper()
    if hemisphere.startswith("S") and value > 0:
        return -value
    return value


def _flagged(rows: Sequence[Row], mapping: Dict[str, str]) -> List[str]:
    """Canonical labels for every flag column set true on any row."""
    found: List[str] = []
    for column, label in mapping.items():
        if label in found:
            continue
        if any(row.flag(column) for row in rows):
            found.append(label)
    return found


def _build_provenance(
    payload: Dict[str, Any],
    tables: Sequence[str],
    source_fields: Sequence[str] = (),
    text: Optional[str] = None,
) -> Provenance:
    meta = _table_meta(payload, tables[0]) if tables else {}
    return Provenance(
        source=payload.get("source", "fishbase"),
        retrieved_at=meta.get("retrieved_at") or payload.get("retrieved_at"),
        source_url=payload.get("source_url"),
        api_url=meta.get("url"),
        tables=list(tables),
        source_fields=list(source_fields),
        citation=payload.get("citation"),
        reference_ids=extract_reference_ids(text),
    )


def _taxonomy(species: Row, taxa: Row) -> Taxon:
    genus = species.text("Genus") or taxa.text("Genus")
    epithet = species.text("Species") or taxa.text("Species")
    scientific_name = " ".join(part for part in (genus, epithet) if part) or None
    return Taxon(
        scientific_name=scientific_name,
        genus=genus,
        species=epithet,
        author=species.text("Author"),
        subfamily=taxa.text("Subfamily") or species.text("Subfamily"),
        family=taxa.text("Family"),
        order=taxa.text("Order"),
        taxon_class=taxa.text("Class"),
        superclass=taxa.text("SuperClass", "Superclass"),
    )


def _common_names(species: Row, comnames: Sequence[Row]) -> List[CommonName]:
    """English common names, FishBase's preferred name first.

    Non-English names are dropped: they inflate the taxonomy chunk without
    helping retrieval over an English corpus.
    """
    names: List[CommonName] = []
    seen = set()

    fbname = species.text("FBname")
    if fbname:
        names.append(CommonName(name=fbname, language="English", is_preferred=True))
        seen.add(fbname.lower())

    for row in comnames:
        name = row.text("ComName", "Comname")
        if not name or name.lower() in seen:
            continue
        language = row.text("Language")
        if language and language.lower() != "english":
            continue
        preferred = bool(row.flag("PreferredName")) and not names
        names.append(CommonName(name=name, language=language or "English", is_preferred=preferred))
        seen.add(name.lower())

    return names


def _depth(species: Row) -> DepthRange:
    return DepthRange(
        shallow_m=species.number("DepthRangeShallow"),
        deep_m=species.number("DepthRangeDeep"),
        common_shallow_m=species.number("DepthRangeComShallow"),
        common_deep_m=species.number("DepthRangeComDeep"),
    )


def _habitat(species: Row, ecology: Sequence[Row], stock: Row) -> Habitat:
    zone_raw = species.text("DemersPelag")
    environments = [
        label for column, label in _ENVIRONMENT_FIELDS if species.flag(column)
    ]
    return Habitat(
        zone=normalize_habitat_zone(zone_raw),
        zone_raw=zone_raw,
        environments=environments,
        pelagic_zones=_flagged(ecology, PELAGIC_ZONE_FIELDS),
        associations=_flagged(ecology, HABITAT_ASSOCIATION_FIELDS),
        climate_zone=stock.text("EnvTemp"),
        temp_min_c=stock.number("TempMin"),
        temp_max_c=stock.number("TempMax"),
        northernmost_deg=_signed_latitude(stock, ("Northernmost",), ("NorthSouthN",)),
        # "Southermost" is FishBase's own spelling; accept both.
        southernmost_deg=_signed_latitude(
            stock, ("Southernmost", "Southermost"), ("SouthNorthS",)
        ),
        range_description=stock.text("StockDefs"),
    )


def _conservation(species: Row, stocks: Sequence[Row], stock: Row) -> Conservation:
    # An IUCN code can sit on a regional stock row rather than the species-level
    # one, so fall back across rows before giving up.
    code = stock.text("IUCN_Code", "IUCNCode")
    assessment = stock.text("IUCN_Assessment", "IUCNAssessment")
    assessed_on = stock.text("IUCN_DateAssessed", "IUCNDateAssessed")
    for row in stocks:
        if not code:
            code = row.text("IUCN_Code", "IUCNCode")
            assessment = assessment or row.text("IUCN_Assessment", "IUCNAssessment")
            assessed_on = assessed_on or row.text("IUCN_DateAssessed", "IUCNDateAssessed")

    normalized = normalize_iucn_code(code)
    vulnerability = species.number("Vulnerability")
    protected = stock.flag("Protected")
    if protected is None:
        protected = next((row.flag("Protected") for row in stocks if row.flag("Protected")), None)

    return Conservation(
        iucn_code=normalized,
        iucn_category=iucn_category_label(normalized),
        iucn_assessment=assessment,
        iucn_date_assessed=assessed_on,
        vulnerability_index=vulnerability,
        vulnerability_band=vulnerability_band(vulnerability),
        resilience=stock.text("Resilience"),
        resilience_remark=None,  # emitted as a text block instead
        protected=protected,
        threat_to_humans=species.text("Dangerous"),
    )


def _biometrics(species: Row) -> Biometrics:
    return Biometrics(
        max_length_cm=species.number("Length"),
        length_type=species.text("LTypeMaxM", "LTypeMax"),
        common_length_cm=species.number("CommonLength"),
        max_weight_g=species.number("Weight"),
        max_age_years=species.number("LongevityWild"),
    )


def _trophic(ecology: Sequence[Row], fooditems: Sequence[Row]) -> Trophic:
    def first_number(*aliases: str) -> Optional[float]:
        for row in ecology:
            value = row.number(*aliases)
            if value is not None:
                return value
        return None

    feeding_type = next((row.text("FeedingType") for row in ecology if row.text("FeedingType")), None)

    prey: List[str] = []
    for row in fooditems:
        name = row.text("Foodname", "FoodName") or row.text("Foodgroup", "FoodGroup")
        if name and name not in prey:
            prey.append(name)

    return Trophic(
        diet_trophic_level=first_number("DietTroph"),
        diet_trophic_se=first_number("DietSeTroph"),
        food_trophic_level=first_number("FoodTroph"),
        food_trophic_se=first_number("FoodSeTroph"),
        feeding_type=feeding_type,
        food_items=prey,
    )


def _text_blocks(payload: Dict[str, Any]) -> List[TextBlock]:
    """Harvest the unstructured half of the corpus.

    Duplicate text is dropped: FishBase repeats the same stock description
    across regional stock rows, and near-duplicate chunks poison retrieval by
    crowding out other results.
    """
    blocks: List[TextBlock] = []
    seen: set = set()

    for table, aliases, section, title in TEXT_FIELDS:
        for row in _table_rows(payload, table):
            text = row.text(*aliases)
            if not text:
                continue
            fingerprint = (section, text)
            if fingerprint in seen:
                continue
            seen.add(fingerprint)
            blocks.append(
                TextBlock(
                    section=section,
                    title=title,
                    text=text,
                    provenance=_build_provenance(payload, [table], [aliases[0]], text),
                )
            )
    return blocks


def normalize_species(payload: Dict[str, Any]) -> SpeciesRecord:
    """Build a canonical record from a fetched FishBase payload."""
    source = payload.get("source", "fishbase")
    species = _first(_table_rows(payload, "species"))
    taxa = _first(_table_rows(payload, "taxa"))
    ecology = _table_rows(payload, "ecology")
    stocks = _table_rows(payload, "stocks")
    comnames = _table_rows(payload, "comnames")
    fooditems = _table_rows(payload, "fooditems")
    stock = _pick_stock_row(stocks)

    speccode = payload.get("speccode") or species.integer("SpecCode")
    identifiers: Dict[str, str] = {}
    if speccode is not None:
        identifiers[f"{source}_speccode"] = str(speccode)
    stock_code = stock.integer("StockCode")
    if stock_code is not None:
        identifiers[f"{source}_stockcode"] = str(stock_code)

    tables_used = [name for name in (payload.get("tables") or {}) if _table_rows(payload, name)]

    return SpeciesRecord(
        record_id=make_record_id(source, str(speccode if speccode is not None else payload.get("query_name", "unknown"))),
        source=source,
        identifiers=identifiers,
        taxon=_taxonomy(species, taxa),
        common_names=_common_names(species, comnames),
        depth=_depth(species),
        habitat=_habitat(species, ecology, stock),
        conservation=_conservation(species, stocks, stock),
        biometrics=_biometrics(species),
        trophic=_trophic(ecology, fooditems),
        text_blocks=_text_blocks(payload),
        provenance=_build_provenance(payload, tables_used or ["species"]),
    )
