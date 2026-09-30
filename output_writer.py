"""
output_writer.py
-----------------
Shared row models + JSON/CSV writers used by all three scrapers.

Three modes, three row classes
------------------------------
    --mode listing   a story list (front page, newest, Ask, Show, jobs, best,
                     a user's submissions, everything from one domain)
                     -> Story
    --mode item      one item's comment tree                  -> Comment
    --mode user      one profile                              -> User

Three kinds of thing, so three dataclasses (the family allows a second one
where a repo genuinely reads more than one kind, §9). What holds them
together is the id: every class carries the site's own id in `sku`, which is
the HN item id for a Story and a Comment (they are one id space: a comment's
`story_id` and `parent_id` are Story ids or Comment ids), and the username
for a User. So one consumer joins the three files on a column with one name.

The dedupe key differs, and the sidecar's `mode` says which is in force:

    Story     `sku` -- an item appears once per run however the pages shifted
    Comment   `sku` -- each comment is its own item
    User      `sku` -- one row

What a cross-mode `diff_runs.py` would mean is nothing: two runs of DIFFERENT
modes hold different kinds of rows, and it refuses (§9).

Something specific to a site whose text is written by its users: **every
string in a Comment and a User was typed by a stranger.** That is why the
CSV writer neutralises formula-shaped cells (see `_csv_escape`) and why the
sidecar counts how many it touched. A comment beginning `=` or `+` or `-` is
ordinary on this site (a list, a diff, a minus sign) rather than an attack.

Everything below is row-class-agnostic: pass `row_cls` so an empty CSV still
gets the right header for the mode that produced it.
"""

import contextlib
import csv
import json
import os
import tempfile
from dataclasses import dataclass, asdict, field, fields
from datetime import datetime, timezone
from typing import Optional, List, Set, Sequence, Any, Type


# The hostname a row came from. One site, one host: every row of every run
# says `news.ycombinator.com`, and which page kind it came from is `mode` in
# the sidecar. Kept as a column because the family's schema has it first and
# consumers read the columns by name across repos.
SOURCE_DEFAULT = "news.ycombinator.com"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class Story:
    """One row per submission on a listing page.

    `source, scraped_at, url, sku, title` open every row class in this repo,
    in this order. Ratings and prices do not exist here, so the rest is what
    a story states.

    Columns that are null BY DESIGN on some rows, so a reader is not left to
    guess (§9): `author`, `points` and `comments` are null on a job posting,
    which has none of the three -- a fact about the site, and the only way a
    row's `kind` is `job`. `rank` is the number the site prints beside the
    row, which continues across pages (page 2 starts at 31) and is null only
    where a page prints none. `domain` is null on a text post.
    """
    source: str = SOURCE_DEFAULT
    scraped_at: str = field(default_factory=_now)
    # Where the story POINTS: the article for a link post, the item page for
    # a text post (Ask HN, a poll). Absolute, as the page links it.
    url: str = ""
    # The HN item id, as a string. The same id space as a Comment's `sku`.
    sku: Optional[str] = None
    title: Optional[str] = None
    author: Optional[str] = None
    # An integer from "538 points". "1 point" is 1. Null on a job.
    points: Optional[int] = None
    # From "231 comments" / "1 comment". "discuss" is what the site prints
    # for a story nobody has commented on, so it is 0 rather than unknown.
    # Null on a job, which has no discussion link at all.
    comments: Optional[int] = None
    # The moment the site stamps on the row, ISO 8601, UTC. Never the
    # "9 hours ago" beside it: that text is only true at the second it was
    # scraped and reads differently on the next run.
    posted_at: Optional[str] = None
    # The host the site prints beside a link post, e.g. github.com. The site
    # SHORTENS it (drops www.), and a domain-listing URL takes it as printed.
    domain: Optional[str] = None
    # story | ask | show | job | text -- see product_parser._kind.
    kind: Optional[str] = None
    # The rank the site prints, 1-based across the whole list.
    rank: Optional[int] = None
    # The discussion page, always on news.ycombinator.com, whatever `url` is.
    hn_url: Optional[str] = None
    page: Optional[int] = None
    position: Optional[int] = None


