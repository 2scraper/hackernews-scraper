# hackernews-scraper

[![release](https://img.shields.io/github/v/release/2scraper/hackernews-scraper?display_name=tag&sort=semver)](https://github.com/2scraper/hackernews-scraper/releases)
[![tests](https://github.com/2scraper/hackernews-scraper/actions/workflows/tests.yml/badge.svg)](https://github.com/2scraper/hackernews-scraper/actions/workflows/tests.yml)
[![canary](https://github.com/2scraper/hackernews-scraper/actions/workflows/canary.yml/badge.svg)](https://github.com/2scraper/hackernews-scraper/actions/workflows/canary.yml)
![python](https://img.shields.io/badge/python-3.9%2B-blue)
![license](https://img.shields.io/badge/license-MIT-green)
![engines](https://img.shields.io/badge/engines-Playwright%20%7C%20Selenium%20%7C%20Puppeteer%20%7C%20Scraper%20API-informational)
![no account](https://img.shields.io/badge/runs%20without%20an%20account-yes-brightgreen)

Scrapes **news.ycombinator.com** — story lists, whole comment trees and
profiles — to JSON and CSV, with Playwright, Selenium, Puppeteer (pyppeteer),
the 2Captcha Scraping Browser API over CDP, or the 2Captcha Scraper API with no
local browser. Proxies and fingerprints are supported. **You need no key, no
proxy and no account**; see [What you need](#what-you-need-nothing-and-what-that-was-measured-on).

| Mode | Reads | One row is |
|---|---|---|
| `listing` (default) | the front page, `/newest`, `/ask`, `/show`, `/jobs`, `/best`, `/active`, one day's `/front`, a user's submissions, everything submitted from one domain | a **story** |
| `item` | one item's whole comment tree (a story, or a single comment and its replies) | a **comment** |
| `user` | one profile | a **profile** |

## Quick start

Install **exactly one** browser engine, in its own virtualenv — the three
pin incompatible versions of their dependencies (see [Engines](#engines)):

```bash
git clone https://github.com/2scraper/hackernews-scraper && cd hackernews-scraper
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt -r requirements-playwright.txt
.venv/bin/playwright install chromium

.venv/bin/python playwright_scraper.py --pages 3 --out front
```

That writes `front.json`, `front.csv` and `front.meta.json` (the run's status,
its stop reason and which pages failed, if any). It takes about a minute,
because it waits 30 seconds between pages: see [Being polite](#being-polite).

```bash
# Ask HN, Show HN, jobs, best, most active
python playwright_scraper.py --feed show --pages 2
# one day's front page
python playwright_scraper.py --feed front --day 2024-01-15
# a user's submissions, and everything submitted from one domain
python playwright_scraper.py --by dang
python playwright_scraper.py --site example.com
# a comment tree, and a profile
python playwright_scraper.py --mode item --item 49901736
python playwright_scraper.py --mode user --user dang
# or give it the address
python playwright_scraper.py --url "https://news.ycombinator.com/newest" --pages 3
```

`--category` is accepted as the family's name for `--feed`; it means the same.
A `?p=N` in `--url` starts the run at that page. `python3 diff_runs.py --old a.json
--new b.json` compares two runs of the same mode and query.

## What you need: nothing, and what that was measured on

Measured **2026-09-30** from a datacentre VPS (netcup, Nuremberg), with no
key, no proxy, no cookie and no account:

| Client | Result |
|---|---|
| plain `curl`, its own User-Agent | HTTP 200, 34,189 bytes for the front page |
| plain `curl`, a Chrome User-Agent | HTTP 200, the same 34,189 bytes |
| headless Chromium, all three browser engines | served, every page kind |
| headful Chromium (under Xvfb), Playwright | served, 1 page tried |
| the 2Captcha Scraper API | served, `$0.0005` a task, 2 tasks run |
| Playwright with a 2Captcha fingerprint (`--fingerprint`, country DE) | served, 1 page tried |

There is no captcha and no challenge on any page this scraper reads, and none
was met: **0** challenge or captcha markers on any served page in the
committed fixtures, and on the 47 raw pages captured while measuring (five of
them the same thread fetched repeatedly) the only occurrences of the word
"captcha" are inside a stranger's comment text on that thread. The site's own
Content-Security-Policy allows exactly one third party, reCAPTCHA, and loads it
on `/login`, which this scraper never visits.

So **this repo does not implement captcha solving**: there has been nothing to
solve. That is a statement about this repository and not about any solver.

What the paid products would buy you here, honestly: more addresses than one
(`--proxy-file`) if you run a lot, and a browser you do not host
(`--cdp-endpoint`, or `scraper_api_client.py`). `--twocaptcha-key` is used by
`--fingerprint` and by the Scraper API client, and by nothing else.

**Not measured, so not claimed:** HN is documented to throttle by address
with HTTP 503 and this repo handles that (a separate retry budget, at the same
exit, never counted as a block), but no request made here drew one; HTTP 403
is handled as a refusal and was never seen either. The canary
(`.github/workflows/canary.yml`) has not run yet: the repository had not been
pushed when this was written.

## Being polite

`robots.txt` on the site says `Crawl-delay: 30` for every agent. That is a
request, not a limit the site enforces, and **`--delay` defaults to 30
seconds** to honour it: three pages take about a minute. Lower it if you
decide to. What was measured: more than fifty requests from one address,
mostly four seconds apart (a few closer, when runs started back to back),
including a 1.6 MB page, drew no throttle. That is one
measurement, from one address, on one day, and it is not a promise.

`--concurrency N` fetches pages 2..N in parallel and so multiplies the rate by
N; it warns when there is no proxy pool to spread it over. It works only for
`?p=N` lists (below).

The site has an official API (`hacker-news.firebaseio.com/v0`) and Algolia
runs a search over it (`hn.algolia.com/api/v1`). Use them when they answer your
question: they return JSON and count as a better citizen. This scraper reads
the **pages**, which carry what those do not: the rank and page a story held
when you looked, and what the site chose to show or to hide.

## Pagination, and what `--concurrency` can do

| List | Paginates by | `--concurrency` |
|---|---|---|
| `/news` (front page), `/show`, `/best`, `/active`, `/front` | `?p=N`, independent pages | works |
| `/newest`, `/jobs`, `/submitted` (`--by`), `/from` (`--site`) | a **cursor**: `?next=ID&n=31` in the More link | ignored, with a warning; walked link to link |
| `/ask` | one page (18 rows measured) | not applicable |
| an item page, a profile | one page | `--pages` is ignored |

The site says which it is on every page, and the scraper reads that instead of
trusting the table: page 1's "More" link is compared with what `?p=2` would
build, and only an exact match lets pages be handed to workers.

Things measured about the ends of lists (2026-09-30):

* The front page has **26 pages of 30**: `?p=26` (ranks 751-780) is the last
  and has no More link; `?p=27` is a 2 KB page with no rows. Both end the run
  as `complete` with stop reason `end_of_listing`.
* **An item page holds every comment, in one response.** A 1,104-comment thread
  was 1.6 MB and the whole run took 12 s; `&p=2`..`&p=6` all returned the
  same page. `--pages` means nothing for `--mode item`.

## Output

Every mode writes `<out>.json`, `<out>.csv` and `<out>.meta.json`. Writes are
atomic (a crash cannot leave a half-written file over yesterday's good one),
a run that finds nothing writes nothing (`--allow-empty` overrides), and an
empty CSV still carries its header.

**CSV cells that would be read as a formula** (a first character of `=`, `+`,
`-`, `@`, tab or newline) are prefixed with an apostrophe, **in the CSV only**;
the JSON keeps the site's bytes, and the sidecar counts the difference as
`csv_cells_escaped`. Comment text is written by strangers and a comment opening
with `+1` is ordinary: a 71-comment thread measured 1 such cell, a
1,104-comment thread measured 0.

### Story (`listing`, 15 columns)

`source, scraped_at, url, sku, title, author, points, comments, posted_at,
domain, kind, rank, hn_url, page, position`

* `sku` is the HN item id, a string; `url` is where the story **points** (its own
  item page for a text post) and `hn_url` is always the discussion.
* `kind` is `story`, `ask`, `show`, `job` or `text` (a self post that is not
  Ask or Show). `posted_at` is the timestamp the site stamps on the row, ISO 8601,
  UTC — never the "9 hours ago" beside it.
* `rank` is the number the site prints, continuing across pages (page 2 starts
  at 31). `page` + `position` is unique across a run; `position` restarts at 1
  on every page.

### Comment (`item`, 13 columns)

`source, scraped_at, url, sku, story_id, parent_id, author, posted_at, depth,
position, status, text, links`

`story_id` and `parent_id` are item ids in the same space as a story's `sku`, so
the tree is two columns; `depth` is the site's own indent, 0 for a top-level
comment. `text` is plain text (paragraphs separated by a blank line) and `links`
holds the **full** addresses, because the site shortens the visible text of a
long link. `status` is `ok`, `flagged`, `dead` or `deleted`.

### Profile (`user`, 8 columns)

`source, scraped_at, url, sku, created_at, karma, about, about_links`

`sku` is the username.

## Things that look like bugs and are not

* **`author`, `points` and `comments` are null on a job posting.** A job has
  none of the three; the row's `kind` is `job`. `comments` is **0**, not null,
  on a story that says `discuss` (25 of 30 rows on `/newest` when measured).
* **`rank` is null on `--site` (`/from`)**: that page prints none.
* **`comments` on a story row can be one off from the item page's**: they are
  read at different moments, and a live list moves between the requests.
* **The item page's comment count leaves flagged comments out.** On a
  1,100-row thread the site said 1,095 and the run held 1,100 rows: exactly the
  1,095 with status `ok` plus five `flagged` ones, whose `text` is the site's
  placeholder `[flagged]`, not what the author wrote.
* **A live list moves.** Two pages of `/newest` fetched seconds apart can share a
  row (dropped by the dedupe, which is by `sku` and in page order) or skip one
  that moved across the boundary. A ranked list at 8 a.m. is not the list at 9.
* **`--url .../front?day=garbage` is refused, not sent.** The site answers a
  malformed day with the latest one, and `?p=abc` or `?p=0` with page 1, all
  HTTP 200. A typo must not turn into a healthy-looking run of the wrong thing,
  so `--day` and `p` are validated first.
* **"Nothing there" is plain text with HTTP 200**: `No such item.`,
  `No such user.`, `We don't have that data yet.` (a future day) and `HN didn't
  exist yet.` These are answers, so they exit **4**, not 3.
* **This scraper never logs in**, so an item, comment or profile is whatever the
  site shows a logged-out reader.

## Exit codes and the sidecar

| Code | Meaning |
|---|---|
| 0 | ok |
| 1 | crash |
| 2 | bad usage (a flag combination that would scrape something nobody asked for) |
| 3 | blocked: HTTP 403 (not observed here) |
| 4 | zero rows: nothing there, a list past its end, or a served page that parsed to none (that last one is a bug in this repo and is logged as one) |
| 5 | the content was never obtained: a timeout, a dead proxy, a throttle that never lifted, a remote API error |
| 6 | partial: some pages arrived and then the run stopped; `pages_failed` in the sidecar names them |

`<out>.meta.json` records `status` (`complete`, `partial`, `failed`),
`stop_reason`, `pages_requested`, `pages_completed`, `pages_failed`, the query,
whether the list is `addressable`, and for `--mode item` the story the comments
sit under. `diff_runs.py` refuses to compare runs that are not both `complete`,
that are different modes, or that asked different queries.

## Engines

| Engine | File | Notes |
|---|---|---|
| Playwright | `playwright_scraper.py` | primary |
| Selenium | `selenium_scraper.py` | cannot use an authenticated `--cdp-endpoint` (chromedriver's `debuggerAddress` takes a bare `host:port`), and `--proxy-server` cannot authenticate, so credentials are stripped with a warning; it cannot see an HTTP status either, so it classifies a page by its content |
| Puppeteer | `puppeteer_scraper.py` | uses `pyppeteer`, which is effectively unmaintained and points at Playwright; it can authenticate a CDP endpoint and a proxy |
| Scraper API | `scraper_api_client.py` | no local browser: one 2Captcha Scraper API task per page (`$0.0005` each, measured); `--key` or `TWOCAPTCHA_KEY` |

The three browser engines and the Scraper API client run **one shared fetch
loop** (`page_flow.py`): each supplies only how to fetch one page, so the
retry, throttle, end-of-list and exit-code decisions cannot drift between them.
On a closed 71-comment thread and on a profile, Playwright, Selenium and
pyppeteer produced **identical rows**, and Playwright and the Scraper API
identical rows for the thread (2026-09-30, every column but `scraped_at`).

No engine sets a user agent of its own: a bare override was measured, on a
sibling site, to get a session refused, and nothing on this site asks for one.
`--fingerprint` applies the fingerprint API's identity instead: user agent,
viewport and screen, device pixel ratio, locale, time zone, platform,
hardware concurrency, device memory, `navigator.languages` and the WebGL
vendor and renderer. It does not set User-Agent Client Hints, and it was not
read back out of the page here: the run only showed HN serving a page under it.

The engines' dependencies conflict: `playwright` and `pyppeteer` pin
incompatible `pyee` versions, `pyppeteer` and `selenium` collide on `urllib3`.
Use a virtualenv per engine; CI installs each one separately and runs
`pip check` in each.

## Configuration

Credentials live in `.env` next to the scripts, never on a command line (a
secret in `argv` is readable by anything that can run `ps`). Copy
`.env.example`; every variable it lists is one the code reads, and the reverse.
Precedence: explicit flag, then exported variable, then `.env`, then default.
`python3 env_config.py` prints what was picked up without printing a secret.

A Scraping Browser profile's credentials last about a day, so none is written
down anywhere in this repo; the endpoint's shape is in `.env.example`.

## Tests, CI and the container

`python3 smoke_test.py` (or `pytest`) runs the offline suite with no engine
installed: parsing is pinned on **real captures** kept as scrubbed fixtures
(`fixtures_generated.json`, cut by `make_fixtures.py`), and the shared fetch loop
is driven end to end by a fake driver. The captures themselves are not
committed: they carry usernames, strangers' comments and per-session tokens.

The sample outputs (`sample_output*.json`, `.csv`) are what this code writes for
those scrubbed fixtures, **not a live run**: usernames and comment text are
placeholders, everything else is what the site served.

CI runs the suite on the oldest and newest supported Python, installs each
engine in its own virtualenv, and builds the Docker image and checks that it
launches Chromium. **The image was not built while this was written** (no access
to a Docker daemon on the machine used), so that job is its first build.

```bash
docker build -t hackernews-scraper .
docker run --rm -v "$PWD/out:/out" hackernews-scraper --feed show --pages 2 --out /out/show
```

## Licence

MIT. See `LICENSE`. This reads **public pages only** and never logs in. See
`SECURITY.md`, `CONTRIBUTING.md` and `TROUBLESHOOTING.md`.
