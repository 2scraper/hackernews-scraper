#!/usr/bin/env python3
"""
hackernews-scraper — Playwright edition (primary engine)
========================================================

Scrapes the three kinds of page news.ycombinator.com serves:

    --mode listing        (default)  a story list: the front page, /newest,
                                     Ask, Show, jobs, best, active, one day's
                                     front page, a user's submissions, or
                                     everything submitted from one domain
    --mode item                      one item's whole comment tree
    --mode user                      one profile

Three engines ship in this repo and they must agree on exit codes, run
status, and whether a run crashes or spends money. The shared decisions live
in output_writer.finish_run() and page_flow.py so they cannot drift apart.

What is different about Hacker News
-----------------------------------
* **It needs nothing.** Measured 2026-09-30 from a datacentre address: every
  page kind answered HTTP 200 to plain curl with no key, no proxy, no cookie
  and no account, and to headless Chromium. There is no captcha, no
  challenge and no login wall on any page this scraper reads. The paid
  products are not needed here, and the README says so.
* **The site asks for a 30-second gap.** `robots.txt` says `Crawl-delay: 30`
  for every agent, and `--delay` defaults to that. A smaller value is yours
  to choose; about fifty requests 4 s apart drew no throttle while this was
  measured.
* **Two ways to page.** `/news`, `/show`, `/best`, `/active` and `/front`
  take `?p=N`, so their pages are independent and `--concurrency` works.
  `/newest`, `/jobs`, `/submitted` and `/from` continue with a cursor in the
  More link, so page 2's address only exists once page 1 is read; they are
  walked link to link and `--concurrency` is ignored for them, with a
  warning saying why.
* **An item page is one request.** It carries every comment (a 1,095-comment
  thread is one 1.6 MB response), so `--pages` means nothing for
  `--mode item` and is ignored.
* **The official API and Algolia's search exist**, and are the better choice
  when they answer your question. This reads the pages: it has the rank and
  page position a story held, and what the site chose to show.

Usage
-----
    python playwright_scraper.py --pages 3 --delay 4

    python playwright_scraper.py --feed show --pages 2

    python playwright_scraper.py --feed front --day 2024-01-15

    python playwright_scraper.py --mode item --item 49901736

    python playwright_scraper.py --mode user --user dang

    python playwright_scraper.py --url "https://news.ycombinator.com/from?site=example.com"

Requires: pip install -r requirements.txt -r requirements-playwright.txt
          then: playwright install chromium   (only if NOT using --cdp-endpoint)
"""

import argparse
import logging
import queue
import re
import sys
import threading
import time

from playwright.sync_api import (sync_playwright, Error as PWError,
                                 TimeoutError as PWTimeout)

from product_parser import FEEDS, MODES
from output_writer import EXIT_API_ERROR
import page_flow
from proxy_pool import (from_args as proxy_pool_from_args, to_playwright, mask,
                        ROTATE_MODES, ProxyError)
import env_config

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("playwright_scraper")


# Chromium's own names for a proxy that could not be used. Distinguished
# from a timeout because the two want opposite responses (CLAUDE.md §8).
_PROXY_ERROR_MARKERS = (
    "ERR_PROXY_CONNECTION_FAILED",
    "ERR_TUNNEL_CONNECTION_FAILED",
    "ERR_PROXY_AUTH_UNSUPPORTED",
    "ERR_PROXY_AUTH_REQUESTED",
    "ERR_PROXY_CERTIFICATE_INVALID",
    "ERR_NO_SUPPORTED_PROXIES",
    "ERR_SOCKS_CONNECTION_FAILED",
    "ERR_MANDATORY_PROXY_CONFIGURATION_FAILED",
)


def _proxy_failure(exc) -> str:
    """The Chromium proxy-error name in `exc`, or "" if it is not one."""
    text = str(exc)
    for marker in _PROXY_ERROR_MARKERS:
        if marker in text:
            return marker
    return ""