@dataclass
class Comment:
    """One row per comment, in thread order.

    `sku` is the comment's own item id, and `url` its permalink. `story_id`
    and `parent_id` are item ids in the same space, so a consumer rebuilds the
    tree from two columns; `depth` is the site's own indent level (0 for a top
    level comment) and `position` counts rows in document order from 1.

    `status` is the site's word for a comment it will not show as normal:
    `flagged`, `dead` or `deleted`, otherwise `ok`. A flagged comment keeps
    its place and its author but its text is the single word `[flagged]`, so
    `text` there is the site's placeholder, not what the author wrote.
    """
    source: str = SOURCE_DEFAULT
    scraped_at: str = field(default_factory=_now)
    url: str = ""
    sku: Optional[str] = None
    story_id: Optional[str] = None
    parent_id: Optional[str] = None
    author: Optional[str] = None
    posted_at: Optional[str] = None
    depth: Optional[int] = None
    position: Optional[int] = None
    status: Optional[str] = None
    # Plain text: paragraphs are blank-line separated, entities decoded.
    text: Optional[str] = None
    # The addresses linked from the text, absolute. The visible text of a
    # long link is shortened by the site, so the full address is only here.
    links: Optional[List[str]] = None


@dataclass
class User:
    """One row per profile."""
    source: str = SOURCE_DEFAULT
    scraped_at: str = field(default_factory=_now)
    url: str = ""
    # The username, which is the site's own id for a user.
    sku: Optional[str] = None
    # ISO 8601, UTC, from the unix time the profile page carries.
    created_at: Optional[str] = None
    karma: Optional[int] = None
    # Plain text of the free-form "about" box; null when it is empty.
    about: Optional[str] = None
    about_links: Optional[List[str]] = None


# Kept under the family's name: the workflow files, a sibling's diff tool and
# a user's script may say `Product`, and a rename that breaks them all for
# the sake of tidiness is a poor trade. A Story is the row a listing run is
# about, which is what `Product` means in the shop scrapers.
Product = Story

ROW_CLASS_BY_MODE = {"listing": Story, "item": Comment, "user": User}

# Every mode's rows are one-per-`sku` and safe to dedupe on it.
UNIQUE_BY_SKU_MODES = ("listing", "item", "user")


def dedupe_by_key(rows: Sequence[Any], seen: Set[str], key: str = "sku") -> List[Any]:
    """Drop rows whose key already appeared earlier in this same run.

    `seen` is mutated in place, so callers thread the same set across pages —
    a stale or repeating next-page link then re-parses a page without
    duplicating its rows into the final output. On this site it fires on
    healthy runs of a LIVE list: /newest gains rows between two page fetches,
    so the last row of page 1 is the first row of page 2. A small non-zero
    drop count here is expected; a large one means the run is re-reading a
    page.

    A row with no key is always kept: there is nothing to check a duplicate
    against, and dropping it would be a silent data loss rather than a
    duplicate removal.

    All three of this repo's modes are one row per `sku`, so `key` is never
    overridden here — the parameter exists because the rest of the family
    shares this function and one of them needs it.
    """
    fresh = []
    for r in rows:
        val = getattr(r, key, None)
        if val is None or val not in seen:
            if val is not None:
                seen.add(val)
            fresh.append(r)
    return fresh


# Kept under its old name: the engines and smoke tests in this family all
# call it, and a listing run does dedupe by sku.
def dedupe_by_sku(rows: Sequence[Any], seen: Set[str]) -> List[Any]:
    return dedupe_by_key(rows, seen, key="sku")


