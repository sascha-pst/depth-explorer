"""Row reading: FishBase's Access heritage and messy free text."""

import pytest

from depth_explorer.sources.fishbase.fields import (
    Row,
    clean_text,
    extract_reference_ids,
    rows_from,
)


class TestCaseInsensitiveLookup:
    def test_finds_field_regardless_of_casing(self):
        row = Row({"SpecCode": 143})
        assert row.integer("speccode") == 143
        assert row.integer("SPECCODE") == 143
        assert row.integer("SpecCode") == 143

    def test_first_matching_alias_wins(self):
        row = Row({"Southermost": 40})
        assert row.number("Southernmost", "Southermost") == 40

    def test_missing_field_is_none_not_an_error(self):
        assert Row({}).number("DepthRangeDeep") is None
        assert Row({}).text("Comments") is None
        assert Row({}).flag("Saltwater") is None

    def test_membership_and_present_fields(self):
        row = Row({"SpecCode": 1, "Genus": "Thunnus"})
        assert "speccode" in row and "missing" not in row
        assert row.present_fields() == {"SpecCode", "Genus"}


class TestAccessBooleans:
    """FishBase stores true as -1, the Microsoft Access convention."""

    @pytest.mark.parametrize("value", [-1, 1, "-1", "1", True, "true", "yes"])
    def test_truthy_values(self, value):
        assert Row({"Saltwater": value}).flag("Saltwater") is True

    @pytest.mark.parametrize("value", [0, "0", False, "false", "no"])
    def test_falsy_values(self, value):
        assert Row({"Fresh": value}).flag("Fresh") is False

    def test_minus_one_is_not_read_as_a_negative_number_flag(self):
        # The trap this guards: treating -1 as "false because it is not 1".
        assert Row({"Oceanic": -1}).flag("Oceanic") is True

    def test_absent_and_null_are_none(self):
        assert Row({"Fresh": None}).flag("Fresh") is None
        assert Row({"Fresh": ""}).flag("Fresh") is None


class TestNumbers:
    def test_parses_numeric_strings(self):
        assert Row({"DepthRangeDeep": "985"}).number("DepthRangeDeep") == 985.0

    def test_parses_embedded_numbers_and_thousands_separators(self):
        assert Row({"Weight": "684,000 g"}).number("Weight") == 684000.0

    def test_null_placeholders_are_none(self):
        for placeholder in ("", "NA", "n/a", "unknown", "-"):
            assert Row({"Length": placeholder}).number("Length") is None

    def test_zero_is_preserved_not_treated_as_missing(self):
        # A shallow limit of 0 m is a real value; conflating it with None would
        # lose the surface bound of every epipelagic species.
        assert Row({"DepthRangeShallow": 0}).number("DepthRangeShallow") == 0.0

    def test_integer_truncates(self):
        assert Row({"SpecCode": "143.0"}).integer("SpecCode") == 143


class TestCleanText:
    def test_strips_tags_and_entities(self):
        assert clean_text("<i>Oceanic</i>&nbsp;and epipelagic") == "Oceanic and epipelagic"

    def test_collapses_whitespace(self):
        assert clean_text("a   b\t\tc") == "a b c"

    def test_null_placeholders_become_none(self):
        assert clean_text("") is None
        assert clean_text("   ") is None
        assert clean_text("N/A") is None

    def test_preserves_paragraph_structure(self):
        assert clean_text("one\n\n\n\ntwo") == "one\n\ntwo"


class TestReferenceExtraction:
    def test_single_reference(self):
        assert extract_reference_ids("epipelagic (Ref. 5288).") == ["5288"]

    def test_multiple_and_comma_separated(self):
        text = "spawns offshore (Ref. 5288, 30874) and migrates (Refs. 26340 and 9987)."
        assert extract_reference_ids(text) == ["5288", "30874", "26340", "9987"]

    def test_deduplicates(self):
        assert extract_reference_ids("(Ref. 5288) ... (Ref. 5288)") == ["5288"]

    def test_no_references(self):
        assert extract_reference_ids("plain prose") == []
        assert extract_reference_ids(None) == []


def test_rows_from_skips_non_dicts():
    assert len(rows_from([{"a": 1}, "junk", None, {"b": 2}])) == 2
    assert rows_from(None) == []
