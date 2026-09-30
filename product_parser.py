"""
product_parser.py
-----------------
Everything this repo knows about news.ycombinator.com lives in this file.

What the site is, measured 2026-09-30 from a datacentre address (netcup,
Nuremberg): plain server-rendered HTML, HTTP 200 to curl with its own
User-Agent and with a Chrome one, no cookie, no challenge, no captcha, no
JavaScript needed to read any of it. There is no JSON-LD (0 blocks on every
page kind) and no embedded state object; the data is a table. So this parser
anchors on the site's OWN class names, which are unusually stable for a
reason: the markup has hardly changed in years (`athing`, `titleline`,
`subtext`, `comtr`, `morelink`), and each is a contract with every browser
extension and userscript written against the site.

Three page kinds, three modes (`--mode`):

    listing   any story list -> Story rows, 30 per page
    item      one story or comment and its comment tree -> Comment rows
    user      one profile -> one User row

How each paginates, which decides what `--concurrency` can do:

    ?p=N       /news /show /best /active /front   addressable, independent
    ?next=ID   /newest /jobs /submitted /from      a CURSOR: page 2's address
                                                   is unknowable until page 1
                                                   is read, so these chain
    (none)     /ask, /item, /user                  one page

The site says which it is on every page: the "More" link is
`<a class="morelink" rel="next">`, and comparing its href with what `?p=N`
would build is how page_flow decides (§7) instead of trusting a table.

Facts measured that the code depends on, each one a way this scraper would
otherwise be wrong while looking healthy:

* **An item page holds EVERY comment.** A 1,095-comment thread came back as
  one 1.6 MB response, and `&p=2`..`&p=6` all returned the same page. There
  is no comment pagination to follow, and requesting one only downloads
  1.6 MB again.
* **Comment rows carry more than one class.** `athing comtr` is the normal
  row; collapsed ones are `athing comtr coll`, flagged ones `athing comtr
  noshow`. A selector on the exact string `"athing comtr"` silently loses
  them: on that thread it finds 1,042 rows and the class-token match finds
  every one.
* **Some parameters are answered with plausible data.** `front?day=garbage`
  serves the latest day, and `news?p=abc` and `news?p=0` serve page 1, all
  HTTP 200. So `day` and `p` are validated here BEFORE anything is sent
  (§26): a typo must not become a healthy-looking run of the wrong thing.
* **"Nothing there" is plain text, not markup.** `item?id=99999999999` is the
  13 bytes `No such item.`, an unknown user is `No such user.`, a `front` day
  in the future is `We don't have that data yet.`, before HN existed is
  `HN didn't exist yet.` -- all HTTP 200. A listing past its last page is a
  2 KB page of site chrome with no rows and no More link. Both are answers
  (EXIT_NO_PRODUCTS), not blocks.
* **Not every listing row has a score, an author or a comment count.** A job
  posting has none of the three; a story with no comments says `discuss`,
  which is zero, not unknown.

Nothing in here imports a browser (§1).
"""

import html as html_lib
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qsl, quote, urlencode, urljoin, urlparse

from bs4 import BeautifulSoup

from output_writer import Comment, SOURCE_DEFAULT, Story, User  # noqa: F401

HOST = "news.ycombinator.com"
ORIGIN_URL = f"https://{HOST}/"

# Upper bound on --pages. A front-page listing ends by itself (the last page
# measured that still held rows was p=25; p=30 was empty), so this exists for
# a cursor list like /newest, which the site lets you walk for much longer.
MAX_PAGES = 100

# Rows the site puts on a listing page. Measured: 30 on every feed but /ask,
# which serves 18 and has no More link (2026-09-30).
PAGE_SIZE = 30

# ---------------------------------------------------------------------------
# The routes
# ---------------------------------------------------------------------------

# feed name -> path. `front` takes a day, the rest take nothing.
FEEDS = {
    "news": "/news",
    "newest": "/newest",
    "ask": "/ask",
    "show": "/show",
    "jobs": "/jobs",
    "best": "/best",
    "active": "/active",
    "front": "/front",
}
DEFAULT_FEED = "news"

