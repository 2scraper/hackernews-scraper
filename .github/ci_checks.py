#!/usr/bin/env python3
"""
CI checks that are too long to live inside the workflow YAML.

They started out as heredocs in `.github/workflows/tests.yml` and moved here
for one practical reason: a Python block nested inside YAML inside a shell
`run:` needs three levels of quoting to stay intact, and shell-quoted regexes
like '(ws|wss)://[^ "'"'"']+' do not survive being copied through a browser.
A separate .py file is copy-paste safe, runs locally, and can be read on its
own.

Run any of these from the repo root:

    python .github/ci_checks.py --help-check
    python .github/ci_checks.py --sample-check
    python .github/ci_checks.py --secret-check
    python .github/ci_checks.py --all

Each prints what it looked at and exits non-zero on failure.
"""

import argparse
import csv
import json
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# Engine libraries are deliberately absent in CI — the offline suite does not
# need them. An ImportError naming one of these is expected, not a failure.
ENGINE_LIBS = ("playwright", "pyppeteer", "selenium", "webdriver_manager")

CLIS = ["playwright_scraper.py", "puppeteer_scraper.py", "selenium_scraper.py",
        "scraper_api_client.py", "fingerprint_client.py", "env_config.py",
        "diff_runs.py"]

# One real run per mode: three row classes, three samples. The MODE is read
# from the file name, because no column of a row says which mode produced it
# (the sidecar does, and a sample is committed without its sidecar).
SAMPLE_FILES = ("sample_output.json", "sample_output.csv",
                "sample_output_item.json", "sample_output_item.csv",
                "sample_output_user.json", "sample_output_user.csv")
SAMPLE_MODE = {"sample_output.json": "listing", "sample_output_item.json": "item",
               "sample_output_user.json": "user"}

# Phrases that show up in hand-written or template sample data. The point of
# committing a sample is that it came from a real run; a placeholder teaches
# readers field names and value shapes that do not exist.
FABRICATION_MARKERS = ("sample-product-", "example brand", "sample product",
                       "product description text", "lorem ipsum",
                       "your_api_key", "123456789")

# A URL carrying real credentials — the shape is scheme://something:something@host
# (deliberately not spelled out as an example here: this file scans itself, and
# an illustrative credential in a comment is a false positive that turns the
# build red for no reason. It happened on the first run.)
CREDENTIALLED_URL = re.compile(r"(?:ws|wss|https?)://[^\s\"'/]+:[^\s\"'/]+@")

# Documented placeholders and test values, which are SUPPOSED to look like the
# real thing — that is the point of them. Each entry earns its place by being
# in a line whose job is to show the shape of a credential or to prove the
# masker removes one; a real secret matches none of these.
#
# Kept as an explicit list rather than a loose pattern so that adding one is a
# decision. The alternative — a regex broad enough to cover them all — would
# also cover a real login.
CREDENTIAL_ALLOWED = (
    # documentation placeholders
    "USER:PASS", "user:pass", "ACCOUNT:PASSWORD", "LOGIN:PASSWORD",
    # A README that documents a proxy URL as `http://username:password@…`.
    # Once such a line is committed it is in the history for good, so
    # --history-check would fail forever on a literal placeholder — which
    # would teach everyone to ignore the one check that exists to be read
    # exactly once, before publishing. Allowed by NAME, so a real login
    # still fails.
    "username:password",
    "{login}", "{user}", "password}@", "***", "u:p@h",
    "login:password@host:port",     # the shape a refusal message prints
    "user:secret@",                 # the proxy-pool masking fixtures
    "u:supersecret@", "login:supersecret@",   # the redaction fixtures
    "u:pass@h1", "u:pass@h2",       # the global-masking fixture
    "only:1",                       # a one-exit pool fixture
)

# A 2captcha API key is a 32-character hex string.
HEX32 = re.compile(r"\b[0-9a-f]{32}\b")

