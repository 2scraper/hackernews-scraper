#!/usr/bin/env python3
"""
hackernews-scraper — 2captcha Scraper API edition (fourth engine)
=================================================================

A fourth way to run this scraper. Unlike the three browser engines, this one
manages **no browser and no CDP session of its own**: it POSTs a URL to
2captcha's separate **Scraper API** (https://scraper.2captcha.com, a
different product from the Scraping Browser API the other three reach
through --cdp-endpoint), gets the rendered page back over plain HTTPS, and
feeds it to this project's product_parser.

Why you would want it: no Chromium to install, runs from a tiny container or
a lambda. It is not a path you NEED: news.ycombinator.com answered plain
curl, headless Chromium and the browser engines here with no key and no
proxy (measured 2026-09-30), so this client is for a setup that has no local
browser, not for getting past anything.

It runs the SAME fetch loop as the browser engines. Every page kind is a GET
of an address, so nothing needs to be refused here: all three modes work,
and the cursor lists (`/newest`, `/jobs`) are walked link to link exactly as
they are elsewhere. The only thing this file supplies is how ONE page is
fetched (page_flow's `ops.fetch`), so the retry, throttle, end-of-list and
exit-code decisions are the ones every other engine makes and cannot drift.

Each attempt is a separate billable task, and a failed one is billed too
(a 422 was, measured 2026-09-23). So `--retries` defaults to 1: one attempt.

Usage
-----
    python3 scraper_api_client.py --pages 2

    python3 scraper_api_client.py --mode item --item 49901736

    # the key comes from $TWOCAPTCHA_KEY and a CDP endpoint from
    # $HACKERNEWS_CDP_ENDPOINT, so neither needs to be typed — a secret in
    # argv is readable by anything that can run `ps`

Requires: pip install -r requirements.txt
          (no playwright/selenium/pyppeteer needed for this engine)
"""

import argparse
import logging
import os
import re
import sys
import time
from typing import Optional

import requests

from product_parser import FEEDS, MODES
from output_writer import EXIT_API_ERROR  # noqa: F401 — re-exported for callers
import page_flow
import env_config

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("scraper_api_client")

API_BASE = "https://scraper.2captcha.com"
SYNC_ENDPOINT = f"{API_BASE}/tasks/sync"

# The API caps `timeout` at 120s and rejects bodies over 10,000 bytes.
MAX_API_TIMEOUT = 120


def _mask_credentials(url: str) -> str:
    """Never print a username:password embedded in a ws://... or http://... URL."""
    if "@" not in url:
        return url
    scheme_sep = url.find("://")
    if scheme_sep == -1:
        return url
    scheme, rest = url[:scheme_sep + 3], url[scheme_sep + 3:]
    _, _, host_part = rest.partition("@")
    return f"{scheme}***:***@{host_part}"


# Credentials embedded ANYWHERE in a blob of text, not just in a string that
# is entirely a URL — and every occurrence, not the first. A masker that
# handles one occurrence prints the password the other four times and looks
# like it is working.
_CREDS_IN_TEXT_RE = re.compile(r"([a-z][a-z0-9+.-]*://)[^/\s'\"@]+@", re.IGNORECASE)
# Same shape as captcha_solver's and fingerprint_client's. A third copy is
# one too many and they should be unified in a family pass; reaching into
# another module's private name to avoid it would be worse.
_KEY_IN_TEXT_RE = re.compile(
    r"((?:client)?key|token|api[_-]?key)=([^&\s'\"]{6,})", re.IGNORECASE)


def _redact_debug_header(value: str) -> str:
    """The x-debug header, safe to log.

    SECURITY.md names this header as one of three places credentials reach a
    log unmasked, and it was logged verbatim: the API echoes back the task it
    ran, so a run driven through a credentialed CDP endpoint put that
    endpoint's username and password into the log, and a key passed as a
    query parameter would go the same way.

    Redaction rather than an allowlist of fields, deliberately: the header is
    the API's own metadata and its shape is not ours to pin, so an allowlist
    would silently drop the cost and timing figures this is logged FOR the
    first time the API adds a field.
    """
    return _KEY_IN_TEXT_RE.sub(r"\1=***",
                               _CREDS_IN_TEXT_RE.sub(r"\1***:***@", value))


