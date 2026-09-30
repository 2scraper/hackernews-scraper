#!/usr/bin/env python3
"""
smoke_test.py — the offline suite for hackernews-scraper.

One file of plain functions. `tests/test_smoke.py` wraps it as a single
pytest test so `pytest` works as an entry point without a second copy of the
checks.

    python3 smoke_test.py            run everything
    python3 smoke_test.py -v         print every check as it passes

It must pass with NO engine library installed at all: every
`import playwright_scraper` / `selenium_scraper` / `puppeteer_scraper` is
guarded and the skip is RECORDED, because "skipped, engine absent" reads
identically to a real import error. CI installs each engine in its own venv
and fails if that engine's group reports a skip.

THE FIXTURES ARE IN `fixtures_generated.json`, NOT INLINE. They are real
pages captured 2026-09-30, trimmed and scrubbed by `make_fixtures.py`, which
proves each one parses the way its untrimmed original did. Not verbatim:
usernames, comment bodies, the profile `about` box, `auth=` tokens and forms
are replaced or removed (see make_fixtures.py for why each). A handful of
tiny SYNTHETIC snippets appear where a check needs one specific shape; each is
marked as such at its use.
"""

import argparse
import ast
import copy
import csv
import inspect
import json
import logging
import os
import re
import stat
import subprocess
import sys
import tempfile
import threading
import types
from dataclasses import asdict, fields

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

FAILURES = []
PASSED = 0
SKIPS = []
VERBOSE = False


def check(name, condition, detail=""):
    global PASSED
    if condition:
        PASSED += 1
        if VERBOSE:
            print("  ok   %s" % name)
    else:
        FAILURES.append("%s%s" % (name, (" — " + detail) if detail else ""))
        print("  FAIL %s%s" % (name, (" — " + detail) if detail else ""))


def equal(name, got, want):
    check(name, got == want, "got %r, want %r" % (got, want))


def skip(group, reason):
    SKIPS.append("%s: %s" % (group, reason))
    print("  SKIP %s — %s" % (group, reason))


FIXTURES_PATH = os.path.join(HERE, "fixtures_generated.json")
FIXTURES = json.load(open(FIXTURES_PATH, encoding="utf-8"))

H = "https://news.ycombinator.com/"


def fx(name) -> str:
    """A fixture as the text the site returned."""
    return FIXTURES[name]


ENGINES = ("playwright_scraper", "selenium_scraper", "puppeteer_scraper")
DRIVER_IMPORTS = {"playwright_scraper": "playwright",
                  "selenium_scraper": "selenium",
                  "puppeteer_scraper": "pyppeteer"}


def _import_engine(name):
    try:
        return __import__(name)
    except ImportError as e:
        skip(name, "engine library absent (%s)" % e)
        return None


def _sig(rows):
    """The columns of a row that are the SITE's, not the run's."""
    return [(getattr(r, "sku", None), getattr(r, "title", None), getattr(r, "url", None),
             getattr(r, "author", None), getattr(r, "kind", None),
             getattr(r, "rank", None), getattr(r, "position", None),
             getattr(r, "domain", None), getattr(r, "posted_at", None)) for r in rows]


# ---------------------------------------------------------------------------
# The fixtures themselves
# ---------------------------------------------------------------------------

def check_fixture_corpus_is_real_and_scrubbed():
    expected = {"news", "news2", "newest", "ask", "jobs", "submitted", "from",
                "front", "show", "best", "active", "p50", "nouser", "itemabc",
                "futureday", "oldday", "item_top", "ask_item", "comment_item",
                "big", "user", "rendered_news", "rendered_newest",
                "rendered_jobs", "rendered_item_top", "rendered_user",
                "rendered_nouser", "chromium_proxy_error"}
    missing = expected - set(FIXTURES)
    check("every fixture the suite uses is in fixtures_generated.json",
          not missing, "missing %s" % sorted(missing))
    blob = json.dumps(FIXTURES)
    check("the corpus is not empty (a scan of nothing passes for the wrong reason)",
          len(blob) > 200000, "%d bytes" % len(blob))
    check("no session token survived the scrub: no auth= parameter",
          not re.search(r"auth=[0-9a-f]{8}", blob))
    check("...and no form, so no hmac input", "<form" not in blob and 'name=\\"hmac\\"' not in blob
          and 'name="hmac"' not in blob)
    check("no 32-hex string survived (the family's credential scan shape)",
          not re.search(r"\b[0-9a-f]{32}\b", blob))
    check("the usernames are placeholders (no handle from the captures)",
          "bryan0" not in blob and "user_a" in blob)
    check("the comment bodies are placeholders",
          "Fixture comment text." in blob and "Nerf Bench" not in blob)
    check("the site's own [flagged] word is kept (it is the site's, not a person's)",
          "[flagged]" in fx("big"))
    check("the fixtures say which are not verbatim", "make_fixtures.py" in
          open(os.path.join(HERE, "make_fixtures.py"), encoding="utf-8").read())


def check_no_challenge_vocabulary_on_any_served_page():
    """The README says no page this repo reads has shown a captcha or a
    challenge, and this is that claim as a count over the served fixtures.
    Comment bodies are placeholders, so what is counted is the site's chrome."""
    # Vendor NAMES that are also headline words (a story about Cloudflare, a
    # thread about Akamai) are deliberately not here: the count is over what
    # only a challenge would carry.
    words = ("captcha", "recaptcha", "hcaptcha", "turnstile", "cf-chl", "px-captcha",
             "datadome", "perimeterx", "challenge-platform", "just a moment",
             "are you a human", "verify you are")
    served = [n for n in FIXTURES if n in (
        "news", "news2", "newest", "ask", "jobs", "submitted", "from", "front", "show",
        "best", "active", "item_top", "ask_item", "comment_item", "big", "user",
        "rendered_news", "rendered_newest", "rendered_jobs", "rendered_item_top")]
    hits = {}
    for n in served:
        low = FIXTURES[n].lower()
        for w in words:
            if w in low:
                hits.setdefault(n, []).append(w)
    check("the served-page count is not vacuous", len(served) >= 15, str(len(served)))
    check("0 challenge/captcha words on any served page", not hits, repr(hits))
    import product_parser as P
    check("detect_bot_challenge matches nothing on any of them (nothing to name)",
          all(P.detect_bot_challenge(FIXTURES[n]) is None for n in served))


# ---------------------------------------------------------------------------
# The parser, asserted on VALUES rather than on coverage (§10)
# ---------------------------------------------------------------------------

def check_front_page_parses_to_the_captured_values():
    import product_parser as P
    rows = P.parse_listing(fx("news"), page=1)
    equal("30 stories on the front page", len(rows), 30)
    r = rows[0]
    equal("row 1: id, title, link", (r.sku, r.title, r.url),
          ("49901736", "Livenerf: Has Opus 5.5 been nerfed yet?",
           "https://github.com/ninjahawk/livenerf"))
    equal("row 1: points and comments as integers", (r.points, r.comments), (536, 230))
    equal("row 1: the moment is the site's stamp, ISO, UTC", r.posted_at,
          "2026-09-29T22:36:14Z")
    equal("row 1: domain as the site prints it (with the path)", r.domain,
          "github.com/ninjahawk")
    equal("row 1: kind, rank, page, position", (r.kind, r.rank, r.page, r.position),
          ("story", 1, 1, 1))
    equal("row 1: the discussion address is always on HN", r.hn_url,
          H + "item?id=49901736")
    equal("row 1: the author is a placeholder", r.author, "user_a")
    equal("row 1: source", r.source, "news.ycombinator.com")
    last = rows[-1]
    equal("row 30", (last.sku, last.rank, last.kind, last.title),
          ("49896757", 30, "show", "Show HN: A working 3D model of an Enigma machine"))
    equal("kinds on the page", sorted({r.kind for r in rows}), ["ask", "show", "story"])
    ask = [r for r in rows if r.kind == "ask"][0]
    equal("an Ask HN row points at its own item page", ask.url, ask.hn_url)
    equal("...and has no domain (there is no link to shorten)", ask.domain, None)
    check("every row is stamped and has a title", all(r.posted_at and r.title for r in rows))


def check_rank_continues_across_pages_and_page_position_is_unique():
    import product_parser as P
    p1 = P.parse_listing(fx("news"), page=1)
    p2 = P.parse_listing(fx("news2"), page=2)
    equal("page 2 starts at rank 31 and ends at 60",
          (p2[0].rank, p2[-1].rank), (31, 60))
    equal("position restarts at 1 on every page", (p1[0].position, p2[0].position), (1, 1))
    keys = [(r.page, r.position) for r in p1 + p2]
    equal("page + position is unique across a two-page run (§18)", len(set(keys)), 60)
    equal("no story id repeats across the two pages",
          len({r.sku for r in p1 + p2}), 60)


def check_a_job_has_no_author_points_or_comments_and_that_is_null_not_zero():
    import product_parser as P
    rows = P.parse_listing(fx("jobs"), page=1)
    r = rows[0]
    equal("a job: kind", r.kind, "job")
    equal("a job: author, points and comments are NULL", (r.author, r.points, r.comments),
          (None, None, None))
    check("...on every row of the jobs page",
          all(x.kind == "job" and x.points is None and x.comments is None for x in rows))
    check("a job still has its title, link and stamp",
          r.title and r.url.startswith("https://") and r.posted_at == "2026-09-24T18:29:18Z")


def check_discuss_is_zero_comments_and_one_comment_is_one():
    import product_parser as P
    rows = P.parse_listing(fx("newest"), page=1)
    zero = [r for r in rows if r.comments == 0]
    check("/newest holds many stories nobody has commented on", len(zero) >= 10, str(len(zero)))
    z = zero[0]
    equal("a `discuss` row: zero comments, one point",
          (z.sku, z.points, z.comments), ("49905773", 1, 0))
    check("...and `1 comment` is 1, not 0",
          any(r.comments == 1 for r in rows + P.parse_listing(fx("news2"), page=2)))
    check("no row lost its comment count while the page has the link",
          all(r.comments is not None for r in rows))


def check_kinds_are_the_sites_own_words():
    import product_parser as P
    equal("the Ask page: ask or text posts, every row pointing at its own page",
          sorted({r.kind for r in P.parse_listing(fx("ask"), page=1)}), ["ask", "text"])
    check("...and only 18 rows: the site serves fewer there",
          len(P.parse_listing(fx("ask"), page=1)) == 18)
    equal("the Show page is all `Show HN`",
          {r.kind for r in P.parse_listing(fx("show"), page=1)}, {"show"})
    # SYNTHETIC: one row is enough to pin the two rules the fixtures cannot.
    row_html = ("<table><tr class='athing submission' id='7'><td><span class='rank'>1.</span></td>"
                "<td class='title'><span class='titleline'><a href='item?id=7'>%s</a></span></td></tr>"
                "<tr><td class='subtext'><span class='score'>3 points</span> by "
                "<a class='hnuser'>x</a> <span class='age' title='2026-01-01T00:00:00'></span> "
                "<a href='item?id=7'>discuss</a></td></tr></table>")
    (r,) = P.parse_listing(row_html % "Tell HN: something", page=1)
    equal("synthetic: a `Tell HN` self post is `text`", r.kind, "text")
    (r,) = P.parse_listing(row_html % "ASK hn: shouting", page=1)
    equal("synthetic: the prefix match ignores case", r.kind, "ask")


def check_from_and_submitted_lists_print_no_rank():
    import product_parser as P
    rows = P.parse_listing(fx("from"), page=1)
    equal("/from prints no rank, so it is null on every row (not a guess)",
          {r.rank for r in rows}, {None})
    check("...while /submitted does print one",
          P.parse_listing(fx("submitted"), page=1)[0].rank == 1)
    check("/from lists rows from several authors", len({r.author for r in rows}) > 3)


