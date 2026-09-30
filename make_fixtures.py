#!/usr/bin/env python3
"""
make_fixtures.py — build fixtures_generated.json from real captures.

    python3 make_fixtures.py [--captures captures/] [--render]

`captures/` holds raw pages fetched from news.ycombinator.com on 2026-09-30
(plain `curl`, and one browser-serialised copy per page kind, see --render).
It is NOT committed: a raw page dump carries the session that fetched it and
strangers' words (CLAUDE.md §10). This script turns each capture into a
smaller, scrubbed fixture and PROVES the trimmed copy parses the way the
original did, so the suite tests the markup of the real site and not a
hand-written imitation of it.

NOT VERBATIM, and each change is a decision:

  * **Usernames** (`user?id=`, every `hnuser` link) become `user_a`,
    `user_b`, ... in order of first appearance. A handle is a person's
    pseudonym, and the checks need the STRUCTURE of an author, not the
    author.
  * **Comment bodies and the profile `about` box** become short placeholders
    (some with a second paragraph, a link, a code block, an entity), because
    they are strangers' writing. The markup AROUND them (`commtext c00`,
    the bare `<p>`, the `reply` div, `noshow`, `coll`) is untouched.
    `[flagged]` is the SITE's word for a flagged body and is kept.
  * **`auth=` tokens and every `<form>`** are removed. The `fave?id=…&auth=`
    links and the comment forms' `hmac` inputs are per-session values the
    page hands even an anonymous visitor.
  * **Comment trees are trimmed** to a prefix plus every flagged/collapsed
    row and its ancestors, so the tree stays consistent. The story header
    keeps the site's own comment count, which is therefore larger than the
    number of rows left; that mismatch is deliberate and pinned.

Everything else -- titles, domains, points, comment counts, ranks, ids,
timestamps, class names -- is exactly what the site served.

`--render` also stores, for each page kind, what Chromium's DOM serialiser
makes of the SAME bytes (`page.set_content` then `page.content()`, no
network), because that is what the browser engines hand the parser and it is
not what curl saw. It needs Playwright and a Chromium.
"""

import argparse
import json
import os
import re
import sys

from bs4 import BeautifulSoup

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import product_parser as P  # noqa: E402

LISTINGS = ("news", "news2", "newest", "ask", "jobs", "submitted", "from",
            "front", "show", "best", "active")
PLAIN = ("p50", "nouser", "itemabc", "futureday", "oldday")
ITEMS = ("item_top", "ask_item", "comment_item", "big")

# How many comment rows of each tree survive, before the flagged/collapsed
# rows and their ancestors are added back.
KEEP_PREFIX = {"item_top": 30, "ask_item": 25, "comment_item": 20, "big": 45}


class Names:
    def __init__(self):
        self.map = {}

    def __call__(self, real):
        if real not in self.map:
            n = len(self.map)
            letters = ""
            while True:
                letters = chr(ord("a") + n % 26) + letters
                n = n // 26 - 1
                if n < 0:
                    break
            self.map[real] = "user_" + letters
        return self.map[real]


def read(captures, name):
    with open(os.path.join(captures, name + ".html"), encoding="utf-8") as f:
        return f.read()


def strip_session(html):
    """Remove per-session material: auth tokens and every form."""
    html = re.sub(r"<form\b.*?</form>", "", html, flags=re.S)
    html = re.sub(r"&amp;auth=[0-9a-f]{20,}", "", html)
    html = re.sub(r"[?&]auth=[0-9a-f]{20,}", "", html)
    return html


def scrub_names(html, names):
    def user_href(m):
        return m.group(1) + names(m.group(2))
    html = re.sub(r"((?:user|submitted|threads|favorites|upvoted)\?id=)([A-Za-z0-9_-]+)",
                  user_href, html)
    # Either attribute order: the raw page writes href first, and a parsed
    # and re-serialised one writes class first.
    html = re.sub(r'(<a [^>]*class="hnuser"[^>]*>)([^<]+)(</a>)',
                  lambda m: m.group(1) + names(m.group(2)) + m.group(3), html)
    return html


