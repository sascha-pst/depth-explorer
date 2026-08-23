"""HTTP client: urls, caching, pagination, retries, rate limiting."""

import json

import pytest

from depth_explorer.sources.fishbase.client import (
    DEFAULT_BASE_URL,
    FishBaseClient,
    FishBaseError,
    FishBaseHTTPError,
)
from tests.conftest import load_fixture


class TestUrlBuilding:
    def test_builds_a_sorted_query_string(self, make_client):
        client = make_client()
        url = client.build_url("species", {"Species": "thynnus", "Genus": "Thunnus"})
        assert url == f"{DEFAULT_BASE_URL}/species?Genus=Thunnus&Species=thynnus"

    def test_drops_none_parameters(self, make_client):
        url = make_client().build_url("species", {"Genus": "Thunnus", "offset": None})
        assert "offset" not in url

    def test_sealifebase_uses_a_path_prefix(self, make_client):
        client = make_client(server="sealifebase")
        assert client.build_url("species", {}) == f"{DEFAULT_BASE_URL}/sealifebase/species"

    def test_server_specific_citation_and_page_url(self, make_client):
        assert "FishBase" in make_client().citation
        sealife = make_client(server="sealifebase")
        assert "SeaLifeBase" in sealife.citation
        assert sealife.species_page_url(7) == "https://www.sealifebase.se/summary/7"

    def test_unknown_server_is_rejected(self):
        with pytest.raises(ValueError, match="unknown server"):
            FishBaseClient(server="worms")


class TestResponseShapes:
    def test_reads_the_data_envelope(self):
        assert FishBaseClient.extract_rows({"count": 1, "data": [{"a": 1}]}) == [{"a": 1}]

    def test_reads_a_bare_array(self):
        # Some mirrors answer with a plain list rather than an envelope.
        assert FishBaseClient.extract_rows([{"a": 1}]) == [{"a": 1}]

    def test_unknown_shape_yields_no_rows(self):
        assert FishBaseClient.extract_rows({"error": "nope"}) == []
        assert FishBaseClient.extract_rows(None) == []

    def test_invalid_json_raises(self, make_client):
        class BadTransport:
            def __call__(self, url, headers, timeout):
                return 200, b"<html>gateway error</html>"

        client = make_client(transport=BadTransport())
        with pytest.raises(FishBaseError, match="invalid JSON"):
            client.request("species", {})


class TestCaching:
    def test_raw_response_is_written_to_disk(self, make_client, bluefin_routes, tmp_path):
        client = make_client(bluefin_routes)
        client.request("species", {"Genus": "Thunnus", "Species": "thynnus"})
        cached = list((tmp_path / "raw" / "fishbase" / "species").glob("*.json"))
        assert len(cached) == 1
        envelope = json.loads(cached[0].read_text())
        assert envelope["table"] == "species"
        assert envelope["retrieved_at"] and envelope["url"]
        assert envelope["payload"]["data"][0]["SpecCode"] == 143

    def test_second_call_is_served_from_cache(self, make_client, bluefin_routes):
        client = make_client(bluefin_routes)
        params = {"Genus": "Thunnus", "Species": "thynnus"}
        client.request("species", params)
        client.request("species", params)
        assert client.stats["requests"] == 1
        assert client.stats["cache_hits"] == 1

    def test_different_params_are_cached_separately(self, make_client, bluefin_routes):
        client = make_client(bluefin_routes)
        client.request("species", {"Genus": "Thunnus"})
        client.request("species", {"Genus": "Katsuwonus"})
        assert client.stats["requests"] == 2

    def test_refresh_bypasses_the_cache(self, make_client, bluefin_routes):
        client = make_client(bluefin_routes)
        client.request("species", {"Genus": "Thunnus"})
        client.force_refresh = True
        client.request("species", {"Genus": "Thunnus"})
        assert client.stats["requests"] == 2

    def test_corrupt_cache_entry_is_treated_as_a_miss(self, make_client, bluefin_routes, tmp_path):
        client = make_client(bluefin_routes)
        client.request("species", {"Genus": "Thunnus"})
        cached = next((tmp_path / "raw" / "fishbase" / "species").glob("*.json"))
        cached.write_text("{ truncated")
        client.request("species", {"Genus": "Thunnus"})
        assert client.stats["requests"] == 2

    def test_cache_can_be_disabled(self, make_client, bluefin_routes):
        client = make_client(bluefin_routes, cache_dir=None)
        client.request("species", {"Genus": "Thunnus"})
        client.request("species", {"Genus": "Thunnus"})
        assert client.stats["requests"] == 2


