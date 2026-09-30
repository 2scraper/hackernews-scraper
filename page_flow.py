"""
page_flow.py
------------
The retry / blocked decision, as DATA rather than as three copies of an
if-chain (CLAUDE.md §1) -- and the fetch loop itself,
written once.

news.ycombinator.com answers one of this repo's requests in five ways, and
they want different responses:

    a page with rows in it (stories, comments, a profile)  -> parse
    a served page with none: `No such item.`, one page past
      the end of a list, a `front` day it has no data for  -> parse, it is an
                                                             answer: exit 4
    HTTP 429 / 503, or its "not able to serve your
      requests this quickly" sentence                      -> wait, same exit
    HTTP 403                                               -> rotate
    anything else (a proxy interstitial, Chromium's own
      network-error page)                                  -> retry

Nothing here imports a browser, and **no JavaScript crosses this boundary**
(§1). Each engine reads the rendered document in its own driver's dialect,
and this is the only loop.

There is no challenge state and no solve step: none was met on any page this
repo reads (see product_parser.detect_bot_challenge), and a state nobody has
seen would be code that looks load-bearing and never runs (§4, §17).
"""

import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from output_writer import dedupe_by_key, finish_run, SOURCE_DEFAULT
from product_parser import (MAX_PAGES, DEFAULT_FEED, FEEDS,  # noqa: F401
                            Query, detect_bot_challenge, detect_page_state,
                            next_link_agrees, next_page_url, page_meta,
                            page_url, pagination_is_addressable, parse_page,
                            query_from_url, request_for)

log = logging.getLogger("page_flow")


# ---------------------------------------------------------------------------
# Timing
# ---------------------------------------------------------------------------

# The delay between two requests unless the user says otherwise.
#
# `robots.txt` on this site says `Crawl-delay: 30` for every agent (read
# 2026-09-30). That is a request, not a mechanism, and this repo chooses to
# honour it by default: a three-page run costs a minute, and nothing
# about reading a news site is urgent enough to spend the operator's
# goodwill on. It is not a measured limit. Requests 4 s apart (about fifty of
# them, including 1.6 MB comment pages) drew no throttle while this was
# built, so a smaller `--delay` is a decision the user can make with that
# number in front of them, and the README says so. The default is read by
# every engine's argparse, so the three cannot disagree.
DEFAULT_DELAY_S = 30.0

# How long one navigation may take. The largest page measured was 1.68 MB (a
# 1,095-comment thread), which arrived in well under ten seconds; the bound
# exists because §8 requires every remote call to have one.
NAV_TIMEOUT_MS = 60_000

# How long to wait at the SAME exit after a throttle before trying again.
# NOT OBSERVED: no request drew one while measuring. HN has said in public
# that it throttles by address and answers 503, so the response is to slow
# down rather than to rotate, and it needs its own budget (§24).
THROTTLE_WAIT_S = 30.0
THROTTLE_RETRIES = 2


# ---------------------------------------------------------------------------
# The policy
# ---------------------------------------------------------------------------

def classify(html: Optional[str], status: Optional[int] = None,
             url: str = "") -> str:
    """Name what the site answered with. See product_parser.detect_page_state.

    The argument ORDER is the contract: every engine calls
    `classify(html, status, url)`. A sibling repo shipped `classify(html,
    url=...)` in two of three engines against a callee that took `status`
    second, and both crashed on their first fetch (§17). `smoke_test.py`
    binds every engine's call against this signature for that reason.
    """
    return detect_page_state(html or "", status, url)


STATE_POLICY = {
    "content":   {"retry": False, "solve": False, "blocked": False, "parse": True},
    # Served, and nothing there. EXIT_NO_PRODUCTS rather than EXIT_BLOCKED:
    # reporting `No such item.` as blocked would send a reader hunting for a
    # proxy problem that does not exist.
    "empty":     {"retry": False, "solve": False, "blocked": False, "parse": True},
    # Rate limited. Retried at the same exit after a wait (THROTTLE_*), and
    # NOT counted as blocked: calling a throttle a block reports exit 3 for a
    # page that was about to come back (§24).
    "throttled": {"retry": True,  "solve": False, "blocked": False, "parse": False},
    # HTTP 403. NOT OBSERVED by this repo. Nothing to solve on this site, so
    # the only move is a different exit.
    "blocked":   {"retry": True,  "solve": False, "blocked": True,  "parse": False},
    # Not the site's page, or a page of its markup with neither rows nor its
    # own "nothing" answer. Worth one more try.
    "unknown":   {"retry": True,  "solve": False, "blocked": False, "parse": False},
}


