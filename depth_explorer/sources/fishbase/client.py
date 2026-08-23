"""HTTP access to the FishBase / SeaLifeBase REST mirror.

Behaviour that matters for a polite, restartable ingest:

* **Raw responses are cached to disk before parsing.** A parser change or crash
  never forces a re-download, and a cached corpus makes the pipeline runnable
  offline and reproducible.
* **Requests are rate limited** to one per ``min_interval`` seconds by default.
* **Transient failures retry with exponential backoff**; 4xx responses other
  than 429 fail immediately, because retrying a bad query only wastes the
  upstream's time.
* **The transport is injectable**, so the test suite exercises pagination,
  retries and caching without touching the network.

SeaLifeBase is the same API with a different path prefix, so the sibling source
named in the project plan is ``server="sealifebase"`` and no new code.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

from ...core.ids import cache_key

#: rOpenSci mirror of the FishBase tables.
DEFAULT_BASE_URL = "https://fishbase.ropensci.org"

#: Path prefix per database served by the mirror.
SERVER_PREFIXES: Dict[str, str] = {"fishbase": "", "sealifebase": "sealifebase"}

#: Attribution required when redistributing FishBase content.
CITATION = (
    "Froese, R. and D. Pauly, Editors. FishBase. World Wide Web electronic "
    "publication. www.fishbase.org"
)
SEALIFEBASE_CITATION = (
    "Palomares, M.L.D. and D. Pauly, Editors. SeaLifeBase. World Wide Web "
    "electronic publication. www.sealifebase.org"
)

#: Status codes worth retrying: rate limiting and server-side faults.
RETRYABLE_STATUS = {408, 425, 429, 500, 502, 503, 504}

Transport = Callable[[str, Dict[str, str], float], Tuple[int, bytes]]


class FishBaseError(RuntimeError):
    """Base class for connector failures."""


class FishBaseHTTPError(FishBaseError):
    """Non-retryable HTTP response from the API."""

    def __init__(self, status: int, url: str, body: str = "") -> None:
        super().__init__(f"HTTP {status} for {url}: {body[:200]}")
        self.status = status
        self.url = url


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def urllib_transport(url: str, headers: Dict[str, str], timeout: float) -> Tuple[int, bytes]:
    """Default transport. Uses stdlib urllib, which honours ``HTTPS_PROXY``."""
    request = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.getcode(), response.read()
    except urllib.error.HTTPError as exc:  # a response, just not a 2xx one
        return exc.code, exc.read()


class FishBaseClient:
    """Rate-limited, caching client for one FishBase-family database."""

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        server: str = "fishbase",
        cache_dir: Optional[str] = "data/raw",
        min_interval: float = 1.0,
        timeout: float = 30.0,
        max_retries: int = 4,
        page_size: int = 200,
        user_agent: str = "depth-explorer/0.1 (biodiversity RAG ingestion)",
        transport: Optional[Transport] = None,
        force_refresh: bool = False,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if server not in SERVER_PREFIXES:
            raise ValueError(
                f"unknown server {server!r}; expected one of {sorted(SERVER_PREFIXES)}"
            )
        self.base_url = base_url.rstrip("/")
        self.server = server
        self.cache_dir = cache_dir
        self.min_interval = min_interval
        self.timeout = timeout
        self.max_retries = max_retries
        self.page_size = page_size
        self.user_agent = user_agent
        self.transport = transport or urllib_transport
        self.force_refresh = force_refresh
        self._sleep = sleep
        self._last_request_at: Optional[float] = None
        #: Counters surfaced in the run report.
        self.stats: Dict[str, int] = {"requests": 0, "cache_hits": 0, "retries": 0}

    # -- url construction -------------------------------------------------

    @property
    def citation(self) -> str:
        return SEALIFEBASE_CITATION if self.server == "sealifebase" else CITATION

    def build_url(self, table: str, params: Optional[Dict[str, Any]] = None) -> str:
        prefix = SERVER_PREFIXES[self.server]
        path = f"{prefix}/{table}" if prefix else table
        query = urllib.parse.urlencode(
            {k: v for k, v in sorted((params or {}).items()) if v is not None}
        )
        return f"{self.base_url}/{path}" + (f"?{query}" if query else "")

    def species_page_url(self, speccode: Any) -> str:
        """Human-facing FishBase page for a species, for citation in answers."""
        site = "sealifebase.se" if self.server == "sealifebase" else "fishbase.se"
        return f"https://www.{site}/summary/{speccode}"

    # -- caching ----------------------------------------------------------

    def _cache_path(self, table: str, url: str) -> Optional[str]:
        if not self.cache_dir:
            return None
        return os.path.join(self.cache_dir, self.server, table, f"{cache_key(url)}.json")

    def _read_cache(self, path: Optional[str]) -> Optional[Dict[str, Any]]:
        if not path or self.force_refresh or not os.path.exists(path):
            return None
        try:
            with open(path, "r", encoding="utf-8") as handle:
                return json.load(handle)
        except (OSError, json.JSONDecodeError):
            return None  # a corrupt cache entry is a miss, not a failure

    def _write_cache(self, path: Optional[str], envelope: Dict[str, Any]) -> None:
        if not path:
            return
        os.makedirs(os.path.dirname(path), exist_ok=True)
        temporary = f"{path}.tmp"
        with open(temporary, "w", encoding="utf-8") as handle:
            json.dump(envelope, handle, ensure_ascii=False, indent=1, sort_keys=True)
        os.replace(temporary, path)

    # -- requests ---------------------------------------------------------

    def _throttle(self) -> None:
        if self._last_request_at is None or self.min_interval <= 0:
            return
        elapsed = time.monotonic() - self._last_request_at
        if elapsed < self.min_interval:
            self._sleep(self.min_interval - elapsed)

    def request(self, table: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """GET one page, returning the cache envelope (cached when available)."""
        url = self.build_url(table, params)
        path = self._cache_path(table, url)

        cached = self._read_cache(path)
        if cached is not None:
            self.stats["cache_hits"] += 1
            return cached

        headers = {"User-Agent": self.user_agent, "Accept": "application/json"}
        last_error: Optional[str] = None

        for attempt in range(self.max_retries + 1):
            self._throttle()
            self._last_request_at = time.monotonic()
            self.stats["requests"] += 1
            try:
                status, body = self.transport(url, headers, self.timeout)
            except Exception as exc:  # noqa: BLE001 - network errors are retryable
                status, body, last_error = 0, b"", f"{type(exc).__name__}: {exc}"
            else:
                if 200 <= status < 300:
                    envelope = {
                        "url": url,
                        "table": table,
                        "server": self.server,
                        "status": status,
                        "retrieved_at": _utcnow(),
                        "payload": self._decode(body, url),
                    }
                    self._write_cache(path, envelope)
                    return envelope
                last_error = f"HTTP {status}"
                if status not in RETRYABLE_STATUS:
                    raise FishBaseHTTPError(status, url, body.decode("utf-8", "replace"))

            if attempt < self.max_retries:
                self.stats["retries"] += 1
                self._sleep(2.0 ** attempt)

        raise FishBaseError(
            f"giving up on {url} after {self.max_retries + 1} attempts ({last_error})"
        )

    @staticmethod
    def _decode(body: bytes, url: str) -> Any:
        try:
            return json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise FishBaseError(f"invalid JSON from {url}: {exc}") from exc

    @staticmethod
    def extract_rows(payload: Any) -> List[Dict[str, Any]]:
        """Pull the row list out of a response.

        The mirror normally answers ``{"count": n, "data": [...]}``, but some
        deployments return a bare array; both are accepted.
        """
        if isinstance(payload, list):
            return [row for row in payload if isinstance(row, dict)]
        if isinstance(payload, dict):
            for key in ("data", "results", "rows"):
                value = payload.get(key)
                if isinstance(value, list):
                    return [row for row in value if isinstance(row, dict)]
        return []

    def fetch_table(
        self,
        table: str,
        params: Optional[Dict[str, Any]] = None,
        max_records: Optional[int] = None,
    ) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
        """Fetch every page of ``table`` for ``params``.

        Returns the accumulated rows and the first page's envelope, which
        carries the url and retrieval time used for provenance.
        """
        rows: List[Dict[str, Any]] = []
        first_envelope: Optional[Dict[str, Any]] = None
        offset = 0

        while True:
            page_params = dict(params or {})
            page_params["limit"] = self.page_size
            if offset:
                page_params["offset"] = offset

            envelope = self.request(table, page_params)
            first_envelope = first_envelope or envelope
            page = self.extract_rows(envelope["payload"])
            rows.extend(page)

            if max_records is not None and len(rows) >= max_records:
                return rows[:max_records], first_envelope
            if len(page) < self.page_size:
                return rows, first_envelope
            offset += self.page_size
