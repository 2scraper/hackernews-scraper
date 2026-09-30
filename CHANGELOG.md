# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project
follows [Semantic Versioning](https://semver.org/) as closely as a CLI
toolkit can: a patch release means **fixes**, not that every flag and
default is frozen. A default that changes behaviour for an existing user is
said so at the top of its release notes.

## [0.1.0] — 2026-09-30

First release. Three modes over news.ycombinator.com's server-rendered pages,
three browser engines and the 2Captcha Scraper API over one shared fetch loop.

> **`--delay` defaults to 30 seconds**, the `Crawl-delay` the site's
> `robots.txt` asks for. A three-page run therefore takes about a minute. Lower
> it deliberately: more than fifty requests, mostly four seconds apart, drew no
> throttle on 2026-09-30, which is one measurement and not a promise.

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

### Not verified

- **The canary has not run.** The repository had not been pushed.
- **HTTP 403 and the 503 throttle were never seen**, so those code paths are
  exercised by the fake driver only.
- **`--fingerprint` was not read back out of the page**, and only the
  Playwright copy of it was run (one page, served). The Selenium and pyppeteer
  copies were not.
- **`--cdp-endpoint` with Selenium** cannot be used by design; `--concurrency`
  through a remote profile is refused by design and was not run.
- Headless was served on every run; headful was tried once. Neither was found to
  matter, which is one page and not a comparison.
- Python 3.9, the floor this claims, was not available locally; the CI matrix
  runs it.
