#!/usr/bin/env python3
"""
hackernews-scraper — pyppeteer edition (secondary engine)
=========================================================

The pyppeteer twin of playwright_scraper.py: the same three modes (story
lists, one item's comment tree, one profile) over news.ycombinator.com, the
same flags, and the same exit codes and run status. The fetch loop that
decides those lives in page_flow.py and is shared, so this file is browser
plumbing and nothing else: how pyppeteer launches, navigates and reads the
rendered document. See playwright_scraper.py's header for what is different
about this site (it needs no key and no proxy, asks for a 30-second gap, and
pages two different ways).

Two things to know before choosing this engine:

  * **pyppeteer is effectively unmaintained** and its own README points at
    Playwright. It is here for parity, and for anyone who already has it.
  * Unlike the Selenium engine, it CAN authenticate a remote CDP endpoint
    (`browserWSEndpoint` takes a full `ws://user:pass@host:port`) and a
    proxy. Proxy credentials are answered over CDP (`Fetch.authRequired`),
    because pyppeteer's own `page.authenticate` needs a protocol method
    that current Chromium has removed.

Usage
-----
    python puppeteer_scraper.py --pages 3 --delay 4

    python puppeteer_scraper.py --feed newest --pages 2

    python puppeteer_scraper.py --mode item --item 49901736

    python puppeteer_scraper.py --url "https://news.ycombinator.com/from?site=example.com"

Requires: pip install -r requirements.txt -r requirements-puppeteer.txt
          (pyppeteer downloads its own Chromium on first run; --chromium-path
          points it at another browser if that one will not start)
"""

import argparse
import asyncio
import concurrent.futures
import logging
import queue
import re
import sys
import threading
import time
from typing import Optional

# At module level, deliberately, and not inside the launch path. The offline
# suite guards `import puppeteer_scraper` behind try/except ImportError and
# REPORTS the skip, and CI's engine-smoke job fails on any reported skip.
# That only works if importing this module actually requires the driver.
from pyppeteer import launch, connect

from product_parser import FEEDS, MODES
from output_writer import EXIT_API_ERROR
import page_flow
from proxy_pool import (from_args as proxy_pool_from_args, mask, ROTATE_MODES,
                        ProxyError, split_credentials)
import env_config

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("puppeteer_scraper")

# Every await in this file goes through the bridge below with a timeout, so a
# hung remote call ends the operation instead of the run. pyppeteer provides
# no connect timeout of its own.
DEFAULT_OP_TIMEOUT = 120
CONNECT_TIMEOUT = 30

# Chromium's own names for a proxy that could not be used.
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


class _AsyncBridge:
    """Runs pyppeteer's coroutines on a private event loop, synchronously.

    Lets this engine drive page_flow's synchronous fetch loop unchanged,
    and gives every call an explicit, enforced timeout: `.result(timeout)`
    returns control even when the browser never answers, which pyppeteer's
    own API does not offer.
    """

    def __init__(self):
        self.loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._serve, daemon=True,
                                        name="pyppeteer-loop")
        self._thread.start()

    def _serve(self):
        asyncio.set_event_loop(self.loop)
        self.loop.set_exception_handler(self._on_loop_exception)
        self.loop.run_forever()

    @staticmethod
    def _on_loop_exception(loop, context):
        # pyppeteer leaves CDP calls in flight when a browser closes, and the
        # loop then logs each as an ERROR after a successful run has printed
        # its results. Only that shape is swallowed; anything else still gets
        # the default handler.
        message = " | ".join(
            str(context.get(k)) for k in ("exception", "message")
            if context.get(k))
        if any(m in message for m in (
                "Target closed", "Connection closed",
                "Task was destroyed but it is pending",
                "Future exception was never retrieved",
                "No session with given id",
                # A rejected --cdp-endpoint handshake, raised by websockets in
                # a task pyppeteer never awaits, AFTER the connect has already
                # timed out and been reported with the reason.
                "server rejected WebSocket connection",
                "Event loop is closed")):
            logger.debug("Ignoring teardown noise from pyppeteer: %s", message)
            return
        loop.default_exception_handler(context)

    def run(self, coro, timeout: Optional[float] = DEFAULT_OP_TIMEOUT):
        future = asyncio.run_coroutine_threadsafe(coro, self.loop)
        try:
            return future.result(timeout)
        except concurrent.futures.TimeoutError:
            future.cancel()
            raise TimeoutError(
                f"pyppeteer call did not return within {timeout}s")

    def close(self):
        """Stop the loop, CANCELLING what it still has in flight, on the loop
        thread, so asyncio does not print a traceback per pending task after
        a successful run."""
        def _cancel_and_stop():
            pending = [t for t in asyncio.all_tasks(self.loop)
                       if t is not asyncio.current_task(self.loop)]
            for task in pending:
                task.cancel()
            self.loop.stop()

        self.loop.call_soon_threadsafe(_cancel_and_stop)
        self._thread.join(timeout=5)


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


