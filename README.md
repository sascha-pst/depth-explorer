# Depth Explorer

Ingestion and chunking pipeline for oceanic species data — the corpus builder
behind a retrieval-augmented generation engine over marine biodiversity.

It collects **structured records** (taxonomy, habitat, depth ranges,
conservation status) and **unstructured text** (species descriptions, field
notes, literature remarks) from open biodiversity APIs, normalizes both into one
schema, and emits retrievable chunks with provenance.

## Status

| Source | Provides | State |
|---|---|---|
| **FishBase** | Biology, ecology, depth, IUCN status, prose | **Implemented** — see [docs/fishbase.md](docs/fishbase.md) |
| **SeaLifeBase** | Same, for non-fish marine life | Implemented — `--server sealifebase` |
| **WoRMS** | Authoritative taxonomy, AphiaID | Planned |
| **OBIS** / **GBIF** | Occurrence records | Planned |
| **IUCN Red List** | Structured threat assessments | Planned |

## Contents

- `depth_explorer/` — the pipeline (`core/` is source-agnostic, `sources/` holds connectors).
- `tests/` — 257 tests, run entirely offline against recorded fixtures.
- `pelagic_species_climate.csv` — 30 curated species, usable as an ingest seed list.

## Quick start

```bash
python -m depth_explorer.cli ingest "Thunnus thynnus" --dry-run
python -m depth_explorer.cli ingest --from-csv pelagic_species_climate.csv
python -m pytest
```

Before the first real run, check the live API against the columns the
normalizer reads:

```bash
python -m depth_explorer.cli verify-schema
```

## How it works

```
fetch  ──►  normalize  ──►  chunk  ──►  data/chunks.jsonl
(cached     (canonical      (shared
 raw JSON)   SpeciesRecord)  renderer)
```

Only `fetch` and `normalize` are source-specific. Everything downstream — the
chunker, the retrieval facets, provenance, and idempotent writes — is shared, so
each new source is one `SourceConnector` subclass and nothing else.

Chunks are self-contained (each names its species), structured facts are
rendered as prose rather than dumped as JSON, and every chunk carries the API
url, tables, columns, fetch time and literature references behind it.
[docs/fishbase.md](docs/fishbase.md) covers the chunk layout, facets and the
FishBase-specific quirks the connector handles.

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

## Collection pipeline design

The original fetch → normalize → merge design is now implemented in
`depth_explorer/`. The principles it was built on still hold:

1. **Seed from known pelagic families** rather than filtering "pelagic" out of
   all marine life.
2. **Join on stable ids, not names.** Records carry an `identifiers` map
   (`{"fishbase_speccode": "143"}`) so a WoRMS AphiaID can become the
   cross-source join key once WoRMS is connected.
3. **Rate-limit and cache.** One request per second; raw JSON is written to
   `data/raw/` before parsing, so a parser failure never forces a re-download.
4. **Make it idempotent.** Ids derive from position, not content, and writes are
   upserts — re-ingesting unchanged data leaves the corpus byte-identical.
5. **Keep the climate column curated.** Taxonomy, ranges and diet come cleanly
   from APIs; documented climate responses live in the literature.

### Data sources

| Source | What it provides | Endpoint |
|---|---|---|
| **FishBase / SeaLifeBase** | Biology, ecology, depth, IUCN status, prose | `https://fishbase.ropensci.org/` |
| **WoRMS** | Verified taxonomy and a stable AphiaID | `https://www.marinespecies.org/rest/` |
| **OBIS** | Marine occurrence records | `https://api.obis.org/v3/` |
| **GBIF** | Occurrence records with coordinates | `https://api.gbif.org/v1/` |
| **IUCN Red List** | Structured threat assessments | `https://apiv3.iucnredlist.org` (API key required) |

## Sources & Caveats

- Species facts reflect published ecological literature and assessments; climate responses mix **documented observations** (e.g., mackerel range expansion, krill contraction) with **model projections** (e.g., skipjack redistribution). Verify against primary sources before citing in formal work.
- Taxonomy follows currently accepted names; check WoRMS for the latest accepted nomenclature.