def check_the_pages_state_which_pagination_they_use():
    import product_parser as P
    cases = {
        "news": (H + "news", True), "show": (H + "show", True),
        "best": (H + "best", True), "active": (H + "active", True),
        "front": (H + "front?day=2024-01-15", True),
        "newest": (H + "newest", False), "jobs": (H + "jobs", False),
        "submitted": (H + "submitted?id=user_cx", False),
        "from": (H + "from?site=example.com", False),
        "ask": (H + "ask", None),
    }
    for name, (url, want) in cases.items():
        equal("%s: the More link %s ?p=2" % (name, {True: "IS", False: "is NOT", None: "does not exist:"}[want]),
              P.next_link_agrees(fx(name), url), want)
    equal("the front-day link keeps the day and adds p",
          P.next_page_url(fx("front"), H + "front?day=2024-01-15"),
          H + "front?day=2024-01-15&p=2")
    check("a cursor link is the site's own, absolute, with next= and n=",
          re.match(r"^https://news\.ycombinator\.com/newest\?next=\d+&n=31$",
                   P.next_page_url(fx("newest"), H + "newest")) is not None)
    check("...and /from carries kind= as well as next=",
          "kind=story&next=" in P.next_page_url(fx("from"), H + "from?site=example.com"))
    equal("a run that starts at ?p=2 compares the link with ?p=3, not ?p=2 (found live)",
          P.next_link_agrees(fx("news2"), H + "news?p=2"), True)
    equal("...and a cursor list is still not addressable wherever it starts",
          P.next_link_agrees(fx("newest"), H + "newest?p=4"), False)
    check("the belief in P_STYLE_PATHS matches every measurement above",
          all(P.pagination_is_addressable(url) == bool(want)
              for _n, (url, want) in cases.items() if want is not None))
    equal("the end of a list is a page with no row and no More link",
          (P.parse_listing(fx("p50"), page=30), P.next_page_url(fx("p50"), H + "news?p=30")),
          ([], None))


def check_page_url_replaces_rather_than_duplicates():
    import product_parser as P
    equal("page 1 is the URL as given", P.page_url(H + "news", 1), H + "news")
    equal("page 3", P.page_url(H + "news", 3), H + "news?p=3")
    equal("an existing p is replaced", P.page_url(H + "news?p=2", 5), H + "news?p=5")
    equal("other parameters survive, and p goes last",
          P.page_url(H + "front?day=2024-01-15", 2), H + "front?day=2024-01-15&p=2")
    equal("...also when p was in the middle",
          P.page_url(H + "front?p=9&day=2024-01-15", 2), H + "front?day=2024-01-15&p=2")


def check_url_shapes():
    import product_parser as P
    ok = {
        H: ("listing", "news"), H + "news": ("listing", "news"),
        H + "newest": ("listing", "newest"), H + "ask": ("listing", "ask"),
        H + "show": ("listing", "show"), H + "jobs": ("listing", "jobs"),
        H + "best": ("listing", "best"), H + "active": ("listing", "active"),
        "news.ycombinator.com/news": ("listing", "news"),
        H + "front?day=2024-01-15": ("listing", "front"),
    }
    for url, (mode, feed) in ok.items():
        q, why = P.query_from_url(url)
        check("%s -> %s %s" % (url, mode, feed),
              q is not None and (q.mode, q.feed) == (mode, feed), why)
    q, _ = P.query_from_url(H + "front?day=2024-01-15")
    equal("the day is kept", q.day, "2024-01-15")
    q, _ = P.query_from_url(H + "item?id=49901736")
    equal("an item URL is item mode", (q.mode, q.item_id), ("item", "49901736"))
    q, _ = P.query_from_url(H + "user?id=dang")
    equal("a user URL is user mode", (q.mode, q.username), ("user", "dang"))
    q, _ = P.query_from_url(H + "submitted?id=dang")
    equal("/submitted is a listing by a user", (q.mode, q.by), ("listing", "dang"))
    q, _ = P.query_from_url(H + "from?site=example.com")
    equal("/from is a listing by a domain", (q.mode, q.site), ("listing", "example.com"))
    q, _ = P.query_from_url(H + "news?p=3")
    equal("a typed ?p=3 starts the run at page 3", q.start_page, 3)
    equal("...and the first request IS page 3", P.request_for(q, 1).url, H + "news?p=3")
    equal("...the second is page 4", P.request_for(q, 2).url, H + "news?p=4")


def check_urls_the_site_would_answer_with_the_wrong_thing_are_refused():
    """Measured 2026-09-30: `front?day=garbage` served the latest day,
    `news?p=abc` and `news?p=0` served page 1, all HTTP 200 (§26)."""
    import product_parser as P
    refused = {
        H + "front?day=garbage": "YYYY-MM-DD",
        H + "front?day=2024-02-30": "not a real date",
        H + "front?day=2099-01-01": "future",
        H + "news?p=abc": "not a page number",
        H + "news?p=0": "not a page number",
        H + "news?p=-2": "not a page number",
        H + "item?id=abc": "numeric",
        H + "item": "numeric",
        H + "user?id=": "username",
        H + "user?id=has space": "username",
        H + "newest?next=49905410&n=31": "cursor",
        H + "newcomments": "does not implement",
        H + "threads?id=dang": "does not implement",
        H + "login": "not a route",
        "https://ycombinator.com/": "not the news site",
        "https://example.com/news": "not news.ycombinator.com",
        "": "empty",
    }
    for url, needle in refused.items():
        q, why = P.query_from_url(url)
        check("refused: %r (%s)" % (url, needle), q is None and needle in why,
              "got %r / %r" % (q, why))
    check("an unsupported route says 'does not implement', never 'cannot'",
          "cannot" not in P.query_from_url(H + "newcomments")[1])


def check_query_validation_and_flag_combinations():
    import page_flow
    import product_parser as P
    errors = []

    def build(**kw):
        args = types.SimpleNamespace(url=None, mode=None, feed=None, day=None, by=None,
                                     site=None, item=None, user=None, pages=1)
        for k, v in kw.items():
            setattr(args, k, v)

        def err(msg):
            errors.append(msg)
            raise SystemExit(2)
        try:
            return page_flow.build_query(args, err)
        except SystemExit:
            return None

    q = build()
    equal("no flags: the front page", (q.mode, q.feed), ("listing", "news"))
    q = build(feed="front", day="2024-01-15")
    equal("--feed front --day builds the day's address", q.url(), H + "front?day=2024-01-15")
    q = build(item="49901736")
    equal("--item alone means item mode", (q.mode, q.item_id), ("item", "49901736"))
    q = build(user="dang")
    equal("--user alone means user mode", q.mode, "user")
    q = build(by="dang")
    equal("--by is a submissions list", q.url(), H + "submitted?id=dang")
    q = build(site="example.com")
    equal("--site is a domain list", q.url(), H + "from?site=example.com")
    equal("--mode listing --feed jobs", build(mode="listing", feed="jobs").url(), H + "jobs")

    for label, kw, needle in [
        ("--day without --feed front", dict(day="2024-01-15"), "only applies to --feed front"),
        ("--day on another feed", dict(feed="news", day="2024-01-15"), "only applies"),
        ("a malformed --day", dict(feed="front", day="15/01/2024"), "YYYY-MM-DD"),
        ("a future --day", dict(feed="front", day="2099-01-01"), "future"),
        ("--feed with --by", dict(feed="news", by="dang"), "pass one"),
        ("--by with --site", dict(by="dang", site="example.com"), "pass one"),
        ("--item that is not a number", dict(mode="item", item="abc"), "numeric"),
        ("--mode item with no --item", dict(mode="item"), "numeric"),
        ("--mode user with no --user", dict(mode="user"), "username"),
        ("--user of the wrong length", dict(mode="user", user="x"), "username"),
        ("--item on a listing", dict(mode="listing", item="1"), "does not apply"),
        ("--feed on an item run", dict(mode="item", item="1", feed="news"), "does not apply"),
        ("--pages 0", dict(pages=0), "at least 1"),
        ("--url together with a query flag", dict(url=H + "news", feed="show"),
         "already carries the query"),
        ("--mode disagreeing with --url", dict(url=H + "item?id=1", mode="listing"),
         "disagrees"),
    ]:
        del errors[:]
        got = build(**kw)
        check("refused: %s" % label, got is None and errors and needle in errors[0],
              "%r / %r" % (got, errors))
    q = build(url=H + "item?id=49901736")
    equal("--url alone is fine", q.mode, "item")


def check_page_states_on_real_captures():
    import page_flow as F
    for name in ("news", "news2", "newest", "ask", "jobs", "submitted", "from",
                 "front", "show", "best", "active", "item_top", "ask_item",
                 "comment_item", "big"):
        equal("served, with rows: %s" % name, F.classify(fx(name), 200, H + "x"), "content")
    equal("a served profile", F.classify(fx("user"), 200, H + "user?id=x"), "content")
    for name in ("rendered_news", "rendered_newest", "rendered_jobs", "rendered_item_top"):
        equal("...still content once Chromium has serialised it: %s" % name,
              F.classify(fx(name), 200, H + "x"), "content")
    equal("...and the rendered profile", F.classify(fx("rendered_user"), 200, H + "user?id=x"),
          "content")
    equal("a list past its end: served, no rows -> empty, not blocked",
          F.classify(fx("p50"), 200, H + "news?p=50"), "empty")
    for name, sentence in (("nouser", "No such user."), ("itemabc", "No such item."),
                           ("futureday", "We don't have that data yet."),
                           ("oldday", "HN didn't exist yet.")):
        equal("the site's own sentence %r -> empty" % sentence,
              (fx(name), F.classify(fx(name), 200, H + "x")), (sentence, "empty"))
    equal("...also as Chromium serialises it (wrapped in <html><body>)",
          F.classify(fx("rendered_nouser"), 200, H + "user?id=x"), "empty")
    equal("Chromium's blank error document is NOT the site's page -> unknown",
          F.classify(fx("chromium_proxy_error"), 200, H + "news"), "unknown")
    equal("an empty body -> unknown", F.classify("", 200, H + "news"), "unknown")
    equal("HTTP 503 -> throttled", F.classify("<html>x</html>", 503, H + "news"), "throttled")
    equal("HTTP 429 -> throttled", F.classify("", 429, H + "news"), "throttled")
    equal("the site's 'too quickly' sentence with no status -> throttled",
          F.classify("Sorry, we're not able to serve your requests this quickly.",
                     None, H + "news"), "throttled")
    equal("HTTP 403 -> blocked", F.classify("<html>Forbidden</html>", 403, H + "news"), "blocked")
    equal("a 200 page of some OTHER site's markup is not the site's page",
          F.classify("<html><body><h1>Just a moment...</h1></body></html>" + "x" * 700, 200,
                     H + "news"), "unknown")
    equal("a throttle status beats the page: a served page under 503 is throttled",
          F.classify(fx("news"), 503, H + "news"), "throttled")


def check_a_served_page_that_parses_to_no_rows_is_called_out_as_our_bug():
    """§20: a page the site served, with rows on it, that parses to none is
    THIS repo's bug and must not read as 'the listing was empty'."""
    import page_flow
    broken = ('<html><body><table id="hnmain"><tr class="athing submission" id="1">'
              '<td>markup that moved</td></tr></table></body></html>')
    equal("the classifier still says content", page_flow.classify(broken, 200, H + "news"),
          "content")
    ops = _FakeOps({H + "news": [(200, broken)]})
    records = []

    class Grab(logging.Handler):
        def emit(self, record):
            records.append(record.getMessage())
    handler = Grab()
    logging.getLogger("page_flow").addHandler(handler)
    try:
        rc, meta, rows, _ = _run(ops, pages=1)
    finally:
        logging.getLogger("page_flow").removeHandler(handler)
    check("the run says the markup has probably changed",
          any("parsed to none" in r for r in records), repr(records))
    equal("...and exits 4 (the catalogue question was answered: nothing readable)", rc, 4)


# ---------------------------------------------------------------------------
# Items and profiles
# ---------------------------------------------------------------------------

def check_comment_tree_parses_to_the_captured_values():
    import product_parser as P
    rows = P.parse_comments(fx("item_top"))
    equal("30 comments kept in the trimmed thread", len(rows), 30)
    r = rows[0]
    equal("comment 1: id, story, parent", (r.sku, r.story_id, r.parent_id),
          ("49902317", "49901736", "49901736"))
    equal("comment 1: author, moment, depth, position, status",
          (r.author, r.posted_at, r.depth, r.position, r.status),
          ("user_gt", "2026-09-29T23:34:38Z", 0, 1, "ok"))
    equal("comment 1: permalink", r.url, H + "item?id=49902317")
    meta = P.story_meta(fx("item_top"))
    equal("the story header: id, type, points, comments",
          (meta["item_id"], meta["item_type"], meta["points"], meta["comments"]),
          ("49901736", "story", 538, 231))
    check("the header's comment count (231) is the SITE's, larger than the trimmed rows",
          meta["comments"] > len(rows))
    check("positions are 1..n in document order",
          [x.position for x in rows] == list(range(1, len(rows) + 1)))
    equal("every comment row carries the story id", {x.story_id for x in rows}, {"49901736"})


