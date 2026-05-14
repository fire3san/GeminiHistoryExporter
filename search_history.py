"""
Full-text search over your exported chat history.

Reads either the aggregated HISTORY.md (or a path you point at) or all
files in `markdown/`, splits them into per-conversation sections by
top-level `# ` / `## ` headings, and prints matches with snippet context.

Usage:
    python search_history.py "asyncio cancel"
    python search_history.py "regex pattern" --regex
    python search_history.py "foo" --path gemini_export/HISTORY.md
    python search_history.py "foo" --dir gemini_export/markdown
    python search_history.py "foo" --context 3 --max 20
    python search_history.py "foo" --titles-only
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import Iterable


HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$")


def _iter_files(path: Path | None, dir_: Path | None) -> Iterable[Path]:
    if path:
        if path.is_file():
            yield path
        return
    if dir_:
        if dir_.is_dir():
            for p in sorted(dir_.rglob("*.md")):
                yield p
        return
    # default search locations
    for cand in (
        Path("HISTORY.md"),
        Path("gemini_export/HISTORY.md"),
    ):
        if cand.exists():
            yield cand
            return
    md = Path("gemini_export/markdown")
    if md.is_dir():
        for p in sorted(md.rglob("*.md")):
            yield p


def _split_sections(lines: list[str]) -> list[tuple[str, int, list[str]]]:
    """Return list of (title, start_line_1based, body_lines).

    A section starts at every `#` / `##` heading. If a file has no
    heading, the whole file is one section titled by the filename.
    """
    sections: list[tuple[str, int, list[str]]] = []
    cur_title = ""
    cur_start = 1
    cur_body: list[str] = []
    for i, line in enumerate(lines, start=1):
        m = HEADING_RE.match(line)
        if m and len(m.group(1)) <= 2:
            if cur_body or cur_title:
                sections.append((cur_title, cur_start, cur_body))
            cur_title = m.group(2)
            cur_start = i
            cur_body = []
        else:
            cur_body.append(line)
    sections.append((cur_title, cur_start, cur_body))
    return sections


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("query", help="Search term (literal substring, case-insensitive by default)")
    ap.add_argument("--regex", action="store_true", help="Treat QUERY as a Python regex")
    ap.add_argument("--case", action="store_true", help="Case-sensitive")
    ap.add_argument("--path", type=Path, help="Search a single file")
    ap.add_argument("--dir", dest="dir_", type=Path, help="Search every .md under this directory")
    ap.add_argument("--context", type=int, default=2, help="Lines of context around each hit (default 2)")
    ap.add_argument("--max", type=int, default=50, help="Max hits to show (default 50)")
    ap.add_argument("--titles-only", action="store_true", help="Only print conversation titles that contain a hit")
    args = ap.parse_args()

    flags = 0 if args.case else re.IGNORECASE
    if args.regex:
        pat = re.compile(args.query, flags)
    else:
        pat = re.compile(re.escape(args.query), flags)

    files = list(_iter_files(args.path, args.dir_))
    if not files:
        print("ERROR: no markdown found (try --path or --dir)", file=sys.stderr)
        return 2

    shown = 0
    files_with_hits = 0
    for f in files:
        try:
            text = f.read_text(encoding="utf-8")
        except Exception as e:
            print(f"skip {f}: {e}", file=sys.stderr)
            continue
        lines = text.splitlines()
        sections = _split_sections(lines)
        file_hit = False
        for title, start, body in sections:
            hit_lines: list[int] = []
            for idx, line in enumerate(body):
                if pat.search(line):
                    hit_lines.append(idx)
            if not hit_lines:
                continue
            file_hit = True
            if args.titles_only:
                print(f"{f}:{start}  {title}  ({len(hit_lines)} hits)")
                shown += 1
                if shown >= args.max:
                    return 0
                continue
            print(f"\n=== {f}  L{start}  {title} ===")
            for hl in hit_lines:
                lo = max(0, hl - args.context)
                hi = min(len(body), hl + args.context + 1)
                for k in range(lo, hi):
                    marker = ">>" if k == hl else "  "
                    print(f"  {marker} L{start + 1 + k}: {body[k]}")
                print()
                shown += 1
                if shown >= args.max:
                    print(f"(reached --max {args.max}; stopping)")
                    return 0
        if file_hit:
            files_with_hits += 1

    print(f"\nDone. {shown} hits in {files_with_hits} file(s).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