# Routes whose "More" link is `?p=N`. Measured 2026-09-30 by reading the link
# on page 1 of each. The others use a cursor (`next=ID&n=31`). This is a
# starting belief that page_flow CHECKS against the page's own link rather
# than a fact the run relies on.
P_STYLE_PATHS = ("/", "/news", "/show", "/best", "/active", "/front")

# HN launched in October 2006. `front?day=2001-01-01` answers "HN didn't
# exist yet." itself, so no lower bound is enforced here; the site's own
# sentence is the better source.
_DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
# Site rule for a username: 2-15 characters of letters, digits, - and _.
_USER_RE = re.compile(r"^[A-Za-z0-9_-]{2,15}$")
_ID_RE = re.compile(r"^\d{1,12}$")
# A domain for /from?site=. Deliberately loose: the site takes a bare host or
# a host/path prefix (github.com/ninjahawk).
_SITE_RE = re.compile(r"^[A-Za-z0-9.-]+(/[A-Za-z0-9._~%-]+)*$")

MODES = ("listing", "item", "user")


@dataclass
class Query:
    """What a run is asking for, reduced to fields the sidecar can record."""
    mode: str = "listing"
    feed: Optional[str] = None         # a FEEDS key
    day: Optional[str] = None          # front only
    by: Optional[str] = None           # /submitted?id=
    site: Optional[str] = None         # /from?site=
    item_id: Optional[str] = None
    username: Optional[str] = None
    # A user-typed listing URL keeps the page it carried, so
    # `--url '.../news?p=2'` starts where it says and not at page 1.
    start_page: int = 1

    def validate(self) -> Optional[str]:
        """The reason this query is unusable, or None. Nothing is sent to
        the site until this is None, because it answers several typos with a
        healthy-looking page of something else (see the module docstring)."""
        if self.mode not in MODES:
            return "--mode must be one of %s." % ", ".join(MODES)
        if self.mode == "item":
            if not self.item_id or not _ID_RE.match(str(self.item_id)):
                return "--item needs a numeric HN item id (from item?id=…)."
            return None
        if self.mode == "user":
            if not self.username or not _USER_RE.match(self.username):
                return ("--user needs an HN username: 2-15 letters, digits, "
                        "'-' or '_'.")
            return None
        chosen = [n for n, v in (("--feed", self.feed), ("--by", self.by),
                                 ("--site", self.site)) if v]
        if len(chosen) > 1:
            return ("%s each pick a different list; pass one of them."
                    % ", ".join(chosen))
        if self.feed and self.feed not in FEEDS:
            return "--feed must be one of %s." % ", ".join(FEEDS)
        if self.day is not None:
            if self.feed != "front":
                return "--day only applies to --feed front."
            if not _DAY_RE.match(self.day):
                return "--day must be YYYY-MM-DD."
            try:
                d = datetime.strptime(self.day, "%Y-%m-%d").date()
            except ValueError:
                return "--day %s is not a real date." % self.day
            if d > datetime.now(timezone.utc).date():
                return "--day %s is in the future." % self.day
        if self.by is not None and not _USER_RE.match(self.by):
            return "--by needs an HN username: 2-15 letters, digits, '-' or '_'."
        if self.site is not None and not _SITE_RE.match(self.site):
            return "--site needs a domain such as github.com (or github.com/user)."
        if self.start_page < 1:
            return "the page number must be at least 1."
        return None

    @property
    def path(self) -> str:
        if self.mode == "item":
            return "/item"
        if self.mode == "user":
            return "/user"
        if self.by:
            return "/submitted"
        if self.site:
            return "/from"
        return FEEDS[self.feed or DEFAULT_FEED]

    def params(self) -> List[Tuple[str, str]]:
        if self.mode == "item":
            return [("id", str(self.item_id or ""))]
        if self.mode == "user":
            return [("id", self.username or "")]
        if self.by:
            return [("id", self.by)]
        if self.site:
            return [("site", self.site)]
        if self.feed == "front" and self.day:
            return [("day", self.day)]
        return []

    def url(self) -> str:
        """The address of the FIRST page of this query (of the page the URL
        started on, when the user typed a `?p=`)."""
        qs = urlencode(self.params(), safe="/")
        base = f"https://{HOST}{self.path}" + (f"?{qs}" if qs else "")
        return page_url(base, self.start_page) if self.start_page > 1 else base


@dataclass
class Request:
    url: str
    label: str


