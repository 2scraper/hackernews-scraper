# Troubleshooting

Find your exit code first (`echo $?` straight after the run), then the
sidecar's `stop_reason` in `<out>.meta.json` if one was written.

Everything below about the site was measured on 2026-09-30 from a datacentre
address, unless it says otherwise.

## Exit 2 — bad usage

A parameter the site would have answered with a plausible wrong result is
checked before anything is sent: `front?day=garbage` serves the latest day,
and `news?p=abc` and `p=0` serve page 1, all without an error. The message
names the value and what is accepted.

## Exit 3 — blocked

HTTP 403. **Not observed**: every page kind this repo reads was served HTTP
200 to plain curl, to headless Chromium and to all three browser engines,
with no key, proxy, cookie, account, captcha or challenge. This repo does not
implement captcha solving, because none was ever seen. If you get a 403, a
different exit (`--proxy`, or `--cdp-endpoint`) is the thing to try, and a
report with the sidecar's `stop_reason` is welcome.

## Exit 4 — zero rows

One of three things, and only the last is a bug:

* **There is nothing there.** `No such item.`, `No such user.`,
  `We don't have that data yet.` and `HN didn't exist yet.` are plain-text
  HTTP 200 answers, and they are exit 4.
* **You started past the end of a list.** A listing past its end is a 2 KB
  page with no rows and no More link: `/news` has 26 pages, `?p=26` is the last
  and `?p=27` is empty. A run that starts inside the list and reaches the end
  is `complete`, exit 0; exit 4 is only for a FIRST page that is already empty,
  such as `--url ".../news?p=30"`.
* **A served page parsed to no rows.** That is a parser bug or a markup
  change. Re-run with `--dump-html page.html` (it writes the exact bytes the
  parser saw, on success too) and open a "Site changed" issue.

Nothing is written on exit 4, so an earlier good file is left alone;
`--allow-empty` writes the empty file.

## Exit 5 — the content was never obtained

* **A timeout or a dead proxy.** The log names which. A dead proxy wants a
  different exit; a timeout wants another try at the same one.
* **A throttle that did not lift.** HN is documented to throttle by address
  with HTTP 503; that was NOT OBSERVED in testing (about fifty requests
  spaced 4 s apart drew none, one measurement). Raise `--delay`, lower
  `--concurrency`, or spread the run over `--proxy-file`.
* **HTTP 401 from a Scraping Browser endpoint.** The profile's credentials
  have expired; they last about a day. Get fresh ones.
* **HTTP 500 `profile_locked` from a Scraping Browser endpoint.** The profile
  has one live connection and something still holds it, including a run that
  disconnected a second ago. Wait, or use another `pid`.

## Exit 6 — partial

Some pages came back and a later one did not. The output holds what was
gathered and the sidecar lists `pages_failed` by number.

## The run takes minutes

`--delay` defaults to 30 seconds, because `robots.txt` asks for
`Crawl-delay: 30` for every agent. A 10-page run is therefore about five
minutes by design. `--delay 4` is what the live tests here use: about fifty
requests spaced that way drew no throttle, from one measurement. Lower it at
your own risk, and not in a loop.

## `--concurrency` is ignored

* **Cursor lists** (`/newest`, `/jobs`, `/submitted`, `/from`) continue with
  `?next=ID&n=31` in the More link, so page 5's address is unknown until
  page 4 loads. They are walked link to link. Only the `?p=N` lists
  (`/news`, `/show`, `/best`, `/active`, `/front`) are independent.
* **`--cdp-endpoint`.** A Scraping Browser profile allows one live
  connection, so workers would collide. Use several `pid`s, one run each.

## `--pages` is ignored for `--mode item` and `--mode user`

An item page carries every comment in one response (a 1,095-comment thread
was a single 1.6 MB page, and `&p=N` is ignored). A profile is one page.
There is nothing to paginate.

## Fewer than 30 stories

`/ask` is one page of about 18. The other lists gave 30 per page while they
had more rows: `--site example.com` gave 15, its whole list, with no More link.

## Some columns are null

* **`rank` on `/from`** — that page does not number its rows.
* **`points`, `comments` and `author` on `/jobs`** — job postings carry none.

## `comments` differs from the item page

The site's own count leaves flagged comments out. `--mode item` keeps them:
the body is `[flagged]` and the status is `flagged`.

## Engine install conflicts

Install exactly one browser engine, in its own virtualenv if you want more
than one. playwright and pyppeteer pin `pyee` differently and pyppeteer and
selenium collide on `urllib3`; `pip check` reports it. See
`requirements.txt`.

`scraper_api_client.py` needs no local browser.

## Selenium and authentication

Selenium cannot use an authenticated `--cdp-endpoint`: chromedriver's
`debuggerAddress` takes a bare `host:port` with nowhere for a password. Use
Playwright or pyppeteer. Its `--proxy-server` cannot authenticate either, so
credentials in a proxy URL are stripped with a warning.

## A run looks fine but a column is wrong

Re-run with `--dump-html page.html`: it writes what the parser was given, on
success too, so a parsing bug can be told apart from a change in what the
site sends. Remove usernames and comment text before sharing it.

## Duplicates dropped, or a row missing between two runs

The listings are live. A story that moves across a page boundary while a run
reads the pages is fetched twice (the dedupe drops it and the log says so),
or not at all. That is the site moving, not the scraper.

## Is this the right tool?

The site's official API (`hacker-news.firebaseio.com/v0`) and Algolia's
search (`hn.algolia.com/api/v1`) exist and are often the better choice. This
repo reads the pages, which carry rank and page position.
