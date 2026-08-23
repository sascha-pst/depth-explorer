"""Chunking: budgets, sentence integrity, self-containment, determinism."""

import pytest

from depth_explorer.core.chunking import (
    ChunkingConfig,
    build_facets,
    record_to_chunks,
    split_sentences,
    split_text,
)
from depth_explorer.core.models import (
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


@pytest.fixture
def record():
    return SpeciesRecord(
        record_id="fishbase:143",
        source="fishbase",
        identifiers={"fishbase_speccode": "143"},
        taxon=Taxon(
            scientific_name="Thunnus thynnus", genus="Thunnus", species="thynnus",
            family="Scombridae", order="Scombriformes", taxon_class="Teleostei",
        ),
        common_names=[CommonName(name="Atlantic bluefin tuna", is_preferred=True)],
        depth=DepthRange(shallow_m=0, deep_m=985, common_shallow_m=0, common_deep_m=100),
        habitat=Habitat(zone="pelagic-oceanic", environments=["marine"],
                        pelagic_zones=["epipelagic", "mesopelagic"]),
        conservation=Conservation(iucn_code="EN", iucn_category="Endangered"),
        trophic=Trophic(diet_trophic_level=4.43),
        text_blocks=[
            TextBlock(section="biology", title="Biology", text="Oceanic and epipelagic. " * 40,
                      provenance=Provenance(source="fishbase", tables=["species"])),
        ],
        provenance=Provenance(source="fishbase", tables=["species"]),
    )


class TestSentenceSplitting:
    def test_splits_on_terminators(self):
        assert split_sentences("One. Two! Three?") == ["One.", "Two!", "Three?"]

    def test_does_not_split_inside_a_reference_marker(self):
        # "(Ref. 5288)" must survive: it is the citation handle for the claim.
        sentences = split_sentences("Occurs offshore (Ref. 5288). Feeds on squid.")
        assert sentences == ["Occurs offshore (Ref. 5288).", "Feeds on squid."]

    def test_does_not_split_on_decimals(self):
        assert split_sentences("Trophic level is 4.43 overall.") == ["Trophic level is 4.43 overall."]

    def test_handles_common_abbreviations(self):
        assert len(split_sentences("Recorded for several spp. in the region.")) == 1

    def test_empty_input(self):
        assert split_sentences("") == []
        assert split_sentences("   ") == []


class TestSplitText:
    def test_short_text_is_one_chunk(self):
        assert split_text("A short note.") == ["A short note."]

    def test_respects_the_character_budget(self):
        config = ChunkingConfig(max_chars=200, overlap_chars=40)
        chunks = split_text("This is a sentence of moderate length. " * 30, config)
        assert len(chunks) > 1
        assert all(len(chunk) <= config.max_chars for chunk in chunks)

    def test_oversized_single_sentence_is_hard_split_within_budget(self):
        config = ChunkingConfig(max_chars=100, overlap_chars=20)
        chunks = split_text("word " * 200, config)
        assert all(len(chunk) <= config.max_chars for chunk in chunks)

    def test_overlap_repeats_context_between_windows(self):
        config = ChunkingConfig(max_chars=200, overlap_chars=60, min_chars=0)
        chunks = split_text("Alpha beta gamma delta epsilon zeta. " * 20, config)
        assert len(chunks) > 1
        # The tail of one window reappears at the head of the next.
        assert chunks[1].startswith(chunks[0].split(". ")[-2].strip()[:20])

    def test_zero_overlap_produces_disjoint_windows(self):
        config = ChunkingConfig(max_chars=120, overlap_chars=0, min_chars=0)
        chunks = split_text("Alpha beta. Gamma delta. Epsilon zeta. Eta theta. " * 6, config)
        assert sum(len(c) for c in chunks) <= len("Alpha beta. Gamma delta. Epsilon zeta. Eta theta. " * 6)

    def test_runt_tail_is_folded_but_never_past_the_budget(self):
        config = ChunkingConfig(max_chars=80, overlap_chars=0, min_chars=40)
        chunks = split_text("Sentence number one here. " * 10, config)
        assert all(len(chunk) <= config.max_chars for chunk in chunks)

    def test_is_deterministic(self):
        text = "Repeatable sentence about depth. " * 25
        assert split_text(text) == split_text(text)


class TestRecordToChunks:
    def test_emits_structured_and_free_text_sections(self, record):
        sections = {c.section for c in record_to_chunks(record)}
        assert {"taxonomy", "habitat_depth", "conservation", "biology"} <= sections

    def test_every_chunk_names_its_species(self, record):
        # A retrieved chunk must stand alone; "0-985 m" without a species is noise.
        for chunk in record_to_chunks(record):
            assert "Thunnus thynnus" in chunk.text
            assert chunk.scientific_name == "Thunnus thynnus"

    def test_chunk_ids_are_stable_across_runs(self, record):
        first = [c.chunk_id for c in record_to_chunks(record)]
        second = [c.chunk_id for c in record_to_chunks(record)]
        assert first == second
        assert len(set(first)) == len(first), "chunk ids must be unique"

    def test_content_hash_changes_when_text_changes(self, record):
        before = {c.chunk_id: c.content_hash for c in record_to_chunks(record)}
        record.depth.deep_m = 1200
        after = {c.chunk_id: c.content_hash for c in record_to_chunks(record)}
        depth_id = "fishbase:143#habitat_depth:0"
        assert before[depth_id] != after[depth_id]

    def test_ordinals_are_contiguous_within_a_section(self, record):
        chunks = [c for c in record_to_chunks(record) if c.section == "biology"]
        assert [c.ordinal for c in chunks] == list(range(len(chunks)))
        assert all(c.n_chunks_in_section == len(chunks) for c in chunks)

    def test_structured_facts_are_rendered_as_prose(self, record):
        habitat = next(c for c in record_to_chunks(record) if c.section == "habitat_depth")
        assert "Depth range: 0-985 m" in habitat.text
        assert "usually 0-100 m" in habitat.text
        assert "{" not in habitat.text, "chunks must not contain raw JSON"

    def test_facets_are_attached_to_every_chunk(self, record):
        for chunk in record_to_chunks(record):
            assert chunk.facets["depth_deep_m"] == 985
            assert chunk.facets["iucn_code"] == "EN"
            assert chunk.facets["is_threatened"] is True
            assert chunk.facets["depth_strata"] == ["epipelagic", "mesopelagic"]

    def test_long_free_text_is_split_into_several_chunks(self, record):
        chunks = [c for c in record_to_chunks(record, ChunkingConfig(max_chars=300))
                  if c.section == "biology" and c.content_type == "free_text"]
        assert len(chunks) > 1

    def test_structured_and_free_text_share_a_section_without_id_collision(self, record):
        # `biology` is fed both by the renderer and by FishBase's Comments prose.
        chunks = [c for c in record_to_chunks(record) if c.section == "biology"]
        assert {c.content_type for c in chunks} == {"structured_summary", "free_text"}
        assert len({c.chunk_id for c in chunks}) == len(chunks)

    def test_header_counts_against_the_chunk_budget(self, record):
        # A budget chosen to fit an embedding window must bound the whole chunk,
        # not just the body it wraps.
        config = ChunkingConfig(max_chars=400, overlap_chars=40)
        for chunk in record_to_chunks(record, config):
            assert chunk.char_len <= config.max_chars

    def test_tight_budget_still_produces_usable_chunks(self, record):
        # A header wider than the budget cannot be dropped; the floor keeps the
        # split from degenerating into one word per chunk.
        chunks = record_to_chunks(record, ChunkingConfig(max_chars=60, overlap_chars=10))
        assert chunks
        assert all(len(chunk.text.split()) > 3 for chunk in chunks)

    def test_empty_record_produces_no_chunks(self):
        empty = SpeciesRecord(record_id="fishbase:0", source="fishbase")
        assert record_to_chunks(empty) == []

    def test_token_estimate_tracks_length(self, record):
        for chunk in record_to_chunks(record):
            assert chunk.char_len == len(chunk.text)
            assert chunk.est_tokens == pytest.approx(chunk.char_len / 4, abs=1)


class TestFacets:
    def test_threatened_flag_only_for_threatened_categories(self):
        for code, expected in [("CR", True), ("EN", True), ("VU", True), ("LC", False)]:
            record = SpeciesRecord(record_id="x", source="fishbase",
                                   conservation=Conservation(iucn_code=code))
            assert build_facets(record)["is_threatened"] is expected

    def test_absent_data_produces_no_facet_key(self):
        record = SpeciesRecord(record_id="x", source="fishbase")
        assert build_facets(record) == {}


class TestProseRendering:
    """Rendered facts have to read as language, not as a field dump."""

    def test_latitudes_render_as_compass_bearings(self):
        record = SpeciesRecord(record_id="x", source="fishbase",
                               taxon=Taxon(scientific_name="Test species"),
                               habitat=Habitat(northernmost_deg=70, southernmost_deg=-40))
        chunk = next(c for c in record_to_chunks(record) if c.section == "habitat_depth")
        assert "70°N to 40°S" in chunk.text
        assert "-40" not in chunk.text

    def test_single_hemisphere_bound(self):
        record = SpeciesRecord(record_id="x", source="fishbase",
                               taxon=Taxon(scientific_name="Test species"),
                               habitat=Habitat(southernmost_deg=-12.5))
        chunk = next(c for c in record_to_chunks(record) if c.section == "habitat_depth")
        assert "Latitudinal extent: 12.5°S." in chunk.text

    def test_mass_is_scaled_for_a_reader(self):
        record = SpeciesRecord(record_id="x", source="fishbase",
                               taxon=Taxon(scientific_name="Test species"),
                               biometrics=Biometrics(max_weight_g=684000))
        chunk = next(c for c in record_to_chunks(record) if c.section == "biology")
        assert "684 kg" in chunk.text

    def test_small_mass_stays_in_grams(self):
        record = SpeciesRecord(record_id="x", source="fishbase",
                               taxon=Taxon(scientific_name="Test species"),
                               biometrics=Biometrics(max_weight_g=45))
        chunk = next(c for c in record_to_chunks(record) if c.section == "biology")
        assert "45 g" in chunk.text

    def test_strata_are_pluralized_correctly(self):
        one = SpeciesRecord(record_id="x", source="fishbase",
                            taxon=Taxon(scientific_name="Test species"),
                            depth=DepthRange(shallow_m=0, deep_m=150))
        text = next(c for c in record_to_chunks(one) if c.section == "habitat_depth").text
        assert "Spans the epipelagic zone by depth." in text

        two = SpeciesRecord(record_id="x", source="fishbase",
                            taxon=Taxon(scientific_name="Test species"),
                            depth=DepthRange(shallow_m=0, deep_m=900))
        text = next(c for c in record_to_chunks(two) if c.section == "habitat_depth").text
        assert "Spans the epipelagic and mesopelagic zones by depth." in text

    def test_redundant_iucn_assessment_is_suppressed(self):
        record = SpeciesRecord(
            record_id="x", source="fishbase", taxon=Taxon(scientific_name="Test species"),
            conservation=Conservation(iucn_code="LC", iucn_category="Least Concern",
                                      iucn_assessment="Least Concern (LC)"),
        )
        chunk = next(c for c in record_to_chunks(record) if c.section == "conservation")
        assert chunk.text.count("Least Concern") == 1

    def test_informative_iucn_assessment_is_kept(self):
        record = SpeciesRecord(
            record_id="x", source="fishbase", taxon=Taxon(scientific_name="Test species"),
            conservation=Conservation(iucn_code="EN", iucn_category="Endangered",
                                      iucn_assessment="Endangered A2bd; population declining"),
        )
        chunk = next(c for c in record_to_chunks(record) if c.section == "conservation")
        assert "population declining" in chunk.text