def request_for(query: Query, page_num: int, url: Optional[str] = None) -> Request:
    """The request for page `page_num` of `query`.

    `url` overrides the address: a cursor list's page 2 is only known from
    page 1's own More link, so the loop hands that in (§7).
    """
    if url:
        return Request(url=url, label=url)
    base = Query(**{**query.__dict__, "start_page": 1}).url()
    target = page_url(base, query.start_page + page_num - 1)
    return Request(url=target, label=target)


def page_url(url: str, page: int) -> str:
    """`url` with `?p=<page>`, replacing rather than duplicating, and keeping
    every other parameter (`front?day=…&p=2`). Page 1 is the URL as given."""
    parts = urlparse(url)
    kept = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
            if k != "p"]
    if page > 1:
        kept.append(("p", str(page)))
    return parts._replace(query=urlencode(kept, safe="/")).geturl()


def path_of(url: str) -> str:
    return (urlparse(url).path or "/").rstrip("/") or "/"


def pagination_is_addressable(url: str) -> bool:
    """Whether `?p=N` is this listing's own convention, from its route alone.

    A belief, not a measurement: page_flow confirms it against page 1's own
    More link before planning a single URL (`next_link_agrees`).
    """
    return path_of(url) in P_STYLE_PATHS


def next_page_url(html: str, base_url: str) -> Optional[str]:
    """The absolute address of the site's own "More" link, or None."""
    soup = BeautifulSoup(html or "", "html.parser")
    a = soup.select_one("a.morelink")
    if a is None or not a.get("href"):
        return None
    return urljoin(base_url, html_lib.unescape(a["href"]))


def _page_of(url: str) -> int:
    """The `p` a URL carries, 1 when it has none or a malformed one."""
    for k, v in parse_qsl(urlparse(url).query, keep_blank_values=True):
        if k == "p" and v.isdigit() and int(v) >= 1:
            return int(v)
    return 1


def next_link_agrees(html: str, page_one_url: str) -> Optional[bool]:
    """Whether the More link on this page is exactly what `?p=N+1` would
    build, N being the page the URL is already on.

    "Page one" is the FIRST page of the RUN, and a run can start anywhere: a
    user who types `--url .../news?p=25` has a first page whose link says
    `?p=26`, and comparing it with a literal `?p=2` called that list
    non-addressable. Found by running exactly that against the live site.

    None when there is no link at all (a one-page list, or the last page).
    False means the link carries a cursor the convention cannot reproduce,
    so the list has to be walked link to link and cannot be split across
    workers.
    """
    link = next_page_url(html, page_one_url)
    if link is None:
        return None
    expected = page_url(page_one_url, _page_of(page_one_url) + 1)
    return _canonical(link) == _canonical(expected)


def _canonical(url: str) -> str:
    p = urlparse(url)
    return f"{p.path}?{urlencode(sorted(parse_qsl(p.query, keep_blank_values=True)))}"


# ---------------------------------------------------------------------------
# --url
# ---------------------------------------------------------------------------

def query_from_url(url: str) -> Tuple[Optional[Query], str]:
    """(Query, "") for a supported address, else (None, why not)."""
    raw = (url or "").strip()
    if not raw:
        return None, "the URL is empty."
    full = raw if "//" in raw else "https://" + raw
    parts = urlparse(full)
    host = (parts.hostname or "").lower()
    if host != HOST:
        if host.endswith("ycombinator.com"):
            return None, ("%s is not the news site. This scraper reads "
                          "news.ycombinator.com only." % host)
        return None, "%s is not news.ycombinator.com." % (host or raw)
    path = path_of(full)
    qs = dict(parse_qsl(parts.query, keep_blank_values=True))

    start = 1
    if "p" in qs:
        if not qs["p"].isdigit() or int(qs["p"]) < 1:
            return None, ("p=%s is not a page number. The site answers that "
                          "with page 1 of the same list, so it is refused "
                          "rather than sent." % qs["p"])
        start = int(qs["p"])
    if "next" in qs:
        return None, ("a cursor address (?next=…) is a place in the middle of "
                      "a moving list; start from %s instead." % path)

    if path == "/item":
        q = Query(mode="item", item_id=qs.get("id"))
    elif path == "/user":
        q = Query(mode="user", username=qs.get("id"))
    elif path == "/submitted":
        q = Query(mode="listing", by=qs.get("id") or "", start_page=start)
    elif path == "/from":
        q = Query(mode="listing", site=qs.get("site") or "", start_page=start)
    elif path in ("/", "/news"):
        q = Query(mode="listing", feed="news", start_page=start)
    else:
        feed = next((k for k, v in FEEDS.items() if v == path), None)
        if feed is None:
            return None, _unsupported(path)
        q = Query(mode="listing", feed=feed, day=qs.get("day"), start_page=start)
    why = q.validate()
    return (None, why) if why else (q, "")