# EMPTY, and that is the stricter arrangement rather than an omission.
#
# Nothing news.ycombinator.com publishes is a 32-hex string: its ids are
# decimal integers (`item?id=49901736`), its usernames are words, and the two
# hex-shaped values its pages DO carry (`auth=` on the favourite link and the
# `hmac` input of the comment form) are per-session, 40 characters, and
# removed from every fixture by `make_fixtures.py`. So a bare 32-hex string
# anywhere in this repo fails, with no exception to reason about. An
# allowlist forgiving one shape is a hole a real key could later hide in.
#
# The mechanism below is kept, empty, because it is family core and the next
# site that publishes such an id needs it: add a compiled pattern for the
# exact context and nothing else.
SITE_PUBLIC_IDS = ()

def _without_site_ids(line):
    """A line with this site's own published identifiers taken out.

    Two passes, and the second is what makes this precise rather than broad.
    The first removes the identifier in the CONTEXTS the site publishes it
    in. The second removes those exact VALUES anywhere else on the same line
    — because a row that has already shown a hex as the ad's public id in its
    URL is not also carrying it as a separate secret, and in CSV that is
    exactly what happens: the URL column and the `listing_uuid` column hold
    the same string, one of them with no surrounding context at all.

    Anything left is a 32-hex the line never justified, and it still fails.
    """
    known = set()
    for pattern in SITE_PUBLIC_IDS:
        for match in pattern.finditer(line):
            known.update(HEX32.findall(match.group(0)))
        line = pattern.sub("SITE-AD-ID", line)
    for value in known:
        line = line.replace(value, "SITE-AD-ID")
    return line


# Contexts in which a 32-hex string is plainly not a key.
HEX32_ALLOWED = ("sha", "hash", "nonce", "example", "md5", "digest",
                 "checksum")

# Files the BARE-HEX rule is not applied to: none. A family sibling exempts
# its generated data files wholesale because its site emits 32-hex ids in
# five public contexts and the rule cried wolf hundreds of times. This site
# emits none, so the strictest rule covers the largest file in the
# repository too (`fixtures_generated.json`), which is the file nobody reads
# line by line and the one most likely to acquire a stray value.
GENERATED_DATA_FILES = ()

# A secret sitting in a field named like one. This is what the bare-hex rule
# was reaching for, said precisely, and it applies to EVERY tracked file
# including the generated ones.
#
# `\\?"` on every quote, and that is not defensive punctuation — it closes a
# hole this check had. `fixtures_generated.json` stores each fixture's markup
# as a JSON STRING, so every quote inside it is escaped: the file contains
# `\"apiKey\": \"…\"`, not `"apiKey": "…"`. The pattern with bare quotes
# therefore matched ZERO times in the largest file in the repository — the
# one holding 427 KB of captured page payload, which is precisely where a
# front-end key would arrive.
#
# Verified in a sibling by planting a real-shaped key in a fixture: before
# the fix the scan reported "nothing credential-shaped" and passed; after it,
# the scan names the file and the line. A front-end key can be 32-char
# ALPHANUMERIC rather than hex, in which case the bare-hex rule does not see
# it either.
#
# The same escaping applies to any JSON-embedded capture, which is how every
# repo in this family stores its fixtures.
KEY_SHAPED_FIELD = re.compile(
    r'\\?"(?:[a-zA-Z_-]*(?:api[_-]?key|apikey|secret|token|password|'
    r'client[_-]?key|access[_-]?key|site[_-]?key))\\?"\s*[:=]\s*'
    r'\\?"(?!REDACTED-|SCRUBBED[_-]|your_|\{|\*\*\*)'
    r'[A-Za-z0-9_-]{16,}\\?"', re.I)

# History findings that have been LOOKED AT and cleared, each with its
# reason. This exists because the history scan is a pre-publication gate: a
# later commit cannot reach what a published tag and a merged PR's refs
# already hold, so the decision has to be made once, before the repo goes
# public — and a decision that is not written down gets made again by the
# next person, differently.
#
# An entry here is a claim that someone read the blob. Anything not listed
# still fails.
HISTORY_DECIDED = {
    # EMPTY, and it should stay that way. This repository was created clean:
    # every blob that has ever existed in it was written by the work that
    # built it, the fixtures were scrubbed by `make_fixtures.py` before their
    # first commit, and the raw captures live in an ignored `captures/`.
    #
    # An entry here is a claim that a human read the blob and decided it is
    # not a leak. Anything not listed still fails.
}

