# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project
follows [Semantic Versioning](https://semver.org/) as closely as a CLI
toolkit can: a patch release means **fixes**, not that every flag and
default is frozen. A default that changes behaviour for an existing user is
said so at the top of its release notes.

## [Unreleased]

### Changed

- CI only: the canary's optional paid secret `HACKERNEWS_PROXY` now reaches it only on a new weekly
  run (Mondays 05:40 UTC) and on a manual dispatch. The daily run keeps its
  schedule and takes the free route without them, so it bills nothing.

## [0.1.0] — 2026-10-15

> **A page that says it has rows and parses to none now exits 5 (or 6), not
> 4 or 0.** When the markup moves, a page can still carry its own More link or
> a comment count while the parser reads nothing from it. That used to be
> reported as an empty answer: exit 4 on page 1, and on a later page the end
> of the list, so a three-page run ended `complete`, exit 0, with a third of
> the data. Such a page is now a failed page with `stop_reason:
> parser_found_nothing`: exit 5 when nothing was gathered, exit 6 (partial)
> when earlier pages had rows. The site's own "nothing here" sentences and a
> real page past the end of a list are still exit 4 / end of list.

> **`--delay` defaults to 30 seconds**, the `Crawl-delay` the site's
> `robots.txt` asks for. A three-page run therefore takes about a minute. Lower
> it deliberately: more than fifty requests, mostly four seconds apart, drew no
> throttle on 2026-09-30, which is one measurement and not a promise.

First release. Three modes over news.ycombinator.com's server-rendered pages,
three browser engines and the 2Captcha Scraper API over one shared fetch loop.

### Added

- `--mode listing`: a story list, one row per story: id, title, link,
  discussion address, author, points, comment count, the site's timestamp, the
  domain, a `kind` (`story`, `ask`, `show`, `job`, `text`), the printed rank and
  the page and position. Feeds: the front page, `/newest`, `/ask`, `/show`,
  `/jobs`, `/best`, `/active`, one day's `/front` (`--day`), a user's
  submissions (`--by`) and everything from one domain (`--site`).
- `--mode item`: one item's whole comment tree, one row per comment, with
  `story_id`, `parent_id`, `depth` and a `status` (`ok`, `flagged`, `dead`,
  `deleted`). Text is plain text with paragraphs kept and the full link
  addresses in `links`. An item page carries every comment in one response
  (a 1,104-comment thread: 1.6 MB), so `--pages` is ignored there.
- `--mode user`: one profile row: karma, creation time, the `about` text.
- Pagination follows the site's own "More" link and compares it with what
  `?p=N` would build: only an exact match lets `--concurrency` split pages
  across workers. `/newest`, `/jobs`, `/submitted` and `/from` continue with a
  cursor and are walked link to link. A list that ends ends the run as
  `complete`: the front page has 26 pages and `?p=27` is empty.
- Every query is validated before it is sent, because the site answers some
  wrong values with a plausible page: a malformed `--day` serves the latest day
  and `?p=abc` or `?p=0` serve page 1, all HTTP 200.
- The site's plain-text answers (`No such item.`, `No such user.`, a day it has
  no data for) are read as answers and exit 4, not as a block.
- A CSV cell that would be read as a spreadsheet formula is prefixed with an
  apostrophe, in the CSV only, and counted as `csv_cells_escaped` in the
  sidecar. Output writes are atomic and keep the umask.
- `--category` is accepted as the family's name for `--feed`.
- `diff_runs.py` diffs two runs of one mode and query by `sku`, over columns
  derived from the row class. It reads the **mode from the sidecar**, because
  no column of a row names it, and refuses when it cannot tell rather than
  compare over no columns.
- `scraper_api_client.py` runs the same fetch loop as the browser engines
  through the 2Captcha Scraper API, so all three modes work through it.
- An offline suite over real, scrubbed captures, including an end-to-end run
  of the shared fetch loop with a fake driver, and a daily canary of all three
  modes that needs no secret.

### Fixed

- **`cp .env.example .env` no longer breaks every query flag.** The example
  set `HACKERNEWS_URL`, and a URL from the environment was treated as typed,
  so `--feed show`, `--item 1` and `--mode user --user dang` were all refused
  with exit 2. The example now ships it commented out, and in every entry
  point a URL from the environment is a default: a typed query flag or
  `--mode` wins over it. A typed `--url` with a query flag is still refused.
