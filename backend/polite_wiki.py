"""Etiquette-compliant English Wikipedia client.

All on-wiki reads for WikiAfCReviewer go through this client so that every request
to the MediaWiki Action API carries a descriptive User-Agent (per Wikimedia's
User-Agent policy), respects server lag via ``maxlag``, is serialized, and is
rate-limited. The agentic review loop never touches the wiki directly — it calls
the ``wiki_*`` custom tools, which dispatch here.

References:
- https://meta.wikimedia.org/wiki/User-Agent_policy
- https://www.mediawiki.org/wiki/API:Etiquette
"""

import logging
import os
import threading
import time

import requests

log = logging.getLogger(__name__)

# Wikimedia REQUIRES a descriptive User-Agent that identifies the tool and gives a
# way to contact the operator; generic or missing UAs can be blocked with HTTP 403.
# Override via the USER_AGENT env var once the Toolforge page exists.
DEFAULT_USER_AGENT = (
    "WikiAfCReviewer/1.0 "
    "(https://en.wikipedia.org/wiki/User:Ordiopside; or@diopside.ai) "
    "python-requests"
)


class PoliteWiki:
    """Serialized, rate-limited, ``maxlag``-aware MediaWiki Action API client."""

    API = "https://en.wikipedia.org/w/api.php"

    def __init__(self, user_agent=None, min_interval=1.0, max_retries=4):
        self._session = requests.Session()
        self._session.headers.update({
            "User-Agent": user_agent or os.environ.get("USER_AGENT", DEFAULT_USER_AGENT),
            "Accept-Encoding": "gzip",  # Wikimedia asks clients to accept gzip.
        })
        self._lock = threading.Lock()      # one in-flight request at a time.
        self._min_interval = min_interval  # seconds between requests.
        self._max_retries = max_retries
        self._last = 0.0

    def _get(self, params):
        """GET against the Action API with maxlag + Retry-After backoff."""
        with self._lock:
            wait = self._min_interval - (time.monotonic() - self._last)
            if wait > 0:
                time.sleep(wait)

            for attempt in range(self._max_retries):
                resp = self._session.get(
                    self.API,
                    params={**params, "format": "json", "formatversion": 2, "maxlag": 5},
                    timeout=15,
                )
                self._last = time.monotonic()
                log.info("wiki GET %s ua=%r -> %s",
                         params.get("action"),
                         self._session.headers["User-Agent"],
                         resp.status_code)

                # Server lag / throttle: honor Retry-After and try again.
                if resp.status_code == 503 and "Retry-After" in resp.headers:
                    time.sleep(int(resp.headers["Retry-After"]))
                    continue

                resp.raise_for_status()
                data = resp.json()

                if isinstance(data, dict) and data.get("error", {}).get("code") == "maxlag":
                    time.sleep(int(resp.headers.get("Retry-After", 5)))
                    continue

                return data

            raise RuntimeError("wiki: exhausted retries (persistent server lag)")

    def get_wikitext(self, title):
        """Return the raw wikitext of a page, or None if it does not exist."""
        data = self._get({
            "action": "query",
            "prop": "revisions",
            "rvprop": "content",
            "rvslots": "main",
            "titles": title,
        })
        pages = data.get("query", {}).get("pages", [])
        if not pages or "missing" in pages[0]:
            return None
        return pages[0]["revisions"][0]["slots"]["main"]["content"]

    def search(self, query, limit=10):
        """Full-text search English Wikipedia; returns [{title, snippet}]."""
        data = self._get({
            "action": "query",
            "list": "search",
            "srsearch": query,
            "srlimit": limit,
        })
        return [
            {"title": r["title"], "snippet": r.get("snippet", "")}
            for r in data.get("query", {}).get("search", [])
        ]