# Raw captures that HAVE been committed at some point, each with the decision
# taken about it. A blob in history cannot be removed by a later commit — a
# merged PR's refs and any published tag keep it — so this is the record the
# pre-publication step asks for: read it once, decide once, BEFORE the repo
# goes public, because afterwards only a fresh repository removes it.
CAPTURES_IN_HISTORY_DECIDED = {
    # Empty, and checked rather than assumed: the history scan was run over
    # every blob before this repo was made public.
    #
    # Worth knowing what a capture of THIS site carries, so the decision can
    # be made quickly: whole HTML pages. Usernames (public pseudonyms) and
    # the full text of strangers' comments, and, on any item or profile page,
    # per-session `auth=` tokens and `hmac` form inputs that the site hands
    # even an anonymous visitor. None of it is ours (no key, no cookie, no
    # proxy credential), but the words and the session values do not belong
    # in a public repository, which is what make_fixtures.py exists for.
}

# Suffixes the HISTORY scan walks. It reads blobs out of git, where a
# binary is expensive to decode and useless to grep, so it stays narrow.
SCANNED_SUFFIXES = (".py", ".md", ".txt", ".yml", ".yaml", ".example")

# The WORKING-TREE scan is the opposite: it reads whatever is TRACKED, at any
# suffix, and that is not tidiness.
#
# A suffix allowlist is a scanner that cannot see the thing most likely to
# leak. `--dump-html live_results` writes `live_results.page1` — no suffix
# the list knew — and in a sibling repo a merge committed two of them, 1.5 MB
# each, carrying that site's front-end keys.
# The check that exists to stop exactly that ran, passed, and never opened
# them.
#
# So the rule inverted: scan every tracked file, skip only what cannot be
# grepped.
BINARY_SUFFIXES = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".pdf",
                   ".zip", ".gz", ".tar", ".whl", ".woff", ".woff2", ".ttf")

# A raw page dump has no business being tracked at all — it is 1.5 MB of
# someone else's session material, and scrubbing it is `make_fixtures.py`'s
# job. Matched on SHAPE rather than on the two names that got through once.
CAPTURE_SHAPES = (
    re.compile(r"\.page\d+$"),
    re.compile(r"_debug\.(?:html|png)$"),
    re.compile(r"^live_results\."),
    re.compile(r"^captures?/"),
    # ANY .html or .png under a working directory, whatever that directory is
    # called. The four rules above all key on a NAME someone chose — a
    # `--dump-html` default, a debug suffix, one particular directory — and
    # that is exactly how a 1.5 MB page dump nearly reached the first commit
    # of this repository: it sat in `live/`, which no pattern here listed and
    # `.gitignore` did not cover either, and `git add -A` staged it while
    # this check reported nothing.
    #
    # A repo's own source has no business keeping rendered HTML or a
    # screenshot in a run directory, so the shape is the directory kind
    # rather than the filename. `.github/` and `tests/` are not run
    # directories and are unaffected; a fixture belongs in
    # fixtures_generated.json, which make_fixtures.py scrubs and verifies.
    re.compile(r"^(?:live|out|output|runs?|results?|tmp|scratch|dumps?)/"
               r".*\.(?:html|htm|png|jpe?g|mhtml)$"),
    # And a bare .html anywhere at the repo ROOT, which is where a
    # `--dump-html foo.html` lands by default.
    re.compile(r"^[^/]+\.(?:html|mhtml)$"),
)


def _git_files(*flags):
    out = subprocess.run(["git", "ls-files", "-z", *flags], cwd=REPO,
                         capture_output=True, text=True)
    if out.returncode != 0:
        return
    for rel in out.stdout.split("\0"):
        if rel:
            yield REPO / rel


def tracked_files():
    """Only what git already tracks. Used by the raw-capture rule, whose
    question is literally "is this committed"."""
    return _git_files("--cached")


