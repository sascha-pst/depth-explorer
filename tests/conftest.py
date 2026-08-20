"""Shared test fixtures.

The FishBase API is exercised through :class:`FixtureTransport`, an injectable
stand-in for the client's HTTP layer. Every test therefore runs offline and
deterministically, while still going through the real url building, pagination,
caching and retry code paths.
"""

from __future__ import annotations

import json
import os
import urllib.parse
from typing import Any, Dict, List, Optional, Tuple

import pytest

FIXTURE_DIR = os.path.join(os.path.dirname(__file__), "fixtures", "fishbase")


def load_fixture(name: str) -> Dict[str, Any]:
    with open(os.path.join(FIXTURE_DIR, f"{name}.json"), encoding="utf-8") as handle:
        return json.load(handle)


class FixtureTransport:
    """Serve recorded payloads by table name, recording every call."""

    def __init__(self, routes: Optional[Dict[str, Any]] = None, status: int = 200) -> None:
        self.routes = routes or {}
        self.status = status
        self.calls: List[Tuple[str, Dict[str, List[str]]]] = []

    def __call__(self, url: str, headers: Dict[str, str], timeout: float):
        parsed = urllib.parse.urlparse(url)
        table = parsed.path.rstrip("/").rsplit("/", 1)[-1]
        params = urllib.parse.parse_qs(parsed.query)
        self.calls.append((table, params))

        route = self.routes.get(table)
        if callable(route):
            route = route(params)
        if route is None:
            route = {"count": 0, "returned": 0, "data": []}
        return self.status, json.dumps(route).encode("utf-8")

    def tables_called(self) -> List[str]:
        return [table for table, _ in self.calls]


@pytest.fixture
def bluefin_routes() -> Dict[str, Any]:
    """Routes returning the full Thunnus thynnus fixture set."""

    def species(params):
        if params.get("Genus", [""])[0] == "Thunnus":
            return load_fixture("species_143")
        return load_fixture("empty")

    return {
        "species": species,
        "taxa": load_fixture("taxa_143"),
        "ecology": load_fixture("ecology_143"),
        "stocks": load_fixture("stocks_143"),
        "comnames": load_fixture("comnames_143"),
        "fooditems": load_fixture("fooditems_143"),
    }


@pytest.fixture
def eel_routes() -> Dict[str, Any]:
    """Sparse, lowercase-keyed fixture set for Anguilla anguilla."""
    return {
        "species": load_fixture("species_88_lowercase"),
        "taxa": load_fixture("empty"),
        "ecology": load_fixture("empty"),
        "stocks": load_fixture("stocks_88_lowercase"),
        "comnames": load_fixture("empty"),
    }


@pytest.fixture
def make_client(tmp_path):
    """Factory for a client wired to a fixture transport and a temp cache."""
    from depth_explorer.sources.fishbase.client import FishBaseClient

    def factory(routes=None, transport=None, **kwargs):
        transport = transport or FixtureTransport(routes)
        kwargs.setdefault("cache_dir", str(tmp_path / "raw"))
        kwargs.setdefault("min_interval", 0)
        kwargs.setdefault("sleep", lambda _seconds: None)
        client = FishBaseClient(transport=transport, **kwargs)
        client.test_transport = transport
        return client

    return factory