def _launch_local(pw, args, pool):
    """Launch our own Chromium on `pool`'s current exit; return (browser, context, page).

    No user agent is set. The browser's own is the one whose TLS handshake and
    client hints agree with it, and a bare override leaves those reporting the
    real browser (§24). This site does not care either way, and the safe
    default is the one that cannot be a contradiction.

    A proxy rotation tears the whole browser down and calls this again, so no
    state issued against one exit is replayed from another (§8).
    """
    launch_kwargs = {"headless": args.headless}
    proxy = to_playwright(pool.current) if pool else None
    if proxy:
        launch_kwargs["proxy"] = proxy
        logger.info("Using proxy exit %s", mask(pool.current))

    browser = pw.chromium.launch(**launch_kwargs)
    ctx_kwargs = {"locale": args.locale}
    init_script = None
    if args.fingerprint:
        from fingerprint_client import (get_fingerprint,
                                        playwright_context_kwargs,
                                        playwright_init_script)
        fp = get_fingerprint(args.twocaptcha_key,
                             tags=args.fp_tags, country=args.fp_country)
        ctx_kwargs.update(playwright_context_kwargs(fp))
        init_script = playwright_init_script(fp)
        logger.info("Using 2captcha fingerprint %s (%s)", fp.get("id"), fp.get("country"))

    context = browser.new_context(**ctx_kwargs)
    if init_script:
        context.add_init_script(init_script)
    return browser, context, context.new_page()


class _Ops:
    """One browser + context + page, exposed as page_flow's named operations.

    page_flow owns the fetch loop for all three engines. This class answers
    only HOW Playwright does each step.

    A rotation means a genuinely FRESH browser, never a proxy swapped under
    a live session (§8).
    """

    def __init__(self, pw, args, pool, remote: bool = False):
        self.pw, self.args, self.pool, self.remote = pw, args, pool, remote
        self.browser = self.context = self.page = None

    def open(self):
        if self.remote:
            self.browser, self.context, self.page = _connect_remote(self.pw, self.args)
        else:
            self.browser, self.context, self.page = _launch_local(
                self.pw, self.args, self.pool)
        return self

    # ---- page_flow's operations -----------------------------------------

    def fetch(self, url: str):
        """Navigate to `url` and return (status, rendered html, error).

        The RENDERED document, not the raw bytes: what the parser sees is what
        a browser built, which is also what `--dump-html` writes. `domcontent-
        loaded` is enough on this site, whose data is in the markup and whose
        scripts only wire up the vote arrows; waiting for `networkidle` would
        wait on nothing (§24).
        """
        try:
            resp = self.page.goto(url, wait_until="domcontentloaded",
                                  timeout=page_flow.NAV_TIMEOUT_MS)
        except (PWTimeout, PWError) as e:
            return None, "", _mask_credentials(str(e))
        status = resp.status if resp is not None else None
        try:
            html = self.page.content()
        except PWError as e:
            return status, "", _mask_credentials(str(e))
        return status, html, None

    def wait_ms(self, ms: int) -> None:
        time.sleep(ms / 1000.0)

    def proxy_failure(self, text: str) -> str:
        return _proxy_failure(text)

    def relaunch(self):
        """A fresh browser on the pool's current exit. On a remote browser
        there is nothing to relaunch: its exit is not ours to change."""
        if self.remote:
            return
        try:
            self.browser.close()
        except Exception as e:  # noqa: BLE001
            logger.debug("Ignoring error while closing browser for rotation: %s", e)
        self.open()

    def close(self):
        try:
            if self.remote:
                self.page.close()  # leave the remote browser app running
            else:
                self.browser.close()
        except Exception as e:  # noqa: BLE001
            logger.debug("Ignoring error during browser teardown: %s", e)


