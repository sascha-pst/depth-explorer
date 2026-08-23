# FishBase ingestion

The first source connected to the corpus. It fetches FishBase tables for a
species, normalizes them into the canonical record, and renders retrievable
chunks with provenance.

## Quick start

```bash
# One species, no writes — useful for eyeballing chunk quality first.
python -m depth_explorer.cli ingest "Thunnus thynnus" --dry-run

# The repository's 30-species seed list.
python -m depth_explorer.cli ingest --from-csv pelagic_species_climate.csv

# SeaLifeBase (invertebrates, marine mammals) — same code, different database.
python -m depth_explorer.cli ingest "Dosidicus gigas" --server sealifebase
```

Outputs land in `--out-dir` (default `data/`):

| Path | Contents |
|---|---|
| `data/raw/<server>/<table>/*.json` | Verbatim API responses, with url and fetch time |
| `data/records.jsonl` | One canonical `SpeciesRecord` per species |
| `data/chunks.jsonl` | Retrievable chunks, ready to embed |

## Verify the schema before trusting a run

FishBase mirrors differ in column casing and occasionally rename columns. Run
this against a live endpoint before the first real ingest:

```bash
python -m depth_explorer.cli verify-schema --sample-species "Thunnus thynnus"
```

It reports, per table, the columns the normalizer reads that the endpoint did
not return, and exits non-zero if any are missing. A missing column means either
an upstream rename (fix the aliases in `normalize.py`) or a mirror that omits
null-valued keys (harmless — the field is simply absent for that species).

**This has not been run against the live API.** The connector was built and
tested against recorded fixtures because the development environment's egress
policy blocks `fishbase.ropensci.org`. Treat `verify-schema` as the first step
in an environment with network access.

## What gets ingested

| Table | Contributes |
|---|---|
| `species` | Taxonomy, habitat zone, depth range, size, vulnerability, biology prose |
| `taxa` | Class, order, family |
| `ecology` | Water-column strata, habitat associations, trophic level, feeding type |
| `stocks` | IUCN status, resilience, temperature and latitude limits, distribution prose |
| `comnames` | English common names |
| `fooditems` | Prey groups (opt-in via `--tables`, one extra request per species) |

## Chunk layout

Each species produces one chunk per populated section, plus one or more per
free-text block. Sections: `taxonomy`, `habitat_depth`, `conservation`,
`biology`, `distribution`, `diet`, `ecology_notes`, `conservation_notes`.

Every chunk opens with a header naming the species, because a retrieved chunk
reading "Depth range: 0-985 m" is useless without it:

```
Thunnus thynnus (Atlantic bluefin tuna) — Habitat and depth
Habitat zone: pelagic-oceanic. Water bodies: marine. Climate zone: subtropical.
Depth range: 0-985 m, usually 0-100 m. Spans the epipelagic and mesopelagic
zones by depth. Latitudinal extent: 70°N to 40°S. ...
```

Structured facts are rendered as prose rather than dumped as JSON — embedding
models index language, and `{"DepthRangeDeep": 985}` retrieves poorly for "how
deep do bluefin tuna dive". The same values are copied into `facets` for exact
filtering, so "mesopelagic species listed as Endangered" is a metadata query
rather than a hopeful semantic one:

```json
{"depth_shallow_m": 0.0, "depth_deep_m": 985.0,
 "depth_strata": ["epipelagic", "mesopelagic"],
 "habitat_zone": "pelagic-oceanic", "iucn_code": "LC",
 "is_threatened": false, "trophic_level": 4.43}
```

## Provenance and citation

Every chunk carries the API url it came from, the tables and columns behind it,
the fetch timestamp, the FishBase citation, and any FishBase reference ids
mentioned in the prose:

```json
{"source": "fishbase",
 "api_url": "https://fishbase.ropensci.org/species?Genus=Thunnus&Species=thynnus",
 "tables": ["species"], "fields": ["Comments"],
 "retrieved_at": "2026-08-20T15:55:03+00:00",
 "reference_ids": ["5288", "26340", "30874"],
 "citation": "Froese, R. and D. Pauly, Editors. FishBase..."}
```

`reference_ids` are the handles for the papers behind a statement — resolvable
at `https://www.fishbase.se/references/FBRefSummary.php?ID=<id>` — so an answer
built on a chunk can cite the literature rather than just the database.

## Re-running and idempotency

Ids are derived from position, never from content or wall-clock time:

- `record_id` — `fishbase:143`
- `chunk_id` — `fishbase:143#habitat_depth:0`
- `content_hash` — truncated SHA-256 of the chunk text

Writes are upserts keyed on those ids, so re-ingesting unchanged data leaves
`chunks.jsonl` byte-identical. When a re-fetch changes only metadata (a new
`retrieved_at`), the row is rewritten but `content_hash` is unchanged —
**use `content_hash`, not row equality, to decide whether to re-embed.**

Raw responses are cached under `data/raw/`, so re-running after a parser change
costs no requests. Use `--refresh` to bypass the cache.

## Politeness

One request per second by default (`--rate-limit`), with exponential backoff on
429 and 5xx. Client errors other than 429 are not retried — retrying a bad query
only wastes the upstream's time. A failed request is never cached.

## FishBase quirks the connector handles

These are the traps that silently produce empty or wrong output:

- **Access booleans.** True is `-1`, not `1`. Reading `Saltwater == 1` marks
  every marine species non-marine.
- **Unstable column casing.** `SpecCode` / `speccode` / `SPECCODE` all occur.
  Every lookup is case-insensitive.
- **`Southermost`.** FishBase's own spelling of the southern range limit. Both
  spellings are accepted.
- **Split hemispheres.** Latitude magnitude and hemisphere live in separate
  columns (`Southermost` + `SouthNorthS`); the sign is reapplied on read.
- **HTML in prose.** `Comments` contains `<i>` and `&nbsp;`, stripped on read.
- **`(Ref. 12345)` markers.** A naive sentence splitter tears these in half;
  the splitter protects abbreviation periods and extracts the ids as citations.
- **Multiple stock rows.** IUCN status can sit on a regional stock rather than
  the species-level one, so the normalizer prefers `Level == "Species level"`
  and falls back across rows.
- **Repeated prose.** The same remark appears on several stock rows; duplicates
  are dropped, since near-identical chunks crowd out other results at retrieval.

## Adding the next source

The canonical schema, chunker, facets and JSONL writer are source-agnostic. A
new source is a `SourceConnector` subclass implementing `fetch` and `normalize`;
it inherits chunking, provenance and idempotency.

The one open piece is cross-source identity. FishBase keys on `SpecCode`, which
no other source knows. Records carry an `identifiers` map
(`{"fishbase_speccode": "143"}`) rather than a single id, so when WoRMS is
connected its `worms_aphia_id` can be merged into the same map and become the
join key the project plan calls for. Until then, records are keyed per source
and matched by scientific name.
