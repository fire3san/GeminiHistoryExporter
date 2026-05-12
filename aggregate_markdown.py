"""
Aggregate every per-conversation Markdown file in gemini_export/markdown/
into a single chronological history file.

Order: uses gemini_export/conversations.json (preserves Gemini's sidebar order
= newest first). Falls back to alphabetical if the JSON is missing.

Usage:
    python aggregate_markdown.py
    python aggregate_markdown.py --in ./gemini_export --out ./gemini_export/HISTORY.md
    python aggregate_markdown.py --reverse        # oldest first
    python aggregate_markdown.py --split-by-month # one file per YYYY-MM in HISTORY/
"""

from __future__ import annotations

import argparse
import json
import re
from collections import defaultdict
from pathlib import Path

SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._ -]+")


def safe_filename(name: str, max_len: int = 80) -> str:
    cleaned = SAFE_NAME_RE.sub("_", name).strip().strip(".")
    cleaned = re.sub(r"\s+", " ", cleaned)
    return (cleaned[:max_len] or "untitled").rstrip()


def load_order(in_dir: Path) -> list[dict]:
    """Return [{title, id, scraped_at, file}] in the order they should appear."""
    md_dir = in_dir / "markdown"
    if not md_dir.is_dir():
        raise SystemExit(f"No markdown folder at {md_dir}")

    json_path = in_dir / "conversations.json"
    items: list[dict] = []
    used: set[Path] = set()

    if json_path.exists():
        data = json.loads(json_path.read_text(encoding="utf-8"))
        used_names: set[str] = set()
        for c in data:
            title = c.get("title") or c.get("id", "untitled")
            base = safe_filename(title) or c.get("id", "untitled")
            name = base
            i = 2
            while name.lower() in used_names:
                name = f"{base} ({i})"
                i += 1
            used_names.add(name.lower())
            f = md_dir / f"{name}.md"
            if f.exists():
                items.append(
                    {
                        "title": title,
                        "id": c.get("id", ""),
                        "scraped_at": c.get("scraped_at", ""),
                        "url": c.get("url", ""),
                        "file": f,
                    }
                )
                used.add(f)

    # Append any orphan markdown files (e.g. from older runs) alphabetically
    for f in sorted(md_dir.glob("*.md")):
        if f in used:
            continue
        items.append(
            {
                "title": f.stem,
                "id": "",
                "scraped_at": "",
                "url": "",
                "file": f,
            }
        )
    return items


def shift_headings(md: str, levels: int = 1) -> str:
    """Demote every ATX heading by `levels` (max ######)."""
    def repl(m: re.Match) -> str:
        hashes = m.group(1)
        new_level = min(len(hashes) + levels, 6)
        return "#" * new_level + m.group(2)

    return re.sub(r"(?m)^(#{1,6})(\s)", repl, md)


def render_block(item: dict, demote: bool = True) -> str:
    body = item["file"].read_text(encoding="utf-8")
    if demote:
        body = shift_headings(body, levels=1)
    anchor = safe_filename(item["title"]).replace(" ", "-").lower()
    header = f"<a id=\"{anchor}\"></a>\n\n"
    sep = "\n\n---\n\n"
    return header + body.rstrip() + sep


def write_single(items: list[dict], out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    parts: list[str] = [
        "# Gemini Conversation History",
        "",
        f"Aggregated {len(items)} conversations.",
        "",
        "## Table of Contents",
        "",
    ]
    for i, item in enumerate(items, 1):
        anchor = safe_filename(item["title"]).replace(" ", "-").lower()
        ts = item.get("scraped_at", "")
        suffix = f" — _{ts}_" if ts else ""
        parts.append(f"{i}. [{item['title']}](#{anchor}){suffix}")
    parts.append("")
    parts.append("---")
    parts.append("")
    for item in items:
        parts.append(render_block(item))
    out_path.write_text("\n".join(parts), encoding="utf-8")
    size_mb = out_path.stat().st_size / 1_048_576
    print(f"Wrote {out_path}  ({len(items)} conversations, {size_mb:.2f} MB)")


def write_split_by_month(items: list[dict], out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    buckets: dict[str, list[dict]] = defaultdict(list)
    for item in items:
        ts = item.get("scraped_at") or ""
        month = ts[:7] if len(ts) >= 7 else "unknown"
        buckets[month].append(item)
    for month, group in sorted(buckets.items()):
        write_single(group, out_dir / f"HISTORY-{month}.md")


def main() -> None:
    ap = argparse.ArgumentParser(description="Aggregate Gemini markdown export.")
    ap.add_argument("--in", dest="in_dir", default="./gemini_export")
    ap.add_argument("--out", dest="out_path", default=None,
                    help="Output file (default: <in>/HISTORY.md)")
    ap.add_argument("--reverse", action="store_true", help="Oldest first")
    ap.add_argument("--split-by-month", action="store_true",
                    help="Write one HISTORY-YYYY-MM.md per month into <in>/HISTORY/")
    args = ap.parse_args()

    in_dir = Path(args.in_dir)
    items = load_order(in_dir)
    if not items:
        raise SystemExit("No markdown files found to aggregate.")
    if args.reverse:
        items = list(reversed(items))

    if args.split_by_month:
        write_split_by_month(items, in_dir / "HISTORY")
    else:
        out_path = Path(args.out_path) if args.out_path else in_dir / "HISTORY.md"
        write_single(items, out_path)


if __name__ == "__main__":
    main()
