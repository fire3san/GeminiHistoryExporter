"""
Gemini Conversation Exporter
============================

Connects to an existing Chrome session (launched with --remote-debugging-port=9222)
that is already logged into gemini.google.com, then walks every conversation in
the "Recent" sidebar and dumps:

    gemini_export/
      conversations.json        # structured: title, url, id, timestamp, turns[]
      markdown/<safe-title>.md  # human-readable
      html/<safe-title>.html    # raw HTML of each turn (for fidelity)

Run *after* you have launched Chrome per the README instructions.

Usage:
    python scrape_gemini.py
    python scrape_gemini.py --limit 5             # only first 5 chats (smoke test)
    python scrape_gemini.py --cdp http://localhost:9222
    python scrape_gemini.py --out ./gemini_export
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Iterable

from bs4 import BeautifulSoup
from markdownify import markdownify as md
from playwright.sync_api import (
    Page,
    Playwright,
    TimeoutError as PlaywrightTimeoutError,
    sync_playwright,
)

# ---------------------------------------------------------------------------
# Selectors — Gemini's DOM changes over time. If something breaks, tweak here.
# Each entry is tried in order until one matches.
# ---------------------------------------------------------------------------
SEL_SIDEBAR_TOGGLE = [
    'button[aria-label="Main menu"]',
    'button[data-test-id="side-nav-menu-button"]',
]
SEL_SHOW_MORE = [
    'button[data-test-id="show-more-button"]',
    'button:has-text("Show more")',
]
SEL_CONVO_ITEMS = [
    'conversations-list a[data-test-id="conversation"]',
    'a[data-test-id="conversation"]',
    '[data-test-id="conversation"]',
]
SEL_CONVO_TITLE = [
    '.conversation-title',
    'div[data-test-id="conversation-title"]',
]
SEL_USER_TURN = [
    'user-query',
    'div.user-query-container',
    '[data-test-id="user-query"]',
]
SEL_MODEL_TURN = [
    'model-response',
    'message-content.model-response-text',
    '[data-test-id="model-response"]',
]
SEL_TURN_CONTAINER = [
    'div.conversation-container',
    'infinite-scroller .conversation-container',
]

DEFAULT_CDP = "http://127.0.0.1:9222"  # IPv4 — Chrome doesn't bind ::1 on Windows
GEMINI_URL = "https://gemini.google.com/app"


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------
@dataclass
class Turn:
    role: str            # "user" or "assistant"
    text: str            # plain-ish text
    markdown: str        # markdown-converted
    html: str            # raw inner HTML


@dataclass
class Conversation:
    id: str
    title: str
    url: str
    scraped_at: str
    turns: list[Turn] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._ -]+")


def safe_filename(name: str, max_len: int = 80) -> str:
    cleaned = SAFE_NAME_RE.sub("_", name).strip().strip(".")
    cleaned = re.sub(r"\s+", " ", cleaned)
    return (cleaned[:max_len] or "untitled").rstrip()


def first_matching(page_or_locator, selectors: Iterable[str]):
    """Return the first selector that has at least one match, else None."""
    for sel in selectors:
        try:
            loc = page_or_locator.locator(sel)
            if loc.count() > 0:
                return loc
        except Exception:
            continue
    return None


def try_click(page: Page, selectors: Iterable[str], timeout: int = 1500) -> bool:
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            if loc.count() > 0 and loc.is_visible():
                loc.click(timeout=timeout)
                return True
        except Exception:
            continue
    return False


# ---------------------------------------------------------------------------
# Sidebar handling
# ---------------------------------------------------------------------------
def ensure_sidebar_open(page: Page) -> None:
    """Best-effort: open the menu so the conversation list is in the DOM."""
    if first_matching(page, SEL_CONVO_ITEMS) is not None:
        return
    try_click(page, SEL_SIDEBAR_TOGGLE)
    page.wait_for_timeout(800)


def expand_all_recents(page: Page, max_rounds: int = 2000) -> list[dict]:
    """Scroll the virtualized sidebar to the bottom, accumulating every
    conversation we see along the way. Returns deduped [{id, title}] list.

    Strategy:
      1. Auto-detect the actual overflowing scroller (an ancestor of the
         conversation list whose scrollHeight > clientHeight).
      2. Scroll it **incrementally** (+80% of viewport) — Angular CDK virtual
         scroll only fires "load more" on real scroll events, not jumps.
      3. Also dispatch wheel events + scroll the last item into view to nudge
         the lazy-loader.
      4. Snapshot every render; merge unseen ids into a dict.
      5. Stop when many consecutive rounds yield no new ids AND no scroll
         progress is possible.
    """
    seen: dict[str, str] = {}

    def snapshot() -> int:
        rows = page.evaluate(
            r"""
            () => {
              const out = [];
              document.querySelectorAll('[data-test-id="conversation"]').forEach(el => {
                let id = null;
                const jslog = el.getAttribute('jslog') || '';
                const m = jslog.match(/"(c_[0-9a-f]+)"/);
                if (m) id = m[1].slice(2);
                if (!id) {
                  const href = el.getAttribute('href') || '';
                  const m2 = href.match(/\/app\/([0-9a-f]+)/);
                  if (m2) id = m2[1];
                }
                const title = (el.innerText || '').trim().split('\n')[0];
                out.push({ id, title });
              });
              return out;
            }
            """
        )
        added = 0
        for r in rows:
            if r["id"] and r["id"] not in seen:
                seen[r["id"]] = r["title"] or f"chat_{r['id'][:8]}"
                added += 1
        return added

    # Cache the detected scroller in window so we don't re-search every round.
    page.evaluate(
        """
        () => {
          // Find every ancestor of any conversation element that is itself scrollable.
          const items = document.querySelectorAll('[data-test-id="conversation"]');
          const scrollers = new Set();
          items.forEach(it => {
            let el = it;
            while (el && el !== document.body) {
              const cs = getComputedStyle(el);
              const oy = cs.overflowY;
              if ((oy === 'auto' || oy === 'scroll') && el.scrollHeight > el.clientHeight + 1) {
                scrollers.add(el);
              }
              el = el.parentElement;
            }
          });
          // Also try named candidates.
          ['infinite-scroller', 'div.chat-history', 'conversations-list'].forEach(sel => {
            const el = document.querySelector(sel);
            if (el && el.scrollHeight > el.clientHeight + 1) scrollers.add(el);
          });
          window.__geminiScrollers = Array.from(scrollers);
          return window.__geminiScrollers.map(el => ({
            tag: el.tagName.toLowerCase(),
            cls: (el.className+'').slice(0, 80),
            scrollHeight: el.scrollHeight,
            clientHeight: el.clientHeight,
          }));
        }
        """
    )
    snapshot()
    last_total = len(seen)
    stable = 0
    for i in range(max_rounds):
        info = page.evaluate(
            """
            () => {
              const scrollers = window.__geminiScrollers || [];
              let progressed = false;
              for (const el of scrollers) {
                const before = el.scrollTop;
                el.scrollTop = before + Math.max(200, el.clientHeight * 0.8);
                if (el.scrollTop > before + 5) progressed = true;
                el.dispatchEvent(new Event('scroll', { bubbles: true }));
                el.dispatchEvent(new WheelEvent('wheel', {
                  deltaY: 1200, bubbles: true, cancelable: true,
                }));
              }
              // Force-mount: scroll the LAST rendered conversation item into view
              const items = document.querySelectorAll('[data-test-id="conversation"]');
              if (items.length) {
                items[items.length - 1].scrollIntoView({block: 'end'});
              }
              // If no scrollers, refresh detection
              if (scrollers.length === 0) {
                window.__geminiScrollers = null;
              }
              return { progressed, items: items.length };
            }
            """
        )
        page.wait_for_timeout(650)
        added = snapshot()
        if added == 0 and not info.get("progressed"):
            stable += 1
            if stable >= 10:
                break
        else:
            stable = 0
        if len(seen) != last_total:
            print(f"  ... {len(seen)} conversations so far  (rendered: {info.get('items')})")
            last_total = len(seen)
        # Re-detect scrollers periodically in case the DOM swapped them
        if i % 50 == 49:
            page.evaluate(
                """
                () => {
                  const items = document.querySelectorAll('[data-test-id="conversation"]');
                  const scrollers = new Set();
                  items.forEach(it => {
                    let el = it;
                    while (el && el !== document.body) {
                      const cs = getComputedStyle(el);
                      if ((cs.overflowY === 'auto' || cs.overflowY === 'scroll')
                          && el.scrollHeight > el.clientHeight + 1) {
                        scrollers.add(el);
                      }
                      el = el.parentElement;
                    }
                  });
                  window.__geminiScrollers = Array.from(scrollers);
                }
                """
            )

    entries = [{"id": cid, "title": title} for cid, title in seen.items()]
    print(f"  sidebar settled at {len(entries)} conversations")
    return entries


def collect_sidebar_entries(page: Page) -> list[dict]:
    """Legacy shim — kept for compatibility; now just re-uses expand result."""
    return expand_all_recents(page)


# ---------------------------------------------------------------------------
# Conversation scraping
# ---------------------------------------------------------------------------
def wait_for_turns(page: Page, timeout_ms: int = 12000) -> None:
    deadline = time.time() + timeout_ms / 1000
    while time.time() < deadline:
        if (
            first_matching(page, SEL_USER_TURN) is not None
            or first_matching(page, SEL_MODEL_TURN) is not None
        ):
            return
        page.wait_for_timeout(300)
    # don't raise — some conversations may legitimately be empty


def extract_turns(page: Page) -> list[Turn]:
    """Walk the DOM and pull alternating user/model turns in document order."""
    # Build a unified JS query so we can read order from the DOM in one pass.
    js = """
    () => {
      const userSels = %s;
      const modelSels = %s;
      const all = [];
      const seen = new Set();
      function push(role, el) {
        if (!el || seen.has(el)) return;
        seen.add(el);
        all.push({ role, html: el.innerHTML, text: el.innerText });
      }
      for (const sel of userSels) {
        document.querySelectorAll(sel).forEach(el => push('user', el));
      }
      for (const sel of modelSels) {
        document.querySelectorAll(sel).forEach(el => push('assistant', el));
      }
      // Sort by document order
      all.sort((a, b) => {
        // We lost original element ref after JSON; recompute via fresh query.
        return 0;
      });
      return all;
    }
    """ % (json.dumps(SEL_USER_TURN), json.dumps(SEL_MODEL_TURN))

    # Better: do everything in JS preserving order.
    js = (
        "(args) => {"
        "const userSels = args.user;"
        "const modelSels = args.model;"
        "const nodes = [];"
        "userSels.forEach(s => document.querySelectorAll(s).forEach(e => nodes.push({role:'user', el:e})));"
        "modelSels.forEach(s => document.querySelectorAll(s).forEach(e => nodes.push({role:'assistant', el:e})));"
        "const uniq = [];"
        "const seen = new Set();"
        "for (const n of nodes) { if (!seen.has(n.el)) { seen.add(n.el); uniq.push(n); } }"
        "uniq.sort((a,b) => {"
        "  const pos = a.el.compareDocumentPosition(b.el);"
        "  if (pos & Node.DOCUMENT_POSITION_FOLLOWING) return -1;"
        "  if (pos & Node.DOCUMENT_POSITION_PRECEDING) return 1;"
        "  return 0;"
        "});"
        "return uniq.map(n => ({role:n.role, html:n.el.innerHTML, text:n.el.innerText}));"
        "}"
    )
    try:
        raw = page.evaluate(js, {"user": SEL_USER_TURN, "model": SEL_MODEL_TURN})
    except Exception as e:
        print(f"  warn: turn extraction JS failed: {e}")
        return []

    turns: list[Turn] = []
    for item in raw:
        html = item.get("html") or ""
        text = (item.get("text") or "").strip()
        try:
            markdown_text = md(html, heading_style="ATX").strip()
        except Exception:
            markdown_text = text
        turns.append(
            Turn(
                role=item.get("role", "unknown"),
                text=text,
                markdown=markdown_text,
                html=html,
            )
        )
    return turns


def scrape_conversation(page: Page, title: str) -> Conversation:
    wait_for_turns(page)
    # Give the model-response stream a moment in case it's still hydrating
    page.wait_for_timeout(600)
    turns = extract_turns(page)
    url = page.url
    convo_id = url.rsplit("/", 1)[-1] if "/" in url else url
    return Conversation(
        id=convo_id,
        title=title or "Untitled",
        url=url,
        scraped_at=datetime.utcnow().isoformat() + "Z",
        turns=turns,
    )


# ---------------------------------------------------------------------------
# Output writers
# ---------------------------------------------------------------------------
def write_outputs(convos: list[Conversation], out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    md_dir = out_dir / "markdown"
    html_dir = out_dir / "html"
    md_dir.mkdir(exist_ok=True)
    html_dir.mkdir(exist_ok=True)

    # JSON master file
    json_path = out_dir / "conversations.json"
    json_path.write_text(
        json.dumps(
            [asdict(c) for c in convos],
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    # Per-conversation Markdown + HTML
    used_names: set[str] = set()
    for c in convos:
        base = safe_filename(c.title) or c.id
        name = base
        i = 2
        while name.lower() in used_names:
            name = f"{base} ({i})"
            i += 1
        used_names.add(name.lower())

        md_lines = [
            f"# {c.title}",
            "",
            f"- **URL:** {c.url}",
            f"- **ID:** `{c.id}`",
            f"- **Scraped:** {c.scraped_at}",
            "",
            "---",
            "",
        ]
        html_lines = [
            f"<!doctype html><meta charset='utf-8'><title>{c.title}</title>",
            f"<h1>{c.title}</h1>",
            f"<p><a href='{c.url}'>{c.url}</a> &middot; id <code>{c.id}</code></p><hr>",
        ]
        for idx, t in enumerate(c.turns, 1):
            role_label = "User" if t.role == "user" else "Gemini"
            md_lines.append(f"## {role_label} (turn {idx})")
            md_lines.append("")
            md_lines.append(t.markdown or t.text)
            md_lines.append("")
            html_lines.append(
                f"<section data-role='{t.role}'><h2>{role_label} (turn {idx})</h2>{t.html}</section>"
            )
        (md_dir / f"{name}.md").write_text("\n".join(md_lines), encoding="utf-8")
        (html_dir / f"{name}.html").write_text("\n".join(html_lines), encoding="utf-8")

    print(f"\nWrote {len(convos)} conversations to {out_dir.resolve()}")
    print(f"  JSON:     {json_path.name}")
    print(f"  Markdown: markdown/  ({len(convos)} files)")
    print(f"  HTML:     html/      ({len(convos)} files)")


# ---------------------------------------------------------------------------
# Main flow
# ---------------------------------------------------------------------------
def find_gemini_page(browser_context_pages: list[Page]) -> Page | None:
    for p in browser_context_pages:
        try:
            if "gemini.google.com" in p.url:
                return p
        except Exception:
            continue
    return None


def load_existing(out_dir: Path) -> list[Conversation]:
    """Load previously-scraped conversations from out_dir/conversations.json
    so we can resume without re-doing work."""
    path = out_dir / "conversations.json"
    if not path.exists():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"  warn: could not parse existing {path.name} ({e}); ignoring")
        return []
    out: list[Conversation] = []
    for item in raw:
        turns = [Turn(**t) for t in item.get("turns", [])]
        out.append(
            Conversation(
                id=item.get("id", ""),
                title=item.get("title", ""),
                url=item.get("url", ""),
                scraped_at=item.get("scraped_at", ""),
                turns=turns,
            )
        )
    return out


def run(cdp_url: str, out_dir: Path, limit: int | None, delay: float, force: bool) -> int:
    print(f"Connecting to Chrome at {cdp_url} ...")
    with sync_playwright() as pw:  # type: Playwright
        browser = None
        last_err: Exception | None = None
        # Try the requested URL, then auto-fall-back between IPv4/IPv6/localhost.
        candidates = [cdp_url]
        for alt in ("http://127.0.0.1:9222", "http://localhost:9222", "http://[::1]:9222"):
            if alt not in candidates:
                candidates.append(alt)
        for url in candidates:
            try:
                browser = pw.chromium.connect_over_cdp(url)
                if url != cdp_url:
                    print(f"  (connected via fallback {url})")
                break
            except Exception as e:
                last_err = e
        if browser is None:
            print(
                "ERROR: could not connect. Did you launch Chrome with "
                "--remote-debugging-port=9222 ? See README.",
                file=sys.stderr,
            )
            print(f"  details: {last_err}", file=sys.stderr)
            return 2

        # Use the default (already-authenticated) context
        if not browser.contexts:
            print("ERROR: no browser contexts found.", file=sys.stderr)
            return 2
        ctx = browser.contexts[0]

        page = find_gemini_page(ctx.pages)
        if page is None:
            page = ctx.new_page()
            page.goto(GEMINI_URL, wait_until="domcontentloaded")
        else:
            page.bring_to_front()
            if "gemini.google.com" not in page.url:
                page.goto(GEMINI_URL, wait_until="domcontentloaded")

        page.wait_for_timeout(1500)
        ensure_sidebar_open(page)
        page.wait_for_timeout(500)

        print("Expanding 'Recent' sidebar (this can take a while for many chats) ...")
        entries = expand_all_recents(page)

        if not entries:
            print(
                "ERROR: could not find any conversations in the sidebar.\n"
                "       Tweak the SEL_CONVO_ITEMS selectors at the top of the script.",
                file=sys.stderr,
            )
            return 3

        # ---- Resume: load existing export and filter out already-done ids ----
        existing: list[Conversation] = [] if force else load_existing(out_dir)
        done_ids = {c.id for c in existing if c.turns}  # only count non-empty as done
        if existing:
            print(
                f"Resume: found {len(existing)} previously-exported conversations "
                f"({len(done_ids)} with turns). Use --force to redo all."
            )
            entries = [e for e in entries if e["id"] not in done_ids]

        if limit:
            entries = entries[:limit]
        print(f"Found {len(entries)} conversations to scrape.")

        convos: list[Conversation] = list(existing)
        for n, entry in enumerate(entries, 1):
            title = entry["title"]
            cid = entry["id"]
            target_url = f"https://gemini.google.com/app/{cid}"
            print(f"[{n}/{len(entries)}] {title[:70]}  ({cid})")
            try:
                page.goto(target_url, wait_until="domcontentloaded", timeout=20000)
            except PlaywrightTimeoutError:
                print("  warn: navigation timeout, skipping")
                continue
            except Exception as e:
                print(f"  warn: navigation failed: {e}")
                continue

            page.wait_for_timeout(int(delay * 1000))

            try:
                convo = scrape_conversation(page, title)
                convo.id = cid  # canonical id
                convos.append(convo)
                print(f"  -> {len(convo.turns)} turns")
            except Exception as e:
                print(f"  warn: scrape failed: {e}")

            # Polite delay between chats
            page.wait_for_timeout(int(delay * 1000))

            # Incremental save every 10 chats so a crash doesn't lose progress
            if n % 10 == 0:
                write_outputs(convos, out_dir)
                print(f"  (checkpoint saved at {n}/{len(entries)})")

        write_outputs(convos, out_dir)
    return 0


def main() -> None:
    ap = argparse.ArgumentParser(description="Export Gemini conversations via CDP.")
    ap.add_argument("--cdp", default=DEFAULT_CDP, help="Chrome DevTools URL")
    ap.add_argument("--out", default="./gemini_export", help="Output directory")
    ap.add_argument("--limit", type=int, default=None, help="Cap number of chats")
    ap.add_argument(
        "--delay",
        type=float,
        default=1.2,
        help="Seconds between navigations (be polite)",
    )
    ap.add_argument(
        "--force",
        action="store_true",
        help="Re-scrape every conversation even if already in conversations.json",
    )
    args = ap.parse_args()
    sys.exit(run(args.cdp, Path(args.out), args.limit, args.delay, args.force))


if __name__ == "__main__":
    main()
