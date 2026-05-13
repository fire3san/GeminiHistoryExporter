"""
Convert gemini_export/conversations.json (this tool's native schema) into
formats accepted by Gemini's "Import memory from another AI app" page
(https://gemini.google/import-memory/) and similar importers.

Supported targets:
    openai  — ChatGPT export schema (list with `mapping`, parent/children UUIDs)
    grok    — Grok export schema (`conversations[].conversation` + `responses`)
    claude  — Anthropic Claude export schema (list with `chat_messages`)

Usage:
    python convert_for_import.py --format openai
    python convert_for_import.py --format grok
    python convert_for_import.py --format claude
    python convert_for_import.py --format all
    python convert_for_import.py --in ./gemini_export/conversations.json --out-dir ./gemini_export/converted

Notes on timestamps:
    The scraper does not capture per-turn timestamps from Gemini, only
    `scraped_at`. We use the conversation's `scraped_at` (or now()) as the
    base and add 1-second offsets per turn so message order is preserved.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------
def load_native(path: Path) -> list[dict]:
    if not path.exists():
        raise SystemExit(f"Input not found: {path}")
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise SystemExit("Expected conversations.json to be a JSON array")
    return data


def parse_iso(ts: str | None) -> float:
    """Return Unix epoch seconds from an ISO-8601 string, or now()."""
    if not ts:
        return datetime.now(tz=timezone.utc).timestamp()
    try:
        # tolerate trailing Z
        if ts.endswith("Z"):
            ts = ts[:-1] + "+00:00"
        return datetime.fromisoformat(ts).timestamp()
    except Exception:
        return datetime.now(tz=timezone.utc).timestamp()


def normalize_role(role: str) -> str:
    r = (role or "").lower()
    if r in ("user", "human"):
        return "user"
    if r in ("assistant", "model", "gemini", "ai"):
        return "assistant"
    return "user"


def _normalize_for_noise(line: str) -> str:
    """Strip Markdown decoration so a heading like '## Gemini 說了' or
    '**You said:**' compares equal to its plain-text version."""
    s = line.strip().lstrip("\u200b").strip()
    # Strip ATX heading markers
    s = re.sub(r"^#{1,6}\s+", "", s)
    # Strip leading/trailing bold/italic markers
    for marker in ("**", "*", "__", "_"):
        if s.startswith(marker) and s.endswith(marker) and len(s) > 2 * len(marker):
            s = s[len(marker):-len(marker)].strip()
    # Strip trailing colons
    s = s.rstrip(":：").strip()
    return s


def _strip_ui_noise(s: str) -> str:
    """Remove standalone UI-label lines (e.g. '你說了', 'Gemini 說了', '顯示思路',
    or their Markdown-decorated variants '## Gemini 說了', '**You said:**')
    while keeping the actual content."""
    if not s:
        return s
    out_lines: list[str] = []
    for raw in s.splitlines():
        norm = _normalize_for_noise(raw)
        if norm in UI_NOISE_LINES:
            continue
        out_lines.append(raw)
    while out_lines and not out_lines[0].strip():
        out_lines.pop(0)
    while out_lines and not out_lines[-1].strip():
        out_lines.pop()
    collapsed: list[str] = []
    blanks = 0
    for ln in out_lines:
        if not ln.strip():
            blanks += 1
            if blanks > 1:
                continue
        else:
            blanks = 0
        collapsed.append(ln)
    return "\n".join(collapsed)


def _clean_turns(raw_turns: list[dict]) -> list[dict]:
    """Filter UI noise out of each turn's text and drop consecutive duplicates
    that arise when the scraper's selectors match overlapping DOM nodes."""
    cleaned: list[dict] = []
    for t in raw_turns or []:
        text = turn_text(t)
        if not text:
            continue
        role = normalize_role(t.get("role", ""))
        # Skip back-to-back identical same-role turns (overlap-match artifact).
        if cleaned and cleaned[-1]["__role"] == role and cleaned[-1]["__text"] == text:
            continue
        # Skip if previous same-role turn's text is a prefix of this one
        # (selectors sometimes match a partial vs full container).
        if cleaned and cleaned[-1]["__role"] == role:
            prev = cleaned[-1]["__text"]
            if text.startswith(prev) or prev.startswith(text):
                # keep the longer of the two
                if len(text) > len(prev):
                    cleaned.pop()
                else:
                    continue
        cleaned.append({"__role": role, "__text": text})
    return cleaned


def turn_text(turn: dict) -> str:
    """Pick the best textual representation of a turn, with Gemini UI labels
    (e.g. '你說了', 'Gemini 說了', '顯示思路') stripped out."""
    raw = (turn.get("markdown") or turn.get("text") or "").strip()
    return _strip_ui_noise(raw).strip()


# Titles like "chat_7d933f46" are placeholders from sidebar virtualization.
BAD_TITLE_RE = re.compile(r"^(chat_[0-9a-f]{6,}|Untitled|)$", re.IGNORECASE)