async def _answer_proxy_auth(page, username: str, password: str) -> None:
    """Answer the proxy's 407 over CDP, for every request this page makes.

    pyppeteer's `page.authenticate()` is built on
    `Network.setRequestInterception`, which current Chromium no longer has, so
    it dies before the first navigation. `Fetch.enable` with
    `handleAuthRequests` is its replacement: every request pauses, is
    continued, and an auth challenge is answered with the credentials only
    when it comes from a PROXY (an origin server asking is not ours to
    answer). Slower, because each request is paused once.

    One coroutine for the whole set-up: pyppeteer's `CDPSession.send`
    returns a Future rather than a coroutine, and the bridge accepts only
    coroutines.
    """
    session = await page.target.createCDPSession()

    def _send(method, params):
        # Called from the loop thread by an event; the result is not awaited,
        # so a failure is logged rather than lost.
        fut = asyncio.ensure_future(session.send(method, params))
        fut.add_done_callback(
            lambda f: f.cancelled() or f.exception() is None or logger.debug(
                "CDP %s failed: %s", method, _mask_credentials(str(f.exception()))))

    def on_paused(event):
        _send("Fetch.continueRequest", {"requestId": event["requestId"]})

    def on_auth(event):
        challenge = event.get("authChallenge") or {}
        if challenge.get("source") == "Proxy":
            response = {"response": "ProvideCredentials",
                        "username": username, "password": password}
        else:
            response = {"response": "Default"}
        _send("Fetch.continueWithAuth", {"requestId": event["requestId"],
                                         "authChallengeResponse": response})

    session.on("Fetch.requestPaused", on_paused)
    session.on("Fetch.authRequired", on_auth)
    await session.send("Fetch.enable", {"handleAuthRequests": True,
                                        "patterns": [{"urlPattern": "*"}]})