def check_parent_from_the_link_agrees_with_parent_from_indentation():
    import product_parser as P
    for name in ("item_top", "big", "ask_item", "comment_item"):
        rows = P.parse_comments(fx(name))
        meta = P.story_meta(fx(name))
        root = meta["item_id"]
        stack, bad = [], 0
        for r in rows:
            while stack and stack[-1][0] >= r.depth:
                stack.pop()
            expected = stack[-1][1] if stack else root
            stack.append((r.depth, r.sku))
            if expected != r.parent_id:
                bad += 1
        equal("%s: parent_id agrees with the indent stack on every one of %d rows" % (name, len(rows)),
              bad, 0)
    big = P.parse_comments(fx("big"))
    check("the deep thread has depth beyond 10", max(r.depth for r in big) >= 10)
    check("a top-level comment's parent is the story",
          all(r.parent_id == "49870367" for r in big if r.depth == 0))


def check_comment_rows_are_selected_by_class_token_not_exact_class():
    """The trap: collapsed and flagged rows carry extra classes, so a
    selector on the exact string `athing comtr` loses them silently. Measured
    on the full 1,095-comment capture: 1,042 by exact class, 1,100 by token."""
    from bs4 import BeautifulSoup
    import product_parser as P
    html = fx("big")
    soup = BeautifulSoup(html, "html.parser")
    exact = [tr for tr in soup.select("table.comment-tree tr.athing")
             if (tr.get("class") or []) == ["athing", "comtr"]]
    token = P.parse_comments(html)
    check("the fixture still exercises the trap (some rows carry more classes)",
          len(exact) < len(token), "%d exact vs %d token" % (len(exact), len(token)))
    equal("every comment row is parsed", len(token), 112)
    check("...including the collapsed and flagged ones",
          {"coll", "noshow"} & {c for tr in soup.select("tr.athing.comtr")
                                for c in (tr.get("class") or [])})


def check_flagged_comments_keep_their_place_and_are_marked():
    import product_parser as P
    rows = P.parse_comments(fx("big"))
    flagged = [r for r in rows if r.status == "flagged"]
    equal("five flagged comments in the thread", len(flagged), 5)
    check("a flagged comment's text is the site's placeholder, not its body",
          all(r.text == "[flagged]" for r in flagged))
    check("...it keeps its author, depth and parent",
          all(r.author and r.depth is not None and r.parent_id for r in flagged))
    equal("every other row is ok", {r.status for r in rows if r.status != "flagged"}, {"ok"})
    meta = P.story_meta(fx("big"))
    equal("the site's count (1095) equals the OK rows in the FULL thread: "
          "it leaves the flagged ones out", meta["comments"], 1095)


def check_comment_text_is_plain_text_with_paragraphs_and_full_links():
    import product_parser as P
    rows = {r.sku: r for r in P.parse_comments(fx("big"))}
    two = rows["49880805"]
    equal("a bare <p> separates paragraphs with a blank line",
          two.text, "First paragraph of a fixture comment.\n\nSecond paragraph, "
                    "after a bare paragraph tag with no closing one.")
    link = rows["49883002"]
    equal("the link's visible text is kept as the page shows it (shortened)",
          link.text, "A fixture comment with a link:\n\nhttps://example.org/a/very/long/pa...")
    equal("...and the FULL address is in links", link.links,
          ["https://example.org/a/very/long/path/that/the/page/would/shorten"])
    ent = [r for r in rows.values() if r.text and "entity" in r.text][0]
    equal("entities are decoded", ent.text, "It's an entity & a fixture.")
    code = [r for r in rows.values() if r.text and "for x in y" in r.text][0]
    check("a code block keeps its own line breaks and indentation",
          "for x in y:\n    print(x)" in code.text, repr(code.text))
    # SYNTHETIC: shapes the fixtures do not hold.
    from bs4 import BeautifulSoup
    node = BeautifulSoup("<div>a<p>b<br>c<p>d&nbsp;e</div>", "html.parser").div
    equal("synthetic: <p> and <br> and a non-breaking space",
          P._text_from_html(node), "a\n\nb\nc\n\nd e")
    equal("synthetic: an empty node is the empty string",
          P._text_from_html(BeautifulSoup("<div></div>", "html.parser").div), "")


def check_a_comment_address_gives_that_comment_and_its_replies():
    import product_parser as P
    meta = P.story_meta(fx("comment_item"))
    equal("the header of a comment page: the comment, and the story it sits under",
          (meta["item_id"], meta["item_type"], meta["story_id"]),
          ("49902317", "comment", "49901736"))
    rows = P.parse_comments(fx("comment_item"))
    check("the replies start at depth 0 with the comment as their parent",
          rows[0].depth == 0 and rows[0].parent_id == "49902317", repr(rows[0].parent_id))
    equal("...and carry the STORY's id, not the comment's",
          {r.story_id for r in rows}, {"49901736"})


def check_an_ask_hn_thread_keeps_the_story_text_in_the_header():
    import product_parser as P
    meta = P.story_meta(fx("ask_item"))
    equal("Ask HN: kind and comment count", (meta["kind"], meta["comments"]), ("ask", 523))
    equal("its own text is the header's `text`, a placeholder here",
          meta["text"], "Fixture story text.\n\nA second paragraph.")


def check_a_profile_parses_to_the_captured_values():
    import product_parser as P
    (u,) = P.parse_user(fx("user"))
    equal("the id is the username", (u.sku, u.url), ("user_cx", H + "user?id=user_cx"))
    equal("karma is an integer and the creation time is ISO UTC from the unix stamp",
          (u.karma, u.created_at), (42810, "2007-08-18T16:35:47Z"))
    equal("about is plain text", u.about, '"A fixture about box."\n\nA second paragraph with a link.')
    equal("...and its link is kept absolute", u.about_links, ["https://example.org/me"])
    equal("nothing to parse on `No such user.`", P.parse_user(fx("nouser")), [])
    # SYNTHETIC: a profile with an empty about box and no karma row.
    html = ('<table id="hnmain"><tr id="bigbox"><td><table><tr class="athing"><td>user:</td>'
            '<td timestamp="1187454947"><a class="hnuser">someone</a></td></tr>'
            '<tr><td>about:</td><td></td></tr></table></td></tr></table>')
    (u,) = P.parse_user(html)
    equal("synthetic: an empty about box is null, and a missing karma row is null",
          (u.about, u.karma, u.about_links), (None, None, None))


def check_the_browsers_serialisation_parses_to_the_same_rows_as_curls_bytes():
    """The browser engines hand the parser what Chromium built from the page,
    not what curl saw. Same document, both routes (make_fixtures --render)."""
    import product_parser as P
    for name in ("news", "newest", "jobs"):
        raw = P.parse_listing(fx(name), page=1)
        rendered = P.parse_listing(fx("rendered_" + name), page=1)
        equal("%s: %d rows, identical in every site column" % (name, len(raw)),
              _sig(raw), _sig(rendered))
        equal("%s: points and comments too" % name,
              [(r.points, r.comments) for r in raw], [(r.points, r.comments) for r in rendered])
        equal("%s: the More link is the same" % name,
              P.next_page_url(fx(name), H + name), P.next_page_url(fx("rendered_" + name), H + name))
    raw = P.parse_comments(fx("item_top"))
    rendered = P.parse_comments(fx("rendered_item_top"))
    equal("item_top: every comment field the same",
          [(r.sku, r.parent_id, r.depth, r.author, r.text, r.links, r.status) for r in raw],
          [(r.sku, r.parent_id, r.depth, r.author, r.text, r.links, r.status) for r in rendered])
    equal("the profile too", [(u.sku, u.karma, u.created_at, u.about) for u in P.parse_user(fx("user"))],
          [(u.sku, u.karma, u.created_at, u.about) for u in P.parse_user(fx("rendered_user"))])


# ---------------------------------------------------------------------------
# Policy
# ---------------------------------------------------------------------------

def check_state_policy():
    import page_flow as F
    equal("every state has a policy", sorted(F.STATE_POLICY),
          ["blocked", "content", "empty", "throttled", "unknown"])
    check("content and empty are parsed, never retried or blocked",
          all(F.should_parse(s) and not F.should_retry(s) and not F.counts_as_blocked(s)
              for s in ("content", "empty")))
    check("throttled: retried, NOT blocked (§24)",
          F.should_retry("throttled") and not F.counts_as_blocked("throttled")
          and not F.should_parse("throttled"))
    check("blocked: retried, blocked, never parsed",
          F.should_retry("blocked") and F.counts_as_blocked("blocked")
          and not F.should_parse("blocked"))
    check("unknown: retried, not blocked, not parsed",
          F.should_retry("unknown") and not F.counts_as_blocked("unknown")
          and not F.should_parse("unknown"))
    check("no state is solved: there is no solve step on this site",
          not any(p["solve"] for p in F.STATE_POLICY.values()))
    equal("a refusal is reported by name", F.refusal_name("blocked"), "http-403")
    check("the refusal advice claims nothing HN has been SEEN doing",
          "NOT a state it has met" in F.refusal_advice("blocked"))
    check("a CDP 401 is explained as expired credentials, not a held pid",
          "expired" in F.cdp_connect_hint("WebSocket error: 401 Unauthorized")
          and "pid" not in F.cdp_connect_hint("401 Unauthorized"))
    check("...and a 500 as a held pid", "pid" in F.cdp_connect_hint("HTTP 500"))
    equal("the default delay is the Crawl-delay robots.txt asks for", F.DEFAULT_DELAY_S, 30.0)


def check_policy_constants_have_a_consumer():
    """§17: a policy constant nothing reads is the same defect as dead code."""
    src = open(os.path.join(HERE, "page_flow.py"), encoding="utf-8").read()
    engines = "".join(open(os.path.join(HERE, m + ".py"), encoding="utf-8").read()
                      for m in ENGINES + ("scraper_api_client",))
    for constant in ("BLOCK_RETRIES_WITHOUT_POOL", "THROTTLE_RETRIES", "THROTTLE_WAIT_S",
                     "NAV_TIMEOUT_MS", "DEFAULT_DELAY_S", "CORE_FIELD_FLOOR",
                     "CDP_CONNECT_ATTEMPTS", "CDP_LOCKED_WAIT_S"):
        uses = len(re.findall(r"\b%s\b" % constant, src))
        check("page_flow.%s is READ, not only defined" % constant,
              uses >= 2 or constant in engines, "%d occurrence(s)" % uses)
    check("the throttle budget is consulted by the loop, not only declared",
          "throttles < THROTTLE_RETRIES" in src and "pause = THROTTLE_WAIT_S" in src)
    check("no engine reads a `solve` policy that does not exist",
          "should_solve" not in engines and "SOLVES_PER_PAGE" not in engines)


# ---------------------------------------------------------------------------
# The shared fetch loop, driven end to end with a fake driver
# ---------------------------------------------------------------------------

class _FakeOps:
    """page_flow's named operations, answering from fixtures by URL.

    `answers` maps a URL to a list of (status, html); the last entry repeats.
    A URL nobody named gets the empty page-past-the-end, which is what the
    real site does.
    """

    def __init__(self, answers, errors=None):
        self.answers = answers
        self.errors = errors or {}
        self.pool = None
        self.relaunches = 0
        self.fetches = []
        self.waits = []

    def fetch(self, url):
        self.fetches.append(url)
        if url in self.errors and self.errors[url]:
            err = self.errors[url].pop(0) if len(self.errors[url]) > 1 else self.errors[url][0]
            if err:
                return None, "", err
        queue = self.answers.get(url) or [(200, fx("p50"))]
        status, text = queue.pop(0) if len(queue) > 1 else queue[0]
        return status, text, None

    def wait_ms(self, ms):
        self.waits.append(ms)

    def relaunch(self):
        self.relaunches += 1

    def proxy_failure(self, text):
        return "ERR_PROXY_CONNECTION_FAILED" if "ERR_PROXY_CONNECTION_FAILED" in (text or "") else ""

    def close(self):
        pass


def _run(ops, pages=3, query=None, concurrency=1, run_concurrently=None, **extra):
    import page_flow
    from product_parser import Query
    with tempfile.TemporaryDirectory() as tmp:
        args = types.SimpleNamespace(
            pages=pages, retries=2, retry_delay=0, delay=0,
            proxy_block_retries=2, out=os.path.join(tmp, "out"), format="json",
            allow_empty=False, dump_html=None, url=None, cdp_endpoint=None,
            concurrency=concurrency)
        for k, v in extra.items():
            setattr(args, k, v)
        q = query or Query("listing", feed="news")
        rc = page_flow.run_pages(lambda: ops, lambda o: None,
                                 run_concurrently or (lambda pages: ([], [], False)),
                                 args, None, q, concurrency)
        meta_path = args.out + ".meta.json"
        meta = json.load(open(meta_path)) if os.path.exists(meta_path) else None
        rows = (json.load(open(args.out + ".json"))
                if os.path.exists(args.out + ".json") else None)
    return rc, meta, rows, ops