class TestPagination:
    def test_follows_pages_until_a_short_one(self, make_client):
        full_page = {"data": [{"SpecCode": i} for i in range(3)]}
        last_page = {"data": [{"SpecCode": 99}]}
        pages = [full_page, full_page, last_page]

        def route(params):
            return pages[int(params.get("offset", ["0"])[0]) // 3]

        client = make_client({"comnames": route}, page_size=3)
        rows, envelope = client.fetch_table("comnames", {"SpecCode": 143})
        assert len(rows) == 7
        assert "offset" not in envelope["url"], "provenance points at the first page"

    def test_single_short_page_makes_one_request(self, make_client, bluefin_routes):
        client = make_client(bluefin_routes)
        rows, _ = client.fetch_table("comnames", {"SpecCode": 143})
        assert len(rows) == 4
        assert client.stats["requests"] == 1

    def test_max_records_caps_the_result(self, make_client):
        page = {"data": [{"SpecCode": i} for i in range(3)]}
        client = make_client({"fooditems": lambda p: page}, page_size=3)
        rows, _ = client.fetch_table("fooditems", {}, max_records=4)
        assert len(rows) == 4

    def test_empty_table_returns_no_rows(self, make_client):
        client = make_client({"stocks": load_fixture("empty")})
        rows, _ = client.fetch_table("stocks", {"SpecCode": 1})
        assert rows == []


class FlakyTransport:
    """Fails with `status` for the first `failures` calls, then succeeds."""

    def __init__(self, failures, status=503, payload=None):
        self.failures = failures
        self.status = status
        self.payload = payload or {"data": [{"SpecCode": 143}]}
        self.calls = 0

    def __call__(self, url, headers, timeout):
        self.calls += 1
        if self.calls <= self.failures:
            if self.status == 0:
                raise ConnectionResetError("connection reset by peer")
            return self.status, b'{"error": "transient"}'
        return 200, json.dumps(self.payload).encode()


class TestRetries:
    def test_retries_a_server_error_then_succeeds(self, make_client):
        transport = FlakyTransport(failures=2)
        client = make_client(transport=transport)
        envelope = client.request("species", {})
        assert envelope["payload"]["data"][0]["SpecCode"] == 143
        assert transport.calls == 3
        assert client.stats["retries"] == 2

    def test_retries_a_network_exception(self, make_client):
        transport = FlakyTransport(failures=1, status=0)
        client = make_client(transport=transport)
        assert client.request("species", {})["status"] == 200

    def test_retries_rate_limiting(self, make_client):
        transport = FlakyTransport(failures=1, status=429)
        client = make_client(transport=transport)
        assert client.request("species", {})["status"] == 200

    def test_gives_up_after_max_retries(self, make_client):
        transport = FlakyTransport(failures=99)
        client = make_client(transport=transport, max_retries=2)
        with pytest.raises(FishBaseError, match="giving up"):
            client.request("species", {})
        assert transport.calls == 3

    def test_client_errors_are_not_retried(self, make_client):
        # Retrying a malformed query only wastes the upstream's time.
        transport = FlakyTransport(failures=99, status=404)
        client = make_client(transport=transport)
        with pytest.raises(FishBaseHTTPError) as excinfo:
            client.request("species", {})
        assert excinfo.value.status == 404
        assert transport.calls == 1

    def test_backoff_is_exponential(self, make_client):
        slept = []
        transport = FlakyTransport(failures=3)
        client = make_client(transport=transport, sleep=slept.append)
        client.request("species", {})
        assert slept == [1.0, 2.0, 4.0]

    def test_a_failed_request_is_not_cached(self, make_client, tmp_path):
        transport = FlakyTransport(failures=99, status=500)
        client = make_client(transport=transport, max_retries=1)
        with pytest.raises(FishBaseError):
            client.request("species", {})
        assert not list((tmp_path / "raw").rglob("*.json"))


class TestRateLimiting:
    def test_sleeps_between_requests(self, make_client):
        slept = []
        client = make_client({"species": {"data": []}}, min_interval=1.0, sleep=slept.append)
        client.request("species", {"a": 1})
        client.request("species", {"a": 2})
        assert len(slept) == 1 and 0 < slept[0] <= 1.0

    def test_no_sleep_before_the_first_request(self, make_client):
        slept = []
        client = make_client({"species": {"data": []}}, min_interval=1.0, sleep=slept.append)
        client.request("species", {})
        assert slept == []

    def test_cache_hits_do_not_consume_the_rate_limit(self, make_client, bluefin_routes):
        slept = []
        client = make_client(bluefin_routes, min_interval=1.0, sleep=slept.append)
        client.request("species", {"Genus": "Thunnus"})
        client.request("species", {"Genus": "Thunnus"})
        assert slept == []
