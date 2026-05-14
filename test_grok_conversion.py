"""
Smoke test for convert_grok_for_import.py.

Runs the converter end-to-end against the Grok export, then validates that
each output file:
  - parses as JSON
  - has the expected top-level shape for that target schema
  - reports the same number of conversations as the input
  - reports the same number of (human + assistant) messages as the input
  - has rescued every "bad" / empty title to either a real title or a snippet
  - does NOT contain any of the Gemini UI-noise labels we strip

Run:
    python test_grok_conversion.py
    python test_grok_conversion.py --in <grok-export.json> --out-dir <dir>
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

# Reuse the same noise list used by the converter.
from convert_for_import import UI_NOISE_LINES, _normalize_for_noise
from convert_grok_for_import import DEFAULT_IN, DEFAULT_OUT, load_grok


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
    convos = load_grok(input_path)
    msgs = sum(len(c.get("turns") or []) for c in convos)
    return len(convos), msgs


def _scan_noise(text: str) -> list[str]:
    """Return any UI-noise labels found as standalone lines in `text`."""
    found: list[str] = []
    for raw in text.splitlines():
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
        c.ok(f"openai[{convo.get('id','?')[:8]}]: has mapping",
             isinstance(convo.get("mapping"), dict))
        if not convo.get("title"):
            titles_blank += 1
        for node in convo["mapping"].values():
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
    for convo in data:
        if not convo.get("name"):
            titles_blank += 1
        for m in convo.get("chat_messages") or []:
            total_msgs += 1
            noisy_lines += len(_scan_noise(m.get("text") or ""))
            c.ok_quiet = True  # keep noise down
            if m.get("sender") not in ("human", "assistant"):
                c.failed.append(f"claude: bad sender '{m.get('sender')}'")
    c.ok(
        "claude: total message count matches",
        total_msgs == expected_msgs,
        f"got {total_msgs}, expected {expected_msgs}",
    )
    c.ok("claude: no blank titles", titles_blank == 0,
         f"{titles_blank} blank titles")
    c.ok("claude: no UI-noise lines in bodies", noisy_lines == 0,
         f"{noisy_lines} noisy lines")


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
            rd = r.get("response") or {}
            total_msgs += 1
            if rd.get("sender") not in ("human", "assistant"):
                bad_sender += 1
            noisy_lines += len(_scan_noise(rd.get("message") or ""))
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


def validate_gemini(path: Path, expected_convos: int, expected_msgs: int, c: Check) -> None:
    # 'gemini' target reuses ChatGPT shape.
    validate_openai(path, expected_convos, expected_msgs, c)


VALIDATORS = {
    "openai": validate_openai,
    "claude": validate_claude,
    "grok":   validate_grok,
    "gemini": validate_gemini,
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
            [sys.executable, "convert_grok_for_import.py",
             "--in", str(in_path), "--out-dir", str(out_dir),
             "--format", "all"],
        )
        if rc != 0:
            print(f"converter exited with code {rc}", file=sys.stderr)
            return rc
        print()

    print("== Counting input ==")
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
        print()

    return check.summary()


if __name__ == "__main__":
    sys.exit(main())