def _connect_remote(pw, args):
    """Attach to an already-running browser over CDP; return (browser, context, page)."""
    logger.info("Connecting to existing browser over CDP: %s",
                _mask_credentials(args.cdp_endpoint))
    browser, e = None, None
    for attempt in range(1, page_flow.CDP_CONNECT_ATTEMPTS + 1):
        try:
            browser = pw.chromium.connect_over_cdp(args.cdp_endpoint, timeout=30000)
            break
        except (PWError, PWTimeout) as err:
            e = err
            if attempt < page_flow.CDP_CONNECT_ATTEMPTS and page_flow.cdp_should_retry(str(err)):
                logger.warning("The Scraping Browser profile is still locked "
                               "(attempt %d/%d) — a previous run may be "
                               "releasing it; retrying in %.0fs.", attempt,
                               page_flow.CDP_CONNECT_ATTEMPTS,
                               page_flow.CDP_LOCKED_WAIT_S)
                time.sleep(page_flow.CDP_LOCKED_WAIT_S)
                continue
            break
    if browser is None:
        # The endpoint carries a password, and Playwright repeats it five
        # times in its error text (§8). Rewritten with it masked, keeping
        # host and port, which are the useful half.
        raise PWError(
            f"could not connect to --cdp-endpoint "
            f"{_mask_credentials(args.cdp_endpoint)}: "
            f"{_mask_credentials(str(e))}\n"
            f"{page_flow.cdp_connect_hint(str(e))}"
        ) from None
    context = browser.contexts[0] if browser.contexts else browser.new_context()
    page = context.new_page()
    return browser, context, page


# Every `scheme://user:pass@` in a string, however many times it occurs.
# Matching GLOBALLY is the point: a Playwright connection error repeats the
# endpoint five times (§8).
_CREDENTIALS_IN_URL_RE = re.compile(r"([a-z][a-z0-9+.\-]*://)[^\s/@]+:[^\s/@]+@",
                                    re.IGNORECASE)


def _mask_credentials(text: str) -> str:
    """`text` with any username:password in an embedded URL replaced."""
    return _CREDENTIALS_IN_URL_RE.sub(r"\1***:***@", text or "")