# Lines that are Gemini's UI labels around each turn — never useful as a title.
UI_NOISE_LINES = {
    "你說了", "你說了：", "你說了:",            # zh-TW "You said:"
    "你说了", "你说了：", "你说了:",            # zh-CN
    "You said", "You said:",
    "Gemini 說了", "Gemini 說了：", "Gemini 說了:",
    "Gemini 说了", "Gemini 说了：", "Gemini 说了:",
    "Gemini said", "Gemini said:",
    "顯示思路", "顯示思考過程", "Show thinking",
    "显示思路", "显示思考过程",
    "Sources", "來源", "来源",
}


def _first_user_snippet(turns: list[dict], max_len: int = 80) -> str:
    for t in turns or []:
        if normalize_role(t.get("role", "")) == "user":
            for raw in turn_text(t).splitlines():
                if not raw.strip():
                    continue
                if _normalize_for_noise(raw) in UI_NOISE_LINES:
                    continue
                return raw.strip()[:max_len]
    return ""


def best_title(c: dict) -> str:
    """Return a human-readable title, falling back to first user prompt if the
    stored title is a placeholder like 'chat_xxxxxxxx' or a Gemini UI label."""
    raw = (c.get("title") or "").strip()
    # Strip trailing colon variants before comparing to UI_NOISE_LINES
    raw_no_colon = raw.rstrip(":：").strip()
    is_bad = (
        not raw
        or BAD_TITLE_RE.match(raw)
        or raw in UI_NOISE_LINES
        or raw_no_colon in UI_NOISE_LINES
    )
    if not is_bad:
        return raw
    snippet = _first_user_snippet(c.get("turns") or [])
    if snippet:
        return snippet
    return raw or "Untitled"


def count_payload(payload: Any) -> int:
    if isinstance(payload, dict) and "conversations" in payload:
        return len(payload["conversations"])
    if isinstance(payload, list):
        return len(payload)
    return 0


# ---------------------------------------------------------------------------
# Format: OpenAI / ChatGPT export
# ---------------------------------------------------------------------------
def to_openai(conversations: list[dict]) -> list[dict]:
    out: list[dict] = []
    for c in conversations:
        base_ts = parse_iso(c.get("scraped_at"))
        convo_id = c.get("id") or str(uuid.uuid4())
        turns = _clean_turns(c.get("turns") or [])

        mapping: dict[str, dict] = {}

        # Hidden system root so the first user message has a parent (matches ChatGPT exports)
        root_id = str(uuid.uuid4())
        mapping[root_id] = {
            "id": root_id,
            "message": None,
            "parent": None,
            "children": [],
        }

        prev_id = root_id
        msg_create_times: list[float] = []
        for i, t in enumerate(turns):
            msg_id = str(uuid.uuid4())
            ts = base_ts + i  # synthetic 1-sec spacing
            msg_create_times.append(ts)
            role = t["__role"]
            text = t["__text"]
            mapping[msg_id] = {
                "id": msg_id,
                "message": {
                    "id": msg_id,
                    "author": {"role": role, "name": None, "metadata": {}},
                    "create_time": ts,
                    "update_time": None,
                    "content": {
                        "content_type": "text",
                        "parts": [text],
                    },
                    "status": "finished_successfully",
                    "end_turn": True if role == "assistant" else None,
                    "weight": 1.0,
                    "metadata": {
                        "source": "gemini-export",
                    },
                    "recipient": "all",
                },
                "parent": prev_id,
                "children": [],
            }
            mapping[prev_id]["children"].append(msg_id)
            prev_id = msg_id

        out.append(
            {
                "title": best_title(c),
                "create_time": msg_create_times[0] if msg_create_times else base_ts,
                "update_time": msg_create_times[-1] if msg_create_times else base_ts,
                "mapping": mapping,
                "moderation_results": [],
                "current_node": prev_id,
                "plugin_ids": None,
                "conversation_id": convo_id,
                "conversation_template_id": None,
                "id": convo_id,
            }
        )
    return out


# ---------------------------------------------------------------------------
# Format: Grok export
# ---------------------------------------------------------------------------
def mongo_date(epoch_seconds: float) -> dict:
    return {"$date": {"$numberLong": str(int(epoch_seconds * 1000))}}


def to_grok(conversations: list[dict]) -> dict:
    out_convos: list[dict] = []
    for c in conversations:
        base_ts = parse_iso(c.get("scraped_at"))
        convo_id = c.get("id") or str(uuid.uuid4())
        turns = _clean_turns(c.get("turns") or [])
        responses: list[dict] = []
        for i, t in enumerate(turns):
            ts = base_ts + i
            sender = "human" if t["__role"] == "user" else "assistant"
            responses.append(
                {
                    "message": t["__text"],
                    "sender": sender,
                    "create_time": mongo_date(ts),
                }
            )
        out_convos.append(
            {
                "conversation": {
                    "title": best_title(c),
                    "create_time": mongo_date(base_ts),
                    "update_time": mongo_date(base_ts + max(0, len(turns) - 1)),
                    "id": convo_id,
                    "source": "gemini-export",
                },
                "responses": responses,
            }
        )
    return {"conversations": out_convos}