class _Ops:
    """One pyppeteer browser + page, exposed as page_flow's named operations.

    Same contract as playwright_scraper._Ops, including the rule that a
    rotation means a genuinely FRESH browser.
    """

    def __init__(self, bridge: _AsyncBridge, args, pool):
        self.bridge, self.args, self.pool = bridge, args, pool
        self.remote = bool(args.cdp_endpoint)
        self.browser = self.page = None

    def open(self):
        if self.remote:
            logger.info("Connecting to an existing browser over CDP: %s",
                        _mask_credentials(self.args.cdp_endpoint))
            err = None
            for attempt in range(1, page_flow.CDP_CONNECT_ATTEMPTS + 1):
                try:
                    self.browser = self.bridge.run(
                        connect(browserWSEndpoint=self.args.cdp_endpoint,
                                ignoreHTTPSErrors=True),
                        timeout=page_flow.CDP_CONNECT_TIMEOUT_S)
                    err = None
                    break
                except Exception as e:  # noqa: BLE001 — see below
                    err = e
                    if (attempt < page_flow.CDP_CONNECT_ATTEMPTS
                            and page_flow.cdp_should_retry(str(e))):
                        logger.warning("The Scraping Browser profile did not "
                                       "accept the connection (attempt %d/%d) "
                                       "— it may still be locked by a previous "
                                       "run; retrying in %.0fs.", attempt,
                                       page_flow.CDP_CONNECT_ATTEMPTS,
                                       page_flow.CDP_LOCKED_WAIT_S)
                        time.sleep(page_flow.CDP_LOCKED_WAIT_S)
                        continue
                    break
            if err is not None:
                e = err
                # websockets' message ("server rejected WebSocket connection:
                # HTTP 500") names neither the endpoint nor the reason.
                # Re-raised masked, with the meaning spelled out, so
                # __main__ can map it onto exit 5.
                raise RuntimeError(
                    "could not connect to --cdp-endpoint %s: %s\n%s"
                    % (_mask_credentials(self.args.cdp_endpoint),
                       _mask_credentials(str(e)),
                       page_flow.cdp_connect_hint(str(e)))) from None
            self.page = self.bridge.run(self.browser.newPage())
            return self

        launch_args = ["--no-sandbox", "--disable-dev-shm-usage",
                       f"--lang={self.args.locale}"]
        launch_kwargs = {}
        if self.args.chromium_path:
            launch_kwargs["executablePath"] = self.args.chromium_path
        credentials = None
        if self.pool:
            exit_url = self.pool.current
            # Credentials are answered over CDP, never put on the command
            # line: --proxy-server= is part of the browser's argv, readable by
            # anything that can run `ps`.
            scrubbed, credentials = split_credentials(exit_url)
            launch_args.append(f"--proxy-server={scrubbed}")
            logger.info("Using proxy exit %s", mask(exit_url))
        # Signal handlers off: pyppeteer installs them inside launch(), and
        # `signal.signal` raises off the main thread, which is where this
        # event loop lives. close() handles teardown instead.
        self.browser = self.bridge.run(
            launch(headless=self.args.headless, args=launch_args,
                   ignoreHTTPSErrors=True, handleSIGINT=False,
                   handleSIGTERM=False, handleSIGHUP=False, **launch_kwargs),
            timeout=CONNECT_TIMEOUT * 2)
        self.page = self.bridge.run(self.browser.newPage())
        # No user agent is set, as in the Playwright twin: the browser's own is
        # the one whose TLS handshake and client hints agree with it, and a
        # bare CDP override leaves those reporting the real browser.
        # --fingerprint is the exception, and it brings a whole identity.
        if self.args.fingerprint:
            self._apply_fingerprint()
        if credentials:
            self.bridge.run(_answer_proxy_auth(self.page, *credentials),
                            timeout=30)
        return self

    def _apply_fingerprint(self):
        """The SAME init script the other two engines install, so no engine
        applies a different half of one fingerprint. The user agent is set
        only together with that script, which reads the client hints and
        platform out of the same fingerprint."""
        from fingerprint_client import (get_fingerprint, fingerprint_user_agent,
                                        playwright_init_script)
        fp = get_fingerprint(self.args.twocaptcha_key, tags=self.args.fp_tags,
                             country=self.args.fp_country)
        ua = fingerprint_user_agent(fp)
        try:
            if ua:
                self.bridge.run(self.page.setUserAgent(ua))
            self.bridge.run(
                self.page.evaluateOnNewDocument(playwright_init_script(fp)))
            logger.info("Using 2captcha fingerprint %s (%s)", fp.get("id"),
                        fp.get("country"))
        except Exception as e:  # noqa: BLE001 — a fingerprint is not the run
            logger.warning("Could not apply the fingerprint (%s) — continuing "
                           "without it.", _mask_credentials(str(e)))

    # ---- page_flow's operations -----------------------------------------

    def fetch(self, url: str):
        """Navigate to `url` and return (status, rendered html, error).

        The RENDERED document, not the raw bytes: what the parser sees is what
        a browser built, which is also what `--dump-html` writes. No
        JavaScript is evaluated in the page, so a site's CSP cannot get in the
        way; `domcontentloaded` is enough here because the data is in
        the markup.
        """
        nav_s = page_flow.NAV_TIMEOUT_MS / 1000.0
        try:
            resp = self.bridge.run(
                self.page.goto(url, waitUntil="domcontentloaded",
                               timeout=page_flow.NAV_TIMEOUT_MS),
                timeout=nav_s + 30)
        except Exception as e:  # noqa: BLE001 — pyppeteer raises several types
            return None, "", _mask_credentials(str(e) or type(e).__name__)
        status = resp.status if resp is not None else None
        try:
            html = self.bridge.run(self.page.content(), timeout=60)
        except Exception as e:  # noqa: BLE001
            return status, "", _mask_credentials(str(e) or type(e).__name__)
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
            self.bridge.run(self.browser.close(), timeout=30)
        except Exception as e:  # noqa: BLE001
            logger.debug("Ignoring error while closing browser: %s", e)
        self.open()

    def close(self):
        """Close the page, and on a REMOTE browser disconnect too: closing
        only the page leaves the websocket open, and its unwinding prints
        tracebacks after the output is written. The remote BROWSER is left
        running; it is not ours."""
        try:
            if self.remote:
                self.bridge.run(self.page.close(), timeout=30)
                self.bridge.run(self.browser.disconnect(), timeout=30)
            else:
                self.bridge.run(self.browser.close(), timeout=30)
        except Exception as e:  # noqa: BLE001
            logger.debug("Ignoring error during browser teardown: %s", e)