# CSV cannot hold a list. Joining with " | " keeps the cell readable in a
# spreadsheet and round-trippable by splitting on the same separator; the
# JSON output keeps the real list, so nothing is lost for a consumer that
# wants structure. `repr()` of a Python list (the default if this is not
# handled) is neither readable nor parseable by anything but Python.
LIST_CSV_SEPARATOR = " | "



@contextlib.contextmanager
def _atomic(path: str, newline: Optional[str] = None):
    """Write to a temporary file beside `path`, then rename over it.

    `open(path, "w")` truncates before a single byte is written, so a crash,
    a kill or a full disk halfway through `json.dump` leaves a SHORTER file
    where a complete one was. That is the same promise `save` keeps when it
    refuses to overwrite good output with an empty result, broken by a
    different route: the previous good run is destroyed by the attempt to
    replace it rather than by its outcome. Measured here before the fix — a
    2,084-byte good `out.json` came back 0 bytes and invalid JSON after an
    interrupted write.

    The temporary file goes in the SAME directory on purpose: `os.replace`
    is only atomic within one filesystem, and a temp file under /tmp can be
    on another one, where the rename degrades to a copy that can itself be
    interrupted. `fsync` before the rename is what makes the content durable
    rather than merely visible, which matters on power loss.
    """
    directory = os.path.dirname(os.path.abspath(path)) or "."
    handle = tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", newline=newline, dir=directory,
        prefix=os.path.basename(path) + ".", suffix=".tmp", delete=False)
    try:
        with handle:
            yield handle
            handle.flush()
            os.fsync(handle.fileno())
        # `NamedTemporaryFile` creates the file 0600, and a rename keeps the
        # temp file's mode, so without this every output would come out
        # readable by its owner alone -- a regression from `open(path, "w")`,
        # which honours the umask. Measured on the first live run of this
        # repo: `-rw-------` on all three files. An existing target keeps its
        # own mode (someone may have tightened it on purpose); a new one gets
        # what `open()` would have given it.
        try:
            mode = os.stat(path).st_mode & 0o777
        except OSError:
            umask = os.umask(0)
            os.umask(umask)
            mode = 0o666 & ~umask
        os.chmod(handle.name, mode)
        os.replace(handle.name, path)
    except BaseException:
        try:
            os.unlink(handle.name)
        except OSError:
            pass
        raise


# A spreadsheet treats a cell beginning with one of these as a FORMULA, not
# as text, and every string in a Comment or a User here was typed by a stranger
# rather than written by this code. `=HYPERLINK(...)`, `+cmd|...` and `@SUM(...)` are the
# classic shapes; the tab and the newline are here because a leading one is
# stripped by some readers, which exposes whatever follows it.
#
# On this site it is not hypothetical: a comment that opens with `-` (a list
# item, a diff hunk, a minus sign) or `=` is ordinary, and the text is written
# by whoever posted it. See the README for the count measured on the run it
# was written against.
CSV_FORMULA_LEADS = ("=", "+", "-", "@", "\t", "\r", "\n")

# Prefixed to a cell that would otherwise be read as a formula. Excel, Google
# Sheets and LibreOffice all treat the apostrophe as "the rest of this cell
# is text" and do not show it; a consumer parsing the CSV with `csv` sees it,
# which is why the count goes in the sidecar rather than staying silent.
CSV_FORMULA_ESCAPE = "'"

def _csv_value(v: Any) -> Any:
    if isinstance(v, (list, tuple)):
        return LIST_CSV_SEPARATOR.join(str(x) for x in v)
    # `tags` and each entry of `variants` are mappings, and a Python repr
    # of one is neither readable in a spreadsheet nor parseable by anything
    # but Python.
    # Compact JSON is both, and round-trips through json.loads.
    if isinstance(v, dict):
        return json.dumps(v, ensure_ascii=False, separators=(",", ":"))
    return v