def _news_ops(**kw):
    return _FakeOps({H + "news": [(200, fx("news"))], H + "news?p=2": [(200, fx("news2"))]}, **kw)


def check_the_shared_loop_walks_a_p_list_and_ends_on_the_empty_page():
    ops = _news_ops()
    rc, meta, rows, ops = _run(ops, pages=5)
    equal("news, 5 pages asked, the third empty: exit 0", rc, 0)
    equal("...complete, stopped on the DATA", (meta["status"], meta["stop_reason"]),
          ("complete", "end_of_listing"))
    equal("...requests: page 1, page 2 and the empty page 3, not 4 and 5",
          ops.fetches, [H + "news", H + "news?p=2", H + "news?p=3"])
    equal("60 rows, ranks 1..60 in page order", [r["rank"] for r in rows], list(range(1, 61)))
    equal("the sidecar records the query and that the list is addressable",
          (meta["query"], meta["addressable"]), ({"path": "/news", "feed": "news"}, True))
    equal("a `products` count for the family", meta["products"], 60)
    equal("mode and source", (meta["mode"], meta["source"]), ("listing", "news.ycombinator.com"))
    keys = {(r["page"], r["position"]) for r in rows}
    equal("page + position unique across the run (§18)", len(keys), 60)
    ops = _news_ops()
    _run(ops, pages=3, delay=2)
    check("--delay is really waited between pages (2000 ms, twice: before page 2 and page 3)",
          ops.waits.count(2000) == 2, repr(ops.waits))


def check_a_cursor_list_is_walked_link_to_link():
    import product_parser as P
    link = P.next_page_url(fx("newest"), H + "newest")
    ops = _FakeOps({H + "newest": [(200, fx("newest"))], link: [(200, fx("jobs"))]})
    from product_parser import Query
    rc, meta, rows, ops = _run(ops, pages=2, query=Query("listing", feed="newest"))
    equal("newest, 2 pages: exit 0", rc, 0)
    equal("page 2 is fetched from the SITE's own More link, not from ?p=2",
          ops.fetches, [H + "newest", link])
    check("...which is not what ?p=2 would have built", link != H + "newest?p=2")
    equal("the sidecar says it is not addressable", meta["addressable"], False)
    equal("60 rows", len(rows), 60)
    rc, meta, rows, ops = _run(_FakeOps({H + "newest": [(200, fx("newest"))]}), pages=1,
                               query=Query("listing", feed="newest"))
    equal("--pages 1 on a cursor list never asks for a second page", (rc, meta["stop_reason"]),
          (0, "completed"))


def check_concurrency_is_ignored_for_a_cursor_list_and_says_why():
    from product_parser import Query
    import product_parser as P
    link = P.next_page_url(fx("newest"), H + "newest")
    called = []
    records = []

    class Grab(logging.Handler):
        def emit(self, record):
            records.append(record.getMessage())
    handler = Grab()
    logging.getLogger("page_flow").addHandler(handler)
    try:
        rc, meta, rows, ops = _run(
            _FakeOps({H + "newest": [(200, fx("newest"))], link: [(200, fx("jobs"))]}),
            pages=2, query=Query("listing", feed="newest"), concurrency=4,
            run_concurrently=lambda pages: called.append(pages) or ([], [], False))
    finally:
        logging.getLogger("page_flow").removeHandler(handler)
    equal("the workers never started", called, [])
    equal("...and the run still completed by walking", (rc, len(rows)), (0, 60))
    check("...with a warning that names the cursor", any("cursor" in r for r in records),
          repr(records))


def check_a_one_page_list_completes_when_more_pages_are_asked_for():
    from product_parser import Query
    rc, meta, rows, ops = _run(_FakeOps({H + "ask": [(200, fx("ask"))]}), pages=3,
                               query=Query("listing", feed="ask"))
    equal("/ask has no More link: exit 0, complete", (rc, meta["status"]), (0, "complete"))
    equal("...one request, 18 rows", (ops.fetches, len(rows)), ([H + "ask"], 18))
    equal("...stopped because the list ended", meta["stop_reason"], "end_of_listing")


def check_an_item_is_one_request_whatever_pages_says():
    from product_parser import Query
    q = Query("item", item_id="49901736")
    rc, meta, rows, ops = _run(_FakeOps({H + "item?id=49901736": [(200, fx("item_top"))]}),
                               pages=5, query=q)
    equal("an item run: exit 0", rc, 0)
    equal("...one request only, though 5 pages were asked for",
          ops.fetches, [H + "item?id=49901736"])
    equal("...complete by construction", (meta["status"], meta["stop_reason"]),
          ("complete", "single_page_mode"))
    equal("30 comments", len(rows), 30)
    equal("the sidecar carries the story the comments sit under",
          (meta["item"]["item_id"], meta["item"]["comments"], meta["item"]["title"]),
          ("49901736", 231, "Livenerf: Has Opus 5.5 been nerfed yet?"))
    equal("...and the query", meta["query"], {"item_id": "49901736"})
    check("a comment row has the comment columns, not a story's",
          "depth" in rows[0] and "points" not in rows[0])


def check_a_profile_is_one_request_and_one_row():
    from product_parser import Query
    rc, meta, rows, ops = _run(_FakeOps({H + "user?id=dang": [(200, fx("user"))]}),
                               pages=3, query=Query("user", username="dang"))
    equal("exit 0, one row, one request", (rc, len(rows), len(ops.fetches)), (0, 1, 1))
    equal("the row is a User", sorted(rows[0]), sorted(f.name for f in fields(
        __import__("output_writer").User)))


def check_nothing_there_is_exit_4_and_writes_nothing():
    from product_parser import Query
    rc, meta, rows, ops = _run(_FakeOps({H + "item?id=99999999999": [(200, fx("itemabc"))]}),
                               query=Query("item", item_id="99999999999"))
    equal("`No such item.`: exit 4 (an answer), nothing written", (rc, rows, meta), (4, None, None))
    equal("...fetched once, never retried", len(ops.fetches), 1)
    rc, meta, rows, ops = _run(_FakeOps({H + "user?id=nobody": [(200, fx("nouser"))]}),
                               query=Query("user", username="nobody"))
    equal("`No such user.`: exit 4", (rc, rows), (4, None))
    rc, meta, rows, ops = _run(_FakeOps({H + "news": [(200, fx("p50"))]}), pages=1)
    equal("an empty front page: exit 4, nothing written", (rc, rows), (4, None))
    rc, meta, rows, ops = _run(_FakeOps({H + "front": [(200, fx("futureday"))]}), pages=1,
                               query=__import__("product_parser").Query("listing", feed="front"))
    equal("the site's own `We don't have that data yet.`: exit 4, nothing written",
          (rc, rows), (4, None))


def check_refusals_throttles_and_transport_failures():
    from product_parser import Query
    rc, meta, rows, ops = _run(_FakeOps({H + "news": [(403, "<html>Forbidden</html>")]}), pages=1)
    equal("a 403 on page 1: exit 3", rc, 3)
    equal("...re-fetched once from a fresh browser (no pool)", (len(ops.fetches), ops.relaunches), (2, 1))
    equal("...and no sidecar beside no output", meta, None)

    rc, meta, rows, ops = _run(_FakeOps({H + "news": [(503, ""), (200, fx("news"))]}), pages=1)
    equal("a 503, then the page: exit 0", rc, 0)
    equal("...at the SAME exit (no relaunch), fetched twice", (ops.relaunches, len(ops.fetches)), (0, 2))
    check("...after a wait", any(w >= 1000 for w in ops.waits), repr(ops.waits))
    rc, meta, rows, ops = _run(_FakeOps({H + "news": [(503, ""), (200, fx("news"))]}),
                               pages=1, retries=1)
    equal("a throttle wait spends its OWN budget, not --retries (§24): with "
          "--retries 1 the page still arrives", rc, 0)

    rc, meta, rows, ops = _run(_FakeOps({H + "news": [(429, "")]}), pages=1)
    equal("a throttle that never lifts: exit 5, NOT 3 (it was never a block)", rc, 5)
    import page_flow
    equal("...the throttle budget was spent, and no more",
          len(ops.fetches), 1 + page_flow.THROTTLE_RETRIES)

    rc, meta, rows, ops = _run(_FakeOps({H + "news": [(200, fx("chromium_proxy_error")),
                                                       (200, fx("news"))]}), pages=1)
    equal("Chromium's error document, then the page: retried, exit 0", rc, 0)

    err = "Page.goto: net::ERR_PROXY_CONNECTION_FAILED at https://news.ycombinator.com/"
    rc, meta, rows, ops = _run(_FakeOps({}, errors={H + "news": [err]}), pages=1)
    equal("a dead proxy, no pool to rotate into: exit 5, not a crash and not 4", rc, 5)
    equal("...not retried at the same dead exit", len(ops.fetches), 1)

    rc, meta, rows, ops = _run(_FakeOps({H + "news": [(200, fx("news"))]},
                                        errors={H + "news": ["timeout 60000ms", None]}), pages=1)
    equal("a timeout is retried at the same exit: exit 0", (rc, len(ops.fetches)), (0, 2))


def check_a_page_two_failure_is_a_partial_run_with_the_page_named():
    ops = _FakeOps({H + "news": [(200, fx("news"))], H + "news?p=2": [(403, "<html>no</html>")]})
    rc, meta, rows, ops = _run(ops, pages=3)
    equal("rows on page 1, refusal on page 2: exit 6", rc, 6)
    equal("...partial, page 2 named", (meta["status"], meta["pages_failed"]), ("partial", [2]))
    equal("...stop reason names the refusal", meta["stop_reason"], "blocked_http-403")
    equal("...page 1's rows are kept", len(rows), 30)


def check_a_page_that_adds_nothing_new_ends_the_run_on_the_data():
    """§7: a re-served page ends a list by the DATA, not by a selector."""
    ops = _FakeOps({H + "news": [(200, fx("news"))], H + "news?p=2": [(200, fx("news"))]})
    rc, meta, rows, ops = _run(ops, pages=4)
    equal("page 2 = page 1 again: exit 0, complete", (rc, meta["status"]), (0, "complete"))
    equal("...stop reason is data-based", meta["stop_reason"], "no_new_products")
    equal("...30 rows, and page 3 was never asked for", (len(rows), len(ops.fetches)), (30, 2))


def check_a_run_that_starts_on_page_three_says_so():
    from product_parser import Query
    q = Query("listing", feed="news", start_page=3)
    # SYNTHETIC edit: the captured page 2's More link says ?p=3, which is the
    # right link for page 2 and the wrong one for a page served as page 3.
    page3 = fx("news2").replace('href="?p=3"', 'href="?p=4"').replace("href='?p=3'", "href='?p=4'")
    assert "?p=4" in page3
    ops = _FakeOps({H + "news?p=3": [(200, page3)], H + "news?p=4": [(200, fx("p50"))]})
    rc, meta, rows, ops = _run(ops, pages=2, query=q)
    equal("a run started at ?p=3 fetches ?p=3 first", ops.fetches[0], H + "news?p=3")
    equal("...and labels its rows with the SITE's page number", rows[0]["page"], 3)
    equal("...the sidecar keeps where it started", meta["start_url"], H + "news?p=3")
    equal("...and the query records the start", meta["query"]["start_page"], 3)
    equal("exit 0 and the end of the list found", (rc, meta["stop_reason"]), (0, "end_of_listing"))


def check_worker_path_uses_independent_addresses():
    """Only a `?p=N` list is handed to workers, with the plan built from N."""
    seen = []

    def fake_workers(pages):
        seen.append(list(pages))
        return [], [], False
    ops = _news_ops()
    rc, meta, rows, ops = _run(ops, pages=6, concurrency=3, run_concurrently=fake_workers)
    equal("pages 2..6 go to the workers", seen, [[2, 3, 4, 5, 6]])
    equal("page 1 was fetched alone first", ops.fetches, [H + "news"])