# A directory holding `pyvenv.cfg` is a virtualenv, whatever it is called.
#
# Structural rather than by name, and that was measured rather than reasoned:
# a clean clone set up the way the README says puts a virtualenv in the
# working tree, and a scan that skips only the names it happens to know walks
# into pip's vendored code and flags a 32-hex string in `_elffile.py` as
# key-shaped. Correct about the string, wrong about the file — and it is the
# FIRST thing a new user sees from `python3 smoke_test.py`. A guard people
# have to argue with is one they learn to suppress.
#
# Deliberately NOT a narrowing of what gets scanned: an untracked file is
# still read, because a key pasted into a scratch file beside the scripts is
# exactly the case this scan exists for.
_VENV_CACHE = {}


def _is_in_virtualenv(path):
    """True if any ancestor of `path` (under REPO) is a virtualenv root."""
    for parent in path.parents:
        try:
            if parent == REPO.parent:
                break
        except Exception:
            pass
        cached = _VENV_CACHE.get(parent)
        if cached is None:
            cached = (parent / "pyvenv.cfg").is_file()
            _VENV_CACHE[parent] = cached
        if cached:
            return True
        if parent == REPO:
            break
    return False


def scanned_files():
    """What the CONTENT rules read: tracked files PLUS new files that are not
    ignored.

    Asked of GIT rather than of the disk, and the two flags are the whole
    design:

      --cached            what is committed, which is what can leak;
      --others            what is new, so a secret is caught BEFORE it is
                          added rather than after;
      --exclude-standard  which drops everything `.gitignore` covers — a
                          developer's own `.env`, their captures and their
                          run output are EXPECTED beside the scripts, and a
                          check that went red on them would be red on every
                          machine that had ever run the scraper for real,
                          which is the machine most likely to run it.
    """
    for path in _git_files("--cached", "--others", "--exclude-standard"):
        if not path.is_file() or path.suffix.lower() in BINARY_SUFFIXES:
            continue
        if _is_in_virtualenv(path):
            continue
        yield path


def help_check():
    failed = []
    for name in CLIS:
        script = REPO / name
        if not script.is_file():
            print(f"missing  {name}")
            failed.append(name)
            continue
        result = subprocess.run([sys.executable, str(script), "--help"],
                                capture_output=True, text=True, cwd=REPO)
        if result.returncode == 0:
            print(f"ok       {name}")
            continue
        blob = result.stdout + result.stderr
        if "ModuleNotFoundError" in blob and any(lib in blob for lib in ENGINE_LIBS):
            print(f"skipped  {name} (engine library not installed here)")
            continue
        print(f"FAILED   {name}\n{blob}")
        failed.append(name)
    return failed


def sample_check():
    """Each committed sample is a real run's output, and a schema test.

    Three modes, three row classes, so three samples. Each file's rows are
    checked against the row class of the mode THEY say they are, in column
    order, and each CSV's header against the same class. Rename a field in
    the code and forget the sample, and this fails rather than the docs
    going stale.
    """
    failed = []
    for name in SAMPLE_FILES:
        if not (REPO / name).is_file():
            failed.append(f"{name} is missing — regenerate it from a real run")
    if failed:
        return failed

    sys.path.insert(0, str(REPO))
    from dataclasses import fields
    from output_writer import ROW_CLASS_BY_MODE

    for json_name in [n for n in SAMPLE_FILES if n.endswith(".json")]:
        rows = json.loads((REPO / json_name).read_text(encoding="utf-8"))
        if not rows:
            failed.append(f"{json_name} is empty — a run that found nothing is not a sample")
            continue
        blob = json.dumps(rows).lower()
        hits = [m for m in FABRICATION_MARKERS if m in blob]
        if hits:
            failed.append(f"{json_name} looks fabricated: {hits}")
        mode = SAMPLE_MODE.get(json_name)
        if mode not in ROW_CLASS_BY_MODE:
            failed.append(f"{json_name}: no mode is registered for this file "
                          f"(SAMPLE_MODE knows {sorted(SAMPLE_MODE)})")
            continue
        expected = [f.name for f in fields(ROW_CLASS_BY_MODE[mode])]
        for i, row in enumerate(rows):
            if list(row.keys()) != expected:
                failed.append(f"{json_name} row {i}: columns differ from "
                              f"output_writer.{ROW_CLASS_BY_MODE[mode].__name__}")
                break
        csv_name = json_name[:-5] + ".csv"
        with (REPO / csv_name).open(newline="", encoding="utf-8") as handle:
            header = next(csv.reader(handle))
        if header != expected:
            failed.append(f"{csv_name} header differs from "
                          f"output_writer.{ROW_CLASS_BY_MODE[mode].__name__}")
        if not failed:
            print(f"ok       {json_name}: {len(rows)} {mode} rows, "
                  f"{len(expected)} columns, schema matches")
    return failed


