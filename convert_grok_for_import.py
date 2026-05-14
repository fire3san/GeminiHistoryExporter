"""
Convert a Grok export (prod-grok-backend.json) into formats accepted by
other AI apps' import flows (Gemini's "Import memory from another AI app",
ChatGPT, Claude, Grok-4).

Input schema (Grok backend export):
    {
      "conversations": [
        {
          "conversation": { "id", "title", "create_time" (ISO Z), "modify_time", ... },
          "responses": [
            {
              "response": {
                "_id", "message", "sender" ("human" | "assistant" | "ASSISTANT"),
                "create_time": { "$date": { "$numberLong": "<ms epoch>" } },
                "model", ...
              }
            }, ...
          ]
        }, ...
      ]
    }

Outputs (default in ./grok_export/converted/<target>/conversations.json):
    openai  - ChatGPT export schema (mapping graph, parent/children UUIDs)
    claude  - Anthropic export schema (chat_messages list)
    grok    - Re-emitted clean Grok schema (same shape as input, deduped/cleaned)
    gemini  - Same as 'openai' (Gemini's import-memory page accepts ChatGPT shape)

Usage:
    python convert_grok_for_import.py
    python convert_grok_for_import.py --format openai
    python convert_grok_for_import.py --in /path/to/prod-grok-backend.json
"""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Reuse the heavy lifting from the Gemini converter
from convert_for_import import (
    BAD_TITLE_RE,
    UI_NOISE_LINES,
    _clean_turns,
    _first_user_snippet,
    best_title,
    count_payload,
    to_claude,
    to_openai,
)


DEFAULT_IN = "./grok_export/prod-grok-backend.json"
DEFAULT_OUT = "./grok_export/converted"


# ---------------------------------------------------------------------------
# Grok -> internal "native-ish" schema
# ---------------------------------------------------------------------------
def _grok_ts_to_epoch(create_time: Any) -> float:
    """Grok responses use {'$date': {'$numberLong': '<ms epoch>'}}.
    Conversations use plain ISO-Z strings. Handle both, fall back to now()."""
    if isinstance(create_time, dict):
        try:
            ms = int(create_time["$date"]["$numberLong"])
            return ms / 1000.0
        except Exception:
            pass
    if isinstance(create_time, str) and create_time:
        try:
            s = create_time
            if s.endswith("Z"):
                s = s[:-1] + "+00:00"
            return datetime.fromisoformat(s).timestamp()
        except Exception:
            pass
    return datetime.now(tz=timezone.utc).timestamp()


