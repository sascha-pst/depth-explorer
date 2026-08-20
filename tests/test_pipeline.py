"""Pipeline: CSV seeding, output layout, idempotent re-ingestion."""

import json
import os

import pytest

from depth_explorer.core.jsonl import read_jsonl, upsert_jsonl
from depth_explorer.pipeline import run_ingest, species_from_csv
from depth_explorer.sources.fishbase import FishBaseConnector

REPO_CSV = os.path.join(os.path.dirname(__file__), "..", "pelagic_species_climate.csv")


class TestSpeciesFromCsv:
    def test_reads_the_repository_seed_dataset(self):
        names = species_from_csv(REPO_CSV)
        assert "Thunnus thynnus" in names
        assert len(names) == 30

    def test_falls_back_to_genus_and_species_columns(self, tmp_path):
        path = tmp_path / "seed.csv"
        path.write_text("genus,species\nThunnus,thynnus\nGadus,morhua\n")
        assert species_from_csv(str(path)) == ["Thunnus thynnus", "Gadus morhua"]

    def test_deduplicates(self, tmp_path):
        path = tmp_path / "seed.csv"
        path.write_text("scientific_name\nThunnus thynnus\nThunnus thynnus\n")
        assert species_from_csv(str(path)) == ["Thunnus thynnus"]

    def test_missing_column_raises_a_helpful_error(self, tmp_path):
        path = tmp_path / "seed.csv"
        path.write_text("common_name\nbluefin\n")
        with pytest.raises(ValueError, match="no 'scientific_name' column"):
            species_from_csv(str(path))


class TestRunIngest:
    def test_writes_records_and_chunks(self, make_client, bluefin_routes, tmp_path):
        connector = FishBaseConnector(client=make_client(bluefin_routes))
        out = tmp_path / "data"
        summary = run_ingest(connector, ["Thunnus thynnus"], out_dir=str(out))

        assert summary["written"] is True
        records = list(read_jsonl(str(out / "records.jsonl")))
        chunks = list(read_jsonl(str(out / "chunks.jsonl")))
        assert len(records) == 1 and records[0]["record_id"] == "fishbase:143"
        assert len(chunks) == summary["chunks"]

    def test_dry_run_writes_nothing(self, make_client, bluefin_routes, tmp_path):
        connector = FishBaseConnector(client=make_client(bluefin_routes))
        out = tmp_path / "data"
        summary = run_ingest(connector, ["Thunnus thynnus"], out_dir=str(out), dry_run=True)
        assert summary["written"] is False
        assert not out.exists()

    def test_reingesting_unchanged_data_changes_nothing(self, make_client, bluefin_routes, tmp_path):
        # Idempotency is what lets the corpus be refreshed without duplicating it.
        connector = FishBaseConnector(client=make_client(bluefin_routes))
        out = tmp_path / "data"
        run_ingest(connector, ["Thunnus thynnus"], out_dir=str(out))
        before = (out / "chunks.jsonl").read_text()

        run_ingest(connector, ["Thunnus thynnus"], out_dir=str(out))
        after = (out / "chunks.jsonl").read_text()
        assert before == after

        summary = run_ingest(connector, ["Thunnus thynnus"], out_dir=str(out))
        assert summary["chunks_written"]["added"] == 0
        assert summary["chunks_written"]["updated"] == 0

    def test_a_second_species_is_appended_not_replaced(self, make_client, bluefin_routes, eel_routes, tmp_path):
        out = tmp_path / "data"
        run_ingest(FishBaseConnector(client=make_client(bluefin_routes)),
                   ["Thunnus thynnus"], out_dir=str(out))
        run_ingest(FishBaseConnector(client=make_client(eel_routes)),
                   ["Anguilla anguilla"], out_dir=str(out))
        ids = {r["record_id"] for r in read_jsonl(str(out / "records.jsonl"))}
        assert ids == {"fishbase:143", "fishbase:88"}

    def test_changed_source_text_updates_the_existing_chunk(self, make_client, bluefin_routes, tmp_path):
        out = tmp_path / "data"
        run_ingest(FishBaseConnector(client=make_client(bluefin_routes)),
                   ["Thunnus thynnus"], out_dir=str(out))

        revised = json.loads(json.dumps(bluefin_routes["species"]({"Genus": ["Thunnus"]})))
        revised["data"][0]["DepthRangeDeep"] = 1100
        routes = dict(bluefin_routes, species=lambda params: revised)
        # A separate cache dir forces a real refetch; reusing the first client's
        # cache would (correctly) replay the original response.
        client = make_client(routes, cache_dir=str(tmp_path / "raw2"))
        summary = run_ingest(FishBaseConnector(client=client),
                             ["Thunnus thynnus"], out_dir=str(out))

        assert summary["chunks_written"]["updated"] >= 1
        assert summary["chunks_written"]["added"] == 0, "an update must not duplicate the chunk"

        depth_chunk = next(c for c in read_jsonl(str(out / "chunks.jsonl"))
                           if c["chunk_id"] == "fishbase:143#habitat_depth:0")
        assert "0-1100 m" in depth_chunk["text"]
        assert depth_chunk["facets"]["depth_deep_m"] == 1100

    def test_summary_reports_http_and_failures(self, make_client, bluefin_routes, tmp_path):
        connector = FishBaseConnector(client=make_client(bluefin_routes))
        summary = run_ingest(connector, ["Thunnus thynnus", "Nonexistus fictus"],
                             out_dir=str(tmp_path / "data"))
        assert summary["source"] == "fishbase"
        assert summary["requested"] == 2
        assert summary["not_found"] == ["Nonexistus fictus"]
        assert summary["http"]["requests"] > 0

    def test_output_is_valid_jsonl(self, make_client, bluefin_routes, tmp_path):
        out = tmp_path / "data"
        run_ingest(FishBaseConnector(client=make_client(bluefin_routes)),
                   ["Thunnus thynnus"], out_dir=str(out))
        for line in (out / "chunks.jsonl").read_text().splitlines():
            chunk = json.loads(line)
            assert chunk["text"] and chunk["chunk_id"] and chunk["content_hash"]


class TestUpsertSemantics:
    def test_preserves_order_of_existing_rows(self, tmp_path):
        path = str(tmp_path / "c.jsonl")
        upsert_jsonl(path, [{"id": "a"}, {"id": "b"}], "id")
        upsert_jsonl(path, [{"id": "b"}, {"id": "c"}], "id")
        assert [r["id"] for r in read_jsonl(path)] == ["a", "b", "c"]

    def test_write_is_atomic_leaving_no_temp_files(self, tmp_path):
        path = str(tmp_path / "c.jsonl")
        upsert_jsonl(path, [{"id": "a"}], "id")
        assert [p.name for p in tmp_path.iterdir()] == ["c.jsonl"]