def should_retry(state: str) -> bool:
    return STATE_POLICY.get(state, STATE_POLICY["unknown"])["retry"]


def counts_as_blocked(state: str) -> bool:
    return STATE_POLICY.get(state, STATE_POLICY["unknown"])["blocked"]


def should_parse(state: str) -> bool:
    return STATE_POLICY.get(state, STATE_POLICY["unknown"])["parse"]


# How many times to re-fetch a refused page when there is no proxy pool to
# rotate into. One: a refusal is about the address, and a second request from
# the same one is a second identical answer. WITH a pool the engines retry
# once per remaining exit instead, because there the retry changes the
# variable the refusal depends on.
BLOCK_RETRIES_WITHOUT_POOL = 1


# ---------------------------------------------------------------------------
# Pagination
# ---------------------------------------------------------------------------

def concurrency_limit(cdp_endpoint: Optional[str]) -> Optional[int]:
    """1 when workers would collide, else None for "no limit imposed here".

    The Scraping Browser API allows ONE live connection per profile, so N
    workers sharing a `pid` collide with `profile_locked`. Several `pid`s,
    one run each, is the way to parallelise that path (§7).
    """
    return 1 if cdp_endpoint else None


def refusal_name(state: str) -> str:
    """The name a refusal is reported by, in logs and in `stop_reason`."""
    return {"blocked": "http-403"}.get(state, state)


def refusal_advice(state: str) -> str:
    """One sentence on what changes the answer. Kept here so the three
    engines cannot give three different pieces of advice."""
    return ("The site refused this address (HTTP 403). HN has not refused "
            "any address this repo used, so this is NOT a state it has met; "
            "a different exit is what changes an address refusal: --proxy / "
            "--proxy-file, or --cdp-endpoint.")


def stop_reason_for(outcome) -> str:
    """The run's stop_reason when `outcome` is the page that ended it."""
    if getattr(outcome, "blocked_by", None):
        return "blocked_%s" % outcome.blocked_by
    if getattr(outcome, "state", None) == "throttled":
        return "throttled"
    return "page_load_timeout"


# ---------------------------------------------------------------------------
# The query, and the end of a run
# ---------------------------------------------------------------------------

# The query flags, by the argparse dest they land in. A flag left at None was
# not typed, which is how build_query tells a user's value from a default.
QUERY_FLAGS = (("feed", "--feed"), ("day", "--day"), ("by", "--by"),
               ("site", "--site"), ("item", "--item"), ("user", "--user"))


def build_query(args, error: Callable[[str], None]) -> Query:
    """The Query a run sends, from --url or from the flags, validated.

    --url and the query flags are two ways to say the same thing. A flag the
    user typed that disagrees with the URL would silently scrape something
    neither of them named, so the combination is refused rather than merged
    (the family's --country rule, §10). `error` is argparse's `p.error`, so
    a refusal is exit 2 with the usage line, as in every engine.
    """
    if args.pages < 1:
        error("--pages must be at least 1")
    if args.url:
        query, why = query_from_url(args.url)
        if query is None:
            error(why)
        if args.mode and args.mode != query.mode:
            error("--mode %s disagrees with --url, which is a %s page."
                  % (args.mode, query.mode))
        typed = [flag for dest, flag in QUERY_FLAGS
                 if getattr(args, dest, None) is not None]
        if typed:
            error("--url already carries the query; %s would have to agree "
                  "with it and nothing checks that they do. Pass a URL or "
                  "the flags, not both." % ", ".join(typed))
    else:
        mode = args.mode
        if mode is None:
            mode = "item" if args.item else "user" if args.user else "listing"
        stray = {"listing": ("item", "user"), "item": ("feed", "day", "by",
                 "site", "user"), "user": ("feed", "day", "by", "site", "item")}
        wrong = [flag for dest, flag in QUERY_FLAGS
                 if dest in stray[mode] and getattr(args, dest, None) is not None]
        if wrong:
            error("%s does not apply to --mode %s." % (", ".join(wrong), mode))
        query = Query(mode=mode, feed=args.feed, day=args.day, by=args.by,
                      site=args.site, item_id=args.item, username=args.user)
        if mode == "listing" and not (query.feed or query.by or query.site):
            query.feed = DEFAULT_FEED
        why = query.validate()
        if why:
            error(why)
    return query


