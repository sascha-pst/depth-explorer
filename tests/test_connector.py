"""Connector orchestration: name resolution, fetch, ingest, schema checks."""

import pytest

from depth_explorer.sources.fishbase import FishBaseConnector
from depth_explorer.sources.fishbase.connector import split_binomial
from tests.conftest import load_fixture


class TestSplitBinomial:
    def test_plain_binomial(self):
        assert split_binomial("Thunnus thynnus") == {"Genus": "Thunnus", "Species": "thynnus"}

    def test_authority_is_stripped(self):
        assert split_binomial("Thunnus thynnus (Linnaeus, 1758)") == {
            "Genus": "Thunnus", "Species": "thynnus"
        }

    def test_casing_is_normalized(self):
        assert split_binomial("thunnus Thynnus") is None, "epithet must be lowercase"
        assert split_binomial("THUNNUS thynnus") == {"Genus": "Thunnus", "Species": "thynnus"}

    def test_subspecies_is_truncated_to_the_binomial(self):
        assert split_binomial("Gadus morhua callarias") == {"Genus": "Gadus", "Species": "morhua"}

    def test_genus_alone_is_not_resolvable(self):
        assert split_binomial("Thunnus") is None
        assert split_binomial("") is None


class TestFetch:
    def test_fetches_every_configured_table(self, make_client, bluefin_routes):
        client = make_client(bluefin_routes)
        connector = FishBaseConnector(client=client)
        payload = connector.fetch("Thunnus thynnus")
        assert set(payload["tables"]) == {"species", "taxa", "ecology", "stocks", "comnames"}
        assert payload["speccode"] == 143
        assert payload["source_url"] == "https://www.fishbase.se/summary/143"
        assert "FishBase" in payload["citation"]

    def test_species_is_queried_first_to_get_the_speccode(self, make_client, bluefin_routes):
        client = make_client(bluefin_routes)
        FishBaseConnector(client=client).fetch("Thunnus thynnus")
        assert client.test_transport.tables_called()[0] == "species"

    def test_dependent_tables_are_keyed_on_speccode(self, make_client, bluefin_routes):
        client = make_client(bluefin_routes)
        FishBaseConnector(client=client).fetch("Thunnus thynnus")
        for table, params in client.test_transport.calls[1:]:
            assert params["SpecCode"] == ["143"]

    def test_unknown_species_returns_none(self, make_client, bluefin_routes):
        connector = FishBaseConnector(client=make_client(bluefin_routes))
        assert connector.fetch("Nonexistus fictus") is None

    def test_non_binomial_input_raises(self, make_client, bluefin_routes):
        connector = FishBaseConnector(client=make_client(bluefin_routes))
        with pytest.raises(ValueError, match="not a binomial"):
            connector.fetch("Thunnus")

    def test_optional_tables_are_only_fetched_when_requested(self, make_client, bluefin_routes):
        client = make_client(bluefin_routes)
        connector = FishBaseConnector(client=client, tables=["species", "fooditems"])
        connector.fetch("Thunnus thynnus")
        assert client.test_transport.tables_called() == ["species", "fooditems"]

    def test_unknown_table_is_rejected_at_construction(self, make_client, bluefin_routes):
        with pytest.raises(ValueError, match="unknown FishBase table"):
            FishBaseConnector(client=make_client(bluefin_routes), tables=["speceis"])

    def test_ambiguous_match_is_recorded(self, make_client):
        two = {"count": 2, "data": load_fixture("species_143")["data"] * 2}
        connector = FishBaseConnector(client=make_client({"species": two}))
        connector.fetch("Thunnus thynnus")
        assert connector.ambiguous == [{"query": "Thunnus thynnus", "matches": 2}]

    def test_missing_speccode_still_yields_the_species_row(self, make_client):
        no_code = {"data": [{"Genus": "Thunnus", "Species": "thynnus"}]}
        client = make_client({"species": no_code})
        payload = FishBaseConnector(client=client).fetch("Thunnus thynnus")
        assert payload["speccode"] is None
        assert list(payload["tables"]) == ["species"]