def check_every_engine_implements_the_operations_page_flow_uses():
    """The fetch loop is shared, so an engine missing ONE operation fails
    only when a live run reaches it. The set is DERIVED from page_flow's own
    source (every `ops.<name>`), not listed by hand."""
    tree = ast.parse(open(os.path.join(HERE, "page_flow.py"), encoding="utf-8").read())
    used = {n.attr for n in ast.walk(tree)
            if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name)
            and n.value.id == "ops"}
    check("page_flow drives the engines through named operations (not vacuous)",
          {"fetch", "wait_ms", "relaunch", "proxy_failure", "pool"} <= used, repr(sorted(used)))
    check("...and the fake driver this suite uses implements every one",
          all(hasattr(_FakeOps({}), name) for name in used),
          repr(sorted(n for n in used if not hasattr(_FakeOps({}), n))))
    for module in ENGINES + ("scraper_api_client",):
        path = os.path.join(HERE, module + ".py")
        tree = ast.parse(open(path, encoding="utf-8").read())
        ops_cls = next((n for n in tree.body
                        if isinstance(n, ast.ClassDef) and n.name == "_Ops"), None)
        if ops_cls is None:
            check("%s defines _Ops" % module, False)
            continue
        methods = {n.name for n in ops_cls.body if isinstance(n, ast.FunctionDef)}
        # Targets can be tuples (`self.args, self.pool = args, pool`), so walk
        # into each target rather than reading it whole.
        attrs = {t.attr for n in ast.walk(ops_cls) if isinstance(n, ast.Assign)
                 for target in n.targets for t in ast.walk(target)
                 if isinstance(t, ast.Attribute)
                 and isinstance(t.value, ast.Name) and t.value.id == "self"}
        missing = sorted(used - methods - attrs)
        check("%s._Ops provides every operation page_flow uses" % module,
              not missing, "missing %s" % missing)
        fetch = next((n for n in ops_cls.body if isinstance(n, ast.FunctionDef)
                      and n.name == "fetch"), None)
        check("%s._Ops.fetch takes (self, url) and returns a 3-tuple" % module,
              fetch is not None and [a.arg for a in fetch.args.args][:2] == ["self", "url"]
              and any(isinstance(n, ast.Return) and isinstance(n.value, ast.Tuple)
                      and len(n.value.elts) == 3 for n in ast.walk(fetch)))


# ---------------------------------------------------------------------------
# The output contract
# ---------------------------------------------------------------------------

def check_row_schema():
    from output_writer import (Comment, Product, ROW_CLASS_BY_MODE, Story, User,
                               UNIQUE_BY_SKU_MODES)
    for cls in (Story, Comment, User):
        names = [f.name for f in fields(cls)]
        equal("%s: the first four columns are the family's, in order (§9)" % cls.__name__,
              names[:4], ["source", "scraped_at", "url", "sku"])
    equal("Story also carries `title` in the family position",
          [f.name for f in fields(Story)][4], "title")
    check("Story ends with the run-describing tail",
          [f.name for f in fields(Story)][-2:] == ["page", "position"])
    check("no commerce column that would be null forever",
          not ({"price", "currency", "brand", "original_price", "discount_pct", "rating"}
               & {f.name for c in (Story, Comment, User) for f in fields(c)}))
    equal("every mode maps to its row class", ROW_CLASS_BY_MODE,
          {"listing": Story, "item": Comment, "user": User})
    equal("every mode is one row per sku", sorted(UNIQUE_BY_SKU_MODES), ["item", "listing", "user"])
    check("the family name `Product` still resolves (workflows import it inline)",
          Product is Story)
    equal("Story has 15 columns", len(fields(Story)), 15)
    equal("Comment has 13 columns", len(fields(Comment)), 13)
    equal("User has 8 columns", len(fields(User)), 8)


def check_csv_and_json_writers():
    from output_writer import Comment, Story, User, write_csv, write_json
    import product_parser as P
    rows = P.parse_listing(fx("news"), page=1)
    comments = P.parse_comments(fx("big"))
    with tempfile.TemporaryDirectory() as tmp:
        csv_path = os.path.join(tmp, "out.csv")
        write_csv(rows, csv_path, row_cls=Story)
        reader = list(csv.reader(open(csv_path, encoding="utf-8")))
        equal("CSV header matches the dataclass, in order", reader[0], [f.name for f in fields(Story)])
        equal("CSV holds every row", len(reader) - 1, len(rows))
        cpath = os.path.join(tmp, "c.csv")
        write_csv(comments, cpath, row_cls=Comment)
        creader = list(csv.reader(open(cpath, encoding="utf-8")))
        check("no Python list repr leaked into the CSV",
              not any(cell.startswith("['") for row in creader[1:] for cell in row))
        links = [row[creader[0].index("links")] for row in creader[1:] if row[creader[0].index("links")]]
        check("a list column becomes one readable cell", links and links[0].startswith("https://"),
              repr(links[:1]))
        two = Comment(sku="9", links=["https://a.example/1", "https://b.example/2"])
        twop = os.path.join(tmp, "two.csv")
        write_csv([two], twop, row_cls=Comment)
        cell = list(csv.DictReader(open(twop, encoding="utf-8", newline="")))[0]["links"]
        equal("...and two links are joined with ' | '", cell, "https://a.example/1 | https://b.example/2")
        check("a multi-paragraph comment survives a CSV round trip",
              any("\n\n" in row[creader[0].index("text")] for row in creader[1:]))
        empty_csv = os.path.join(tmp, "empty.csv")
        write_csv([], empty_csv, row_cls=User)
        equal("an EMPTY csv still carries its header",
              list(csv.reader(open(empty_csv, encoding="utf-8"))), [[f.name for f in fields(User)]])
        json_path = os.path.join(tmp, "out.json")
        write_json(comments, json_path)
        loaded = json.load(open(json_path, encoding="utf-8"))
        check("a list column stays a real list in JSON",
              any(isinstance(r["links"], list) for r in loaded))
        check("ids stay strings in JSON (HN ids are ~2**26 today but are not arithmetic)",
              all(isinstance(r["sku"], str) for r in loaded))


def check_csv_cells_that_would_be_formulas_are_neutralised_and_counted():
    """Comment text is written by strangers, and a comment opening with `-` or
    `=` is ordinary on this site. CSV only; JSON keeps the site's bytes."""
    from output_writer import Comment, save
    rows = [Comment(sku="1", text="=HYPERLINK(\"http://x\",\"y\")", depth=0, author="a"),
            Comment(sku="2", text="- a list item", depth=1, author="b"),
            Comment(sku="3", text="+1 agreed", depth=2, author="c"),
            Comment(sku="4", text="@here", depth=3, author="d"),
            Comment(sku="5", text="\tindented", depth=4, author="e"),
            Comment(sku="6", text="an ordinary comment", depth=-5, author="f")]
    with tempfile.TemporaryDirectory() as tmp:
        prefix = os.path.join(tmp, "out")
        stats = {}
        from output_writer import ROW_CLASS_BY_MODE  # noqa: F401
        rc = save(rows, prefix, "both", row_cls=Comment, stats=stats)
        equal("saved", rc, 0)
        got = list(csv.DictReader(open(prefix + ".csv", encoding="utf-8", newline="")))
        equal("five formula-shaped cells are prefixed with an apostrophe",
              [g["text"][0] for g in got[:5]], ["'"] * 5)
        equal("an ordinary cell is untouched", got[5]["text"], "an ordinary comment")
        equal("a NEGATIVE NUMBER is not escaped (only strings are)", got[5]["depth"], "-5")
        equal("the count is reported for the sidecar", stats["csv_cells_escaped"], 5)
        as_json = json.load(open(prefix + ".json", encoding="utf-8"))
        equal("the JSON keeps the site's bytes exactly", as_json[0]["text"], rows[0].text)


def check_writes_are_atomic_and_keep_the_umask():
    import output_writer as O
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "out.json")
        with open(path, "w", encoding="utf-8") as f:
            f.write('[{"sku": "yesterday"}]')
        os.chmod(path, 0o640)
        try:
            O.write_json([O.Story(sku="1"), object()], path)   # object() cannot be dumped
        except TypeError:
            pass
        equal("a write that fails halfway leaves the previous good file intact",
              open(path, encoding="utf-8").read(), '[{"sku": "yesterday"}]')
        check("...and no temporary file is left behind",
              [n for n in os.listdir(tmp) if n.endswith(".tmp")] == [])
        O.write_json([O.Story(sku="1")], path)
        equal("an existing file keeps the mode someone gave it",
              stat.S_IMODE(os.stat(path).st_mode), 0o640)
        fresh = os.path.join(tmp, "fresh.json")
        O.write_json([O.Story(sku="1")], fresh)
        umask = os.umask(0)
        os.umask(umask)
        equal("a NEW file gets what open() would have given it, not the temp file's 0600",
              stat.S_IMODE(os.stat(fresh).st_mode), 0o666 & ~umask)
        O.write_run_meta(os.path.join(tmp, "m"), {"status": "complete"})
        equal("the sidecar is atomic and readable the same way",
              stat.S_IMODE(os.stat(os.path.join(tmp, "m.meta.json")).st_mode), 0o666 & ~umask)


def check_exit_codes():
    import output_writer as O
    equal("3 blocked / 4 empty / 5 never obtained / 6 partial",
          (O.EXIT_BLOCKED, O.EXIT_NO_PRODUCTS, O.EXIT_FETCH_FAILED, O.EXIT_PARTIAL),
          (3, 4, 5, 6))
    for reason in ("end_of_listing", "single_page_mode", "no_new_products", "completed"):
        check("%s is a COMPLETE stop reason" % reason, reason in O.COMPLETE_STOP_REASONS)
    for reason in ("throttled", "page_load_timeout", "blocked_http-403", "pages_unattempted"):
        check("%s is NOT complete" % reason, reason not in O.COMPLETE_STOP_REASONS)
    rc = O.finish_run([], "/nonexistent/x", "json", False, blocked=False,
                      stop_reason="throttled", pages_requested=1, pages_completed=0,
                      start_url="u", final_url="u")
    equal("finish_run: nothing gathered and not complete is 5, never 4", rc, 5)


def check_a_run_that_finds_nothing_writes_nothing():
    from output_writer import save
    with tempfile.TemporaryDirectory() as tmp:
        prefix = os.path.join(tmp, "out")
        with open(prefix + ".json", "w", encoding="utf-8") as f:
            f.write('[{"sku": "yesterday"}]')
        equal("an empty run exits 4", save([], prefix, "json", allow_empty=False), 4)
        equal("...and leaves the previous good file alone",
              open(prefix + ".json", encoding="utf-8").read(), '[{"sku": "yesterday"}]')
        equal("--allow-empty writes it, and still reports exit 4",
              save([], prefix, "json", allow_empty=True), 4)


def check_diff_runs_tracks_the_real_columns():
    """A sibling family's diff reported 0 changed on real changes for weeks
    because its column list was copied from a repo with different rows. Here
    the list is derived; assert it is never empty and that a real change is
    seen, and that the tool REFUSES when it cannot tell the mode."""
    import diff_runs as D
    import product_parser as P
    for mode in ("listing", "item", "user"):
        check("%s: tracked columns are derived and non-empty" % mode,
              len(D.tracked_fields(mode)) >= 2, repr(D.tracked_fields(mode)))
    check("points and comments are tracked on a story", {"points", "comments", "title"}
          <= set(D.tracked_fields("listing")))
    check("rank and position are NOT tracked (a live list reorders itself)",
          not ({"rank", "position", "page"} & set(D.tracked_fields("listing"))))
    check("text and status are tracked on a comment", {"text", "status"}
          <= set(D.tracked_fields("item")))
    check("karma is tracked on a profile", "karma" in D.tracked_fields("user"))
    old = [asdict(r) for r in P.parse_listing(fx("news"), page=1)]
    new = copy.deepcopy(old)
    new[0]["points"] = 999
    new[1]["rank"] = 7
    del new[2]
    result = D.diff_products(old, new, "listing")
    equal("one changed (the rank move is not a change)",
          [c["sku"] for c in result["changed"]], [old[0]["sku"]])
    equal("...with points named", list(result["changed"][0]["changes"]), ["points"])
    equal("one removed", len(result["removed"]), 1)
    try:
        D.diff_products(old, new)
        refused = False
    except ValueError:
        refused = True
    check("a diff with no mode REFUSES rather than compare on zero columns", refused)
    with tempfile.TemporaryDirectory() as tmp:
        a, b = os.path.join(tmp, "a.json"), os.path.join(tmp, "b.json")
        json.dump(old, open(a, "w"))
        json.dump(new, open(b, "w"))
        for path, rows in ((a, old), (b, new)):
            json.dump({"status": "complete", "mode": "listing", "query": {"feed": "news"}},
                      open(path[:-5] + ".meta.json", "w"))
        args = types.SimpleNamespace(old=a, new=b, mode=None)
        equal("the mode comes from the sidecars", D.resolved_mode(args), "listing")
        check("two complete runs of one query are comparable", D._check_comparable(args))
        json.dump({"status": "complete", "mode": "listing", "query": {"feed": "show"}},
                  open(b[:-5] + ".meta.json", "w"))
        check("two different QUERIES are refused", not D._check_comparable(args))
        json.dump({"status": "complete", "mode": "item", "query": {"feed": "news"}},
                  open(b[:-5] + ".meta.json", "w"))
        check("two different MODES are refused", not D._check_comparable(args))
        json.dump({"status": "partial", "mode": "listing", "query": {"feed": "news"},
                   "pages_completed": 1, "pages_requested": 3, "stop_reason": "x"},
                  open(b[:-5] + ".meta.json", "w"))
        check("a partial run is refused", not D._check_comparable(args))
        os.remove(a[:-5] + ".meta.json")
        os.remove(b[:-5] + ".meta.json")
        equal("with no sidecar and no --mode the mode is unknown", D.resolved_mode(args), None)
        equal("--mode supplies it", D.resolved_mode(types.SimpleNamespace(old=a, new=b, mode="listing")),
              "listing")
        rc = subprocess.run([sys.executable, os.path.join(HERE, "diff_runs.py"), "--old", a,
                             "--new", b], capture_output=True, text=True)
        equal("the CLI refuses (exit 2) when it cannot tell the mode", rc.returncode, 2)
        rc = subprocess.run([sys.executable, os.path.join(HERE, "diff_runs.py"), "--old", a,
                             "--new", b, "--mode", "listing", "--fail-on-change"],
                            capture_output=True, text=True)
        equal("the CLI with --mode sees the change and --fail-on-change exits 1",
              rc.returncode, 1)
        check("...and prints the points change", "points: " in rc.stdout, rc.stdout[-300:])