def query_summary(query: Query) -> dict:
    """The query as the sidecar records it: only the fields its mode uses."""
    if query.mode == "item":
        return {"item_id": query.item_id}
    if query.mode == "user":
        return {"username": query.username}
    out: Dict[str, Any] = {"path": query.path}
    for k, v in (("feed", query.feed), ("day", query.day), ("by", query.by),
                 ("site", query.site)):
        if v:
            out[k] = v
    if query.start_page != 1:
        out["start_page"] = query.start_page
    return out


def finish(args, query: Query, outcomes: List, stop_reason: str,
           blocked: bool, extra: Optional[dict] = None) -> int:
    """Merge the pages in PAGE order, write the output, return the exit code.

    One implementation for the three engines, so the merge order, the
    dedupe and the sidecar cannot differ between them (§6).
    """
    rows, seen = [], set()
    for oc in sorted(outcomes, key=lambda o: o.page_num):
        fresh = dedupe_by_key(oc.products, seen, key="sku")
        if len(fresh) < len(oc.products):
            log.info("Page %d: dropped %d duplicate row(s) — the live list "
                     "moved between page fetches.", oc.page_num,
                     len(oc.products) - len(fresh))
        rows.extend(fresh)

    ok_pages = [o for o in outcomes if o.ok]
    failed_pages = sorted(o.page_num for o in outcomes if not o.ok)
    first = next((o for o in outcomes if o.page_num == 1), None)
    meta = dict(getattr(first, "meta", None) or {})
    reported = meta.get("comments")
    if query.mode == "item" and rows and reported is not None:
        shown = sum(1 for r in rows if getattr(r, "status", None) == "ok")
        log.info("The page says %d comment(s); this run holds %d row(s), %d of "
                 "them status ok. (Measured on a 1,100-row thread: the site's "
                 "count equalled the ok rows and left out the 5 flagged ones.)",
                 reported, len(rows), shown)
    last_ok = max([o.page_num for o in ok_pages] or [1])
    last_url = next((o.url for o in outcomes if o.page_num == last_ok), None)
    sidecar = {"query": query_summary(query)}
    if meta:
        sidecar["item"] = meta
    if first is not None and first.next_agrees is not None:
        sidecar["addressable"] = bool(first.next_agrees)
    sidecar.update(extra or {})
    return finish_run(
        rows, args.out, args.format, args.allow_empty,
        blocked=blocked, stop_reason=stop_reason,
        pages_requested=args.pages, pages_completed=len(ok_pages),
        pages_failed=failed_pages, mode=query.mode, source=SOURCE_DEFAULT,
        start_url=query.url(), final_url=last_url or query.url(),
        extra=sidecar)


# ---------------------------------------------------------------------------
# The fetch loop, driven through named operations
# ---------------------------------------------------------------------------
#
# Everything about fetching one page lives here, once: retrying a transport
# failure, waiting out a throttle, rotating on a refusal, and parsing what
# came back. The three engines differ only in HOW they ask their driver, so
# each passes in an object with these operations and no JavaScript crosses
# this boundary (§1):
#
#     ops.fetch(url)          -> (status, html, error_or_None). Navigates to
#                                `url` and returns the RENDERED document.
#                                error is a transport failure's text.
#     ops.wait_ms(ms)
#     ops.relaunch()          -> a fresh browser (on the pool's current exit)
#     ops.proxy_failure(text) -> the driver's proxy-error name in text, or ""
#     ops.pool                -> the ProxyPool this worker owns, or None
#
# One copy is how "the three must agree on exit codes and on whether a run
# spends money" (§6) holds by construction.

class TransportError(Exception):
    """A navigation that did not complete: a timeout, a dead proxy."""