def _fetch_pages_concurrently(args, pool, query, page_nums, concurrency: int):
    """Fetch `page_nums` across `concurrency` workers.

    Each worker owns its own Playwright instance, browser and exit: with the
    sync API a browser belongs to the thread that made it, so sharing one is
    not an option even in principle (§7). The page loop itself is
    page_flow.worker_loop, shared by all three engines.
    """
    work = queue.Queue()
    for n in page_nums:
        work.put(n)
    results, results_lock = [], threading.Lock()
    exhausted = threading.Event()

    def worker(index: int):
        name = f"worker-{index + 1}"
        try:
            with sync_playwright() as pw:
                ops = _Ops(pw, args, page_flow.worker_pool(pool, index)).open()
                try:
                    page_flow.worker_loop(ops, args, query, work, results,
                                          results_lock, exhausted, name,
                                          _mask_credentials)
                finally:
                    ops.close()
        except Exception:  # noqa: BLE001 — a dead worker must not hang the run
            logger.exception("[%s] died; its pages will be reported as failed.", name)

    threads = [threading.Thread(target=worker, args=(i,), name=f"page-worker-{i + 1}")
               for i in range(concurrency)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    unattempted = []
    while True:
        try:
            unattempted.append(work.get_nowait())
        except queue.Empty:
            break
    return results, sorted(unattempted), exhausted.is_set()


def scrape(args) -> int:
    pool = proxy_pool_from_args(args)
    if pool and args.cdp_endpoint:
        logger.warning("Ignoring --proxy/--proxy-file: with --cdp-endpoint the "
                       "remote browser has its own exit, and layering a second "
                       "proxy on top would contradict it.")
        pool = None
    concurrency = page_flow.concurrency_for(args, pool)
    with sync_playwright() as pw:
        return page_flow.run_pages(
            lambda: _Ops(pw, args, pool, remote=bool(args.cdp_endpoint)).open(),
            lambda ops: ops.close(),
            lambda pages: _fetch_pages_concurrently(args, pool, args.query,
                                                    pages, concurrency),
            args, pool, args.query, concurrency, _mask_credentials)


def parse_args(argv=None):
    p = argparse.ArgumentParser(
        description="news.ycombinator.com scraper — story lists, comment "
                    "trees and profiles (Playwright edition)")
    p.add_argument("--mode", choices=list(MODES), default=None,
                   help="listing (default): a story list. item: one item's "
                        "whole comment tree. user: one profile. Inferred from "
                        "--url, --item or --user when one is given.")
    p.add_argument("--url", default=None,
                   help="A news.ycombinator.com address to read the query "
                        "from instead of the flags: /news, /newest, /ask, "
                        "/show, /jobs, /best, /active, /front?day=, "
                        "/submitted?id=, /from?site=, /item?id= or /user?id=. "
                        "A `?p=N` starts the run at that page. A cursor "
                        "address (?next=…) is refused. Also read from "
                        "HACKERNEWS_URL.")
    g = p.add_argument_group("listing")
    g.add_argument("--feed", "--category", dest="feed", choices=list(FEEDS), default=None,
                   help="Which list (default news, the front page). newest "
                        "and jobs page by cursor and cannot use "
                        "--concurrency; ask has one page of about 18. --category "
                        "is the family's name for this flag and means the same.")
    g.add_argument("--day", default=None, metavar="YYYY-MM-DD",
                   help="With --feed front: that day's front page. Validated "
                        "here: the site answers a malformed day with the "
                        "latest one instead of an error.")
    g.add_argument("--by", default=None, metavar="USER",
                   help="A user's submissions (/submitted?id=USER).")
    g.add_argument("--site", default=None, metavar="DOMAIN",
                   help="Everything submitted from a domain "
                        "(/from?site=DOMAIN), e.g. example.com.")
    g = p.add_argument_group("item")
    g.add_argument("--item", default=None, metavar="ID",
                   help="An HN item id (the number in item?id=). A story "
                        "gives its whole comment tree; a comment gives that "
                        "comment's replies.")
    g = p.add_argument_group("user")
    g.add_argument("--user", default=None, metavar="NAME",
                   help="An HN username (user?id=NAME).")
    p.add_argument("--pages", type=int, default=1,
                   help="Pages to fetch (30 stories each). A list that ends "
                        "sooner ends the run as complete. Ignored for "
                        "--mode item and user, which are one page.")
    p.add_argument("--delay", type=float, default=page_flow.DEFAULT_DELAY_S,
                   help="Delay between pages, seconds (default %(default)s, "
                        "the Crawl-delay robots.txt asks for). Lower it "
                        "knowingly: 4 s drew no throttle over ~50 requests, "
                        "and that is one measurement.")
    p.add_argument("--concurrency", type=int, default=1, metavar="N",
                   help="Fetch pages through N parallel workers (default 1). "
                        "Only for ?p=N lists; a cursor list is walked in "
                        "order. Each worker runs its own browser and holds "
                        "its own proxy exit. N workers are N times the "
                        "request rate. Ignored with --cdp-endpoint.")
    p.add_argument("--retries", type=int, default=3,
                   help="Attempts per page on a transport failure (default 3). "
                        "The pause doubles each time.")
    p.add_argument("--retry-delay", type=float, default=2.0,
                   help="Seconds before the first retry, doubling thereafter.")
    p.add_argument("--format", choices=["json", "csv", "both"], default="both")
    p.add_argument("--out", default="hackernews_rows", help="Output file prefix")
    p.add_argument("--locale", default="en-US",
                   help="Browser locale (default en-US). It changes nothing "
                        "in the data: the site is English only.")
    p.add_argument("--proxy", default=None,
                   help="Proxy URL, e.g. http://ACCOUNT:PASSWORD@HOST:9999 "
                        "(2captcha.com/proxy)")
    p.add_argument("--proxy-file", default=None,
                   help="File with one proxy URL per line to rotate across. "
                        "Wins over --proxy.")
    p.add_argument("--proxy-rotate", choices=list(ROTATE_MODES), default="per-run",
                   help="per-run (default): one exit for the whole run. "
                        "per-page: a new exit, and a fresh browser, per page.")
    p.add_argument("--proxy-shuffle", action="store_true",
                   help="Shuffle the pool at startup.")
    p.add_argument("--proxy-block-retries", type=int, default=2,
                   help="When a page is refused (HTTP 403), retry it from "
                        "this many OTHER exits (default 2). Needs a pool of "
                        "more than one.")
    p.add_argument("--twocaptcha-key", default=None,
                   help="2captcha.com API key. Used by --fingerprint. This "
                        "repo does not implement captcha solving: no page it "
                        "reads has shown one.")
    p.add_argument("--allow-empty", action="store_true",
                   help="Write output files even when 0 rows were found.")
    p.add_argument("--fingerprint", action="store_true",
                   help="Apply a browser fingerprint from 2captcha's "
                        "Fingerprint API. Needs --twocaptcha-key. Ignored with "
                        "--cdp-endpoint. Nothing on this site asks for one.")
    p.add_argument("--fp-tags", default="Windows",
                   help="ONE OS-family tag for the fingerprint filter: "
                        "Windows, Microsoft Windows or Android. NOT a list — "
                        "Chrome, Desktop and Mobile are each rejected by the "
                        "API with 400. (default: Windows)")
    p.add_argument("--fp-country", default=None,
                   help="Fingerprint country, ISO 3166-1 alpha-2. Match it to "
                        "your proxy's exit country.")
    p.add_argument("--captcha-api", choices=["v2", "v1"], default="v2",
                   help="Kept for the family's CLI. Nothing here solves a "
                        "captcha, so nothing reads it.")
    p.add_argument("--solve-captcha", choices=["when-blocked", "always"],
                   default="when-blocked",
                   help="Kept for the family's CLI. No page this repo reads "
                        "has shown a captcha, so nothing reads it.")
    p.add_argument("--min-score", type=float, default=0.7,
                   help="Kept for the family's CLI. Nothing reads it.")
    p.add_argument("--cdp-endpoint", default=None,
                   help="Connect to an already-running browser over CDP "
                        "instead of launching Chromium, e.g. the Scraping "
                        "Browser API endpoint ws://user:pass@host:port. "
                        "--proxy and --headless/--headful are ignored.")
    p.add_argument("--dump-html", default=None, metavar="PATH",
                   help="Save the exact document the parser is given, on "
                        "success as well as failure.")
    p.add_argument("--headless", action="store_true", default=True)
    p.add_argument("--headful", dest="headless", action="store_false")
    args = p.parse_args(argv)
    env_config.apply(args)
    args.query = page_flow.build_query(args, p.error)
    args.mode = args.query.mode
    return args


if __name__ == "__main__":
    args = parse_args()
    if args.fingerprint and not args.twocaptcha_key:
        logger.error("--fingerprint needs --twocaptcha-key (the Fingerprint API "
                     "uses the same key, though it's a separate subscription "
                     "from solving).")
        sys.exit(2)
    if args.fingerprint and args.cdp_endpoint:
        logger.warning("--fingerprint is ignored with --cdp-endpoint: the "
                       "Scraping Browser supplies its own fingerprint.")
    try:
        sys.exit(scrape(args))
    except ProxyError as e:
        logger.error("%s", e)
        sys.exit(2)
    except PWError as e:
        # A remote browser refusing the connection is a REMOTE API failure
        # (exit 5), not a crash in this code (exit 1).
        text = _mask_credentials(str(e))
        if "profile_locked" in text or "connect to --cdp-endpoint" in text:
            logger.error("%s", text)
            sys.exit(EXIT_API_ERROR)
        raise