def _unsupported(path: str) -> str:
    named = {
        "/newcomments": "the newest-comments feed",
        "/threads": "a user's comment threads",
        "/favorites": "a user's favourites",
        "/upvoted": "upvoted submissions",
        "/pool": "the second-chance pool",
        "/classic": "the classic feed",
        "/noobstories": "the new-accounts feed",
        "/leaders": "the karma leaderboard",
        "/bestcomments": "best comments",
    }
    if path in named:
        return ("this repo does not implement %s (%s) yet. It reads the story "
                "feeds, /item and /user." % (named[path], path))
    return ("%s is not a route this scraper reads. It reads: %s, /item?id=, "
            "/user?id=, /submitted?id= and /from?site=."
            % (path, ", ".join(FEEDS.values())))


# ---------------------------------------------------------------------------
# Page state
# ---------------------------------------------------------------------------

# The site's own one-sentence answers. Compared against the page's TEXT, so a
# browser's `<html><body>No such item.</body></html>` wrapping is irrelevant.
NOT_FOUND_SENTENCES = (
    "no such item.", "no such user.", "we don't have that data yet.",
    "hn didn't exist yet.",
)

# Sent when a client is too fast. NOT OBSERVED by this repo: no request made
# while measuring drew it, at 4 s gaps over ~50 fetches on 2026-09-30. It is
# here because it is what the site is known to say, and a throttle is retried
# at the same exit while a block is not (§24).
THROTTLE_SENTENCE = "we're not able to serve your requests this quickly"

_TAG_RE = re.compile(r"<[^>]+>")


def _text_of(html: str) -> str:
    return re.sub(r"\s+", " ", html_lib.unescape(_TAG_RE.sub(" ", html or ""))).strip()


def references_own_markup(html: str) -> bool:
    """Whether the page is built out of HN's own table.

    Every served page has `<table id="hnmain">`; Chromium's network-error
    page, a proxy's interstitial and a CDN block page do not. This is the §8
    "a served page is built out of the site's own assets" test, applied to
    markup because the site loads nothing from anywhere else (its CSP is
    `default-src 'self'` plus reCAPTCHA on /login).
    """
    h = html or ""
    return 'id="hnmain"' in h or "id='hnmain'" in h


def detect_bot_challenge(html: str, url: str = "") -> Optional[str]:
    """Name a challenge vendor on the page, else None.

    None on every page measured: 0 of the captures carried a challenge, a
    captcha widget or a vendor script, and the site's own CSP allows exactly
    one third party, reCAPTCHA, which it loads on /login and not on any page
    this scraper reads. Kept as the family's entry point so a future
    refusal has a place to be named; it deliberately matches nothing today
    rather than guessing at markers nobody has seen (§18). This repo does not
    implement captcha solving: there has been nothing to solve.
    """
    return None


def detect_page_state(html: str, status: Optional[int] = None,
                      url: str = "") -> str:
    """Name what the site answered with.

        content     a page with rows (stories, comments, or a profile)
        empty       served, and nothing there: no such item/user, or a
                    list past its end -- an ANSWER, not a block
        throttled   HTTP 429/503, or the site's "too quickly" sentence
        blocked     HTTP 403
        unknown     not the site's page at all (a network-error page, a
                    proxy interstitial), or a page of its own markup that
                    holds neither rows nor the site's "nothing" answer

    Ordered by how much each signal PROVES, not by how cheap it is (§17):
    the site's own sentence and its own rows first, the status second.
    """
    text = html or ""
    if status in (429, 503) or (THROTTLE_SENTENCE in text.lower()
                                and len(text) < 2000):
        return "throttled"
    if status == 403:
        return "blocked"
    if not text.strip():
        return "unknown"

    if len(text) < 600 and _text_of(text).lower() in NOT_FOUND_SENTENCES:
        return "empty"

    if not references_own_markup(text):
        return "unknown"

    soup = BeautifulSoup(text, "html.parser")
    if url and path_of(url) == "/user":
        return "content" if soup.select_one("#bigbox a.hnuser") else "empty"
    if soup.select_one("tr.athing"):
        return "content"
    # The site's own chrome and no row at all: a list past its end.
    return "empty"