@dataclass
class PageOutcome:
    """What one page produced.

    Collected per page and merged afterwards, in page order, rather than
    folded into shared state as the loop goes, so the output cannot depend
    on which page happened to finish first (§8).
    """
    page_num: int
    url: str
    products: List = field(default_factory=list)
    blocked_by: Optional[str] = None
    load_failed: bool = False
    # The state the page came back as. Carried so the caller can tell an
    # EMPTY page (the end of a list) from a failed one: both hold zero rows
    # and they mean opposite things.
    state: Optional[str] = None
    # The site's own "More" link, absolute, or None. Where a cursor list's
    # next page is only knowable from (§7).
    next_url: Optional[str] = None
    # Whether that link is exactly what `?p=N` would build: True means the
    # list can be split across workers, False means it must be walked, None
    # means there was no link (a one-page list).
    next_agrees: Optional[bool] = None
    # Facts about the page that are not rows (the item at the top of a thread).
    meta: Dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.load_failed and self.blocked_by is None


# The columns the site filled on EVERY row of every capture, per mode. Below
# this share the markup has moved rather than the data being unusual.
# Deliberately NOT here: `author` and `text` on a Comment (null on a deleted
# one and a placeholder on a flagged one, by design) and `points`, `author`
# and `comments` on a Story (null on a job posting).
CORE_FIELD_FLOOR = 99
CORE_FIELDS = {
    "listing": ("sku", "title", "url", "posted_at", "hn_url"),
    "item": ("sku", "story_id", "parent_id", "posted_at", "depth"),
    "user": ("sku", "created_at", "karma"),
}


def _core_field_warnings(rows: List, mode: str, page_num: int) -> None:
    for name in CORE_FIELDS.get(mode, ()):
        if not rows:
            return
        filled = sum(1 for r in rows if getattr(r, name, None) not in (None, "", []))
        share = 100.0 * filled / len(rows)
        if share < CORE_FIELD_FLOOR:
            log.warning("Only %.0f%% of page %d carries `%s`, against a "
                        "measured floor of %d%%. Every row of every capture "
                        "had one, so the markup has moved — re-run with "
                        "--dump-html.", share, page_num, name, CORE_FIELD_FLOOR)


def _dump(args, page_num: int, text: str) -> None:
    """Write the exact document the parser was given, on success too (§9)."""
    if not args.dump_html:
        return
    path = args.dump_html if args.pages == 1 else f"{args.dump_html}.page{page_num}"
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    log.info("Saved the document the parser sees to %s (%d bytes).",
             path, len(text))


def _save_debug(args, page_num: int, text: str) -> str:
    path = f"{args.out}_page{page_num}_debug.html"
    with open(path, "w", encoding="utf-8") as f:
        f.write(text or "")
    return path