class TestIngest:
    def test_end_to_end_produces_records_and_chunks(self, make_client, bluefin_routes):
        connector = FishBaseConnector(client=make_client(bluefin_routes))
        result = connector.ingest(["Thunnus thynnus"])
        assert len(result.records) == 1
        assert result.chunks
        assert {c.source for c in result.chunks} == {"fishbase"}

    def test_chunks_carry_species_metadata_for_filtering(self, make_client, bluefin_routes):
        connector = FishBaseConnector(client=make_client(bluefin_routes))
        chunk = connector.ingest(["Thunnus thynnus"]).chunks[0]
        assert chunk.taxonomy["family"] == "Scombridae"
        assert chunk.identifiers["fishbase_speccode"] == "143"
        assert chunk.facets["habitat_zone"] == "pelagic-oceanic"
        assert chunk.facets["depth_strata"] == ["epipelagic", "mesopelagic"]

    def test_chunks_carry_citable_provenance(self, make_client, bluefin_routes):
        connector = FishBaseConnector(client=make_client(bluefin_routes))
        chunks = connector.ingest(["Thunnus thynnus"]).chunks
        biology = next(c for c in chunks if c.section == "biology" and c.content_type == "free_text")
        assert biology.provenance.api_url.startswith("https://fishbase.ropensci.org/species")
        assert biology.provenance.reference_ids

    def test_unknown_species_is_reported_not_raised(self, make_client, bluefin_routes):
        connector = FishBaseConnector(client=make_client(bluefin_routes))
        result = connector.ingest(["Thunnus thynnus", "Nonexistus fictus"])
        assert result.not_found == ["Nonexistus fictus"]
        assert len(result.records) == 1, "one bad name must not abort the run"

    def test_a_failing_species_does_not_abort_the_run(self, make_client, bluefin_routes):
        connector = FishBaseConnector(client=make_client(bluefin_routes))
        result = connector.ingest(["Thunnus", "Thunnus thynnus"])
        assert result.failed[0][0] == "Thunnus"
        assert len(result.records) == 1

    def test_summary_counts_by_section(self, make_client, bluefin_routes):
        connector = FishBaseConnector(client=make_client(bluefin_routes))
        summary = connector.ingest(["Thunnus thynnus"]).summary()
        assert summary["records"] == 1
        assert summary["chunks_by_section"]["taxonomy"] == 1
        assert summary["est_tokens"] > 0

    def test_sealifebase_runs_through_the_same_code_path(self, make_client, bluefin_routes):
        client = make_client(bluefin_routes, server="sealifebase")
        connector = FishBaseConnector(client=client)
        record = connector.ingest(["Thunnus thynnus"]).records[0]
        assert record.source == "sealifebase"
        assert record.record_id == "sealifebase:143"
        assert client.test_transport.calls[0][0] == "species"


class TestVerifySchema:
    def test_reports_ok_when_all_expected_columns_are_present(self, make_client, bluefin_routes):
        connector = FishBaseConnector(client=make_client(bluefin_routes))
        report = connector.verify_schema("Thunnus thynnus")
        assert report["resolved"] is True
        assert report["tables"]["species"]["missing_fields"] == []
        assert report["tables"]["species"]["rows"] == 1

    def test_names_the_columns_an_upstream_rename_would_break(self, make_client, bluefin_routes):
        renamed = {"data": [{"SpecCode": 143, "Genus": "Thunnus", "Species": "thynnus"}]}
        routes = dict(bluefin_routes, species=lambda params: renamed)
        connector = FishBaseConnector(client=make_client(routes))
        report = connector.verify_schema("Thunnus thynnus")
        assert report["ok"] is False
        assert "DepthRangeDeep" in report["tables"]["species"]["missing_fields"]

    def test_unresolvable_sample_is_reported(self, make_client, bluefin_routes):
        connector = FishBaseConnector(client=make_client(bluefin_routes))
        report = connector.verify_schema("Nonexistus fictus")
        assert report["resolved"] is False