def _csv_escape(v: Any) -> Any:
    """Neutralise a formula-shaped cell. Returns (value, was_escaped).

    Only STRINGS are touched. Escaping a number would turn `-5` into the text
    `'-5` and break every sum a consumer writes over the column — the risk
    being defended against is text that a stranger chose, and a numeric field
    of ours is not that.

    CSV only. The JSON output keeps the site's bytes exactly as served, so
    the two files deliberately differ; `csv_cells_escaped` in the sidecar is
    what declares that divergence instead of leaving it to be discovered.
    """
    if isinstance(v, str) and v.startswith(CSV_FORMULA_LEADS):
        return CSV_FORMULA_ESCAPE + v, True
    return v, False

def write_json(rows: Sequence[Any], path: str) -> None:
    with _atomic(path) as f:
        json.dump([asdict(r) for r in rows], f, ensure_ascii=False, indent=2)


def write_csv(rows: Sequence[Any], path: str, row_cls: Type = Story) -> int:
    # An empty result still gets the header row. A zero-byte file makes a
    # consumer fail on read (no columns to parse) instead of reading a valid
    # table with zero rows — and "an empty result is still a well-formed
    # result" is the same principle as `save` refusing to overwrite good data.
    #
    # The header comes from `row_cls`, not from the first row, so an empty
    # run still writes the columns of the mode that produced it.
    fieldnames = [f.name for f in fields(row_cls)]
    escaped = 0
    # `newline=""` is the csv module's documented requirement and has to
    # survive the move to a temp file, or every row gains a stray CR on
    # Windows.
    with _atomic(path, newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for r in rows:
            row = {}
            for k, v in asdict(r).items():
                # Escape AFTER `_csv_value`: a list joined into one cell is
                # text too, and its first element can be formula-shaped
                # while the list itself is not a string.
                value, was_escaped = _csv_escape(_csv_value(v))
                row[k] = value
                escaped += was_escaped
            writer.writerow(row)
    return escaped


# Exit code used when a run completes but produced nothing. Distinct from 1
# (crash) so a caller can tell "ran, found nothing" from "blew up".
EXIT_NO_PRODUCTS = 4

# Exit code for a run blocked by a bot-check/challenge page before parsing
# even started — distinct from EXIT_NO_PRODUCTS so a caller can tell "the
# search genuinely matched nothing" from "something stood between us and the
# content". See product_parser.detect_bot_challenge.
#
# On this site this code specifically does NOT cover the ways to get a real
# answer with no rows in it: `No such item.` and `No such user.` (plain text,
# HTTP 200), a `front` day the site has no data for, and one page past the
# end of a listing. All are EXIT_NO_PRODUCTS — the request was served exactly
# as asked and there is nothing there. Reporting any of them as blocked would
# send a user hunting for a proxy problem that does not exist.
#
# What EXIT_BLOCKED means here: HTTP 403. NOT OBSERVED by this repo, which
# was served by every address it tried; the state is kept because a site that
# throttles by IP has a ban to go with it.
EXIT_BLOCKED = 3

# Exit code for a run that gathered SOME rows and then stopped early — a
# page-load timeout, a 503 throttle, or a challenge on page 3 of 10. The
# output file is still written (throwing away three good pages would be
# worse), but it is not a complete picture, and a consumer that cannot tell
# the difference will read the pages that were never fetched as stories that
# dropped off the list. See write_run_meta.
# A REMOTE service failed — the Scraping Browser refusing the connection
# (`profile_locked` is the common one: a profile allows a single live
# connection), or the Scraper API answering an error. Distinct from 1 (a
# crash in this code) and from 2 (bad usage) because it means "try again, or
# use a different profile", not "there is a bug here". Defined once, here,
# because the browser engines and scraper_api_client.py both return it and
# two definitions of the same code is exactly how a family's exit contract
# drifts.
EXIT_API_ERROR = 5

EXIT_PARTIAL = 6


# Exit code for a run that never GOT its pages: a navigation timeout, a dead
# or unauthenticated proxy, a DNS failure, or an edge answering with
# something that is not the page that was asked for.
#
# Distinct from EXIT_NO_PRODUCTS because those are opposite facts. Exit 4 is
# a statement about the SITE — "we asked, and the answer was nothing" —
# so handing it to a run that never reached the site tells a pipeline the
# listing is empty when nothing was read at all.
#
# 5 rather than a new number, and 5 rather than EXIT_PARTIAL:
#
#   * this family's contract already reserves 5 for a transport failure
#     (scraper_api_client has used it for a remote API error since it was
#     written), so this needs no new code and no per-repo table for a caller
#     driving more than one of these scrapers;
#   * EXIT_PARTIAL (6) means "some rows were gathered and the output is
#     incomplete". A run holding nothing writes no output at all, so a
#     consumer that reads the file on a 6 finds either nothing or the
#     PREVIOUS run's good data, which `save` deliberately does not
#     overwrite. Exit 5 promises no file.
#
# Deliberately NOT applied when rows WERE gathered: a timeout on page 7 of
# 10 is a partial run (exit 6, output written), which is already right. This
# decides only what a run holding nothing reports.
EXIT_FETCH_FAILED = 5


def write_run_meta(out_prefix: str, meta: dict) -> str:
    """Write a run-metadata sidecar next to the output, return its path.

    Deliberately a separate `<out>.meta.json` rather than columns on every
    row: this describes the RUN, not the product, and repeating it across
    every row would both bloat the output and change the schema every
    consumer of this project already parses.

    diff_runs.py reads it to refuse a comparison between runs that are not
    both complete, and between runs of different `mode`.
    """
    path = f"{out_prefix}.meta.json"
    # Atomic for the same reason as the rows, and more urgently: this is the
    # file a consumer BRANCHES on, so a truncated sidecar beside good rows is
    # worse than truncated rows — `status` unreadable reads as a broken run
    # over data that is fine.
    with _atomic(path) as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)
    print(f"[+] Wrote run metadata -> {path} (status={meta.get('status')})")
    return path


