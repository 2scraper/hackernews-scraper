#!/usr/bin/env python3
"""
hackernews-scraper — Selenium edition (secondary engine)
========================================================

The Selenium twin of playwright_scraper.py: same modes, same flags, same
rows. It must agree with its twins on exit codes, run status, and whether a
run crashes or spends money. The fetch loop that decides all three lives in
page_flow.py and is shared, so this file is browser plumbing and nothing else.

    --mode listing        (default)  a story list (front page, /newest, Ask,
                                     Show, jobs, best, active, one day's
                                     front page, a user's submissions, or
                                     everything submitted from one domain)
    --mode item                      one item's whole comment tree
    --mode user                      one profile

Three limits of this engine, stated here rather than left to be discovered.
None is a bug in this code and none can be fixed from here:

  * **Selenium cannot use an authenticated remote CDP endpoint.**
    chromedriver's `debuggerAddress` takes a bare `host:port` and has nowhere
    to put a password, so a credentialled --cdp-endpoint (the Scraping
    Browser API) is refused with exit 2 rather than connected to and
    silently failing.
  * **Selenium cannot authenticate a proxy at all.** `--proxy-server=`
    accepts no credentials. They are stripped and a warning says so.
  * **Selenium reports no HTTP status for a navigation.** Every page is
    therefore judged on what the document holds (the site's own rows, its
    own throttle sentence), which page_flow's classifier does when the
    status is None. A run that hits a status-only refusal reads it as
    whatever the body says.

Nothing on this site needs the first two: every page kind answered plain
curl with no key and no proxy (see playwright_scraper.py).

Selenium-only flag: `--chromedriver PATH` names a chromedriver binary to use
instead of letting Selenium Manager find or download one.

Usage
-----
    python selenium_scraper.py --pages 3 --delay 4

    python selenium_scraper.py --feed newest --pages 2

    python selenium_scraper.py --url "https://news.ycombinator.com/show?p=2"

    python selenium_scraper.py --mode user --user dang

Requires: pip install -r requirements.txt -r requirements-selenium.txt
          A local Chrome or Chromium must be installed. Selenium 4 fetches a
          matching chromedriver itself unless --chromedriver is given.
"""

import argparse
import logging
import queue
import re
import sys
import threading
import time
from urllib.parse import urlsplit

from selenium import webdriver
from selenium.common.exceptions import WebDriverException
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service

from product_parser import FEEDS, MODES
from output_writer import EXIT_API_ERROR
import page_flow
from proxy_pool import (from_args as proxy_pool_from_args, mask, ROTATE_MODES,
                        ProxyError, split_credentials)
import env_config

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("selenium_scraper")

# Explicit, because a driver that stops answering otherwise hangs the run.
PAGE_LOAD_TIMEOUT = page_flow.NAV_TIMEOUT_MS / 1000.0

# Chromium's own names for "the proxy is the problem, not the site".
_PROXY_ERROR_MARKERS = (
    "ERR_PROXY_CONNECTION_FAILED", "ERR_TUNNEL_CONNECTION_FAILED",
    "ERR_PROXY_AUTH_UNSUPPORTED", "ERR_PROXY_AUTH_REQUESTED",
    "ERR_UNEXPECTED_PROXY_AUTH", "ERR_PROXY_CERTIFICATE_INVALID",
    "ERR_NO_SUPPORTED_PROXIES", "ERR_SOCKS_CONNECTION_FAILED",
    "ERR_MANDATORY_PROXY_CONFIGURATION_FAILED",
)


# Every `scheme://user:pass@` in a string, however many times it occurs.
_CREDENTIALS_IN_URL_RE = re.compile(r"([a-z][a-z0-9+.\-]*://)[^\s/@]+:[^\s/@]+@",
                                    re.IGNORECASE)


def _mask_credentials(text: str) -> str:
    """`text` with any username:password in an embedded URL replaced."""
    return _CREDENTIALS_IN_URL_RE.sub(r"\1***:***@", text or "")


def _proxy_failure(text) -> str:
    """The Chromium proxy-error name in `text`, or "" if it is not one."""
    text = str(text)
    for marker in _PROXY_ERROR_MARKERS:
        if marker in text:
            return marker
    return ""