# ---------------------------------------------------------------------------
# Small readers
# ---------------------------------------------------------------------------

def _int_or_none(value: Any) -> Optional[int]:
    m = re.search(r"\d[\d,]*", str(value or ""))
    return int(m.group(0).replace(",", "")) if m else None


def _item_id_from(href: Optional[str]) -> Optional[str]:
    m = re.search(r"[?&]id=(\d+)", href or "")
    return m.group(1) if m else None


def item_url(item_id: Optional[str]) -> Optional[str]:
    return f"https://{HOST}/item?id={item_id}" if item_id else None


def _iso(age_title: Optional[str]) -> Optional[str]:
    """The `title` of an age span. HN puts an ISO timestamp there, without a
    zone, and (on some builds) a unix time after a space. A row stamped 22:36
    read "9 hours ago" at about 07:50 UTC, so it is UTC."""
    if not age_title:
        return None
    first = age_title.split()[0]
    if re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}$", first):
        return first + "Z"
    return None


def _text_from_html(node) -> str:
    """Plain text of a comment or `about` body.

    HN separates paragraphs with a bare `<p>` (no closing tag), which
    `get_text()` runs together into "…against that.A deviation". Blocks
    become blank lines, `<br>` a newline, and `<pre>` keeps its own
    whitespace. Link text is kept as shown; the addresses go in `links`,
    because the text of a long URL is shortened on the page.
    """
    if node is None:
        return ""
    out: List[str] = []

    def walk(n):
        if isinstance(n, str):
            out.append(str(n))
            return
        name = getattr(n, "name", None)
        if name in ("script", "style"):
            return
        if name == "br":
            out.append("\n")
            return
        if name in ("p", "pre"):
            out.append("\n\n")
        for c in getattr(n, "children", []):
            walk(c)
        if name == "pre":
            out.append("\n\n")

    walk(node)
    text = "".join(out).replace("\xa0", " ")
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _links_in(node) -> List[str]:
    if node is None:
        return []
    seen, out = set(), []
    for a in node.select("a[href]"):
        href = html_lib.unescape(a["href"])
        if href.startswith(("javascript:", "#")):
            continue
        full = urljoin(ORIGIN_URL, href)
        if full not in seen:
            seen.add(full)
            out.append(full)
    return out


def _classes(tag) -> List[str]:
    return list(tag.get("class") or [])


# ---------------------------------------------------------------------------
# Listing: Story rows
# ---------------------------------------------------------------------------

def _kind(title: str, url: str, sku: str, has_author: bool, has_score: bool) -> str:
    """story | ask | show | job | text.

    A job posting has no author and no score. `Ask HN:` and `Show HN:` are
    the site's own title prefixes. Any other post whose link points back at
    its own item page is a text post (`Tell HN:`, a poll)."""
    if not has_author and not has_score:
        return "job"
    low = (title or "").lower()
    if low.startswith("ask hn"):
        return "ask"
    if low.startswith("show hn"):
        return "show"
    if url.endswith(f"item?id={sku}"):
        return "text"
    return "story"