def fetch_html(args, url: str):
    """(rendered html, the target's HTTP status) for `url`, via the API.

    Raises RuntimeError for an API-side failure (402 out of balance, 422 a
    task that errored, 408 the sync wait) and requests.RequestException for
    a network one; the caller turns both into a page that never arrived.
    """
    payload = {
        "task_type": "scrape",
        "url": url,
        "data_format": "raw",   # we want the page; product_parser does the rest
        "format": "json",       # {"status": verdict, "http_code": target status, "headers", "body"}
        "timeout": min(args.timeout, MAX_API_TIMEOUT),
    }
    if args.cdp_url:
        payload["cdpurl"] = args.cdp_url
        logger.info("Routing through an existing browser session: %s",
                    _mask_credentials(args.cdp_url))

    logger.info("POST %s (url=%s)", SYNC_ENDPOINT, url)
    resp = requests.post(
        SYNC_ENDPOINT,
        headers={"Authorization": f"Bearer {args.key}", "Content-Type": "application/json"},
        json=payload,
        # Give the HTTP call more headroom than the API-side task timeout,
        # otherwise a task that legitimately runs the full 120s looks like
        # a client-side network failure.
        timeout=min(args.timeout, MAX_API_TIMEOUT) + 30,
    )

    # The API returns its own per-task metadata (price, timings, status)
    # in an x-debug header — worth logging, it's the only place the real
    # cost of the call shows up.
    debug = resp.headers.get("x-debug")
    if debug:
        logger.info("x-debug: %s", _redact_debug_header(debug))

    if resp.status_code != 200:
        # 422 = task ran but errored (this is what a bad/unreachable
        # cdpurl produces: "CDP connect failed (user cdpurl) after N
        # attempts"); 402 = out of balance; 408 = sync wait exceeded.
        raise RuntimeError(
            f"Scraper API returned HTTP {resp.status_code}: {resp.text[:500]}"
        )

    body = resp.json()
    html = body.get("body") or ""
    # The TARGET's HTTP status is `http_code`. `status` is the API's own
    # verdict string ("success"), measured 2026-09-23 -- passing it on
    # handed the page classifier a string, so a target 403/503 was never
    # seen. Fall back to `status` only if it is itself an integer.
    upstream_status = body.get("http_code")
    if not isinstance(upstream_status, int):
        legacy = body.get("status")
        upstream_status = legacy if isinstance(legacy, int) and not isinstance(legacy, bool) else None
    logger.info("Upstream page HTTP status %s, %d bytes of HTML.", upstream_status, len(html))
    # The STATUS is returned alongside the HTML, not thrown away: it is the
    # only signal a bare 429/503 carries, and the classifier needs it.
    return html, upstream_status


class _Ops:
    """page_flow's named operations, answered by the Scraper API.

    There is no browser to relaunch and no exit to rotate: the API picks its
    own. So `relaunch` does nothing and `proxy_failure` never names one, and
    a refusal is retried once and then reported, as it is for a browser
    engine with no pool.
    """

    def __init__(self, args):
        self.args = args
        self.pool = None

    def fetch(self, url: str):
        try:
            html, status = fetch_html(self.args, url)
        except requests.RequestException as e:
            return None, "", "network error talking to the Scraper API: %s" % e
        except RuntimeError as e:
            return None, "", str(e)
        return status, html, None

    def wait_ms(self, ms: int) -> None:
        time.sleep(ms / 1000.0)

    def relaunch(self) -> None:
        return None

    def proxy_failure(self, text: str) -> str:
        return ""

    def close(self) -> None:
        return None


def main(argv=None) -> int:
    args = parse_args(argv)
    if not args.key:
        logger.error("No 2captcha API key. Pass --key, or better, export TWOCAPTCHA_KEY.")
        return 2
    ops = _Ops(args)
    return page_flow.run_pages(lambda: ops, lambda o: o.close(),
                               lambda pages: ([], [], False),
                               args, None, args.query, 1, _mask_credentials)


