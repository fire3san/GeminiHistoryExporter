"""
Smoke test for convert_for_import.py (Gemini scraper output → other AI apps).

Runs the converter end-to-end against gemini_export/conversations.json, then
validates that each output file:
  - parses as JSON
  - has the expected top-level shape for that target schema
  - reports the same number of conversations as the input
  - reports the same number of messages as the cleaned input
    (after UI-noise stripping + consecutive-duplicate collapse — the same
    transform the converter applies)
  - has no blank titles (placeholders should have been rescued)
  - does NOT contain any of the Gemini UI-noise labels we strip

Run:
    python test_gemini_conversion.py
    python test_gemini_conversion.py --in <conversations.json> --out-dir <dir>
    python test_gemini_conversion.py --skip-run   # validate existing files only
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

from schemas import SCHEMA_VALIDATORS

from convert_for_import import (
    UI_NOISE_LINES,
    _clean_turns,
    _normalize_for_noise,
    load_native,
)


DEFAULT_IN = "./gemini_export/conversations.json"
DEFAULT_OUT = "./gemini_export/converted"


# ------------------------------------------------------------------ helpers
class Check:
    def __init__(self) -> None:
        self.passed = 0
        self.failed: list[str] = []

    def ok(self, label: str, cond: bool, detail: str = "") -> None:
        if cond:
            self.passed += 1
            print(f"  PASS  {label}")
        else:
            self.failed.append(f"{label} - {detail}" if detail else label)
            print(f"  FAIL  {label}  {detail}")

    def summary(self) -> int:
        print()
        print(f"Passed: {self.passed}")
        print(f"Failed: {len(self.failed)}")
        for f in self.failed:
            print(f"  - {f}")
        return 0 if not self.failed else 1


def _count_input(input_path: Path) -> tuple[int, int]:
    """Count conversations and post-clean messages, matching what the
    converter actually writes."""
    convos = load_native(input_path)
    msgs = sum(len(_clean_turns(c.get("turns") or [])) for c in convos)
    return len(convos), msgs


def _scan_noise(text: str) -> list[str]:
    found: list[str] = []
    for raw in (text or "").splitlines():
        if _normalize_for_noise(raw) in UI_NOISE_LINES:
            found.append(raw.strip())
    return found


# ------------------------------------------------------------------ validators
def validate_openai(path: Path, expected_convos: int, expected_msgs: int, c: Check) -> None:
    data = json.loads(path.read_text(encoding="utf-8"))
    c.ok("openai: top-level is list", isinstance(data, list))
    c.ok(
        "openai: conversation count matches",
        len(data) == expected_convos,
        f"got {len(data)}, expected {expected_convos}",
    )

    total_msgs = 0
    titles_blank = 0
    noisy_lines = 0
    for convo in data:
        if not convo.get("title"):
            titles_blank += 1
        for node in (convo.get("mapping") or {}).values():
            msg = node.get("message")
            if not msg:
                continue
            total_msgs += 1
            for part in msg.get("content", {}).get("parts", []) or []:
                noisy_lines += len(_scan_noise(part))

    c.ok(
        "openai: total message count matches",
        total_msgs == expected_msgs,
        f"got {total_msgs}, expected {expected_msgs}",
    )
    c.ok("openai: no blank titles", titles_blank == 0,
         f"{titles_blank} blank titles")
    c.ok("openai: no UI-noise lines in bodies", noisy_lines == 0,
         f"{noisy_lines} noisy lines")


def validate_claude(path: Path, expected_convos: int, expected_msgs: int, c: Check) -> None:
    data = json.loads(path.read_text(encoding="utf-8"))
    c.ok("claude: top-level is list", isinstance(data, list))
    c.ok(
        "claude: conversation count matches",
        len(data) == expected_convos,
        f"got {len(data)}, expected {expected_convos}",
    )
    total_msgs = 0
    titles_blank = 0
    noisy_lines = 0
    bad_sender = 0
    for convo in data:
        if not convo.get("name"):
            titles_blank += 1
        for m in convo.get("chat_messages") or []:
            total_msgs += 1
            if m.get("sender") not in ("human", "assistant"):
                bad_sender += 1
            noisy_lines += len(_scan_noise(m.get("text") or ""))
    c.ok(
        "claude: total message count matches",
        total_msgs == expected_msgs,
        f"got {total_msgs}, expected {expected_msgs}",
    )
    c.ok("claude: no blank titles", titles_blank == 0,
         f"{titles_blank} blank titles")
    c.ok("claude: no UI-noise lines in bodies", noisy_lines == 0,
         f"{noisy_lines} noisy lines")
    c.ok("claude: senders normalized", bad_sender == 0,
         f"{bad_sender} bad senders")


def validate_grok(path: Path, expected_convos: int, expected_msgs: int, c: Check) -> None:
    data = json.loads(path.read_text(encoding="utf-8"))
    c.ok("grok: top-level dict with 'conversations'",
         isinstance(data, dict) and "conversations" in data)
    convs = data.get("conversations") or []
    c.ok(
        "grok: conversation count matches",
        len(convs) == expected_convos,
        f"got {len(convs)}, expected {expected_convos}",
    )
    total_msgs = 0
    titles_blank = 0
    noisy_lines = 0
    bad_sender = 0
    for entry in convs:
        convo = entry.get("conversation") or {}
        if not convo.get("title"):
            titles_blank += 1
        for r in entry.get("responses") or []:
            total_msgs += 1
            if r.get("sender") not in ("human", "assistant"):
                bad_sender += 1
            noisy_lines += len(_scan_noise(r.get("message") or ""))
    c.ok(
        "grok: total message count matches",
        total_msgs == expected_msgs,
        f"got {total_msgs}, expected {expected_msgs}",
    )
    c.ok("grok: no blank titles", titles_blank == 0,
         f"{titles_blank} blank titles")
    c.ok("grok: no UI-noise lines in bodies", noisy_lines == 0,
         f"{noisy_lines} noisy lines")
    c.ok("grok: senders normalized", bad_sender == 0,
         f"{bad_sender} bad senders")


VALIDATORS = {
    "openai": validate_openai,
    "claude": validate_claude,
    "grok":   validate_grok,
}


# ------------------------------------------------------------------ main
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="in_path", default=DEFAULT_IN)
    ap.add_argument("--out-dir", default=DEFAULT_OUT)
    ap.add_argument("--skip-run", action="store_true",
                    help="Don't re-run the converter, just validate existing files")
    args = ap.parse_args()

    in_path = Path(args.in_path)
    out_dir = Path(args.out_dir)

    if not in_path.exists():
        print(f"ERROR: input not found: {in_path}", file=sys.stderr)
        return 2

    print(f"Input:  {in_path}")
    print(f"Output: {out_dir}\n")

    if not args.skip_run:
        print("== Running converter ==")
        rc = subprocess.call(
            [sys.executable, "convert_for_import.py",
             "--in", str(in_path), "--out-dir", str(out_dir),
             "--format", "all"],
        )
        if rc != 0:
            print(f"converter exited with code {rc}", file=sys.stderr)
            return rc
        print()

    print("== Counting input (after clean) ==")
    expected_convos, expected_msgs = _count_input(in_path)
    print(f"  conversations: {expected_convos}")
    print(f"  messages:      {expected_msgs}\n")

    check = Check()
    for fmt, validator in VALIDATORS.items():
        path = out_dir / fmt / "conversations.json"
        print(f"== Validating {fmt} ({path}) ==")
        if not path.exists():
            check.ok(f"{fmt}: file exists", False, str(path))
            continue
        check.ok(f"{fmt}: file exists", True)
        try:
            validator(path, expected_convos, expected_msgs, check)
        except Exception as e:
            check.ok(f"{fmt}: validator crashed", False, repr(e))
        schema_fn = SCHEMA_VALIDATORS.get(fmt)
        if schema_fn is not None:
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                schema_errs = schema_fn(data)
                check.ok(
                    f"{fmt}: schema-valid",
                    not schema_errs,
                    f"{len(schema_errs)} errors; first: {schema_errs[0]}" if schema_errs else "",
                )
            except Exception as e:
                check.ok(f"{fmt}: schema check crashed", False, repr(e))
        print()

    return check.summary()


if __name__ == "__main__":
    sys.exit(main())