def _iso_z(epoch_seconds: float) -> str:
    return datetime.fromtimestamp(epoch_seconds, tz=timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def load_grok(path: Path) -> list[dict]:
    """Load Grok export and convert into the internal `conversations.json`
    shape that the other writers (to_openai / to_claude / etc.) expect:

        [{ "id", "title", "scraped_at", "turns": [{"role", "text", "markdown"}] }]
    """
    if not path.exists():
        raise SystemExit(f"Input not found: {path}")
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or "conversations" not in raw:
        raise SystemExit(
            "Expected a Grok export with a top-level 'conversations' array."
        )

    out: list[dict] = []
    skipped = 0
    for entry in raw["conversations"]:
        convo = entry.get("conversation") or {}
        responses = entry.get("responses") or []

        cid = convo.get("id") or str(uuid.uuid4())
        title = (convo.get("title") or "").strip()
        base_ts = _grok_ts_to_epoch(convo.get("create_time"))

        turns: list[dict] = []
        for r in responses:
            rd = r.get("response") if isinstance(r, dict) else None
            if not rd:
                continue
            sender = (rd.get("sender") or "").lower()
            text = (rd.get("message") or "").strip()
            if not text:
                continue
            role = "user" if sender in ("human", "user") else "assistant"
            turns.append(
                {
                    "role": role,
                    "text": text,
                    "markdown": text,  # Grok messages are already Markdown
                    "html": "",
                }
            )

        if not turns:
            skipped += 1
            continue

        out.append(
            {
                "id": cid,
                "title": title,
                "url": f"https://grok.com/chat/{cid}",
                "scraped_at": _iso_z(base_ts),
                "turns": turns,
            }
        )

    if skipped:
        print(f"  skipped {skipped} empty Grok conversations")
    return out


# ---------------------------------------------------------------------------
# Re-emit clean Grok schema
# ---------------------------------------------------------------------------
def _mongo_date_ms(epoch_seconds: float) -> dict:
    return {"$date": {"$numberLong": str(int(epoch_seconds * 1000))}}


def to_grok_native(conversations: list[dict]) -> dict:
    """Re-emit in Grok's own backend export shape, after title rescue +
    UI-noise stripping + duplicate-turn collapse. Suitable for Grok-4 import
    flows and as a clean canonical Grok backup.
    """
    out_convos: list[dict] = []
    for c in conversations:
        cid = c.get("id") or str(uuid.uuid4())
        # Parse the scraped_at we stored as ISO-Z
        try:
            base_ts = datetime.fromisoformat(
                (c.get("scraped_at") or "").replace("Z", "+00:00")
            ).timestamp()
        except Exception:
            base_ts = datetime.now(tz=timezone.utc).timestamp()

        turns = _clean_turns(c.get("turns") or [])
        responses: list[dict] = []
        last_ts = base_ts
        for i, t in enumerate(turns):
            ts = base_ts + i  # synthetic 1-sec spacing preserves order
            last_ts = ts
            sender = "human" if t["__role"] == "user" else "assistant"
            responses.append(
                {
                    "response": {
                        "_id": str(uuid.uuid4()),
                        "conversation_id": cid,
                        "message": t["__text"],
                        "sender": sender,
                        "create_time": _mongo_date_ms(ts),
                        "metadata": {},
                        "model": "grok-4",
                    },
                    "share_link": None,
                }
            )

        out_convos.append(
            {
                "conversation": {
                    "id": cid,
                    "user_id": "",
                    "anon_user_id": None,
                    "create_time": datetime.fromtimestamp(
                        base_ts, tz=timezone.utc
                    ).isoformat().replace("+00:00", "Z"),
                    "modify_time": datetime.fromtimestamp(
                        last_ts, tz=timezone.utc
                    ).isoformat().replace("+00:00", "Z"),
                    "title": best_title(c),
                    "summary": "",
                    "starred": False,
                    "system_prompt_id": None,
                    "asset_ids": [],
                    "media_types": [],
                    "source": "grok-export-cleaned",
                },
                "responses": responses,
            }
        )
    return {"conversations": out_convos}


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------
WRITERS: dict[str, Any] = {
    "openai": to_openai,
    "claude": to_claude,
    "grok":   to_grok_native,
    "gemini": to_openai,   # Gemini's import-memory page accepts ChatGPT shape
}


def write_one(fmt: str, conversations: list[dict], out_dir: Path) -> Path:
    fn = WRITERS[fmt]
    target_dir = out_dir / fmt
    target_dir.mkdir(parents=True, exist_ok=True)
    payload = fn(conversations)
    out_path = target_dir / "conversations.json"
    out_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    size_mb = out_path.stat().st_size / 1_048_576
    n = count_payload(payload)
    expected = len(conversations)
    status = "OK" if n == expected else f"MISMATCH (expected {expected})"
    print(f"[{fmt:6s}] wrote {out_path}  ({n} conversations, {size_mb:.2f} MB)  {status}")
    if n != expected:
        raise SystemExit(
            f"  conversion produced {n} conversations but input had {expected}"
        )
    # Re-read to sanity check the file on disk
    reread = json.loads(out_path.read_text(encoding="utf-8"))
    if count_payload(reread) != expected:
        raise SystemExit(f"  re-read mismatch for {out_path}")
    return out_path


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Convert a Grok export into other AI-app import formats."
    )
    ap.add_argument("--in", dest="in_path", default=DEFAULT_IN)
    ap.add_argument("--out-dir", default=DEFAULT_OUT)
    ap.add_argument(
        "--format",
        choices=["openai", "claude", "grok", "gemini", "all"],
        default="all",
        help="Target schema (default: all)",
    )
    args = ap.parse_args()

    print(f"Loading Grok export from {args.in_path} ...")
    conversations = load_grok(Path(args.in_path))
    print(f"Loaded {len(conversations)} non-empty conversations")

    # Title diagnostics (same logic as Gemini converter)
    def _is_bad(c: dict) -> bool:
        r = (c.get("title") or "").strip()
        return (
            (not r)
            or bool(BAD_TITLE_RE.match(r))
            or r in UI_NOISE_LINES
            or r.rstrip(":：").strip() in UI_NOISE_LINES
        )

    bad = [c for c in conversations if _is_bad(c)]
    if bad:
        rescuable = [c for c in bad if _first_user_snippet(c.get("turns") or [])]
        print(
            f"  Titles needing rescue: {len(bad)}  "
            f"(rescuable from first user prompt: {len(rescuable)}; "
            f"will stay 'Untitled': {len(bad) - len(rescuable)})"
        )

    out_dir = Path(args.out_dir)
    formats = (
        ["openai", "claude", "grok", "gemini"]
        if args.format == "all"
        else [args.format]
    )
    for fmt in formats:
        write_one(fmt, conversations, out_dir)

    print(
        "\nUpload tips:\n"
        "  - Gemini import (https://gemini.google/import-memory/): use the 'gemini' or\n"
        "    'openai' file (ChatGPT-shape) and pick 'ChatGPT' on the upload page.\n"
        "  - ChatGPT data import: use the 'openai' file.\n"
        "  - Claude: use the 'claude' file.\n"
        "  - Grok-4: use the 'grok' file (cleaned re-emit of the original schema).\n"
        "If one format is rejected (size or schema), try another — each is in its\n"
        "own subfolder so you can upload independently."
    )


if __name__ == "__main__":
    main()