def fetch_one_page(ops, args, pool, query: Query, page_num: int,
                   mask: Callable[[str], str] = lambda s: s,
                   url: Optional[str] = None) -> PageOutcome:
    """Fetch and parse one page. Retries, rotations and debug dumps live here.

    `url` is the address to fetch when the caller knows it better than the
    page number does: a cursor list's page 2 is the address in page 1's own
    More link. Otherwise it is built from `page_num`.

    Never raises for an EXPECTED failure. A timeout, a refusal and a dead
    exit are all recorded on the outcome, because what the run should do
    about them differs between the sequential and the concurrent paths.
    """
    req = request_for(query, page_num, url)
    outcome = PageOutcome(page_num=page_num, url=req.label)

    has_pool = bool(pool and len(pool) > 1)
    block_retries = (args.proxy_block_retries if has_pool
                     else BLOCK_RETRIES_WITHOUT_POOL)
    throttles = 0
    state, text, last_error, exit_failed = "unknown", "", None, None

    for block_attempt in range(block_retries + 1):
        log.info("Fetching page %d/%d: %s", page_num, args.pages, req.label)
        exit_failed = None
        attempt = 0
        while attempt < args.retries:
            attempt += 1
            status, text, last_error = ops.fetch(req.url)
            if last_error:
                state = "load_failed"
                exit_failed = ops.proxy_failure(last_error) or None
                if exit_failed:
                    break  # a different exit is the only thing that helps
            else:
                state = classify(text, status, req.url)
            if state == "throttled" and throttles < THROTTLE_RETRIES:
                throttles += 1
                attempt -= 1  # a throttle wait spends its own budget (§24)
                pause = THROTTLE_WAIT_S * throttles
                log.warning("Rate-limited on page %d (HTTP 429/503) — waiting "
                            "%.0fs at the same exit (%d/%d).", page_num, pause,
                            throttles, THROTTLE_RETRIES)
                ops.wait_ms(int(pause * 1000))
                continue
            if state in ("load_failed", "unknown") and attempt < args.retries:
                pause = args.retry_delay * (2 ** (attempt - 1))
                log.warning("Page %d came back %s (attempt %d/%d)%s — retrying "
                            "in %.1fs.", page_num, state, attempt, args.retries,
                            f": {mask(last_error)}" if last_error else "", pause)
                ops.wait_ms(int(pause * 1000))
                continue
            break

        if exit_failed and has_pool and block_attempt < block_retries:
            log.warning("Exit %s is unusable (%s) — rotating to another one "
                        "(%d/%d).", mask(pool.current), exit_failed,
                        block_attempt + 1, block_retries)
            pool.advance(f"unusable exit: {exit_failed}")
            ops.relaunch()
            continue
        if (counts_as_blocked(state) and should_retry(state)
                and block_attempt < block_retries):
            if has_pool:
                log.warning("Page %d refused (%s) at %s — rotating to another "
                            "exit (%d/%d).", page_num, refusal_name(state),
                            mask(pool.current), block_attempt + 1, block_retries)
                pool.advance(f"refused: {refusal_name(state)}")
            else:
                log.warning("Page %d refused (%s) — re-fetching once from a "
                            "fresh browser.", page_num, refusal_name(state))
            ops.relaunch()
            continue
        break

    outcome.state = state
    if state == "load_failed" or exit_failed:
        outcome.load_failed = True
        log.error("Gave up on page %d: %s", page_num,
                  mask(last_error or "the request never completed"))
        return outcome
    if counts_as_blocked(state):
        outcome.blocked_by = refusal_name(state)
        debug = _save_debug(args, page_num, text)
        log.error("Blocked by %s on page %d — saved to %s. This is exit 3, "
                  "distinct from an empty result (exit 4). %s",
                  outcome.blocked_by, page_num, debug, refusal_advice(state))
        return outcome
    if not should_parse(state):
        # throttled past its budget, or never the site's page at all
        outcome.load_failed = True
        debug = _save_debug(args, page_num, text)
        log.error("Page %d never came back as the site's page (%s) — saved "
                  "to %s. %s", page_num, state, debug,
                  "Raise --delay, or spread the run over --proxy-file."
                  if state == "throttled" else "")
        return outcome

    _dump(args, page_num, text)
    site_page = query.start_page + page_num - 1
    rows = parse_page(text, query, site_page)
    outcome.products = rows
    outcome.next_url = next_page_url(text, req.url) if query.mode == "listing" else None
    if page_num == 1 and query.mode == "listing":
        outcome.next_agrees = next_link_agrees(text, req.url)
    outcome.meta = page_meta(text, query)
    if state == "empty" and not rows:
        log.info("Page %d: the site answered with nothing to list (%s).",
                 page_num, _empty_reason(text))
    else:
        log.info("Parsed %d row(s) from page %d.", len(rows), page_num)
    if state == "content" and not rows:
        # A page the site served, with its rows on it, that parses to none is
        # THIS repo's bug rather than an empty answer (§20).
        log.warning("Page %d was served with rows on it and parsed to none — "
                    "the markup has probably changed. Re-run with --dump-html "
                    "and open an issue.", page_num)
    _core_field_warnings(rows, query.mode, page_num)
    return outcome


def _empty_reason(text: str) -> str:
    from product_parser import _text_of
    short = _text_of(text) if len(text or "") < 600 else ""
    return short or "a served page with no rows"