# ---------------------------------------------------------------------------
# Format: Claude (Anthropic) export
# ---------------------------------------------------------------------------
def iso_z(epoch_seconds: float) -> str:
    return datetime.fromtimestamp(epoch_seconds, tz=timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%S.%fZ"
    )


def to_claude(conversations: list[dict]) -> list[dict]:
    out: list[dict] = []
    for c in conversations:
        base_ts = parse_iso(c.get("scraped_at"))
        convo_id = c.get("id") or str(uuid.uuid4())
        turns = _clean_turns(c.get("turns") or [])
        chat_messages = []
        for i, t in enumerate(turns):
            ts = base_ts + i
            role = t["__role"]
            sender = "human" if role == "user" else "assistant"
            text = t["__text"]
            chat_messages.append(
                {
                    "uuid": str(uuid.uuid4()),
                    "text": text,
                    "sender": sender,
                    "created_at": iso_z(ts),
                    "updated_at": iso_z(ts),
                    "attachments": [],
                    "files": [],
                    "content": [{"type": "text", "text": text}],
                }
            )
        out.append(
            {
                "uuid": convo_id,
                "name": best_title(c),
                "created_at": iso_z(base_ts),
                "updated_at": iso_z(base_ts + max(0, len(turns) - 1)),
                "account": {"uuid": "00000000-0000-0000-0000-000000000000"},
                "chat_messages": chat_messages,
            }
        )
    return out


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------
WRITERS: dict[str, tuple[str, Any]] = {
    "openai": ("conversations.json", to_openai),     # ChatGPT-style filename
    "grok":   ("conversations.json", to_grok),       # Grok uses same filename
    "claude": ("conversations.json", to_claude),     # Claude uses same filename
}


def write_one(fmt: str, conversations: list[dict], out_dir: Path) -> Path:
    fname, fn = WRITERS[fmt]
    target_dir = out_dir / fmt
    target_dir.mkdir(parents=True, exist_ok=True)
    payload = fn(conversations)
    out_path = target_dir / fname
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                        encoding="utf-8")
    size_mb = out_path.stat().st_size / 1_048_576
    n = count_payload(payload)
    expected = len(conversations)
    status = "OK" if n == expected else f"MISMATCH (expected {expected})"
    print(f"[{fmt:6s}] wrote {out_path}  ({n} conversations, {size_mb:.2f} MB)  {status}")
    if n != expected:
        raise SystemExit(
            f"  conversion produced {n} conversations but input had {expected}"
        )

    # Verify by re-reading the file from disk
    reread = json.loads(out_path.read_text(encoding="utf-8"))
    rn = count_payload(reread)
    if rn != expected:
        raise SystemExit(
            f"  re-read of {out_path} reports {rn} conversations; expected {expected}"
        )
    return out_path


def main() -> None:
    ap = argparse.ArgumentParser(description="Convert Gemini export to other AI-app formats.")
    ap.add_argument("--in", dest="in_path", default="./gemini_export/conversations.json")
    ap.add_argument("--out-dir", default="./gemini_export/converted")
    ap.add_argument(
        "--format",
        choices=["openai", "grok", "claude", "all"],
        default="all",
        help="Target schema (default: all)",
    )
    args = ap.parse_args()

    conversations = load_native(Path(args.in_path))
    print(f"Loaded {len(conversations)} conversations from {args.in_path}")

    # Title diagnostics
    def _is_bad(c: dict) -> bool:
        r = (c.get("title") or "").strip()
        return (not r) or bool(BAD_TITLE_RE.match(r)) \
            or r in UI_NOISE_LINES or r.rstrip(":：").strip() in UI_NOISE_LINES

    bad = [c for c in conversations if _is_bad(c)]
    rescuable = [c for c in bad if _first_user_snippet(c.get("turns") or [])]
    if bad:
        print(
            f"  Titles needing rescue: {len(bad)}  "
            f"(of which {len(rescuable)} have a usable first-user-prompt; "
            f"{len(bad) - len(rescuable)} will remain 'Untitled')"
        )

    out_dir = Path(args.out_dir)
    formats = ["openai", "grok", "claude"] if args.format == "all" else [args.format]
    for fmt in formats:
        write_one(fmt, conversations, out_dir)

    print(
        "\nUpload tip: Gemini's import-memory page accepts the file produced for the\n"
        "AI app you choose during import. If 'openai' fails, try 'claude' or 'grok'.\n"
        "Each format is in its own subfolder so you can upload independently."
    )


if __name__ == "__main__":
    main()