def parse_story_row(row, *, page: Optional[int], position: int) -> Optional[Story]:
    sku = row.get("id")
    if not sku:
        return None
    line = row.select_one("span.titleline")
    link = line.find("a") if line else None
    if link is None:
        return None
    title = link.get_text(" ", strip=True) or None
    href = html_lib.unescape(link.get("href", ""))
    url = urljoin(ORIGIN_URL, href)

    sub = None
    nxt = row.find_next_sibling("tr")
    if nxt is not None and nxt.select_one("td.subtext"):
        sub = nxt.select_one("td.subtext")

    score = sub.select_one("span.score") if sub else None
    author = sub.select_one("a.hnuser") if sub else None
    age = sub.select_one("span.age") if sub else None
    site = row.select_one("span.sitestr")

    # The comment count is the link in the subtext that points at this item
    # and reads "N comments" / "1 comment" / "discuss". "discuss" is what a
    # story with NO comments says, so it is 0, not unknown. A job has no such
    # link at all, which IS unknown.
    comments = None
    if sub is not None:
        for a in sub.select("a"):
            label = a.get_text(" ", strip=True).replace("\xa0", " ")
            if _item_id_from(a.get("href")) != sku:
                continue
            if label == "discuss":
                comments = 0
            else:
                m = re.match(r"^(\d[\d,]*)\s+comments?$", label)
                if m:
                    comments = int(m.group(1).replace(",", ""))

    rank_el = row.select_one("span.rank")
    rank = _int_or_none(rank_el.get_text()) if rank_el else None

    return Story(
        url=url,
        sku=sku,
        title=title,
        author=author.get_text(strip=True) if author else None,
        points=_int_or_none(score.get_text()) if score else None,
        comments=comments,
        posted_at=_iso(age.get("title")) if age else None,
        domain=site.get_text(strip=True) if site else None,
        kind=_kind(title or "", url, sku, author is not None, score is not None),
        rank=rank,
        hn_url=item_url(sku),
        page=page,
        position=position,
    )


def parse_listing(html: str, *, page: Optional[int] = None) -> List[Story]:
    """Story rows from a listing page, in page order.

    `position` counts the rows this function EMITS, from 1 on every page, so
    `page`+`position` is unique across a run (§18) and does not depend on how
    many rows the page held that could not be read (§24).
    """
    soup = BeautifulSoup(html or "", "html.parser")
    rows: List[Story] = []
    for tr in soup.select("tr.athing"):
        if "submission" not in _classes(tr):
            continue
        story = parse_story_row(tr, page=page, position=len(rows) + 1)
        if story is not None:
            rows.append(story)
    return rows


# ---------------------------------------------------------------------------
# Item: Comment rows
# ---------------------------------------------------------------------------

def _fat_item(soup):
    return soup.select_one("table.fatitem")


def story_meta(html: str) -> Dict[str, Any]:
    """The item at the top of an /item page, for the run's sidecar.

    A story, or -- when the address was a comment's -- that comment with the
    story it sits under. The comments go in rows; this is the context they
    need and that no row of theirs repeats.
    """
    soup = BeautifulSoup(html or "", "html.parser")
    fat = _fat_item(soup)
    if fat is None:
        return {}
    tr = fat.select_one("tr.athing")
    if tr is None:
        return {}
    item_id = tr.get("id")
    if "submission" in _classes(tr):
        s = parse_story_row(tr, page=None, position=1)
        body = fat.select_one("div.toptext")
        return {
            "item_id": item_id, "item_type": "story",
            "title": s.title if s else None, "url": s.url if s else None,
            "author": s.author if s else None, "points": s.points if s else None,
            "comments": s.comments if s else None,
            "posted_at": s.posted_at if s else None, "kind": s.kind if s else None,
            "text": _text_from_html(body) or None,
        }
    head = tr.select_one("span.comhead")
    on = head.select_one("span.onstory a") if head else None
    return {
        "item_id": item_id, "item_type": "comment",
        "story_id": _item_id_from(on.get("href")) if on else None,
        "title": on.get("title") if on else None,
    }


def _comment_status(head, body_text: str, has_author: bool) -> str:
    """ok | flagged | dead | deleted, from what the page says.

    `[flagged]` is the whole visible body of a flagged comment. `[dead]`
    sits in the header of a killed one. A deleted comment has no author.
    Each is the site's own word, read as shown."""
    head_text = head.get_text(" ", strip=True) if head else ""
    if "[dead]" in head_text or body_text.startswith("[dead]"):
        return "dead"
    if body_text == "[flagged]" or "[flagged]" in head_text:
        return "flagged"
    if body_text == "[deleted]" or (not has_author and not body_text):
        return "deleted"
    return "ok"