def run_pages(open_ops, close_ops, run_concurrently, args, pool,
              query: Query, concurrency: int,
              mask: Callable[[str], str] = lambda s: s) -> int:
    """The whole run after argument handling, shared by the three engines.

    `open_ops()` returns a ready ops object on `pool`, `close_ops(ops)`
    tears it down, and `run_concurrently(page_nums)` returns
    (outcomes, unattempted, exhausted) for pages 2..N fetched by workers. The
    engines supply those three because a browser's lifecycle (and on
    Playwright, its thread) is the one thing that cannot be shared.
    """
    outcomes: List[PageOutcome] = []
    blocked, stop_reason = False, "completed"
    ops = open_ops()
    try:
        # Page 1 is always fetched alone: whether the site's own link to page
        # 2 agrees with `?p=2` decides whether the rest can be split up (§7).
        first = fetch_one_page(ops, args, pool, query, 1, mask)
        outcomes.append(first)
        if not first.ok:
            stop_reason = stop_reason_for(first)
            blocked = first.blocked_by is not None
        elif query.mode != "listing":
            # An item page holds every comment and a profile is one row: there
            # is no page 2 to ask for, whatever --pages says.
            stop_reason = "single_page_mode"
            if args.pages > 1:
                log.info("--pages %d ignored: --mode %s is one page (an item "
                         "page carries its whole comment tree).",
                         args.pages, query.mode)
        elif not first.products:
            stop_reason = "end_of_listing"
        elif first.next_url is None:
            if args.pages > 1:
                log.info("The site links no next page from page 1 — this "
                         "list is one page, so the run is complete.")
            stop_reason = "end_of_listing" if args.pages > 1 else "completed"
        elif args.pages > 1:
            plan = min(args.pages, MAX_PAGES)
            splittable = first.next_agrees is True
            if concurrency > 1 and not splittable:
                log.warning("--concurrency %d ignored: this list's More link "
                            "carries a cursor (?next=…) that ?p=N cannot "
                            "reproduce, so page 2's address is only known "
                            "from page 1. Walking it link to link.",
                            concurrency)
            if concurrency > 1 and splittable:
                close_ops(ops)
                ops = None
                rest = list(range(2, plan + 1))
                log.info("Fetching pages 2-%d across %d workers%s.", plan,
                         concurrency, f" over {len(pool)} exit(s)" if pool else "")
                more, unattempted, exhausted = run_concurrently(rest)
                outcomes.extend(more)
                failed = [o for o in more if not o.ok]
                if failed:
                    stop_reason = stop_reason_for(min(failed, key=lambda o: o.page_num))
                    blocked = any(o.blocked_by for o in more)
                elif exhausted:
                    stop_reason = "end_of_listing"
                elif unattempted:
                    stop_reason = "pages_unattempted"
            else:
                previous = first
                for page_num in range(2, plan + 1):
                    ops.wait_ms(int(args.delay * 1000))
                    if pool and pool.rotates_per_page():
                        pool.advance(f"per-page rotation, page {page_num}")
                        ops.relaunch()
                    # The site's own link is the address, always: correct for
                    # a `?p=N` list and the only way for a cursor one (§7).
                    outcome = fetch_one_page(ops, args, pool, query, page_num,
                                             mask, url=previous.next_url)
                    outcomes.append(outcome)
                    if not outcome.ok:
                        stop_reason = stop_reason_for(outcome)
                        blocked = outcome.blocked_by is not None
                        break
                    if not outcome.products:
                        log.info("Page %d came back empty — the list ended.",
                                 page_num)
                        stop_reason = "end_of_listing"
                        break
                    seen_before = {r.sku for o in outcomes[:-1] for r in o.products}
                    if all(r.sku in seen_before for r in outcome.products):
                        # The DATA says the list is over, not a selector: a
                        # page that adds nothing new is a re-served page (§7).
                        log.info("Page %d added no new story — treating the "
                                 "list as ended.", page_num)
                        stop_reason = "no_new_products"
                        break
                    if outcome.next_url is None:
                        log.info("Page %d has no More link — the list ends "
                                 "here.", page_num)
                        stop_reason = "end_of_listing"
                        break
                    previous = outcome
    finally:
        if ops is not None:
            close_ops(ops)
    return finish(args, query, outcomes, stop_reason, blocked)