PLACEHOLDERS = (
    "Fixture comment text.",
    "First paragraph of a fixture comment.<p>Second paragraph, after a bare "
    "paragraph tag with no closing one.",
    "A fixture comment with a link:<p><a href=\"https:&#x2F;&#x2F;example.org&#x2F;"
    "a&#x2F;very&#x2F;long&#x2F;path&#x2F;that&#x2F;the&#x2F;page&#x2F;would&#x2F;shorten\" "
    "rel=\"nofollow\">https:&#x2F;&#x2F;example.org&#x2F;a&#x2F;very&#x2F;long&#x2F;pa...</a>",
    "It&#x27;s an entity &amp; a fixture.",
    "Code follows:<p><pre><code>for x in y:\n    print(x)\n</code></pre>",
)


def scrub_title(soup):
    """A comment page's <title> is the first words of the comment itself
    (`We also have Nerf Bench: https://...`), which is a stranger's writing
    outside any `commtext`. A story page's title is the headline, and stays."""
    fat = soup.select_one("table.fatitem tr.athing")
    title = soup.find("title")
    if fat is not None and title is not None and "submission" not in (fat.get("class") or []):
        title.string = "Fixture comment text."


def scrub_bodies(soup):
    i = 0
    for node in soup.select("div.commtext"):
        node.clear()
        node.append(BeautifulSoup(PLACEHOLDERS[i % len(PLACEHOLDERS)], "html.parser"))
        i += 1
    for node in soup.select("div.toptext"):
        if node.get_text(strip=True):
            node.clear()
            node.append(BeautifulSoup("Fixture story text.<p>A second paragraph.",
                                      "html.parser"))


def trim_tree(soup, keep_prefix):
    """Keep a prefix of the comment rows, every flagged/collapsed row and the
    ancestors of all of them. Returns (kept, dropped)."""
    tree = soup.select_one("table.comment-tree")
    if tree is None:
        return 0, 0
    rows = [tr for tr in tree.select("tr.athing") if "comtr" in (tr.get("class") or [])]
    parent_of = {}
    stack = []
    for tr in rows:
        depth = int(tr.select_one("td.ind").get("indent", 0))
        while stack and stack[-1][0] >= depth:
            stack.pop()
        parent_of[tr["id"]] = stack[-1][1] if stack else None
        stack.append((depth, tr["id"]))
    keep = {tr["id"] for tr in rows[:keep_prefix]}
    for tr in rows:
        cls = tr.get("class") or []
        if "noshow" in cls or "coll" in cls:
            keep.add(tr["id"])
    closed = set(keep)
    for rid in list(keep):
        p = parent_of.get(rid)
        while p and p not in closed:
            closed.add(p)
            p = parent_of.get(p)
    dropped = 0
    for tr in rows:
        if tr["id"] not in closed:
            tr.decompose()
            dropped += 1
    return len(closed), dropped


def listing_signature(rows):
    return [(r.sku, r.title, r.url, r.points, r.comments, r.posted_at, r.domain,
             r.kind, r.rank, r.position) for r in rows]


