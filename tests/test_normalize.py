"""Normalization of raw FishBase tables into the canonical record."""

import pytest

from depth_explorer.sources.fishbase.normalize import normalize_species
from tests.conftest import load_fixture


def _payload(tables, speccode=143, source="fishbase"):
    return {
        "source": source,
        "query_name": "Thunnus thynnus",
        "speccode": speccode,
        "citation": "Froese, R. and D. Pauly, Editors. FishBase.",
        "source_url": f"https://www.fishbase.se/summary/{speccode}",
        "tables": {
            name: {"rows": payload["data"], "url": f"https://api/{name}",
                   "retrieved_at": "2026-08-20T00:00:00+00:00"}
            for name, payload in tables.items()
        },
    }


@pytest.fixture
def bluefin():
    return normalize_species(_payload({
        "species": load_fixture("species_143"),
        "taxa": load_fixture("taxa_143"),
        "ecology": load_fixture("ecology_143"),
        "stocks": load_fixture("stocks_143"),
        "comnames": load_fixture("comnames_143"),
        "fooditems": load_fixture("fooditems_143"),
    }))


@pytest.fixture
def eel():
    return normalize_species(_payload({
        "species": load_fixture("species_88_lowercase"),
        "stocks": load_fixture("stocks_88_lowercase"),
    }, speccode=88))


class TestTaxonomy:
    def test_binomial_and_authority(self, bluefin):
        assert bluefin.taxon.scientific_name == "Thunnus thynnus"
        assert bluefin.taxon.author == "(Linnaeus, 1758)"

    def test_higher_taxonomy_comes_from_the_taxa_table(self, bluefin):
        assert bluefin.taxon.family == "Scombridae"
        assert bluefin.taxon.order == "Scombriformes"
        assert bluefin.taxon.taxon_class == "Teleostei"
        assert bluefin.taxon.superclass == "Actinopterygii"

    def test_rank_path_skips_missing_ranks(self, eel):
        assert eel.taxon.rank_path() == []

    def test_record_id_uses_the_speccode(self, bluefin):
        assert bluefin.record_id == "fishbase:143"
        assert bluefin.identifiers["fishbase_speccode"] == "143"

    def test_stock_code_is_captured_as_an_identifier(self, bluefin):
        assert bluefin.identifiers["fishbase_stockcode"] == "156"


class TestCommonNames:
    def test_fbname_is_the_preferred_name(self, bluefin):
        assert bluefin.preferred_common_name() == "Atlantic bluefin tuna"

    def test_english_synonyms_are_kept(self, bluefin):
        names = [n.name for n in bluefin.common_names]
        assert "Northern bluefin tuna" in names

    def test_non_english_names_are_dropped(self, bluefin):
        assert "Thon rouge" not in [n.name for n in bluefin.common_names]

    def test_no_duplicates(self, bluefin):
        names = [n.name.lower() for n in bluefin.common_names]
        assert len(names) == len(set(names))

    def test_display_name_combines_both(self, bluefin):
        assert bluefin.display_name == "Thunnus thynnus (Atlantic bluefin tuna)"


class TestDepthAndHabitat:
    def test_depth_ranges(self, bluefin):
        assert (bluefin.depth.shallow_m, bluefin.depth.deep_m) == (0.0, 985.0)
        assert (bluefin.depth.common_shallow_m, bluefin.depth.common_deep_m) == (0.0, 100.0)

    def test_habitat_zone_is_normalized_and_the_raw_term_kept(self, bluefin):
        assert bluefin.habitat.zone == "pelagic-oceanic"
        assert bluefin.habitat.zone_raw == "pelagic-oceanic"

    def test_environment_flags_use_access_booleans(self, bluefin):
        # Saltwater is -1 (true); Fresh and Brack are 0 (false).
        assert bluefin.habitat.environments == ["marine"]

    def test_multiple_environments(self, eel):
        assert set(eel.habitat.environments) == {"marine", "brackish", "freshwater"}

    def test_pelagic_strata_are_unioned_across_ecology_rows(self, bluefin):
        # Row 1 flags epipelagic+mesopelagic, row 2 flags epipelagic+bathypelagic.
        assert set(bluefin.habitat.pelagic_zones) == {"epipelagic", "mesopelagic", "bathypelagic"}

    def test_habitat_associations(self, bluefin):
        assert set(bluefin.habitat.associations) == {"neritic", "oceanic"}

    def test_climate_and_temperature_from_stocks(self, bluefin):
        assert bluefin.habitat.climate_zone == "subtropical"
        assert (bluefin.habitat.temp_min_c, bluefin.habitat.temp_max_c) == (3.0, 30.0)

    def test_southern_latitude_is_signed_by_hemisphere(self, bluefin):
        # FishBase stores the magnitude and the hemisphere in separate columns.
        assert bluefin.habitat.northernmost_deg == 70.0
        assert bluefin.habitat.southernmost_deg == -40.0

    def test_upstream_southermost_misspelling_is_accepted(self, eel):
        assert eel.habitat.southernmost_deg == -30.0

    def test_range_description_from_the_species_level_stock(self, bluefin):
        assert "Mediterranean" in bluefin.habitat.range_description