def parse_args(argv=None):
    p = argparse.ArgumentParser(
        description="news.ycombinator.com scraper — 2captcha Scraper API "
                    "edition (no local browser). Runs the same fetch loop as "
                    "the browser engines.")
    # NOT required: prefer the TWOCAPTCHA_KEY env var (a key in argv is
    # visible to anyone who can run `ps`).
    p.add_argument("--key", default=os.environ.get("TWOCAPTCHA_KEY"),
                   help="2captcha.com API key (sent as a Bearer token). "
                        "Defaults to $TWOCAPTCHA_KEY, which is the safer way to pass it.")
    p.add_argument("--mode", choices=list(MODES), default=None,
                   help="listing (default), item or user. Inferred from --url, "
                        "--item or --user when one is given.")
    p.add_argument("--url", default=None,
                   help="A news.ycombinator.com address to read the query from "
                        "instead of the flags. Also read from HACKERNEWS_URL.")
    p.add_argument("--feed", "--category", dest="feed", choices=list(FEEDS), default=None,
                   help="Which list (default news, the front page). --category is "
                        "the family's name for this flag.")
    p.add_argument("--day", default=None, metavar="YYYY-MM-DD",
                   help="With --feed front: that day's front page.")
    p.add_argument("--by", default=None, metavar="USER",
                   help="A user's submissions.")
    p.add_argument("--site", default=None, metavar="DOMAIN",
                   help="Everything submitted from a domain.")
    p.add_argument("--item", default=None, metavar="ID",
                   help="An HN item id, for --mode item.")
    p.add_argument("--user", default=None, metavar="NAME",
                   help="An HN username, for --mode user.")
    p.add_argument("--pages", type=int, default=1,
                   help="Pages of 30 stories (default 1). Ignored for --mode "
                        "item and user, which are one page.")
    p.add_argument("--delay", type=float, default=page_flow.DEFAULT_DELAY_S,
                   help="Seconds between pages (default %(default)s, the "
                        "Crawl-delay robots.txt asks for).")
    p.add_argument("--format", choices=["json", "csv", "both"], default="both")
    p.add_argument("--out", default="hackernews_rows_scraperapi", help="Output file prefix")
    p.add_argument("--timeout", type=int, default=60,
                   help=f"API-side task timeout in seconds (1-{MAX_API_TIMEOUT}, default 60)")
    p.add_argument("--cdp-url", default=None,
                   help="Route the fetch through an existing browser session over CDP "
                        "(sent as the API's `cdpurl` param), e.g. ws://user:pass@host:port")
    p.add_argument("--allow-empty", action="store_true",
                   help="Write output files even when 0 rows were parsed.")
    p.add_argument("--retries", type=int, default=1,
                   help="Attempts per page (default 1). Each attempt is a "
                        "separate BILLABLE task, and a failed one is billed "
                        "too, so this is not raised by default.")
    p.add_argument("--retry-delay", type=float, default=10.0,
                   help="Seconds before the first retry, doubling (default 10)")
    p.add_argument("--dump-html", default=None,
                   help="Also write the page the parser sees to this path, "
                        "even on success")
    args = p.parse_args(argv)
    # This client uses --key and --cdp-url rather than --twocaptcha-key and
    # --cdp-endpoint, so the env mapping is spelled out instead of defaulted.
    env_config.apply(args, keys={
        "TWOCAPTCHA_KEY": "key",
        "HACKERNEWS_CDP_ENDPOINT": "cdp_url",
        "HACKERNEWS_URL": "url",
    })
    # What page_flow reads from `args` and this client has no flag for. Set
    # here, once, so the loop's contract is visible in one place.
    args.proxy_block_retries = 0
    args.cdp_endpoint = None
    args.concurrency = 1
    args.query = page_flow.build_query(args, p.error)
    args.mode = args.query.mode
    return args


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(1)