def check_sidecar_shape():
    from output_writer import run_meta
    meta = run_meta(status="complete", stop_reason="completed", pages_requested=3,
                    pages_completed=3, pages_failed=[], products=90,
                    mode="listing", source="news.ycombinator.com",
                    start_url=H + "news", final_url=H + "news?p=3",
                    extra={"query": {"feed": "news"}, "addressable": True})
    for key in ("status", "stop_reason", "pages_requested", "pages_completed",
                "pages_failed", "mode", "source", "products", "query", "addressable"):
        check("the sidecar records %r" % key, key in meta)
    check("pages_failed is a LIST", isinstance(meta["pages_failed"], list))


# ---------------------------------------------------------------------------
# The engines — the checks CLAUDE.md §17 says to steal
# ---------------------------------------------------------------------------

def check_engines_import_their_driver_at_module_level():
    for module, driver in DRIVER_IMPORTS.items():
        tree = ast.parse(open(os.path.join(HERE, module + ".py"), encoding="utf-8").read())
        top = set()
        for node in tree.body:
            if isinstance(node, ast.Import):
                top.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                top.add(node.module.split(".")[0])
        check("%s imports %s at MODULE level" % (module, driver), driver in top,
              "top-level imports: %s" % sorted(top))


def check_shared_calls_bind_against_the_real_signature():
    """§17's check #1. Every call from an engine (and the Scraper API client,
    diff_runs and page_flow itself) into a shared module is bound against the
    callee's real signature. A name that does not exist FAILS (§22). A name
    bound in the calling file shadows a same-named module."""
    import output_writer
    import page_flow
    import product_parser
    import proxy_pool
    targets = {"page_flow": page_flow, "product_parser": product_parser,
               "output_writer": output_writer, "proxy_pool": proxy_pool}
    bound = 0
    for module in ENGINES + ("scraper_api_client", "diff_runs", "page_flow",
                             "make_fixtures"):
        tree = ast.parse(open(os.path.join(HERE, module + ".py"), encoding="utf-8").read())
        local_names = {n.arg for n in ast.walk(tree) if isinstance(n, ast.arg)}
        direct = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module in targets:
                for alias in node.names:
                    direct[alias.asname or alias.name] = (targets[node.module], alias.name)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name in targets and alias.asname:
                        direct[alias.asname] = (targets[alias.name], None)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func, owner, attr = node.func, None, None
            if (isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name)
                    and func.value.id in targets and func.value.id not in local_names):
                owner, attr = targets[func.value.id], func.attr
            elif (isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name)
                  and func.value.id in direct and direct[func.value.id][1] is None):
                owner, attr = direct[func.value.id][0], func.attr
            elif isinstance(func, ast.Name) and func.id in direct and direct[func.id][1]:
                owner, attr = direct[func.id]
            if owner is None:
                continue
            if not hasattr(owner, attr):
                check("%s.%s exists (called from %s:%d)" % (owner.__name__, attr,
                      module, node.lineno), False, "AttributeError on a live run")
                continue
            callee = getattr(owner, attr)
            if not callable(callee):
                continue
            try:
                sig = inspect.signature(callee)
            except (TypeError, ValueError):
                continue
            if any(kw.arg is None for kw in node.keywords) or any(
                    isinstance(a, ast.Starred) for a in node.args):
                continue
            try:
                sig.bind(*[None] * len(node.args), **{kw.arg: None for kw in node.keywords})
                bound += 1
            except TypeError as e:
                check("%s:%d %s.%s(...) binds against its real signature"
                      % (module, node.lineno, owner.__name__, attr), False,
                      "%s; signature is %s" % (e, sig))
    check("the binding walk checked something (%d calls)" % bound, bound > 40,
          "only %d calls were bound — is the walk finding them?" % bound)


def check_every_name_imported_from_a_local_module_exists():
    """§22, and the family's `credential-gated paths hide dead calls`: an import
    INSIDE a function is never bound by a run that does not reach it, so a
    `--fingerprint` path can name a helper that does not exist and every
    keyless check stays green. Walk EVERY `from <local> import name`, at any
    depth, and require the name to exist in that module."""
    local = {f[:-3] for f in os.listdir(HERE) if f.endswith(".py")}
    skipped = set(ENGINES) | {"smoke_test", "make_fixtures"}
    seen = 0
    for filename in sorted(f for f in os.listdir(HERE) if f.endswith(".py")):
        tree = ast.parse(open(os.path.join(HERE, filename), encoding="utf-8").read())
        for node in ast.walk(tree):
            if not (isinstance(node, ast.ImportFrom) and node.module in local):
                continue
            if node.module in skipped:
                continue
            module = __import__(node.module)
            for alias in node.names:
                seen += 1
                check("%s:%d imports %s.%s, which exists" % (filename, node.lineno,
                                                            node.module, alias.name),
                      hasattr(module, alias.name))
    check("the local-import walk found imports to check (%d)" % seen, seen > 30)


def check_every_page_flow_call_in_every_engine_names_something_that_exists():
    """§22: a check that resolves before it tests must say what it does when
    the resolution FAILS. Every `page_flow.X` an engine touches must exist."""
    import page_flow
    for module in ENGINES + ("scraper_api_client",):
        tree = ast.parse(open(os.path.join(HERE, module + ".py"), encoding="utf-8").read())
        local = {n.arg for n in ast.walk(tree) if isinstance(n, ast.arg)}
        names = {n.attr for n in ast.walk(tree)
                 if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name)
                 and n.value.id == "page_flow" and "page_flow" not in local}
        missing = sorted(a for a in names if not hasattr(page_flow, a))
        check("%s: every page_flow.<name> it uses exists (%d names)" % (module, len(names)),
              not missing and names, "missing %s" % missing)


def _argparse_flags(module_name):
    tree = ast.parse(open(os.path.join(HERE, module_name + ".py"), encoding="utf-8").read())
    parsers = {"p"}
    for node in ast.walk(tree):
        if (isinstance(node, ast.Assign) and isinstance(node.value, ast.Call)
                and isinstance(node.value.func, ast.Attribute)
                and node.value.func.attr in ("add_argument_group",
                                             "add_mutually_exclusive_group")):
            parsers.update(t.id for t in node.targets if isinstance(t, ast.Name))
    flags = set()
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "add_argument"
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id in parsers):
            flags.update(a.value for a in node.args if isinstance(a, ast.Constant)
                         and isinstance(a.value, str) and a.value.startswith("--"))
    return flags


# The family's flag contract (CLAUDE.md §9, including the five it omitted
# for months), plus this repo's own query flags.
CONTRACT_FLAGS = {
    "--url", "--pages", "--category", "--format", "--out", "--delay",
    "--retries", "--retry-delay", "--concurrency", "--proxy", "--proxy-file",
    "--proxy-rotate", "--proxy-shuffle", "--proxy-block-retries",
    "--twocaptcha-key", "--captcha-api", "--solve-captcha", "--min-score",
    "--cdp-endpoint", "--allow-empty", "--dump-html", "--headless", "--headful",
    "--fingerprint", "--fp-country", "--fp-tags", "--locale", "--mode",
}
HN_FLAGS = {"--feed", "--day", "--by", "--site", "--item", "--user"}
# Flags an engine has and its twins do not, each because of its driver. The
# exception list IS the documentation (§20): closing one must be a decision.
DOCUMENTED_DIFFERENCES = {"puppeteer_scraper": {"--chromium-path"},
                          "selenium_scraper": {"--chromedriver"}}


def check_engine_flag_sets():
    """§17's check #2: against the contract AND against each other, both
    ways."""
    sets = {m: _argparse_flags(m) for m in ENGINES}
    for module, flags in sets.items():
        missing = (CONTRACT_FLAGS | HN_FLAGS) - flags
        check("%s defines every contract flag" % module, not missing,
              "missing %s" % sorted(missing))
    names = sorted(sets)
    for i in range(len(names)):
        for j in range(i + 1, len(names)):
            a, b = names[i], names[j]
            only_a = sets[a] - sets[b] - DOCUMENTED_DIFFERENCES.get(a, set())
            only_b = sets[b] - sets[a] - DOCUMENTED_DIFFERENCES.get(b, set())
            check("%s and %s define the same flags" % (a, b), not only_a and not only_b,
                  "only in %s: %s; only in %s: %s" % (a, sorted(only_a), b, sorted(only_b)))
    for module, extra in DOCUMENTED_DIFFERENCES.items():
        check("the documented difference still exists in %s (closing it must be a decision)" % module,
              extra <= sets[module], repr(extra - sets[module]))
    api = _argparse_flags("scraper_api_client")
    check("the Scraper API client takes the same query flags",
          (HN_FLAGS | {"--url", "--pages", "--mode", "--category"}) <= api,
          repr(sorted((HN_FLAGS | {"--url", "--pages", "--mode"}) - api)))


def check_engines_default_delay_is_the_shared_one_and_parse_the_same_way():
    """The default is read from page_flow by every engine, so the three cannot
    disagree about it. Parsed for real: `--help` output is not evidence."""
    import page_flow
    for module in ENGINES + ("scraper_api_client",):
        engine = _import_engine(module)
        if engine is None:
            continue
        old = os.environ.pop("HACKERNEWS_URL", None)
        try:
            args = engine.parse_args(["--feed", "show"] + (["--key", "k" * 8]
                                                          if module == "scraper_api_client" else []))
        finally:
            if old is not None:
                os.environ["HACKERNEWS_URL"] = old
        equal("%s: --delay defaults to the shared Crawl-delay" % module, args.delay,
              page_flow.DEFAULT_DELAY_S)
        equal("%s: --feed show builds the Show list" % module, args.query.url(), H + "show")
        equal("%s: --category is the same flag" % module,
              engine.parse_args(["--category", "jobs"] + (["--key", "k" * 8]
                                if module == "scraper_api_client" else [])).query.url(), H + "jobs")
        equal("%s: --url picks the mode" % module,
              engine.parse_args(["--url", H + "user?id=dang"] + (["--key", "k" * 8]
                                if module == "scraper_api_client" else [])).mode, "user")
        check("%s: output prefix is this repo's" % module,
              args.out.startswith("hackernews_rows"))


def check_banned_and_removed_flags():
    """Scoped to the engines. `--country` is banned: it could disagree with
    the --url. The captcha-solving flags exist for the CLI and nothing reads
    them, which the help says."""
    for module in ENGINES:
        source = open(os.path.join(HERE, module + ".py"), encoding="utf-8").read()
        for flag in ("--antidetect", "--country", "--country-code", "--category-id"):
            check("%s does not define %s" % (module, flag), '"%s"' % flag not in source)


