"""Command line entry point: ``depth-explorer <command>``."""

from __future__ import annotations

import argparse
import json
import sys
from typing import List, Optional, Sequence

from .core.chunking import ChunkingConfig
from .pipeline import run_ingest, species_from_csv
from .sources.fishbase import FishBaseClient, FishBaseConnector
from .sources.fishbase.client import DEFAULT_BASE_URL
from .sources.fishbase.tables import ALL_TABLES, DEFAULT_TABLES


def _add_client_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL, help="API base url")
    parser.add_argument(
        "--server", default="fishbase", choices=["fishbase", "sealifebase"],
        help="which database the mirror should serve",
    )
    parser.add_argument("--cache-dir", default="data/raw", help="raw response cache ('' disables)")
    parser.add_argument("--refresh", action="store_true", help="ignore cached responses")
    parser.add_argument("--rate-limit", type=float, default=1.0, help="min seconds between requests")
    parser.add_argument("--timeout", type=float, default=30.0, help="per-request timeout in seconds")
    parser.add_argument("--max-retries", type=int, default=4, help="retries on transient failures")


def _build_connector(args: argparse.Namespace) -> FishBaseConnector:
    client = FishBaseClient(
        base_url=args.base_url,
        server=args.server,
        cache_dir=args.cache_dir or None,
        min_interval=args.rate_limit,
        timeout=args.timeout,
        max_retries=args.max_retries,
        force_refresh=getattr(args, "refresh", False),
    )
    return FishBaseConnector(client=client, tables=getattr(args, "tables", None))


def _resolve_species(args: argparse.Namespace) -> List[str]:
    names: List[str] = list(args.species or [])
    if args.from_csv:
        names.extend(species_from_csv(args.from_csv, args.csv_column))
    deduped: List[str] = []
    for name in names:
        if name not in deduped:
            deduped.append(name)
    if args.limit:
        deduped = deduped[: args.limit]
    return deduped


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="depth-explorer",
        description="Ingest oceanic species data into a chunked RAG corpus.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    ingest = subparsers.add_parser(
        "ingest", help="fetch, normalize and chunk species from FishBase/SeaLifeBase"
    )
    ingest.add_argument("species", nargs="*", help="scientific names, e.g. 'Thunnus thynnus'")
    ingest.add_argument("--from-csv", help="read species names from a CSV file")
    ingest.add_argument("--csv-column", default="scientific_name", help="CSV column holding names")
    ingest.add_argument("--limit", type=int, help="ingest at most this many species")
    ingest.add_argument("--out-dir", default="data", help="where records.jsonl/chunks.jsonl go")
    ingest.add_argument(
        "--tables", nargs="+", choices=sorted(ALL_TABLES), default=list(DEFAULT_TABLES),
        help="FishBase tables to fetch",
    )
    ingest.add_argument("--max-chars", type=int, default=1200, help="chunk size budget in characters")
    ingest.add_argument("--overlap-chars", type=int, default=150, help="overlap between text chunks")
    ingest.add_argument("--dry-run", action="store_true", help="do not write output files")
    _add_client_arguments(ingest)

    verify = subparsers.add_parser(
        "verify-schema",
        help="check a live endpoint against the columns the normalizer reads",
    )
    verify.add_argument("--sample-species", default="Thunnus thynnus")
    verify.add_argument(
        "--tables", nargs="+", choices=sorted(ALL_TABLES), default=list(DEFAULT_TABLES)
    )
    _add_client_arguments(verify)

    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)

    if args.command == "ingest":
        species = _resolve_species(args)
        if not species:
            print("no species requested; pass names or --from-csv", file=sys.stderr)
            return 2
        connector = _build_connector(args)
        summary = run_ingest(
            connector,
            species,
            out_dir=args.out_dir,
            config=ChunkingConfig(max_chars=args.max_chars, overlap_chars=args.overlap_chars),
            dry_run=args.dry_run,
        )
        print(json.dumps(summary, indent=2, ensure_ascii=False))
        return 1 if summary.get("failed") else 0

    if args.command == "verify-schema":
        connector = _build_connector(args)
        report = connector.verify_schema(args.sample_species)
        print(json.dumps(report, indent=2, ensure_ascii=False))
        return 0 if report.get("ok") else 1

    return 2


if __name__ == "__main__":
    raise SystemExit(main())