- **More key shapes are masked in logged errors and the Scraper API's
  `x-debug` header**: `clientKey: …` and JSON `"key": "…"` as well as
  `key=…`.
- Documentation: a Scraping Browser profile's credentials expire, but not on
  a fixed one-day schedule; a 401 on connect is the signal to fetch fresh
  ones. The exit-code table now says Selenium, which sees no HTTP status,
  reports an unrecognised 403 page as exit 5 rather than 3.
- Comments and docstrings state their reasons in their own words instead of
  pointing at an unpublished document, and a check keeps it that way. Stale
  comments describing other sites' columns were reworded.
- CI tests the newest Python (3.14) beside the oldest (3.9).
- **`--retries 0` sent no request and reported an unreadable page.** The flag
  counts attempts and the loop is `while attempt < retries`, so zero attempts
  fetched nothing and the run exited 5 with an empty debug file, which reads
  exactly like a browser fault. Found by a third-party audit that ran
  `--retries 0` and measured exit 5 against a curl that returned 200; the same
  command without that flag returned 30 rows. It is now refused at parse time
  with exit 2, like `--pages 0`.

### Verified live (2026-09-30, from a datacentre address)

- Every page kind answered HTTP 200 to plain `curl`, to headless Chromium, to
  Playwright, Selenium and pyppeteer, to headful Chromium under Xvfb (Playwright,
  one page), to the Scraper API (two tasks, `$0.0005` each) and to Playwright
  with a 2Captcha fingerprint (one page).
- On a closed 71-comment thread and on a profile, Playwright, Selenium and
  pyppeteer wrote identical rows; Playwright and the Scraper API wrote identical
  rows for the thread.
- Docker: the image builds, `--help` answers, Chromium launches, a `--mode user`
  run inside it writes its files, and it carries no `.env`, tests or fixtures.
- `--cdp-endpoint` (a 2Captcha Scraping Browser profile) served a listing page
  and a 71-comment thread through Playwright, and a listing page through
  pyppeteer; Selenium refused a credentialled endpoint with exit 2, as designed.
- `--proxy` (a 2Captcha residential proxy) served one page through Playwright
  and pyppeteer; pyppeteer answered the proxy's real 407 through
  `Fetch.authRequired`. Selenium's credentialled proxy was stripped with a
  warning and the run exited 5 (`unknown` page), as documented.
- A run started at `?p=25` reached the last page (`?p=26`, no More link) and
  stopped `complete`. A cursor list (`/newest`, `/jobs`) was walked over three and
  two pages. Two workers on a `?p=N` list fetched pages 2 and 3 together.

### Found by running it

- **A run that starts on `?p=25` was called non-addressable.** The agreement
  check compared the page's More link with a literal `?p=2`, so `?p=26` looked
  like a cursor and `--concurrency` would have been refused for no reason. The
  comparison is now against the page the URL is on plus one.
- **The atomic writer made every output file mode 0600.** A temporary file is
  created 0600 and a rename keeps its mode, so a fresh `out.json` was readable
  by its owner alone, which `open(path, "w")` does not do. New files now get
  what `open()` would have given them; an existing file keeps its mode.
- **`diff_runs.py` would have reported "0 changed" on real changes.** Its mode
  came from a `mode` column of the rows, which none of these row classes has, so
  it tracked no columns at all.
- CI, on the pushed branch (2026-09-30): the offline suite on Python 3.9 and
  3.12, each engine in its own virtualenv, the Docker build, and a manual
  dispatch of the canary from a GitHub runner (all four jobs passed).

### Not verified

- **HTTP 403 and the 503 throttle were never seen**, so those code paths are
  exercised by the fake driver only.
- **`--fingerprint` was not read back out of the page**, and only the
  Playwright copy of it was run (one page, served). The Selenium and pyppeteer
  copies were not.
- **`--cdp-endpoint` with Selenium** cannot be used by design; `--concurrency`
  through a remote profile is refused by design and was not run.
- Headless was served on every run; headful was tried once. Neither was found to
  matter, which is one page and not a comparison.
