#!/usr/bin/env python3
"""
diff_runs.py
------------
Diff two hackernews-scraper JSON outputs of the same mode by `sku`.

    added      in the new run, not the old one
    removed    in the old run, not the new one
    changed    in both, with a tracked column that differs

What each means depends on the mode, and it is worth being exact:

    listing   a story that entered the list (`added`) or left it
              (`removed`), or whose points, comment count or title moved.
              `removed` does NOT mean deleted: it fell off the slice this
              run fetched, and on the front page that is the ORDINARY way
              a story leaves. Two front-page runs an hour apart are mostly
              `changed` (points and comments move constantly) plus a few
              added and removed.
    item      a comment that appeared, or one the site stopped rendering
              (deleted, or flagged into the placeholder `[flagged]`, which
              shows up as a changed `status` and `text`).
    user      one row, with karma or the about text moved.

**The MODE comes from the sidecar, not from the rows.** Unlike some repos in
this family, no column of a row says which mode produced it, so without the
`<out>.meta.json` beside each file there is nothing to derive the tracked
columns from, and the tool refuses rather than compare on none: a diff over
zero tracked columns reports "0 changed" on real changes, which is worse than
refusing. `--mode` supplies it when a sidecar is gone.

**The tracked columns are DERIVED from the row class, not listed by hand.**
A hand-written list here is how a sibling family of repos came to report
"0 changed" on real changes for weeks: the list had been copied from a repo
whose rows had different columns, and every field it named was absent from
both sides, so every comparison was None == None. So TRACKED_FIELDS is
"every column of the mode's row class, minus the ones that describe the run
rather than the thing" (UNTRACKED_FIELDS), and a check asserts it is never
empty.

Refused, with --force as the escape hatch:
  * runs that are not both `complete` — a short run's unfetched pages read as
    `removed`;
  * runs of different modes — their rows share no columns worth comparing;
  * runs of different QUERIES (feed, day, user, domain, item), from the
    sidecar — every line would describe the query change.
"""

import argparse
import json
import re
import sys
from dataclasses import fields
from typing import Dict, List, Optional, Tuple

from output_writer import ROW_CLASS_BY_MODE, UNIQUE_BY_SKU_MODES

# Columns that describe the RUN, or that restate the key, rather than the
# story/comment/profile. `page`, `position` and `rank` are here because a live
# list reorders itself between runs: a story moving from rank 4 to 5 is the
# ordering, not the story. (Its points and comment count are tracked: those
# ARE the story's figures.) `depth` and `parent_id` never change for a
# comment, and stay tracked so a re-parenting would show.
UNTRACKED_FIELDS = frozenset({
    "source", "scraped_at", "sku", "page", "position", "rank", "mode", "sort",
    "data_source", "url",
})


def tracked_fields(mode: str) -> Tuple[str, ...]:
    row_cls = ROW_CLASS_BY_MODE.get(mode)
    if row_cls is None:
        return ()
    return tuple(f.name for f in fields(row_cls) if f.name not in UNTRACKED_FIELDS)


def _load(path: str) -> List[dict]:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _by_sku(rows: List[dict]) -> Tuple[Dict[str, dict], int]:
    indexed = {}
    unmatchable = 0
    for r in rows:
        sku = r.get("sku")
        if sku is None or sku in indexed:
            # No key, or a duplicate within one file: counted, never allowed
            # to clobber the first row silently.
            unmatchable += 1
            continue
        indexed[sku] = r
    return indexed, unmatchable


def mode_of(rows: List[dict]) -> Optional[str]:
    """The mode a row says it came from, if any does. None of THIS repo's
    row classes carries one, so this is normally None and the sidecar decides
    (see `resolved_mode`); it is kept for files written by a repo that does."""
    modes = {r.get("mode") for r in rows if r.get("mode")}
    return modes.pop() if len(modes) == 1 else None


def diff_products(old: List[dict], new: List[dict],
                  mode: Optional[str] = None) -> dict:
    mode = mode or mode_of(new) or mode_of(old)
    tracked = tracked_fields(mode or "")
    if not tracked:
        # Never diff over nothing: every comparison would be None == None
        # and the result would read "0 changed" (module docstring).
        raise ValueError("no mode to derive the tracked columns from: pass "
                         "mode=, or keep the .meta.json beside the run")
    old_by_sku, old_unmatchable = _by_sku(old)
    new_by_sku, new_unmatchable = _by_sku(new)

    added = [new_by_sku[s] for s in new_by_sku.keys() - old_by_sku.keys()]
    removed = [old_by_sku[s] for s in old_by_sku.keys() - new_by_sku.keys()]
    changed = []
    for sku in old_by_sku.keys() & new_by_sku.keys():
        before, after = old_by_sku[sku], new_by_sku[sku]
        deltas = {f: {"old": before.get(f), "new": after.get(f)}
                  for f in tracked if before.get(f) != after.get(f)}
        if deltas:
            changed.append({"sku": sku, "title": after.get("title"),
                            "changes": deltas})
    return {"mode": mode, "added": added, "removed": removed,
            "changed": changed, "unmatchable_old": old_unmatchable,
            "unmatchable_new": new_unmatchable}