def build(captures, render):
    names = Names()
    out = {}
    notes = []

    for name in LISTINGS:
        raw = read(captures, name)
        before = P.parse_listing(raw, page=1)
        scrubbed = scrub_names(strip_session(raw), names)
        after = P.parse_listing(scrubbed, page=1)
        assert len(before) == len(after) and before, name
        assert listing_signature(before) == listing_signature(after), name
        assert [r.author and names(r.author) for r in before] == \
            [r.author for r in after], name
        out[name] = scrubbed
        notes.append("%s: %d rows" % (name, len(after)))

    for name in PLAIN:
        out[name] = strip_session(read(captures, name))

    for name in ITEMS:
        raw = read(captures, name)
        full = P.parse_comments(raw)
        soup = BeautifulSoup(strip_session(raw), "html.parser")
        scrub_bodies(soup)
        scrub_title(soup)
        kept, dropped = trim_tree(soup, KEEP_PREFIX[name])
        html = scrub_names(str(soup), names)
        after = P.parse_comments(html)
        assert len(after) == kept, (name, len(after), kept)
        by_id = {c.sku: c for c in full}
        for c in after:
            o = by_id[c.sku]
            assert (c.parent_id, c.depth, c.posted_at, c.status) == \
                (o.parent_id, o.depth, o.posted_at, o.status), (name, c.sku)
            assert c.author == (names(o.author) if o.author else None), (name, c.sku)
        meta = P.story_meta(raw)
        meta_after = P.story_meta(html)
        for k in ("item_id", "item_type", "comments", "posted_at", "kind"):
            assert meta.get(k) == meta_after.get(k), (name, k)
        out[name] = html
        notes.append("%s: %d of %d rows kept" % (name, len(after), len(full)))

    user_raw = read(captures, "user")
    soup = BeautifulSoup(strip_session(user_raw), "html.parser")
    about = soup.select_one("#bigbox td[style]")
    if about is not None:
        about.clear()
        about.append(BeautifulSoup(
            "&quot;<i>A fixture about box.</i>&quot;<p>A second paragraph with "
            "a <a href=\"https:&#x2F;&#x2F;example.org&#x2F;me\" rel=\"nofollow\">link</a>.",
            "html.parser"))
    user_html = scrub_names(str(soup), names)
    before, after = P.parse_user(user_raw), P.parse_user(user_html)
    assert before[0].karma == after[0].karma and before[0].created_at == after[0].created_at
    out["user"] = user_html

    if render:
        from playwright.sync_api import sync_playwright
        rendered = {}
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            page = browser.new_page()
            for name in ("news", "newest", "jobs", "item_top", "user", "nouser"):
                page.set_content(out[name], wait_until="domcontentloaded")
                rendered[name] = page.content()
            browser.close()
            # Chromium's own network-error page, produced for real: a proxy
            # that refuses the connection. It is NOT the site's page, and
            # nothing in it names a vendor, so only "is it built out of the
            # site's own markup" tells it from a served page (§18).
            browser = pw.chromium.launch(proxy={"server": "http://127.0.0.1:9"})
            page = browser.new_page()
            try:
                page.goto("https://news.ycombinator.com/", timeout=15000)
            except Exception:  # noqa: BLE001 — the failure is the point
                pass
            # Measured: Playwright's page.content() after a failed goto() is the
            # BLANK document `<html><head></head><body></body></html>`. The
            # error's name (ERR_PROXY_CONNECTION_FAILED) is in the exception
            # goto() raised, not on the page, so a page that holds no marker
            # of any vendor and none of the site's own is what a reader sees.
            page.wait_for_timeout(500)
            rendered["chromium_proxy_error"] = page.content()
            browser.close()
        assert "hnmain" not in rendered["chromium_proxy_error"]
        for name, html in rendered.items():
            out[name if name.startswith("chromium") else "rendered_" + name] = html
            notes.append("rendered_%s: %d bytes" % (name, len(html)))

    out["_notes"] = notes
    out["_captured"] = "2026-09-30, news.ycombinator.com, plain curl"
    out["_authors"] = len(names.map)
    return out


def write_samples(fixtures, here):
    """sample_output*.{json,csv}: a few rows per mode, written by the real
    parser and the real writer from the committed, scrubbed fixtures.

    NOT cut from a live run, on purpose: a live item page is strangers'
    words, and a live listing carries their handles. What is committed is
    what THIS code writes for a page of the real site with those two things
    replaced, so the column shapes, ids, timestamps, depths and counts are
    the site's and the names and prose are not. The README says so.
    """
    import output_writer as O
    modes = (("sample_output", "listing", P.parse_listing(fixtures["news"], page=1)[:5]),
             ("sample_output_item", "item", P.parse_comments(fixtures["item_top"])[:8]),
             ("sample_output_user", "user", P.parse_user(fixtures["user"])))
    for name, mode, rows in modes:
        assert rows, name
        O.write_json(rows, os.path.join(here, name + ".json"))
        O.write_csv(rows, os.path.join(here, name + ".csv"), row_cls=O.ROW_CLASS_BY_MODE[mode])
        print("wrote %s.json/.csv (%d %s rows)" % (name, len(rows), mode))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--samples", action="store_true",
                    help="also (re)write sample_output*.json/.csv from the fixtures")
    ap.add_argument("--captures", default=os.path.join(HERE, "captures"))
    ap.add_argument("--render", action="store_true")
    ap.add_argument("--out", default=os.path.join(HERE, "fixtures_generated.json"))
    args = ap.parse_args()
    fixtures = build(args.captures, args.render)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(fixtures, f, ensure_ascii=False, indent=1, sort_keys=True)
    size = os.path.getsize(args.out)
    print("wrote %s (%d bytes)" % (args.out, size))
    if args.samples:
        write_samples(fixtures, HERE)
    for n in fixtures["_notes"]:
        print("  " + n)
    return 0


if __name__ == "__main__":
    sys.exit(main())