def run_meta(status: str, stop_reason: str, pages_requested: int,
             pages_completed: int, start_url: str, final_url: str,
             products: int, pages_failed: Optional[List[int]] = None,
             mode: str = "listing", source: str = SOURCE_DEFAULT,
             extra: Optional[dict] = None) -> dict:
    """Build the metadata dict for a finished run.

    `status` is the field a consumer branches on:
      complete — every requested page was fetched, or the site's own
                 pagination genuinely ran out (nothing more existed to get)
      partial  — rows were gathered, then the run stopped early
      failed   — nothing was gathered at all

    `mode` and `source` are recorded because `mode` is not implied by the
    repo: the same output prefix can hold a listing run or an item run,
    and those hold different KINDS of row (a Story, a Comment, a User), so
    a diff across them would report every row as changed. diff_runs.py
    refuses a pair whose modes or sources differ. `source` is
    `news.ycombinator.com` on every row of every run here; it is kept because
    consumers read these columns by name across the family.

    `extra` carries facts about the run that are not about any single row:
    the language each page was served in, and the site's own cap arithmetic
    for the query as page 1 states it. They belong to the run rather than
    repeated down a column.

    `pages_failed` lists the pages that did not yield data, by number.
    `pages_completed` alone was enough only while pages were fetched strictly
    in order, where "3 of 10 completed" could only mean 1-2-3: a count is not
    a description once pages can be fetched independently and page 3 can fail
    while 4 and 5 succeed. Recording the numbers keeps the sidecar honest
    about WHICH part of the list is missing, not just how much.
    """
    meta = {
        "source": source,
        "mode": mode,
        "status": status,
        "stop_reason": stop_reason,
        "pages_requested": pages_requested,
        "pages_completed": pages_completed,
        "pages_failed": pages_failed or [],
        # The family's name for the row count. A row here is a story, a
        # comment or a profile; consumers read this key by name across repos.
        "products": products,
        "start_url": start_url,
        "final_url": final_url,
        "finished_at": datetime.now(timezone.utc).isoformat(),
    }
    if extra:
        # Merged rather than nested under a key, so a consumer reads
        # `total_comments` at the top level beside `products`. Run fields win a
        # name collision: a caller cannot accidentally overwrite `status`.
        meta.update({k: v for k, v in extra.items() if k not in meta})
    return meta