def _cdp_host_port(endpoint: str) -> str:
    """`host:port` for chromedriver's debuggerAddress, or exit 2 with a reason."""
    parts = urlsplit(endpoint if "//" in endpoint else f"//{endpoint}")
    if parts.username or parts.password:
        logger.error(
            "This --cdp-endpoint carries credentials (%s), and Selenium cannot "
            "send them: chromedriver's debuggerAddress is a bare host:port. "
            "Use playwright_scraper.py or puppeteer_scraper.py for a "
            "credentialed endpoint such as the Scraping Browser API — both "
            "authenticate on the WebSocket upgrade.",
            _mask_credentials(endpoint))
        sys.exit(2)
    host = parts.hostname or endpoint
    port = f":{parts.port}" if parts.port else ""
    return f"{host}{port}"


class _Ops:
    """One Chrome driver, exposed as page_flow's named operations.

    Same contract as playwright_scraper._Ops, including the rule that a
    rotation means a genuinely FRESH browser.
    """

    def __init__(self, args, pool):
        self.args, self.pool = args, pool
        self.remote = bool(args.cdp_endpoint)
        self.driver = None

    def _service(self):
        path = getattr(self.args, "chromedriver", None)
        return Service(executable_path=path) if path else None

    def open(self):
        options = Options()
        # `eager` returns at DOMContentLoaded, the twin's wait_until: this
        # site's data is in the markup and its scripts only wire up arrows.
        options.page_load_strategy = "eager"
        if self.remote:
            options.debugger_address = _cdp_host_port(self.args.cdp_endpoint)
            logger.info("Attaching to an existing browser at %s.",
                        options.debugger_address)
            # No UA, no proxy, no fingerprint on this path: the remote browser
            # brings its own.
            self.driver = webdriver.Chrome(options=options, service=self._service())
            self.driver.set_page_load_timeout(PAGE_LOAD_TIMEOUT)
            return self

        if self.args.headless:
            options.add_argument("--headless=new")
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--window-size=1600,1000")
        options.add_argument(f"--lang={self.args.locale}")
        if self.pool:
            scrubbed, credentials = split_credentials(self.pool.current)
            options.add_argument(f"--proxy-server={scrubbed}")
            logger.info("Using proxy exit %s", mask(self.pool.current))
            if credentials:
                logger.warning(
                    "This proxy has credentials and SELENIUM CANNOT SEND "
                    "THEM: --proxy-server accepts an address only. They have "
                    "been stripped, so the exit will most likely refuse the "
                    "requests. Use playwright_scraper.py or "
                    "puppeteer_scraper.py for an authenticated proxy.")

        self.driver = webdriver.Chrome(options=options, service=self._service())
        self.driver.set_page_load_timeout(PAGE_LOAD_TIMEOUT)
        # No user agent is set, as in the twin: a bare override leaves the
        # client hints reporting the real browser. Only --fingerprint,
        # which supplies a whole identity, overrides anything.
        if self.args.fingerprint:
            self._apply_fingerprint()
        return self

    def _apply_fingerprint(self):
        """The SAME init script the other two engines install, plus the UA
        and timezone the fingerprint states, over CDP."""
        from fingerprint_client import (get_fingerprint, fingerprint_user_agent,
                                        playwright_init_script)
        fp = get_fingerprint(self.args.twocaptcha_key, tags=self.args.fp_tags,
                             country=self.args.fp_country)
        ua = fingerprint_user_agent(fp)
        intl = fp.get("intl") or {}
        try:
            if ua:
                override = {"userAgent": ua}
                languages = intl.get("languages")
                if isinstance(languages, list) and languages:
                    override["acceptLanguage"] = ",".join(languages)
                self.driver.execute_cdp_cmd("Network.setUserAgentOverride", override)
            if intl.get("timeZone"):
                self.driver.execute_cdp_cmd("Emulation.setTimezoneOverride",
                                            {"timezoneId": intl["timeZone"]})
            self.driver.execute_cdp_cmd(
                "Page.addScriptToEvaluateOnNewDocument",
                {"source": playwright_init_script(fp)})
            logger.info("Using 2captcha fingerprint %s (%s)", fp.get("id"),
                        fp.get("country"))
        except WebDriverException as e:
            logger.warning("Could not apply the fingerprint over CDP (%s) — "
                           "continuing without it.", _mask_credentials(str(e)))

    # ---- page_flow's operations -----------------------------------------

    def fetch(self, url: str):
        """Navigate to `url` and return (status, rendered html, error).

        Selenium reports no HTTP status, so it is None and page_flow judges
        the document. No JavaScript is evaluated in the page.
        """
        try:
            self.driver.get(url)
        except WebDriverException as e:
            return None, "", _mask_credentials(str(e))
        try:
            html = self.driver.page_source or ""
        except WebDriverException as e:
            return None, "", _mask_credentials(str(e))
        return None, html, None

    def wait_ms(self, ms: int) -> None:
        time.sleep(ms / 1000.0)

    def proxy_failure(self, text: str) -> str:
        return _proxy_failure(text)

    def relaunch(self):
        """A fresh browser on the pool's current exit. On a remote browser
        there is nothing to relaunch: its exit is not ours to change."""
        if self.remote:
            return
        self.close()
        self.open()

    def close(self):
        try:
            if self.driver is not None:
                # quit(), not close(): close() leaves the driver process
                # running, which a per-page rotation would leak once a page.
                self.driver.quit()
        except Exception as e:  # noqa: BLE001
            logger.debug("Ignoring error during driver teardown: %s", e)