class TestConservation:
    def test_iucn_code_and_label(self, bluefin):
        assert bluefin.conservation.iucn_code == "LC"
        assert bluefin.conservation.iucn_category == "Least Concern"
        assert bluefin.conservation.iucn_date_assessed == "2021-01-01"

    def test_species_level_stock_wins_over_a_regional_stock(self, bluefin):
        # The regional row has no IUCN code; the species-level row must be chosen.
        assert bluefin.conservation.iucn_code == "LC"

    def test_lowercase_iucn_code_is_normalized(self, eel):
        assert eel.conservation.iucn_code == "CR"
        assert eel.conservation.iucn_category == "Critically Endangered"

    def test_vulnerability_is_banded(self, bluefin):
        assert bluefin.conservation.vulnerability_index == 78.2
        assert bluefin.conservation.vulnerability_band == "High to very high"

    def test_resilience_and_threat_to_humans(self, bluefin):
        assert bluefin.conservation.resilience.startswith("Very low")
        assert bluefin.conservation.threat_to_humans == "harmless"


class TestBiometricsAndTrophic:
    def test_biometrics(self, bluefin):
        assert bluefin.biometrics.max_length_cm == 458.0
        assert bluefin.biometrics.length_type == "TL"
        assert bluefin.biometrics.max_weight_g == 684000.0
        assert bluefin.biometrics.max_age_years == 20.0

    def test_trophic_levels_and_feeding_type(self, bluefin):
        assert bluefin.trophic.diet_trophic_level == 4.43
        assert bluefin.trophic.food_trophic_level == 4.4
        assert bluefin.trophic.feeding_type == "hunting macrofauna (predator)"

    def test_prey_groups_from_fooditems(self, bluefin):
        assert bluefin.trophic.food_items == ["bony fish", "cephalopods", "crustaceans"]

    def test_missing_tables_leave_fields_empty_rather_than_failing(self, eel):
        assert eel.trophic.food_items == []
        assert eel.trophic.diet_trophic_level is None
        assert eel.biometrics.max_length_cm is None


class TestTextBlocks:
    def test_biology_comments_are_captured_and_cleaned(self, bluefin):
        block = next(b for b in bluefin.text_blocks if b.title == "Biology and life history")
        assert block.section == "biology"
        assert block.text.startswith("Oceanic and epipelagic")
        assert "<i>" not in block.text and "&nbsp;" not in block.text

    def test_reference_ids_are_extracted_for_citation(self, bluefin):
        block = next(b for b in bluefin.text_blocks if b.title == "Biology and life history")
        assert "5288" in block.provenance.reference_ids
        assert "30874" in block.provenance.reference_ids

    def test_distribution_text_comes_from_stocks(self, bluefin):
        sections = {b.section for b in bluefin.text_blocks}
        assert "distribution" in sections and "conservation_notes" in sections

    def test_duplicate_text_across_stock_rows_is_dropped(self, bluefin):
        # The same diet remark appears on both ecology rows.
        diet_texts = [b.text for b in bluefin.text_blocks if b.section == "diet"]
        assert len(diet_texts) == len(set(diet_texts))

    def test_every_block_carries_provenance(self, bluefin):
        for block in bluefin.text_blocks:
            assert block.provenance.source == "fishbase"
            assert block.provenance.tables
            assert block.provenance.retrieved_at


class TestRobustness:
    def test_lowercase_column_names_are_handled(self, eel):
        assert eel.taxon.scientific_name == "Anguilla anguilla"
        assert eel.depth.deep_m == 700.0
        assert eel.preferred_common_name() == "European eel"

    def test_empty_payload_does_not_raise(self):
        record = normalize_species({"source": "fishbase", "tables": {}, "query_name": "Nothing here"})
        assert record.taxon.scientific_name is None
        assert record.record_id == "fishbase:Nothing here"

    def test_serialization_drops_empty_fields(self, eel):
        data = eel.to_dict()
        assert "taxon" in data and "class" not in data["taxon"]
        assert data["identifiers"]["fishbase_speccode"] == "88"

    def test_sealifebase_source_names_its_identifiers(self):
        payload = _payload({"species": load_fixture("species_143")}, source="sealifebase")
        record = normalize_species(payload)
        assert record.record_id == "sealifebase:143"
        assert "sealifebase_speccode" in record.identifiers
