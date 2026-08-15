# Pelagic Species & Climate Change Dataset

A curated dataset of pelagic (open-ocean) species with taxonomy, geographic range, diet, trophic role, and documented responses to climate change — plus a pipeline design for expanding it programmatically.

## Contents

- `pelagic_species_climate.csv` — 30 species spanning bony fish, sharks and rays, marine mammals, sea turtles, cephalopods, crustaceans, cnidarians, and seabirds.
- (Planned) `collect_species.py` — script to fetch and append new species from open biodiversity APIs.

## Dataset Schema

| Column | Description |
|---|---|
| `common_name` | Common English name |
| `genus` | Genus (e.g., *Thunnus*) |
| `species` | Species epithet (e.g., *thynnus*) |
| `scientific_name` | Full binomial name |
| `taxonomic_group` | Broad group + family (e.g., "Bony fish (Scombridae)") |
| `pelagic_zone` | Depth zone occupied: epipelagic (0–200 m), mesopelagic (200–1,000 m), bathypelagic (below 1,000 m), or neustonic (surface) |
| `regions` | Ocean basins / current systems where the species occurs |
| `diet` | Primary prey items |
| `trophic_role` | Functional role in the food web (apex predator, forage fish, keystone grazer, filter feeder, etc.) |
| `climate_change_response` | Documented or projected response to ocean warming, deoxygenation, acidification, or sea-ice loss |

### How to read the climate column

Species respond along three main axes: they **move** (range shifts, e.g., Atlantic mackerel expanding into Icelandic waters), they **reschedule** (phenology changes, e.g., herring spawning mismatching copepod prey timing), or they get **squeezed** (habitat compression, e.g., billfish pushed toward the surface as oxygen minimum zones expand). A few species — Humboldt squid, moon jellyfish — are relative "winners" that tolerate conditions their competitors cannot.

## Analysis Notes

The dataset is structured for exploratory analysis:

- `taxonomic_group`, `pelagic_zone`, and `trophic_role` work as categorical groupings.
- `regions` is a delimited text field; split it into one row per species–region pair for region-level `groupby` operations.
- Possible extensions: IUCN conservation status, numeric trophic level (from FishBase), max body length, response-type category (range shift / phenology / compression / winner).

## Expanding the Dataset: Collection Pipeline

Rather than scraping, use the free biodiversity APIs. The pipeline follows a **fetch → normalize → merge** pattern.

### Data sources

| Source | What it provides | Endpoint |
|---|---|---|
| **WoRMS** (World Register of Marine Species) | Authoritative taxonomy: verified genus, species, family, and a stable AphiaID | `https://www.marinespecies.org/rest/` |
| **GBIF** | Occurrence records with coordinates — derive regions from actual observations | `https://api.gbif.org/v1/` |
| **FishBase / SeaLifeBase** | Diet, trophic level, habitat zone | via the `rfishbase` R package or API mirrors |
| **IUCN Red List** (optional) | Structured threat assessments as a climate-response proxy | `https://apiv3.iucnredlist.org` (free API key required) |

### Design principles

1. **Seed from known pelagic families.** Query WoRMS for members of families like Scombridae, Clupeidae, or Myctophidae rather than filtering "pelagic" out of all marine life.
2. **Join on AphiaID, not names.** Species names have synonyms and spelling variants (*Physeter macrocephalus* vs. *P. catodon*); the AphiaID is the stable primary key across sources.
3. **Rate-limit and cache.** Sleep ~1 s between calls and save raw JSON responses to disk before parsing, so a parser failure never forces a re-download.
4. **Make it idempotent.** Load the existing CSV, check which AphiaIDs are already present, and fetch only new species — the script grows the dataset instead of rebuilding it.
5. **Keep the climate column curated.** Taxonomy, regions, and diet come cleanly from APIs, but climate responses live in the literature. Either pull IUCN assessments as a structured proxy or maintain that column manually.

### Skeleton code

```python
import requests, time
import pandas as pd

def get_worms_record(name):
    url = f"https://www.marinespecies.org/rest/AphiaRecordsByName/{name}"
    r = requests.get(url, params={"like": "false", "marine_only": "true"})
    r.raise_for_status()
    rec = r.json()[0]  # first = best match
    return {"scientific_name": rec["scientificname"],
            "genus": rec["genus"], "family": rec["family"],
            "aphia_id": rec["AphiaID"]}

def get_gbif_regions(scientific_name, n=300):
    r = requests.get("https://api.gbif.org/v1/occurrence/search",
                     params={"scientificName": scientific_name,
                             "hasCoordinate": "true", "limit": n})
    recs = r.json()["results"]
    # derive ocean basins from lat/lon here
    return [(x["decimalLatitude"], x["decimalLongitude"]) for x in recs]

species_list = ["Thunnus obesus", "Scomber japonicus", "Euphausia pacifica"]
rows = []
for sp in species_list:
    rows.append({**get_worms_record(sp)})
    time.sleep(1)  # be polite to the API

pd.DataFrame(rows).to_csv("new_species.csv", index=False)
```

> **Caveat:** the snippet is a verified-syntax starting point, not a guaranteed-running script. API response field names should be confirmed against one raw JSON response before building out the full pipeline.

## Sources & Caveats

- Species facts reflect published ecological literature and assessments; climate responses mix **documented observations** (e.g., mackerel range expansion, krill contraction) with **model projections** (e.g., skipjack redistribution). Verify against primary sources before citing in formal work.
- Taxonomy follows currently accepted names; check WoRMS for the latest accepted nomenclature.