def _fetch_pages_concurrently(args, pool, query, page_nums, concurrency: int):
    """Fetch `page_nums` across `concurrency` workers, each with its own
    driver and exit; the page loop is page_flow.worker_loop."""
    work = queue.Queue()
    for n in page_nums:
        work.put(n)
    results, results_lock = [], threading.Lock()
    exhausted = threading.Event()

    def worker(index: int):
        name = f"worker-{index + 1}"
        try:
            ops = _Ops(args, page_flow.worker_pool(pool, index)).open()
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
    return page_flow.run_pages(
        lambda: _Ops(args, pool).open(),
        lambda ops: ops.close(),
        lambda pages: _fetch_pages_concurrently(args, pool, args.query,
                                                pages, concurrency),
        args, pool, args.query, concurrency, _mask_credentials)


def parse_args(argv=None):
    p = argparse.ArgumentParser(
        description="news.ycombinator.com scraper — story lists, comment "
                    "trees and profiles (Selenium edition)")
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
                        "(2captcha.com/proxy). Selenium cannot send the "
                        "credentials: they are stripped with a warning.")
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
                   help="Attach to an already-running browser at host:port. "
                        "Selenium-limited: an endpoint carrying credentials "
                        "(the Scraping Browser API) is refused with exit 2, "
                        "because chromedriver's debuggerAddress has nowhere "
                        "to put them. --proxy and --headless/--headful are "
                        "ignored.")
    p.add_argument("--dump-html", default=None, metavar="PATH",
                   help="Save the exact document the parser is given, on "
                        "success as well as failure.")
    p.add_argument("--chromedriver", default=None, metavar="PATH",
                   help="Selenium only: path to a chromedriver binary. "
                        "Default: Selenium Manager finds or downloads one.")
    p.add_argument("--headless", action="store_true", default=True)
    p.add_argument("--headful", dest="headless", action="store_false")
    args = p.parse_args(argv)
    # Whether --url was TYPED, read before .env fills it: a URL from the
    # environment is a default, and must not refuse the query flags.
    args.typed_url = args.url
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
                       "remote browser supplies its own fingerprint.")
    try:
        sys.exit(scrape(args))
    except ProxyError as e:
        logger.error("%s", e)
        sys.exit(2)
    except WebDriverException as e:
        # A remote browser that will not accept the attachment is a REMOTE
        # failure (exit 5), not a crash in this code (exit 1).
        text = _mask_credentials(str(e))
        if args.cdp_endpoint and ("cannot connect" in text.lower()
                                  or "debugger" in text.lower()):
            logger.error("Could not attach to --cdp-endpoint: %s", text)
            sys.exit(EXIT_API_ERROR)
        raise