def parse_comments(html: str, *, story_id: Optional[str] = None) -> List[Comment]:
    """Comment rows in document order, which is thread order.

    Selected by class TOKEN (`athing` and `comtr` both present): collapsed and
    flagged rows carry extra classes and the exact-string selector loses
    them (see the module docstring).

    `parent_id` comes from the row's own "parent" link where it has one
    (every comment below the top level does) and from the indentation stack
    otherwise; the two agree on every capture and a check pins that.
    """
    soup = BeautifulSoup(html or "", "html.parser")
    fat = _fat_item(soup)
    fat_tr = fat.select_one("tr.athing") if fat else None
    fat_id = fat_tr.get("id") if fat_tr else None
    if story_id is None:
        m = story_meta(html)
        story_id = m.get("item_id") if m.get("item_type") == "story" else m.get("story_id")

    rows: List[Comment] = []
    stack: List[Tuple[int, str]] = []      # (indent, id) of the open ancestors
    tree = soup.select_one("table.comment-tree")
    if tree is None:
        return rows
    for tr in tree.select("tr.athing"):
        if "comtr" not in _classes(tr):
            continue
        sku = tr.get("id")
        if not sku:
            continue
        ind = tr.select_one("td.ind")
        depth = (_int_or_none(ind.get("indent")) if ind is not None else 0) or 0
        while stack and stack[-1][0] >= depth:
            stack.pop()
        by_stack = stack[-1][1] if stack else fat_id
        stack.append((depth, sku))

        head = tr.select_one("span.comhead")
        parent_link = None
        if head is not None:
            for a in head.select("span.navs a"):
                if a.get_text(strip=True) == "parent":
                    parent_link = (a.get("href") or "").lstrip("#") or None
        parent_id = parent_link or by_stack

        author = head.select_one("a.hnuser") if head else None
        age = head.select_one("span.age") if head else None
        body = tr.select_one("div.comment")
        text_node = body.select_one("div.commtext") if body else None
        if text_node is not None:
            text = _text_from_html(text_node)
        elif body is not None:
            # A flagged or dead body has no `.commtext`: the sentence is the
            # div's own text, and its `.reply` child is not part of it.
            clone = BeautifulSoup(str(body), "html.parser")
            for r in clone.select("div.reply"):
                r.decompose()
            text = clone.get_text(" ", strip=True)
        else:
            text = ""
        rows.append(Comment(
            url=item_url(sku),
            sku=sku,
            story_id=story_id,
            parent_id=parent_id,
            author=author.get_text(strip=True) if author else None,
            posted_at=_iso(age.get("title")) if age else None,
            depth=depth,
            position=len(rows) + 1,
            status=_comment_status(head, text, author is not None),
            text=text or None,
            links=_links_in(text_node) or None,
        ))
    return rows


# ---------------------------------------------------------------------------
# User: one row
# ---------------------------------------------------------------------------

def parse_user(html: str) -> List[User]:
    soup = BeautifulSoup(html or "", "html.parser")
    box = soup.select_one("#bigbox") or soup
    cells: Dict[str, Any] = {}
    for tr in box.select("tr"):
        tds = tr.find_all("td", recursive=False)
        if len(tds) != 2:
            continue
        label = tds[0].get_text(strip=True).rstrip(":").lower()
        if label in ("user", "created", "karma", "about"):
            cells[label] = tds[1]
    if "user" not in cells:
        return []
    name = cells["user"].get_text(strip=True)
    created = None
    ts = str(cells["user"].get("timestamp", ""))
    if ts.isdigit():
        created = datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ")
    karma = _int_or_none(cells["karma"].get_text()) if "karma" in cells else None
    about_node = cells.get("about")
    return [User(url=f"https://{HOST}/user?id={quote(name)}", sku=name,
                 created_at=created, karma=karma,
                 about=_text_from_html(about_node) or None,
                 about_links=_links_in(about_node) or None)]


# ---------------------------------------------------------------------------
# The entry points the shared loop calls
# ---------------------------------------------------------------------------

def parse_page(html: str, query: Query, page_num: int = 1) -> List[Any]:
    """Rows for one fetched page of `query`. `page_num` is the SITE's page
    number, so a run started on `?p=2` labels its first rows page 2."""
    if query.mode == "item":
        return parse_comments(html)
    if query.mode == "user":
        return parse_user(html)
    return parse_listing(html, page=page_num)


def page_meta(html: str, query: Query) -> Dict[str, Any]:
    """Facts about the page that are not rows (the sidecar's `item`)."""
    return story_meta(html) if query.mode == "item" else {}