def save(rows: Sequence[Any], out_prefix: str, fmt: str,
         allow_empty: bool = False, row_cls: Type = Story,
         stats: Optional[dict] = None) -> int:
    """Write JSON/CSV and return a process exit code.

    Returns 0 when rows were written, EXIT_NO_PRODUCTS when there were none.
    Callers are expected to exit with it.

    On zero rows, nothing is written at all unless `allow_empty`. Two reasons,
    and a live run demonstrated both. A page-load timeout produced
    `Saved 0 rows -> out.json` and exit 0: a two-byte `[]` that a
    consuming pipeline reads as a successful run with no stock. Worse, if the
    file already held a good result from an earlier run, that result is now
    gone — the failure destroyed the last known good data. So an empty result
    leaves the previous file intact and says why.

    `allow_empty=True` is for the legitimate case: a filter that genuinely
    matches nothing, where an empty file is the answer.
    """
    if not rows and not allow_empty:
        print(f"[!] 0 rows — refusing to write {out_prefix}.json/.csv, so an "
              f"earlier good result isn't overwritten with an empty one. "
              f"Pass --allow-empty if an empty result is the expected answer.")
        return EXIT_NO_PRODUCTS

    if fmt in ("json", "both"):
        write_json(rows, f"{out_prefix}.json")
        print(f"[+] Saved {len(rows)} rows -> {out_prefix}.json")
    if fmt in ("csv", "both"):
        escaped = write_csv(rows, f"{out_prefix}.csv", row_cls=row_cls)
        print(f"[+] Saved {len(rows)} rows -> {out_prefix}.csv")
        # Reported through `stats` rather than as a return value because
        # this function's return IS the exit code and every engine exits
        # with it. The caller threads it into the sidecar, where a consumer
        # comparing the CSV against the JSON can see that they diverge and
        # by how much, instead of finding a stray apostrophe and guessing.
        if stats is not None:
            stats["csv_cells_escaped"] = escaped
        if escaped:
            print(f"[!] {escaped} CSV cell(s) began with a formula character "
                  f"and were prefixed with {CSV_FORMULA_ESCAPE!r} so a "
                  f"spreadsheet reads them as text. The JSON output is "
                  f"unchanged — see csv_cells_escaped in the sidecar.")
    return 0 if rows else EXIT_NO_PRODUCTS


# Stop reasons that mean the run saw everything there was to see. Anything
# else ended the page loop early, so the result is only a partial view.
#
# "no_new_products" belongs here and "pagination_exhausted" is kept for the
# engines that still stop on a missing next-link: the first is a property of
# the DATA (a page contributed nothing not already seen, so the listing is
# over), while the second is a property of a CSS SELECTOR and is therefore
# the weaker signal — a renamed attribute looks identical to a short
# list. Here the site's own "More" link is the primary signal and an empty
# page the second: a front-page listing past its last page is a 2 KB page of
# chrome with no rows and no link, HTTP 200.
#
# "single_page_mode" is complete by construction: --mode item and --mode user
# read one page because one page is all there is.
# `end_of_listing` is a COMPLETE result and leaving it out of this tuple is a
# bug worth naming: a sibling produced exit 6 for a correct run that way.
# Reaching the real end of a list means the run has everything the site will
# give it.
COMPLETE_STOP_REASONS = ("completed", "pagination_exhausted", "no_new_products",
                         "single_page_mode", "end_of_listing")