def _headline(row: dict) -> str:
    """The one detail per row that says what it is, by mode."""
    if "points" in row:                       # a Story
        return "%s points, %s comments" % (row.get("points"), row.get("comments"))
    if "depth" in row:                        # a Comment
        return "by %s, depth %s" % (row.get("author"), row.get("depth"))
    if "karma" in row:                        # a User
        return "karma %s" % row.get("karma")
    return ""


def _print_summary(result: dict) -> None:
    print(f"[+] {result['mode']}: {len(result['added'])} added, "
          f"{len(result['removed'])} removed, {len(result['changed'])} changed.")
    for r in result["added"]:
        print(f"  + {r.get('sku')}  {r.get('title')}  {_headline(r)}")
    for r in result["removed"]:
        print(f"  - {r.get('sku')}  {r.get('title')}  {_headline(r)}")
    for c in result["changed"]:
        deltas = ", ".join(f"{f}: {v['old']!r} -> {v['new']!r}"
                           for f, v in c["changes"].items())
        print(f"  ~ {c['sku']}  {c['title']}  {deltas}")
    unmatchable = result["unmatchable_old"] + result["unmatchable_new"]
    if unmatchable:
        print(f"[!] {unmatchable} row(s) across both files had no sku or a "
              f"duplicate sku, and could not be matched across runs.")


def _run_status(path: str) -> Tuple[Optional[str], Optional[dict]]:
    """(status, meta) from the `<out>.meta.json` beside a run's JSON output,
    or (None, None) when there is no sidecar."""
    meta_path = re.sub(r"\.json$", "", path) + ".meta.json"
    try:
        with open(meta_path, "r", encoding="utf-8") as f:
            meta = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None, None
    return meta.get("status"), meta


def resolved_mode(args) -> Optional[str]:
    """The mode of the two runs: --mode, else the sidecars."""
    if getattr(args, "mode", None):
        return args.mode
    found = set()
    for path in (args.old, args.new):
        _status, meta = _run_status(path)
        if meta and meta.get("mode"):
            found.add(meta["mode"])
    return found.pop() if len(found) == 1 else None


def _check_comparable(args) -> bool:
    problems = []
    modes, queries = {}, {}
    for label, path in (("--old", args.old), ("--new", args.new)):
        status, meta = _run_status(path)
        try:
            rows_mode = mode_of(_load(path))
        except (OSError, ValueError):
            rows_mode = None
        mode = rows_mode or (meta or {}).get("mode")
        if mode:
            modes[label] = mode
            if mode not in UNIQUE_BY_SKU_MODES:
                problems.append(f"{label} ({path}) is a {mode!r} run, which "
                                f"is not one row per sku.")
        if meta and meta.get("query") is not None:
            queries[label] = meta["query"]
        if status is not None and status != "complete":
            problems.append(
                f"{label} ({path}) was a {status!r} run — stopped after "
                f"{meta.get('pages_completed')} of {meta.get('pages_requested')} "
                f"page(s), reason {meta.get('stop_reason')!r}")
    if len(set(modes.values())) > 1:
        problems.append(f"the two runs are different modes ({modes}); their "
                        f"rows have different columns.")
    if len(queries) == 2 and queries["--old"] != queries["--new"]:
        problems.append(
            f"the two runs asked different questions ({queries['--old']} vs "
            f"{queries['--new']}). A different feed, day, user or domain is a "
            f"different list, so every line would describe the query rather "
            f"than the site.")
    if not problems:
        return True
    print("[!] Refusing to diff these two runs:")
    for line in problems:
        print(f"      {line}")
    print("    Re-run the odd side, or pass --force to compare anyway.")
    return False


def parse_args():
    p = argparse.ArgumentParser(
        description="Diff two hackernews-scraper JSON outputs by sku.")
    p.add_argument("--old", required=True, help="Earlier run's JSON output.")
    p.add_argument("--new", required=True, help="Later run's JSON output.")
    p.add_argument("--out", default=None,
                   help="Write the full diff as JSON to this path too.")
    p.add_argument("--fail-on-change", action="store_true",
                   help="Exit 1 if anything was added, removed or changed — "
                        "for a cron job that should only notify on a real diff.")
    p.add_argument("--mode", choices=sorted(ROW_CLASS_BY_MODE), default=None,
                   help="The mode both files came from. Needed only when a "
                        "run's .meta.json is missing: the rows carry no mode.")
    p.add_argument("--force", action="store_true",
                   help="Diff even when the runs are partial, different modes "
                        "or different queries.")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    if not args.force and not _check_comparable(args):
        return 2
    try:
        old = _load(args.old)
        new = _load(args.new)
    except (OSError, json.JSONDecodeError) as e:
        print(f"[!] Could not read one of the input files: {e}")
        return 2
    mode = resolved_mode(args)
    if mode is None:
        print("[!] Cannot tell which mode these runs are: no .meta.json beside "
              "them names one and the rows carry none. Pass --mode.")
        return 2
    result = diff_products(old, new, mode)
    _print_summary(result)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
        print(f"[+] Full diff written to {args.out}")
    if args.fail_on_change and (result["added"] or result["removed"] or result["changed"]):
        return 1
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(1)