def check_undefined_names_in_every_module():
    """§10: compileall proves a file PARSES, not that its names RESOLVE.
    Kept COARSE (pooled bindings) so it under-reports rather than invents."""
    import builtins
    for filename in sorted(f for f in os.listdir(HERE) if f.endswith(".py")):
        tree = ast.parse(open(os.path.join(HERE, filename), encoding="utf-8").read())
        defined = set(dir(builtins)) | {"__file__", "__name__", "__doc__",
                                        "__package__", "__spec__"}
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                for alias in node.names:
                    defined.add((alias.asname or alias.name).split(".")[0])
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                defined.add(node.name)
            elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
                defined.add(node.id)
            elif isinstance(node, ast.arg):
                defined.add(node.arg)
            elif isinstance(node, ast.ExceptHandler) and node.name:
                defined.add(node.name)
        used = {n.id for n in ast.walk(tree)
                if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}
        unresolved = sorted(used - defined)
        check("%s: every name resolves" % filename, not unresolved, repr(unresolved))


def check_no_statement_is_unreachable():
    """A statement after a return/raise/break/continue in the SAME block."""
    for filename in sorted(f for f in os.listdir(HERE) if f.endswith(".py")):
        tree = ast.parse(open(os.path.join(HERE, filename), encoding="utf-8").read())
        dead = []
        for node in ast.walk(tree):
            for fld in ("body", "orelse", "finalbody"):
                block = getattr(node, fld, None)
                if not isinstance(block, list):
                    continue
                for i, stmt in enumerate(block[:-1]):
                    if isinstance(stmt, (ast.Return, ast.Raise, ast.Continue, ast.Break)):
                        dead.append(block[i + 1].lineno)
                        break
        check("%s: no statement the control flow can never reach" % filename,
              not dead, "first at line %d" % min(dead) if dead else "")


