# Contributing

Bug reports, site-change reports and pull requests are all welcome. This file
covers the few things specific to a scraper, which are not the usual ones.

## Before you open anything

Run the offline suite. It needs no network, no browser and no API key, and takes
about a second:

```bash
pip install -r requirements.txt
python3 smoke_test.py
pytest
```

It prints its own check count, and lists any group it had to skip because an
engine library is absent.

**The suite must pass with no engine installed at all.** CI installs only
`beautifulsoup4` and `requests`, so any import of `playwright_scraper`,
`puppeteer_scraper` or `selenium_scraper` in a test has to sit inside
`try/except ImportError` with the skip recorded. This is easy to get wrong
locally, where you almost certainly have an engine installed and an unguarded
import passes.

If the suite fails on a clean clone, that is itself the bug — say so.

## Never commit a credential

`.env` is in `.gitignore`. Keep it there.

The scrapers mask `user:pass@` in their own log lines, but three things are **not**
masked: raw HTML dumps, the Scraper API's `x-debug` response header, and your
shell history. Before pasting any output into an issue or a PR, replace keys,
proxy passwords and full `ws://user:pass@host:9222` endpoints with `***`.

CI fails the build if something that looks like a credential is committed. That
check is a backstop, not a review — a leaked key has to be rotated whether or
not the check caught it.

## Reporting a site change

Hacker News changing its markup is the normal way this stops working, and it
has its own issue template. The parser reads the site's server-rendered HTML
(`product_parser.py` holds every selector), so a break is usually a renamed
class or a changed layout. Rows are matched by class token, not by the whole
attribute: comment rows carry extra classes (`coll`, `noshow`), and an exact
match on the class string loses them.

If you are reporting a break, attach the `--dump-html` output: the exact bytes
the parser was given, on success as well as failure. **It contains usernames
and strangers' comment text**, so trim it to the few rows that show the
problem.

## Fixtures and captures

`smoke_test.py` runs against `fixtures_generated.json`, which
`make_fixtures.py` regenerates from real captures kept in `captures/`. That
directory is deliberately out of git, because a raw capture carries usernames,
strangers' comment text and per-session `auth=` tokens (item pages carry one in
the `favorite` link and a `hmac` value in the comment form, both handed to an
anonymous visitor; what they would authorise was not tested, so treat them as
credentials). **Scrub a capture before any of it is committed**:
replace usernames, comment text and every `auth=` value with obvious
placeholders, leave everything the site generates around them untouched, and
say which fixtures are not verbatim. The credential scan guards against the
token shape, not against a person's words.

## Before this repository goes public

One item cannot be undone later, so it belongs on a checklist rather than in
someone's head. **A commit on top cannot reach what a published tag and a
merged PR's refs already hold** — those stay attached to the PR and cannot be
deleted from it. Afterwards, only a fresh repository removes anything.

```bash
python3 .github/ci_checks.py --history-check
```

That applies the same credential rules CI enforces to **every blob that has
ever existed**, not just the working tree. It is deliberately not part of
`--all` and not run by CI: it shells out to git once per object, and a dirty
history needs a decision, not a red check on every push.

Then the rest of the presentation, in the order that matters:

1. `python3 smoke_test.py` green, and the canary dispatched at least once.
   It runs weekly (Mondays) with no secrets at all and is expected to be green, because
   no mode needs a credential and a green badge there is exactly the claim
   the README makes.
2. The repo description, homepage and topics set (see the family notes on
   what those should say).
3. Only then the row in the org profile README — and check it with an
   ANONYMOUS request rather than your own logged-in browser. A row pointing
   at a private repo is a 404 for every visitor, which costs more trust than
   the missing row.

## Pull requests

**Add a test for the behaviour you are changing.** `smoke_test.py` is a single
file of plain functions. Its fixtures are real pages, trimmed, in
`fixtures_generated.json`. Copy the nearest existing check and edit it.