def secret_check():
    failed = []
    scanned = 0
    for path in scanned_files():
        scanned += 1
        for lineno, line in enumerate(
                path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
            rel = path.relative_to(REPO)

            if CREDENTIALLED_URL.search(line) and not any(
                    token in line for token in CREDENTIAL_ALLOWED):
                failed.append(f"{rel}:{lineno} looks like a URL with real "
                              f"credentials in it")

            # Applies everywhere, generated data included — this is the rule
            # the bare-hex one was reaching for, said precisely.
            for hit in KEY_SHAPED_FIELD.findall(line):
                failed.append(f"{rel}:{lineno} has a secret-shaped value in a "
                              f"key-shaped field")

            if rel.name in GENERATED_DATA_FILES:
                continue

            for match in HEX32.findall(_without_site_ids(line)):
                if any(token in line.lower() for token in HEX32_ALLOWED):
                    continue
                # A value already decided for the history scan is decided
                # here too — and this branch is not hypothetical: writing
                # HISTORY_DECIDED down put one of those strings into the
                # working tree, so without it this check failed on the very
                # file that records the decision.
                if match in HISTORY_DECIDED:
                    continue
                failed.append(f"{rel}:{lineno} contains {match[:6]}… — a "
                              f"32-char hex string, the shape of a 2captcha key")

    # A raw capture must not be tracked AT ALL, whatever is in it. This is
    # separate from the content scan on purpose: the two dumps that got
    # through carried nothing of OURS -- no key, no proxy password, no
    # cookie -- so a content rule would have passed them. What was wrong was
    # that they were committed: 3 MB of someone else's session material,
    # unscrubbed, in a repository whose own rules say captures stay out.
    for path in tracked_files():
        rel = path.relative_to(REPO).as_posix()
        if any(shape.search(rel) for shape in CAPTURE_SHAPES):
            failed.append(f"{rel} is a raw page capture and is TRACKED. "
                          f"Captures stay out of the repo; run them through "
                          f"make_fixtures.py, which scrubs them and proves "
                          f"the trim parses identically.")

    if not failed:
        print(f"ok       {scanned} files scanned, nothing credential-shaped, "
              f"no raw capture tracked")
    return failed


def history_check():
    """The same rules, applied to every blob that has EVER existed.

    `secret_check` reads the working tree, which is the right scope for CI:
    it fails a pull request before the mistake lands. This one is for the
    step CI cannot do anything about — publishing.

    A commit on top cannot reach what a published tag and a merged PR's refs
    already hold; those stay attached to the PR and cannot be deleted from
    it. So the decision has to be made BEFORE the repository goes public,
    and afterwards only a fresh repository removes anything. Run this then:

        python .github/ci_checks.py --history-check

    Deliberately NOT part of `--all` and not run by CI. It shells out to git
    once per object, which is fine for a hundred and wasteful on every push,
    and a repo whose history is dirty needs a decision rather than a red
    check.
    """
    try:
        listing = subprocess.run(["git", "rev-list", "--objects", "--all"],
                                 cwd=REPO, capture_output=True, text=True,
                                 check=True).stdout
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        return [f"could not read the git history ({e}) — run this inside a "
                f"clone, not an export"]

    objects = []
    for line in listing.splitlines():
        parts = line.split(None, 1)
        if parts:
            objects.append((parts[0], parts[1] if len(parts) > 1 else ""))

    failed, decided, scanned = [], [], 0

    # RAW CAPTURES THAT HAVE EVER BEEN COMMITTED, reported by name and size
    # whether or not they are still in the tree. Removing one in a later
    # commit does not reach the blob: a merged PR's refs and any published
    # tag keep it, and only a fresh repository removes it. So this is not a
    # pass/fail rule — it is the thing a reader has to make a decision about
    # before the repo goes public, which is the whole reason this scan
    # exists. A decision taken is recorded in CAPTURES_IN_HISTORY_DECIDED.
    for sha, path in objects:
        if not path or not any(s.search(path) for s in CAPTURE_SHAPES):
            continue
        size = subprocess.run(["git", "cat-file", "-s", sha], cwd=REPO,
                              capture_output=True, text=True).stdout.strip()
        note = CAPTURES_IN_HISTORY_DECIDED.get(path)
        if note:
            decided.append(f"{path} ({size} bytes, in history forever) — {note}")
        else:
            failed.append(
                f"{path} ({size} bytes) is a raw page capture that has been "
                f"COMMITTED at some point. A later commit cannot remove the "
                f"blob. Read it, decide, and record the decision in "
                f"CAPTURES_IN_HISTORY_DECIDED — or start a fresh repository.")

    for sha, path in objects:
        if not (path.endswith(SCANNED_SUFFIXES) or path in ("Dockerfile",)):
            continue
        kind = subprocess.run(["git", "cat-file", "-t", sha], cwd=REPO,
                              capture_output=True, text=True).stdout.strip()
        if kind != "blob":
            continue
        scanned += 1
        body = subprocess.run(["git", "cat-file", "blob", sha], cwd=REPO,
                              capture_output=True, text=True,
                              errors="replace").stdout
        for lineno, line in enumerate(body.splitlines(), 1):
            if CREDENTIALLED_URL.search(line) and not any(
                    token in line for token in CREDENTIAL_ALLOWED):
                failed.append(f"{path}:{lineno} (in a past commit) looks like "
                              f"a URL with real credentials in it")
            for match in HEX32.findall(_without_site_ids(line)):
                if any(token in line.lower() for token in HEX32_ALLOWED):
                    continue
                if match in HISTORY_DECIDED:
                    decided.append(f"{path}:{lineno} {match[:6]}… — "
                                   f"{HISTORY_DECIDED[match]}")
                    continue
                failed.append(f"{path}:{lineno} (in a past commit) contains "
                              f"{match[:6]}… — the shape of a 2captcha key")
    for note in sorted(set(decided)):
        print(f"decided  {note}")

    if not failed:
        print(f"ok       {scanned} blob(s) across {len(objects)} object(s) "
              f"that have ever existed — nothing credential-shaped")
    else:
        print("         NOTE: a later commit cannot remove any of these. A "
              "published tag and a merged PR's refs keep them, so this needs "
              "a decision BEFORE the repo goes public.")
    return failed


CHECKS = {"help": help_check, "sample": sample_check,
          "secret": secret_check, "history": history_check}


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--help-check", action="store_true",
                        help="Every shipped CLI answers --help")
    parser.add_argument("--sample-check", action="store_true",
                        help="sample_output.* exist, are real, match the schema")
    parser.add_argument("--secret-check", action="store_true",
                        help="No credentials committed anywhere")
    parser.add_argument("--history-check", action="store_true",
                        help="The same rules over every blob that has EVER "
                             "existed. For before publishing, not for CI — "
                             "see history_check(). Not included in --all.")
    parser.add_argument("--all", action="store_true",
                        help="help, sample and secret. NOT history: that one "
                             "is a pre-publication step, and it shells out to "
                             "git once per object.")
    args = parser.parse_args()

    selected = [name for name in CHECKS
                if getattr(args, f"{name}_check")
                or (args.all and name != "history")]
    if not selected:
        parser.error("pick at least one check, or --all")

    failures = []
    for name in selected:
        print(f"--- {name} check")
        failures += [f"[{name}] {line}" for line in CHECKS[name]()]

    if failures:
        print()
        for line in failures:
            print("FAILED:", line)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
