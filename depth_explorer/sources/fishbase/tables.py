"""FishBase tables this connector reads, and what it expects from each.

``expected_fields`` is not a schema contract — it is the list the live
verifier (``depth-explorer verify-schema``) checks a real response against, so
an upstream rename shows up as a report rather than as silently empty output.
The connector itself never requires a field to be present.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List


@dataclass(frozen=True)
class TableSpec:
    """One FishBase table and how to query it."""

    name: str
    #: What this table contributes to the canonical record.
    contributes: str
    #: Column the table is keyed on for per-species lookups.
    key_field: str = "SpecCode"
    #: Columns the normalizer reads, checked by the live schema verifier.
    expected_fields: List[str] = field(default_factory=list)
    #: True when a species may have many rows here (stocks, common names, prey).
    multi_row: bool = False


SPECIES = TableSpec(
    name="species",
    contributes="taxonomy, habitat zone, depth range, size, vulnerability, biology text",
    expected_fields=[
        "SpecCode", "Genus", "Species", "Author", "FBname", "Subfamily",
        "Fresh", "Brack", "Saltwater", "DemersPelag", "AnaCat",
        "DepthRangeShallow", "DepthRangeDeep",
        "DepthRangeComShallow", "DepthRangeComDeep",
        "Length", "LTypeMaxM", "CommonLength", "Weight", "LongevityWild",
        "Vulnerability", "Dangerous", "Importance", "Comments", "Remark",
    ],
)

TAXA = TableSpec(
    name="taxa",
    contributes="higher taxonomy (class, order, family)",
    expected_fields=["SpecCode", "Genus", "Species", "Subfamily", "Family", "Order", "Class"],
)

ECOLOGY = TableSpec(
    name="ecology",
    contributes="water-column strata, habitat associations, trophic level, feeding type",
    expected_fields=[
        "SpecCode", "StockCode", "Neritic", "Oceanic",
        "Epipelagic", "Mesopelagic", "Bathypelagic", "Abyssopelagic", "Hadopelagic",
        "Estuaries", "Mangroves", "Intertidal", "SubLittoral", "Caves", "Coralreefs",
        "FeedingType", "DietTroph", "DietSeTroph", "FoodTroph", "FoodSeTroph",
        "DietRemark", "FoodRemark", "AddRems",
    ],
    multi_row=True,
)

STOCKS = TableSpec(
    name="stocks",
    contributes="IUCN status, resilience, temperature and latitude limits, distribution text",
    expected_fields=[
        "SpecCode", "StockCode", "StockDefs", "StockDefsGeneral", "Level",
        "IUCN_Code", "IUCN_Assessment", "IUCN_DateAssessed", "Protected",
        "Northernmost", "Southermost", "TempMin", "TempMax", "EnvTemp",
        "Resilience", "ResilienceRemark", "Comments",
    ],
    multi_row=True,
)

COMNAMES = TableSpec(
    name="comnames",
    contributes="common names by language",
    expected_fields=["SpecCode", "ComName", "Language", "PreferredName", "NameType"],
    multi_row=True,
)

FOODITEMS = TableSpec(
    name="fooditems",
    contributes="recorded prey groups",
    expected_fields=["SpecCode", "StockCode", "Foodgroup", "Foodname", "PreyStage", "FoodI", "FoodII"],
    multi_row=True,
)

#: Tables fetched for every species, in dependency order.
ALL_TABLES: Dict[str, TableSpec] = {
    spec.name: spec for spec in (SPECIES, TAXA, ECOLOGY, STOCKS, COMNAMES, FOODITEMS)
}

#: Fetched by default. ``fooditems`` is opt-in: it is the largest table and only
#: contributes a prey list, so it is not worth the extra request per species
#: unless diet is being ingested.
DEFAULT_TABLES: List[str] = ["species", "taxa", "ecology", "stocks", "comnames"]

#: Ecology columns that are water-column strata flags, mapped to canonical names.
PELAGIC_ZONE_FIELDS: Dict[str, str] = {
    "Epipelagic": "epipelagic",
    "Mesopelagic": "mesopelagic",
    "Bathypelagic": "bathypelagic",
    "Abyssopelagic": "abyssopelagic",
    "Hadopelagic": "hadopelagic",
}

#: Ecology columns that are habitat-association flags.
HABITAT_ASSOCIATION_FIELDS: Dict[str, str] = {
    "Neritic": "neritic",
    "Oceanic": "oceanic",
    "Estuaries": "estuaries",
    "Mangroves": "mangroves",
    "Coralreefs": "coral reefs",
    "Intertidal": "intertidal",
    "SubLittoral": "sublittoral",
    "SoftBottom": "soft bottom",
    "HardBottom": "hard bottom",
    "SeaGrassBeds": "seagrass beds",
    "Caves": "caves",
}

#: Free-text columns harvested as unstructured blocks, with their target section.
TEXT_FIELDS = [
    # (table, column aliases, canonical section, human title)
    ("species", ("Comments",), "biology", "Biology and life history"),
    ("species", ("Remark", "Remarks"), "biology", "Taxonomic remark"),
    ("stocks", ("StockDefs",), "distribution", "Stock distribution"),
    ("stocks", ("StockDefsGeneral",), "distribution", "General distribution"),
    ("stocks", ("Comments",), "distribution", "Stock notes"),
    ("stocks", ("ResilienceRemark",), "conservation_notes", "Resilience remark"),
    ("ecology", ("DietRemark",), "diet", "Diet remark"),
    ("ecology", ("FoodRemark",), "diet", "Food remark"),
    ("ecology", ("AddRems",), "ecology_notes", "Ecology remark"),
]