def worker_loop(ops, args, query: Query, work, results, results_lock,
                exhausted, name: str, mask: Callable[[str], str] = lambda s: s):
    """One concurrent worker's page loop, after its engine opened `ops`.

    Takes pages until the queue is empty or a page comes back empty (the
    list ended before the plan did), which sets `exhausted` so the other
    workers stop taking work too. Only reached for a `?p=N` list, whose page
    addresses are independent; a cursor list is walked by run_pages.
    """
    first = True
    while not exhausted.is_set():
        try:
            page_num = work.get_nowait()
        except Exception:  # queue.Empty
            break
        if not first:
            ops.wait_ms(int(args.delay * 1000))
        first = False
        outcome = fetch_one_page(ops, args, ops.pool, query, page_num, mask)
        with results_lock:
            results.append(outcome)
        if outcome.ok and not outcome.products:
            log.info("[%s] page %d returned no rows — the list ended; "
                     "stopping dispatch.", name, page_num)
            exhausted.set()


def concurrency_for(args, pool) -> int:
    """How many workers this run may use, with the warnings said once for
    all three engines."""
    concurrency = max(1, args.concurrency)
    if concurrency <= 1:
        return 1
    if concurrency_limit(args.cdp_endpoint) == 1:
        log.warning("--concurrency is ignored with --cdp-endpoint: the Scraping "
                    "Browser API allows one live connection per profile, and "
                    "several workers would collide on it (profile_locked). Use "
                    "several pids instead.")
        return 1
    if not pool:
        log.warning("--concurrency %d with no proxy pool: every worker leaves "
                    "from the SAME address, so the request rate is %d times "
                    "what robots.txt's Crawl-delay: 30 asks for, and HN "
                    "throttles by address. Pass --proxy-file to spread the "
                    "load, or leave --concurrency at 1.",
                    concurrency, concurrency)
    if concurrency > 8:
        log.warning("--concurrency %d means %d browsers at once (~150-300MB "
                    "each).", concurrency, concurrency)
    return concurrency


def worker_pool(pool, worker_index: int):
    """A private ProxyPool for one worker, starting at a different exit.

    Workers start on distinct exits and share no mutable state, so rotation
    needs no lock (§7).
    """
    if not pool:
        return None
    from proxy_pool import ProxyPool
    proxies = pool.proxies
    offset = worker_index % len(proxies)
    return ProxyPool(proxies[offset:] + proxies[:offset], rotate="per-run")


def cdp_connect_hint(error_text: str) -> str:
    """What a failed --cdp-endpoint connection means, from its status.

    Two answers that want opposite fixes. Measured 2026-09-24 against four
    Scraping Browser endpoints left in sibling repos' .env files: all four
    answered 401 Unauthorized, because a profile's credentials last about a
    day. The message used to explain a 500 (a pid another run still holds)
    whatever the status was, which sent the reader to wait for a run that
    did not exist.
    """
    if "401" in (error_text or ""):
        return ("HTTP 401: the endpoint's credentials were refused. A Scraping "
                "Browser profile's credentials last about a day, so an "
                "endpoint copied from an older .env has usually expired. "
                "Get a fresh one from your 2Captcha dashboard.")
    return ("A Scraping Browser profile allows ONE live connection at a time, "
            "so an HTTP 500 here usually means another run still holds this "
            "`pid`. Wait for it to finish, or use a different pid.")


# Connecting to a Scraping Browser profile right after the previous run let
# go of it answers HTTP 500 `profile_locked`: the service releases a profile
# 1.6-1.9 s after a clean disconnect (measured 3 of 3, 2026-09-24). Two
# back-to-back runs therefore failed with exit 5 in the first live matrix
# through --cdp-endpoint. Three attempts 3 s apart ride that out, and a
# profile genuinely held by another run still fails, after ~9 s, with the
# pid explanation.
CDP_CONNECT_ATTEMPTS = 3
CDP_LOCKED_WAIT_S = 3.0
# pyppeteer does not surface the 500 at all: its connect() waits on a future
# the rejected handshake never resolves, so only a timeout ends it. A
# successful connect measured 0.8-0.95 s, so 10 s is an order of magnitude
# of headroom and a third of the 30 s it used to wait per attempt.
CDP_CONNECT_TIMEOUT_S = 10


def cdp_should_retry(error_text: str) -> bool:
    """Whether a failed --cdp-endpoint connection is worth another attempt:
    a locked profile (500) or a connect that never answered. A 401 is not:
    expired credentials stay expired."""
    text = error_text or ""
    if "401" in text:
        return False
    return ("profile_locked" in text or " 500" in text or "HTTP 500" in text
            or "did not return within" in text)