def finish_run(rows: Sequence[Any], out_prefix: str, fmt: str,
               allow_empty: bool, *, blocked: bool, stop_reason: str,
               pages_requested: int, pages_completed: int,
               start_url: str, final_url: str,
               pages_failed: Optional[List[int]] = None,
               mode: str = "listing", source: str = SOURCE_DEFAULT,
               extra: Optional[dict] = None) -> int:
    """Write output + the run-metadata sidecar; return the exit code.

    Shared by all three browser engines so the status/exit-code mapping
    cannot drift between them.

    The metadata sidecar is written ONLY when the row file was written.
    Otherwise a failed run would leave a "status": "failed" sidecar next to
    the previous run's still-intact good output (which `save` deliberately
    does not overwrite) — the two files would contradict each other, and
    diff_runs.py would refuse to compare data that is in fact fine.
    """
    # Completeness is decided by the reason AND by the evidence. A named
    # list of stop reasons cannot cover a failure recorded somewhere else,
    # and `pages_failed` is somewhere else: a run whose loop ended for a
    # COMPLETE reason while individual pages failed reported exit 0 and
    # `status: complete` with a non-empty `pages_failed` in the same
    # sidecar — a file that contradicts itself, and a pipeline branching
    # on `status` reading a short run as a whole one.
    #
    # Found by a third-party audit of a sibling repo and measured across
    # the family by CALLING each `finish_run` rather than grepping for the
    # fix: 28 of 32 repos behaved this way. Same shape as the exit-code
    # unification this file already carries — a rule keyed on a list of
    # names has a hole for every name nobody added to it.
    complete = stop_reason in COMPLETE_STOP_REASONS and not pages_failed
    row_cls = ROW_CLASS_BY_MODE.get(mode, Story)
    write_stats: dict = {}
    rc = save(rows, out_prefix, fmt, allow_empty=allow_empty, row_cls=row_cls,
              stats=write_stats)
    wrote_output = bool(rows) or allow_empty

    if wrote_output:
        status = "complete" if (rows and complete) else (
            "partial" if rows else "failed")
        # The CSV/JSON divergence is a property of THIS run's output, so it
        # belongs beside the other run facts. Merged under the caller's
        # `extra` rather than over it: an engine's own extras win, because a
        # collision here would be this code silently dropping site data.
        if write_stats:
            extra = {**write_stats, **(extra or {})}
        write_run_meta(out_prefix, run_meta(
            status=status, stop_reason=stop_reason,
            pages_requested=pages_requested, pages_completed=pages_completed,
            pages_failed=pages_failed, mode=mode, source=source,
            start_url=start_url, final_url=final_url, products=len(rows),
            extra=extra))

    if not rows:
        # Nothing gathered at all, and WHY decides the code. The three
        # outcomes are different facts and a pipeline branches on them
        # (blocked is not empty is not "never reached"):
        #
        #   blocked            something stood between the run and the content
        #   did not complete   we never got the pages — a dead proxy, a load
        #                      timeout, an edge serving something else
        #   completed          we asked, and the answer was nothing
        #
        # Keyed on `not complete` rather than on a list of stop reasons, on
        # purpose: a list cannot cover a reason nobody has added to it yet,
        # so a new one falls silently through to "the list is empty" —
        # which is the defect this branch exists to prevent.
        if blocked:
            return EXIT_BLOCKED
        if not complete:
            print(f"[!] Nothing was gathered and the run did not finish "
                  f"({stop_reason}) — exit {EXIT_FETCH_FAILED}, NOT an empty "
                  f"result (exit {EXIT_NO_PRODUCTS}). Nothing can be "
                  f"concluded about the site from this run.")
            return EXIT_FETCH_FAILED
        return rc
    if not complete:
        print(f"[!] Partial run: stopped after {pages_completed} of "
              f"{pages_requested} page(s) ({stop_reason}). The output holds "
              f"what was gathered, but it is NOT a complete view — see "
              f"{out_prefix}.meta.json.")
        return EXIT_PARTIAL
    return rc