def _import_graph(entrypoint):
    local = {f[:-3] for f in os.listdir(HERE) if f.endswith(".py")}
    seen, todo = set(), [entrypoint]
    while todo:
        name = todo.pop()
        if name in seen or name not in local:
            continue
        seen.add(name)
        tree = ast.parse(open(os.path.join(HERE, name + ".py"), encoding="utf-8").read())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                todo.extend(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                todo.append(node.module.split(".")[0])
    return seen


def check_no_module_is_dead_code():
    """A module nothing imports and nothing runs is dead code that looks
    load-bearing. Every top-level .py is an entry point (an engine, a tool) or
    is imported by one."""
    entry = set(ENGINES) | {"scraper_api_client", "diff_runs", "make_fixtures",
                            "smoke_test", "env_config", "fingerprint_client"}
    reachable = set()
    for e in entry:
        reachable |= _import_graph(e)
    local = {f[:-3] for f in os.listdir(HERE) if f.endswith(".py")}
    dead = sorted(local - reachable - entry)
    check("every module is an entry point or imported by one", not dead, repr(dead))
    check("captcha_solver.py is not shipped: this repo does not implement solving, "
          "and a solver nothing imports would say otherwise",
          "captcha_solver" not in local)


def check_dockerfile_copies_everything_the_entrypoint_imports():
    dockerfile = open(os.path.join(HERE, "Dockerfile"), encoding="utf-8").read()
    copy_lines, joining = [], False
    for line in dockerfile.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        if joining or stripped.upper().startswith("COPY "):
            copy_lines.append(stripped)
            joining = stripped.endswith("\\")
    copied = set(re.findall(r"([A-Za-z_][A-Za-z0-9_]*)\.py", " ".join(copy_lines)))
    missing = sorted(_import_graph("playwright_scraper") - copied)
    check("the Dockerfile COPYs every module playwright_scraper.py imports",
          not missing, "missing %s" % missing)
    check("the image does not carry the test suite", "smoke_test" not in copied)
    check("...nor the fixture generator, nor a .env", "make_fixtures" not in copied
          and ".env" not in " ".join(copy_lines).replace(".env.example", ""))


def check_env_example_documents_exactly_what_the_loader_reads():
    import env_config
    text = open(os.path.join(HERE, ".env.example"), encoding="utf-8").read()
    documented = set(re.findall(r"^([A-Z][A-Z0-9_]+)=", text, re.M))
    read = set(env_config.ENV_KEYS)
    equal("the example and the loader name the same variables",
          sorted(documented), sorted(read))
    check("the per-site variables carry the HACKERNEWS_ prefix",
          {"HACKERNEWS_CDP_ENDPOINT", "HACKERNEWS_PROXY", "HACKERNEWS_URL"} <= read)
    check("no donor prefix is left behind", not any("BINANCE" in k for k in read)
          and "BINANCE" not in text)


def check_a_copied_env_example_reads_as_UNSET():
    import env_config
    text = open(os.path.join(HERE, ".env.example"), encoding="utf-8").read()
    values = dict(re.findall(r"^([A-Z][A-Z0-9_]+)=(.*)$", text, re.M))
    credentials = {"TWOCAPTCHA_KEY", "HACKERNEWS_CDP_ENDPOINT", "HACKERNEWS_PROXY"}
    before = dict(os.environ)
    try:
        for name, raw in values.items():
            os.environ[name] = raw
            got = env_config.env_value(name)
            if name in credentials:
                check("a copied .env.example leaves %s unset" % name, got is None, repr(got))
            else:
                check("...while %s stays a usable default" % name, got == raw.strip(), repr(got))
        os.environ["TWOCAPTCHA_KEY"] = "not-a-real-key-but-a-real-value"
        equal("a real value is still read", env_config.env_value("TWOCAPTCHA_KEY"),
              "not-a-real-key-but-a-real-value")
    finally:
        os.environ.clear()
        os.environ.update(before)


def check_credential_scan_is_one_implementation_invoked_from_both():
    script = os.path.join(HERE, ".github", "ci_checks.py")
    if not os.path.isdir(os.path.join(HERE, ".github")):
        # Inside the Docker image, which copies no .github at all. Triggered
        # by the WHOLE directory being absent, never by one file in it (§22).
        skip("ci_checks", "no .github directory (the image)")
        return
    check("the credential scan exists as a script", os.path.exists(script))
    workflow = open(os.path.join(HERE, ".github", "workflows", "tests.yml"),
                    encoding="utf-8").read()
    check("CI INVOKES the script rather than reimplementing it", "ci_checks.py" in workflow)
    result = subprocess.run([sys.executable, script, "--secret-check", "--sample-check"],
                            cwd=HERE, capture_output=True, text=True)
    check("the credential scan and sample check pass on this tree",
          result.returncode == 0, (result.stdout + result.stderr)[-600:])


def check_no_workflow_imports_the_code_inline():
    """The first push of a sibling went red on an inline heredoc in tests.yml
    importing the donor repo's row class, invisible to every local run because
    nothing local executes a workflow. A workflow calls ci_checks.py or the
    CLIs; it does not carry its own copy of a check that imports the code."""
    wf_dir = os.path.join(HERE, ".github", "workflows")
    if not os.path.isdir(wf_dir):
        skip("workflows", "no .github directory (the image)")
        return
    local = {f[:-3] for f in os.listdir(HERE) if f.endswith(".py")}
    pattern = re.compile(r"^\s*(?:from|import)\s+(%s)\b" % "|".join(sorted(local)), re.M)
    for name in sorted(os.listdir(wf_dir)):
        hits = pattern.findall(open(os.path.join(wf_dir, name), encoding="utf-8").read())
        check("%s imports no local module inline" % name, not hits, repr(hits))


def check_engine_smoke_checks_the_import_not_a_phrase():
    """§26: a grep for a phrase nobody prints can never fail. The step must
    import the engine, which fails by name because every engine imports its
    driver at module level."""
    path = os.path.join(HERE, ".github", "workflows", "tests.yml")
    if not os.path.exists(path):
        skip("tests.yml", "no .github directory (the image)")
        return
    text = open(path, encoding="utf-8").read()
    check("engine-smoke imports the engine module", 'import ${{ matrix.engine }}_scraper' in text
          or "import ${{ matrix.engine }}_scraper" in text or "_scraper\"" in text, "see tests.yml")
    check("...and does not grep for 'could not be imported'", "could not be imported" not in text)


# Assembled from pieces, so this file can be scanned like every other rather
# than exempted (§22: the file most likely to acquire a stray phrase is the
# one a wholesale exemption never reads).
BANNED_WORDING = (
    "cloud" + " browser", "anti" + "detect browser", "2scraper " + "Anti" + "detect Browser",
    "gate." + "2prx.com", "ANTI" + "DETECT_LOCAL_API",
)


def check_banned_wording():
    """§12, enforced by this test rather than by review."""
    scanned = 0
    for root, dirs, files in os.walk(HERE):
        dirs[:] = [d for d in dirs if d not in (".git", "__pycache__", ".pytest_cache",
                                                "live", "captures", ".claude", ".venv",
                                                "venv")]
        for filename in files:
            if not filename.endswith((".py", ".md", ".yml", ".yaml", ".txt",
                                      ".toml", ".html", ".example", ".json")):
                continue
            path = os.path.join(root, filename)
            text = open(path, encoding="utf-8", errors="replace").read().lower()
            scanned += 1
            for phrase in BANNED_WORDING:
                if phrase.lower() in text:
                    check("%s contains no banned phrase #%d" % (
                        os.path.relpath(path, HERE), BANNED_WORDING.index(phrase)), False)
    check("the banned-wording scan read the repo (%d files)" % scanned, scanned > 20)


def check_no_donor_name_is_left_in_shipped_files():
    """A repo bootstrapped from another carries its donor's name in places no
    test looks: comments, help text, a workflow, a CHANGELOG."""
    donor = ("binance", "bbb-scraper", "rakuten", "P2P advert", "copy-trading",
             "announcement catalogue", "aws waf", "aws-waf", "amazontask")
    scanned = 0
    for root, dirs, files in os.walk(HERE):
        dirs[:] = [d for d in dirs if d not in (".git", "__pycache__", ".pytest_cache",
                                                "live", "captures", ".claude", ".venv",
                                                "venv")]
        for filename in files:
            if filename in ("smoke_test.py", "fixtures_generated.json", "LICENSE"):
                continue
            if not filename.endswith((".py", ".md", ".yml", ".yaml", ".txt", ".toml",
                                      ".html", ".example", ".cfg")) and filename != "Dockerfile":
                continue
            path = os.path.join(root, filename)
            text = open(path, encoding="utf-8", errors="replace").read()
            scanned += 1
            low = text.lower()
            for word in donor:
                if word.lower() in low:
                    idx = low.index(word.lower())
                    # A sibling named as PROVENANCE ("measured on rakuten") in
                    # a comment is history, not a leftover; a leftover is a
                    # URL, a variable or a sentence about THIS repo's site.
                    line = text[:idx].count("\n") + 1
                    check("%s:%d does not mention %r" % (os.path.relpath(path, HERE), line, word),
                          False, text.splitlines()[line - 1].strip()[:100])
    check("the donor-name scan read the repo (%d files)" % scanned, scanned > 20)


def check_concurrency_with_the_browser_stubbed():
    """§10: a live run cannot always reach this machinery. Driven through the
    SHARED worker loop with the fetch replaced."""
    import page_flow
    import queue as queue_mod
    fetched, lock = [], threading.Lock()
    real = page_flow.fetch_one_page

    def fake(ops, args, pool, query, page_num, mask=None, url=None):
        with lock:
            fetched.append(page_num)
        o = page_flow.PageOutcome(page_num=page_num, url="u")
        o.products = [] if page_num >= 6 else [object()]
        o.state = "empty" if page_num >= 6 else "content"
        return o

    work = queue_mod.Queue()
    for n in range(2, 51):
        work.put(n)
    results, rlock, exhausted = [], threading.Lock(), threading.Event()
    args = types.SimpleNamespace(delay=0)
    page_flow.fetch_one_page = fake
    try:
        threads = [threading.Thread(target=page_flow.worker_loop,
                                    args=(_FakeOps({}), args, None, work, results,
                                          rlock, exhausted, "w%d" % i))
                   for i in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(10)
    finally:
        page_flow.fetch_one_page = real
    check("every page fetched was fetched exactly once", len(fetched) == len(set(fetched)))
    check("dispatch STOPPED at the end of the list", exhausted.is_set())
    check("...so 49 queued pages cost far fewer fetches", len(fetched) < 15,
          "fetched %d" % len(fetched))
    equal("attempted + unattempted covers the whole queue",
          len(set(fetched)) + work.qsize(), 49)


def check_a_dead_worker_neither_hangs_nor_loses_its_siblings():
    engine = _import_engine("playwright_scraper")
    if engine is None:
        return
    import page_flow

    def exploding(ops, args, pool, query, page_num, mask=None, url=None):
        if page_num == 3:
            raise RuntimeError("worker died")
        o = page_flow.PageOutcome(page_num=page_num, url="u")
        o.products = [object()]
        o.state = "content"
        return o

    class FakeOps(_FakeOps):
        def __init__(self, *a, **k):
            super().__init__({})

        def open(self):
            return self

    class FakePlaywright:
        def __enter__(self):
            return None

        def __exit__(self, *a):
            return False

    real = (page_flow.fetch_one_page, engine._Ops, engine.sync_playwright)
    page_flow.fetch_one_page = exploding
    engine._Ops = FakeOps
    engine.sync_playwright = lambda: FakePlaywright()
    try:
        results, unattempted, exhausted = engine._fetch_pages_concurrently(
            types.SimpleNamespace(delay=0), None, None, list(range(2, 8)), 3)
    finally:
        page_flow.fetch_one_page, engine._Ops, engine.sync_playwright = real
    check("the dead worker's siblings still delivered their pages",
          len(results) >= 3, "%d results" % len(results))
    check("page 3 is not reported as a success", 3 not in [o.page_num for o in results])


def check_worker_pools_start_on_different_exits():
    import page_flow
    from proxy_pool import ProxyPool
    pool = ProxyPool(["http://a:1", "http://b:2", "http://c:3"], rotate="per-run")
    equal("three workers start on three different exits",
          len({page_flow.worker_pool(pool, i).current for i in range(3)}), 3)
    equal("a missing pool stays missing", page_flow.worker_pool(None, 0), None)


def check_concurrency_warnings_are_said_once_for_all_engines():
    import page_flow
    records = []

    class Grab(logging.Handler):
        def emit(self, record):
            records.append(record.getMessage())
    handler = Grab()
    logging.getLogger("page_flow").addHandler(handler)
    try:
        equal("--concurrency 1 is 1 and silent",
              page_flow.concurrency_for(types.SimpleNamespace(concurrency=1, cdp_endpoint=None), None), 1)
        equal("--concurrency with --cdp-endpoint is forced to 1",
              page_flow.concurrency_for(types.SimpleNamespace(concurrency=4, cdp_endpoint="ws://x"), None), 1)
        equal("--concurrency 4 with no pool is allowed",
              page_flow.concurrency_for(types.SimpleNamespace(concurrency=4, cdp_endpoint=None), None), 4)
    finally:
        logging.getLogger("page_flow").removeHandler(handler)
    check("...and warns that it multiplies the rate robots.txt asks for",
          any("Crawl-delay" in r for r in records), repr(records))


def check_fingerprint_kwargs_are_ones_the_driver_accepts():
    engine = _import_engine("playwright_scraper")
    if engine is None:
        return
    from fingerprint_client import playwright_context_kwargs
    import playwright.sync_api as pw_api
    sample = {"id": "x", "country": "US", "userAgent": "Mozilla/5.0 Chrome/140.0.0.0",
              "screen": {"width": 1920, "height": 1080},
              "timezone": "America/New_York", "language": "en-US", "devicePixelRatio": 2}
    kwargs = playwright_context_kwargs(sample)
    signature = inspect.signature(pw_api.Browser.new_context)
    unknown = [k for k in kwargs if k not in signature.parameters]
    check("every fingerprint kwarg is one new_context accepts", not unknown, repr(unknown))


def check_no_engine_overrides_the_user_agent_without_a_fingerprint():
    """§24: a bare UA override leaves the client hints reporting the real
    browser, and only a complete identity is safe. Nothing on this site asks
    for one, so no engine sets a user agent of its own outside --fingerprint."""
    for module in ENGINES:
        src = open(os.path.join(HERE, module + ".py"), encoding="utf-8").read()
        check("%s has no hard-coded Chrome user-agent literal" % module,
              not re.search(r"Mozilla/5\.0 \(Windows NT", src))
        check("%s defines no _chrome_ua helper" % module, "_chrome_ua" not in src)


def check_engines_do_not_evaluate_a_string_in_the_browser():
    """§18: wait_for_function evaluates a string, which a CSP without
    unsafe-eval kills. This site reads no page state through JavaScript at
    all, so no engine calls anything that evaluates."""
    for module in ENGINES:
        tree = ast.parse(open(os.path.join(HERE, module + ".py"), encoding="utf-8").read())
        called = {n.func.attr for n in ast.walk(tree)
                  if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
        for banned in ("wait_for_function", "waitForFunction", "waitFor", "evaluate",
                       "execute_script"):
            check("%s never CALLS %s" % (module, banned), banned not in called)


def check_credentials_never_reach_a_log():
    for module in ENGINES:
        engine = _import_engine(module)
        if engine is None:
            continue
        masked = engine._mask_credentials(
            "tried ws://u:supersecret@h1:9222 and ws://u:supersecret@h2:9222 "
            "and again ws://u:supersecret@h1:9222")
        check("%s masks EVERY occurrence" % module, "supersecret" not in masked, masked)
        check("%s keeps host and port" % module, "h1:9222" in masked and "h2:9222" in masked)
    from proxy_pool import mask
    masked = mask("http://user:secret@exit.example.com:2334")
    check("proxy_pool.mask hides the password", "secret" not in masked)
    check("proxy_pool.mask keeps the exit", "exit.example.com:2334" in masked)


def check_scraper_api_runs_the_shared_loop_and_reads_http_code():
    """Measured 2026-09-23 against the live Scraper API: a JSON-encoded
    STRING waitFor is answered HTTP 422 and still billed, and the target's
    status is `http_code`, while `status` is the API's own verdict. This
    client sends no waitFor at all (the site's data is in the markup)."""
    try:
        import scraper_api_client as sac
    except ImportError as e:
        skip("scraper_api_client", str(e))
        return
    sent = {}

    class Resp:
        status_code = 200
        headers = {}
        text = ""

        def json(self):
            return {"status": "success", "http_code": 503, "headers": {}, "body": "<html></html>"}

    def post(url, **kw):
        sent.update(kw.get("json") or {})
        sent["_headers"] = kw.get("headers")
        return Resp()

    args = types.SimpleNamespace(key="k" * 8, timeout=60, cdp_url=None)
    real = sac.requests.post
    sac.requests.post = post
    try:
        _html, status = sac.fetch_html(args, H + "news")
    finally:
        sac.requests.post = real
    equal("the target status handed onward is http_code, not the API's verdict", status, 503)
    equal("the task fetches the URL it was given, as raw", (sent["url"], sent["data_format"]),
          (H + "news", "raw"))
    check("no waitFor is sent", "waitFor" not in sent)
    check("the key rides in a header, never in the body", "k" * 8 not in json.dumps(
        {k: v for k, v in sent.items() if k != "_headers"}))
    ops = sac._Ops(types.SimpleNamespace(key="k" * 8, timeout=60, cdp_url=None))
    sac.requests.post = lambda *a, **k: (_ for _ in ()).throw(sac.requests.ConnectionError("boom"))
    try:
        status, html, err = ops.fetch(H + "news")
    finally:
        sac.requests.post = real
    check("a network failure is a page that never arrived (an error string, no crash)",
          status is None and html == "" and "boom" in err)
    # Driven through the real loop: a full listing, and a throttle.
    class Api(sac._Ops):
        def __init__(self, table):
            super().__init__(types.SimpleNamespace())
            self.table = table
            self.urls = []

        def fetch(self, url):
            self.urls.append(url)
            status, text = self.table[url]
            return status, text, None
    api = Api({H + "news": (200, fx("news"))})
    rc, meta, rows, _ = _run(api, pages=1)
    equal("the Scraper API engine drives the SAME loop: exit 0, 30 rows", (rc, len(rows)), (0, 30))


def check_x_debug_header_is_redacted():
    try:
        import scraper_api_client as sac
    except ImportError:
        return
    pw = "SeCr" + "EtPw"
    key = "abcdef01" * 4
    raw = ("cdpurl=ws://acct-zone-scraping_browser-pid-7:" + pw
           + "@cb.2captcha.com:9222 cost=0.00145 key=" + key + " status=200")
    out = sac._redact_debug_header(raw)
    check("x-debug: the credential and the key are gone", pw not in out and key not in out)
    check("x-debug: the cost, host and status survive",
          "cost=0.00145" in out and "cb.2captcha.com:9222" in out and "status=200" in out)
    check("x-debug: the log line calls the redactor",
          'logger.info("x-debug: %s", _redact_debug_header(debug))' in inspect.getsource(sac))


def check_captcha_capability_claims_match_the_code():
    """§19: the most expensive bug this family can ship is a SENTENCE. This
    repo makes ONE claim about captchas and it is a claim about the repo."""
    readme = open(os.path.join(HERE, "README.md"), encoding="utf-8").read()
    low = readme.lower()
    for phrase in ("cannot be solved", "can't be solved", "is not solvable",
                   "solver is inapplicable", "no solver can", "unsolvable"):
        check("README: no %r — write 'this repo does not implement X'" % phrase,
              phrase not in low)
    check("README says the repo does not implement captcha solving",
          "does not implement captcha solving" in low)
    check("...and says why: none was seen", "none" in low and "no captcha" in low)
    check("README quotes the Scraper API's price as measured ($0.0005 a task)",
          "$0.0005" in readme or "`$0.0005`" in readme)


def check_readme_numbers_are_not_stale():
    """§17's check #4: a claim in the README is a claim about the code."""
    readme = open(os.path.join(HERE, "README.md"), encoding="utf-8").read()
    from output_writer import ROW_CLASS_BY_MODE
    sizes = {len(fields(c)) for c in ROW_CLASS_BY_MODE.values()}
    for number in re.findall(r"(\d+)\s+columns", readme):
        check("the README's '%s columns' is a row class's size" % number,
              int(number) in sizes, "sizes are %s" % sorted(sizes))
    import product_parser as P
    import page_flow
    for claim, value in (("30 stories", P.PAGE_SIZE),):
        if claim in readme:
            equal("the README's %r matches the code" % claim, value, int(claim.split()[0]))
    if "Crawl-delay" in readme:
        check("the README's 30-second default is the code's",
              "30" in readme and page_flow.DEFAULT_DELAY_S == 30.0)
    for flag in re.findall(r"`(--[a-z][a-z0-9-]+)`", readme):
        known = _argparse_flags("playwright_scraper") | _argparse_flags("scraper_api_client") | \
            {"--secret-check", "--sample-check", "--help-check", "--force", "--old", "--new",
             "--fail-on-change", "--chromedriver", "--chromium-path", "--key", "--cdp-url",
             "--timeout", "--tags", "--country", "--render", "--captures", "--verbose",
             "--no-sandbox", "--proxy-server", "--help"}
        check("README names only flags that exist: %s" % flag, flag in known)


_TREE_BEFORE = None


def _tree_state():
    result = subprocess.run(["git", "status", "--porcelain"], cwd=HERE,
                            capture_output=True, text=True)
    if result.returncode != 0:
        return None
    return sorted(line for line in result.stdout.splitlines() if not line.endswith(".pyc"))


def check_no_test_mutates_the_working_tree():
    if _TREE_BEFORE is None:
        skip("git status", "not a git repository")
        return
    changed = sorted(set(_tree_state()) - set(_TREE_BEFORE))
    check("the suite itself changed nothing in the working tree", not changed, repr(changed))


CHECKS = [v for k, v in sorted(globals().items()) if k.startswith("check_")]


def main():
    global VERBOSE, _TREE_BEFORE
    parser = argparse.ArgumentParser(description="hackernews-scraper offline suite")
    parser.add_argument("-v", "--verbose", action="store_true")
    VERBOSE = parser.parse_args().verbose
    _TREE_BEFORE = _tree_state()
    for fn in CHECKS:
        if VERBOSE:
            print("\n== %s" % fn.__name__)
        try:
            fn()
        except Exception as e:  # noqa: BLE001 — a broken check is a failure
            import traceback
            FAILURES.append("%s raised %s: %s" % (fn.__name__, type(e).__name__, e))
            print("  ERROR %s raised %s: %s" % (fn.__name__, type(e).__name__, e))
            if VERBOSE:
                traceback.print_exc()
    print("\n%d checks passed, %d failed, %d group(s) skipped."
          % (PASSED, len(FAILURES), len(SKIPS)))
    for line in SKIPS:
        print("  skipped: %s" % line)
    if FAILURES:
        print("\nFailures:")
        for line in FAILURES:
            print("  - %s" % line)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