Properties in this repo that are measured rather than assumed, and that tests
pin so a PR that breaks one fails instead of silently regressing:

- **Parameters the site answers wrongly are validated before sending.** The
  site serves a plausible page for `front?day=garbage` (the latest day) and for
  `news?p=abc` and `p=0` (page 1). Each turns a typo into a run that looks
  healthy.
- **"Nothing there" is a plain-text HTTP 200.** `No such item.`,
  `No such user.`, `We don't have that data yet.` and `HN didn't exist yet.`
  are exit 4, never a retry and never a block.
- **A listing past its end is a 2 KB page with no rows and no More link**,
  which ends the run as complete, not as an error.
- **Cursor lists cannot be addressed independently.** `/newest`, `/jobs`,
  `/submitted` and `/from` continue with `?next=ID&n=31` in the More link, so
  they are walked link to link and `--concurrency` is ignored for them.
- **Flagged comments are kept**, with the body `[flagged]` and status
  `flagged`; the site's own count leaves them out, so `comments` differs from
  the number of rows in a thread.
- **CSV cells that start with `=`, `+`, `-`, `@` or a tab are prefixed with an
  apostrophe** (CSV only) and counted in the sidecar as `csv_cells_escaped`,
  because comment text is typed by strangers.
- **The listings are live**, so a multi-page run can see a row twice. The
  dedupe drops it and the log says so. A non-zero count there is the site
  moving, not a bug.

Plus the family's own invariants, which are not negotiable:

- **A run that finds nothing writes nothing.** It must not replace a good
  output file with `[]`. `--allow-empty` is the opt-out. Writes are atomic.
- **Exit codes are a contract**, not decoration: `0` ok, `1` crash, `2` bad
  usage, `3` blocked, `4` zero rows — including an item or user that does not
  exist, which is a correct answer — `5` the content was never obtained,
  `6` partial. A pipeline branches on these.
- **An EMPTY page is never retried and never counted as blocked.**
- **Credentials never reach argv or a log, and an exception message is a
  log.** The masker is global rather than first-occurrence: a Playwright
  connection error repeats the endpoint five times.
- **Merge in page order, not arrival order**, so concurrency cannot change
  the output.

### If your change needs a live run

Most do not: the suite covers the parser, the writers, the classifier and
the CLI contract against real, trimmed pages. If yours genuinely needs
news.ycombinator.com, be gentle with it:

* **Use `--delay 4` at most, two pages at most, and only a handful of
  requests.** The site's `robots.txt` asks for `Crawl-delay: 30`, which is why
  `--delay` defaults to 30 seconds; the shorter delay is for a short test,
  not for a loop. Never loop a live run. About fifty requests spaced 4 s
  apart drew no throttle on 2026-09-30, from one datacentre address, which is
  one measurement.
* **The canary is the scheduled live check.** It is three pages weekly (Mondays) from a
  GitHub runner and needs no secret.

Say in the PR what you ran (engine, mode, URL), and what you got, including
the sidecar's `status` and `stop_reason`.

**Run more than the primary engine.** "Mirror them exactly" is a design
rule, not a verification. The fetch loop is shared (`page_flow.run_pages`),
but each engine's driver plumbing is its own, and only running it proves it.

## Scope

This repo reads **public pages** on news.ycombinator.com: listings, one
item's comment tree and one user's profile, exactly as the site serves them
to an anonymous visitor. It never logs in. For many uses the site's official
API (`hacker-news.firebaseio.com/v0`) and Algolia's search
(`hn.algolia.com/api/v1`) are the better tool; this repo exists for the rank
and page position the pages carry.

Out of scope: anything behind a login, anything that votes, flags, replies or
submits any other form (the paths `robots.txt` disallows, such as `/vote`,
`/login`, `/reply` and `/flag`, are none of them read), and anything that
defeats a protection rather than passing it the way an ordinary browser does.

## Licence

MIT. By opening a pull request you agree your contribution ships under it.
