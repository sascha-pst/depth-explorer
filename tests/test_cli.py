"""CLI wiring: argument parsing, species resolution, exit codes."""

import json
import os

import pytest

from depth_explorer import cli
from tests.conftest import FixtureTransport

REPO_CSV = os.path.join(os.path.dirname(__file__), "..", "pelagic_species_climate.csv")


@pytest.fixture
def patched_transport(monkeypatch, bluefin_routes):
    """Route the CLI's real client through the fixture transport."""
    transport = FixtureTransport(bluefin_routes)
    original = cli.FishBaseClient

    def factory(**kwargs):
        kwargs["transport"] = transport
        kwargs["min_interval"] = 0
        kwargs["sleep"] = lambda _s: None
        return original(**kwargs)

    monkeypatch.setattr(cli, "FishBaseClient", factory)
    return transport


class TestArgumentParsing:
    def test_command_is_required(self):
        with pytest.raises(SystemExit):
            cli.build_parser().parse_args([])

    def test_ingest_defaults(self):
        args = cli.build_parser().parse_args(["ingest", "Thunnus thynnus"])
        assert args.species == ["Thunnus thynnus"]
        assert args.out_dir == "data"
        assert args.rate_limit == 1.0
        assert args.server == "fishbase"

    def test_invalid_table_is_rejected(self):
        with pytest.raises(SystemExit):
            cli.build_parser().parse_args(["ingest", "X y", "--tables", "nope"])

    def test_invalid_server_is_rejected(self):
        with pytest.raises(SystemExit):
            cli.build_parser().parse_args(["ingest", "X y", "--server", "worms"])


class TestSpeciesResolution:
    def test_combines_arguments_and_csv(self):
        args = cli.build_parser().parse_args(
            ["ingest", "Gadus morhua", "--from-csv", REPO_CSV, "--limit", "3"]
        )
        names = cli._resolve_species(args)
        assert names[0] == "Gadus morhua"
        assert len(names) == 3

    def test_deduplicates_across_sources(self):
        args = cli.build_parser().parse_args(
            ["ingest", "Thunnus thynnus", "--from-csv", REPO_CSV]
        )
        names = cli._resolve_species(args)
        assert names.count("Thunnus thynnus") == 1


class TestIngestCommand:
    def test_writes_output_and_exits_zero(self, patched_transport, tmp_path, capsys):
        code = cli.main([
            "ingest", "Thunnus thynnus",
            "--out-dir", str(tmp_path / "data"),
            "--cache-dir", str(tmp_path / "raw"),
            "--rate-limit", "0",
        ])
        assert code == 0
        summary = json.loads(capsys.readouterr().out)
        assert summary["records"] == 1 and summary["chunks"] > 0
        assert (tmp_path / "data" / "chunks.jsonl").exists()

    def test_no_species_exits_two(self, capsys):
        assert cli.main(["ingest"]) == 2
        assert "no species requested" in capsys.readouterr().err

    def test_dry_run_writes_nothing(self, patched_transport, tmp_path, capsys):
        cli.main([
            "ingest", "Thunnus thynnus", "--dry-run",
            "--out-dir", str(tmp_path / "data"),
            "--cache-dir", str(tmp_path / "raw"), "--rate-limit", "0",
        ])
        assert json.loads(capsys.readouterr().out)["written"] is False
        assert not (tmp_path / "data").exists()

    def test_chunk_size_flags_are_applied(self, patched_transport, tmp_path, capsys):
        cli.main([
            "ingest", "Thunnus thynnus", "--max-chars", "200", "--overlap-chars", "20",
            "--out-dir", str(tmp_path / "data"), "--cache-dir", str(tmp_path / "raw"),
            "--rate-limit", "0",
        ])
        capsys.readouterr()
        chunks = [json.loads(line) for line in
                  (tmp_path / "data" / "chunks.jsonl").read_text().splitlines()]
        # The budget covers the whole chunk, header included.
        assert all(chunk["char_len"] <= 200 for chunk in chunks)
        assert len(chunks) > 1

    def test_a_bad_name_reports_failure_and_exits_one(self, patched_transport, tmp_path, capsys):
        code = cli.main([
            "ingest", "Thunnus",
            "--out-dir", str(tmp_path / "data"), "--cache-dir", str(tmp_path / "raw"),
            "--rate-limit", "0",
        ])
        assert code == 1
        assert json.loads(capsys.readouterr().out)["failed"][0]["species"] == "Thunnus"


class TestVerifySchemaCommand:
    def test_reports_ok_against_a_conforming_endpoint(self, patched_transport, tmp_path, capsys):
        code = cli.main([
            "verify-schema", "--cache-dir", str(tmp_path / "raw"), "--rate-limit", "0",
        ])
        report = json.loads(capsys.readouterr().out)
        assert report["ok"] is True and code == 0
        assert report["tables"]["species"]["missing_fields"] == []

    def test_exits_nonzero_when_columns_are_missing(self, monkeypatch, tmp_path, capsys):
        transport = FixtureTransport({"species": {"data": [{"SpecCode": 143, "Genus": "Thunnus",
                                                            "Species": "thynnus"}]}})
        original = cli.FishBaseClient
        monkeypatch.setattr(cli, "FishBaseClient",
                            lambda **kw: original(**{**kw, "transport": transport,
                                                     "min_interval": 0, "sleep": lambda _s: None}))
        code = cli.main([
            "verify-schema", "--cache-dir", str(tmp_path / "raw"), "--rate-limit", "0",
        ])
        assert code == 1
        assert json.loads(capsys.readouterr().out)["ok"] is False