def _fetch_pages_concurrently(args, pool, query, page_nums, concurrency: int):
    """Fetch `page_nums` across `concurrency` workers. Each owns its own event
    loop, browser and exit; the page loop is page_flow.worker_loop."""
    work = queue.Queue()
    for n in page_nums:
        work.put(n)
    results, results_lock = [], threading.Lock()
    exhausted = threading.Event()

    def worker(index: int):
        name = f"worker-{index + 1}"
        bridge = _AsyncBridge()
        try:
            ops = _Ops(bridge, args, page_flow.worker_pool(pool, index)).open()
            try:
                page_flow.worker_loop(ops, args, query, work, results,
                                      results_lock, exhausted, name,
                                      _mask_credentials)
            finally:
                ops.close()
        except Exception:  # noqa: BLE001 — a dead worker must not hang the run
            logger.exception("[%s] died; its pages will be reported as failed.", name)
        finally:
            bridge.close()

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
    bridge = _AsyncBridge()
    try:
        return page_flow.run_pages(
            lambda: _Ops(bridge, args, pool).open(),
            lambda ops: ops.close(),
            lambda pages: _fetch_pages_concurrently(args, pool, args.query,
                                                    pages, concurrency),
            args, pool, args.query, concurrency, _mask_credentials)
    finally:
        bridge.close()


def parse_args(argv=None):
    p = argparse.ArgumentParser(
        description="news.ycombinator.com scraper — story lists, comment "
                    "trees and profiles (pyppeteer edition)")
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
    p.add_argument("--chromium-path", default=None, metavar="PATH",
                   help="Browser executable to drive, instead of the Chromium "
                        "pyppeteer downloads for itself. Needed where that "
                        "build will not start: on an Apple Silicon Mac "
                        "pyppeteer fetches an x86_64 Chromium that runs under "
                        "Rosetta far enough to print --version and then fails "
                        "to open its DevTools socket. Point it at a Chrome or "
                        "Chromium of your own — Playwright's, if installed.")
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
                       "Scraping Browser supplies its own fingerprint.")
    try:
        sys.exit(scrape(args))
    except ProxyError as e:
        logger.error("%s", e)
        sys.exit(2)
    except RuntimeError as e:
        # A remote browser refusing the connection is a REMOTE API failure
        # (exit 5), not a crash in this code (exit 1).
        text = _mask_credentials(str(e))
        if "connect to --cdp-endpoint" in text:
            logger.error("%s", text)
            sys.exit(EXIT_API_ERROR)
        raise
