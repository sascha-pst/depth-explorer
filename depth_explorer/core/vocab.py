"""Controlled vocabularies shared by every source.

Sources spell the same concept differently — FishBase says ``pelagic-oceanic``,
OBIS says ``pelagic``, IUCN says ``Least Concern`` where FishBase says ``LC``.
Normalizing here (rather than in each connector) keeps metadata filters
meaningful across a mixed corpus.
"""

from __future__ import annotations

from typing import Dict, List, Optional

#: Canonical habitat zones. Keys are lowercased source terms.
HABITAT_ZONES: Dict[str, str] = {
    "pelagic": "pelagic",
    "pelagic-neritic": "pelagic-neritic",
    "pelagic-oceanic": "pelagic-oceanic",
    "benthopelagic": "benthopelagic",
    "demersal": "demersal",
    "bathydemersal": "bathydemersal",
    "bathypelagic": "bathypelagic",
    "reef-associated": "reef-associated",
    "host": "host-associated",
    "sessile": "sessile",
}

#: Zones whose occupants spend their lives in the water column.
PELAGIC_ZONES = {"pelagic", "pelagic-neritic", "pelagic-oceanic", "bathypelagic", "benthopelagic"}

#: Vertical strata of the water column, shallowest first.
PELAGIC_STRATA: List[str] = [
    "epipelagic",
    "mesopelagic",
    "bathypelagic",
    "abyssopelagic",
    "hadopelagic",
]

#: Approximate depth bounds (metres) for each stratum, for facet derivation.
STRATUM_BOUNDS = {
    "epipelagic": (0.0, 200.0),
    "mesopelagic": (200.0, 1000.0),
    "bathypelagic": (1000.0, 4000.0),
    "abyssopelagic": (4000.0, 6000.0),
    "hadopelagic": (6000.0, 11000.0),
}

#: IUCN Red List category codes to full labels.
IUCN_CATEGORIES: Dict[str, str] = {
    "EX": "Extinct",
    "EW": "Extinct in the Wild",
    "CR": "Critically Endangered",
    "EN": "Endangered",
    "VU": "Vulnerable",
    "NT": "Near Threatened",
    "LC": "Least Concern",
    "DD": "Data Deficient",
    "NE": "Not Evaluated",
    "LR/cd": "Lower Risk/conservation dependent",
    "LR/nt": "Lower Risk/near threatened",
    "LR/lc": "Lower Risk/least concern",
}

#: Categories the IUCN counts as threatened.
THREATENED_CATEGORIES = {"CR", "EN", "VU"}


def normalize_habitat_zone(raw: Optional[str]) -> Optional[str]:
    """Map a source habitat term onto :data:`HABITAT_ZONES`."""
    if not raw:
        return None
    key = raw.strip().lower()
    return HABITAT_ZONES.get(key, key or None)


def normalize_iucn_code(raw: Optional[str]) -> Optional[str]:
    """Normalize an IUCN code's casing, e.g. ``"vu "`` -> ``"VU"``."""
    if not raw:
        return None
    key = raw.strip()
    if not key:
        return None
    for code in IUCN_CATEGORIES:
        if code.lower() == key.lower():
            return code
    return key.upper()


def iucn_category_label(code: Optional[str]) -> Optional[str]:
    """Full label for an IUCN code, or ``None`` if unrecognized."""
    normalized = normalize_iucn_code(code)
    return IUCN_CATEGORIES.get(normalized) if normalized else None


def vulnerability_band(index: Optional[float]) -> Optional[str]:
    """Band a FishBase vulnerability index (0-100) onto its published labels."""
    if index is None:
        return None
    if index < 20:
        return "Very low"
    if index < 40:
        return "Low to moderate"
    if index < 60:
        return "Moderate to high"
    if index < 80:
        return "High to very high"
    return "Very high"


def strata_for_depth(shallow_m: Optional[float], deep_m: Optional[float]) -> List[str]:
    """Vertical strata spanned by a depth range, shallowest first.

    Used as a retrieval facet so "what lives below 1000 m" can be answered by a
    metadata filter rather than by hoping the embedding captured it.
    """
    if shallow_m is None and deep_m is None:
        return []
    low = shallow_m if shallow_m is not None else deep_m
    high = deep_m if deep_m is not None else shallow_m
    if low is None or high is None:
        return []
    if low > high:
        low, high = high, low
    spanned = []
    for name in PELAGIC_STRATA:
        start, end = STRATUM_BOUNDS[name]
        if low < end and high >= start:
            spanned.append(name)
    return spanned
