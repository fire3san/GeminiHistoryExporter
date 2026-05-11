"""
Diagnostic: connect to the running Chrome and dump info about the Gemini
sidebar / DOM so we can pick the right selectors.

Usage:
    python inspect_gemini.py
"""

from __future__ import annotations

import sys
from pathlib import Path
from playwright.sync_api import sync_playwright

CANDIDATES = [
    # Conversation list items
    '[data-test-id="conversation"]',
    'div[data-test-id="conversation"]',
    'conversations-list div[role="button"]',
    'div.conversation-items-container div[role="button"]',
    'div.conversation',
    'a[href*="/app/"]',
    'nav a[href*="/app/"]',
    'aside a',
    'side-navigation a',
    '[jsname]',
    # Show-more
    'button[data-test-id="show-more-button"]',
    'button:has-text("Show more")',
    # Side nav toggle
    'button[aria-label="Main menu"]',
    'button[data-test-id="side-nav-menu-button"]',
    'button[aria-label*="menu" i]',
    # Turns
    'user-query',
    'div.user-query-container',
    'model-response',
    'message-content',
]


def main() -> int:
    out_dir = Path("./gemini_export")
    out_dir.mkdir(parents=True, exist_ok=True)
    candidates_url = [
        "http://127.0.0.1:9222",
        "http://localhost:9222",
        "http://[::1]:9222",
    ]
    with sync_playwright() as pw:
        browser = None
        for url in candidates_url:
            try:
                browser = pw.chromium.connect_over_cdp(url)
                print(f"Connected via {url}")
                break
            except Exception as e:
                print(f"  {url} -> {e.__class__.__name__}: {e}")
        if browser is None:
            print("Could not connect to Chrome on 9222.", file=sys.stderr)
            return 2

        ctx = browser.contexts[0]
        page = None
        for p in ctx.pages:
            if "gemini.google.com" in p.url:
                page = p
                break
        if page is None:
            print("No gemini.google.com tab found. Open one and rerun.", file=sys.stderr)
            return 3
        page.bring_to_front()
        page.wait_for_timeout(800)

        print(f"\nPage URL: {page.url}")
        print(f"Page title: {page.title()}")
        print("\n--- Selector match counts ---")
        for sel in CANDIDATES:
            try:
                n = page.locator(sel).count()
            except Exception as e:
                n = f"err:{e.__class__.__name__}"
            print(f"  {n!s:>6}  {sel}")

        # Heuristic dump: find every element whose role/aria suggests a chat row
        print("\n--- Heuristic scan: nav <a> / role=listitem / role=button under aside ---")
        info = page.evaluate(
            """
            () => {
              const out = [];
              const push = (tag, sel, sample) => out.push({tag, sel, sample});
              const collectFrom = (root, tag) => {
                root.querySelectorAll('a, [role="button"], [role="listitem"]').forEach(el => {
                  const text = (el.innerText || '').trim().slice(0, 60);
                  if (!text) return;
                  out.push({
                    tag,
                    sel: el.tagName.toLowerCase()
                       + (el.id ? '#' + el.id : '')
                       + (el.className ? '.' + (''+el.className).split(/\\s+/).join('.') : ''),
                    attrs: Object.fromEntries(
                      Array.from(el.attributes)
                        .filter(a => /^(data-|aria-|role|jsname|jslog)/.test(a.name))
                        .map(a => [a.name, a.value])
                    ),
                    text,
                  });
                });
              };
              document.querySelectorAll('aside, nav, side-navigation, bard-sidenav, conversations-list').forEach(n => collectFrom(n, n.tagName));
              return out.slice(0, 40);
            }
            """
        )
        for row in info:
            print(f"  [{row['tag']}] {row['sel']}")
            print(f"      attrs: {row.get('attrs')}")
            print(f"      text:  {row['text']!r}")

        # Save sidebar HTML for offline inspection
        html = page.evaluate(
            """() => {
              const el = document.querySelector('aside, nav, side-navigation, bard-sidenav, conversations-list');
              return el ? el.outerHTML : document.body.outerHTML.slice(0, 200000);
            }"""
        )
        sidebar_path = out_dir / "sidebar_dump.html"
        sidebar_path.write_text(html, encoding="utf-8")
        print(f"\nWrote sidebar HTML snapshot -> {sidebar_path.resolve()}")

        # ---- Step 2: open the first conversation and probe turn selectors ----
        print("\n--- Opening first conversation to probe turn selectors ---")
        first = page.locator('[data-test-id="conversation"]').first
        try:
            first.click(timeout=4000)
        except Exception as e:
            print(f"  click failed: {e}")
            return 0
        page.wait_for_load_state("domcontentloaded")
        page.wait_for_timeout(2500)
        print(f"  URL now: {page.url}")

        turn_candidates = [
            'user-query',
            'user-query-content',
            'div.user-query-bubble-with-background',
            'div.query-text',
            'model-response',
            'message-content',
            'div.model-response-text',
            'div.response-container',
            'div.response-content',
            'message-actions',
            '[data-test-id="user-query"]',
            '[data-test-id="model-response"]',
            'div.conversation-container',
            'div[class*="conversation-container"]',
            'div[class*="message"]',
        ]
        print("\n  Turn-selector match counts:")
        for sel in turn_candidates:
            try:
                n = page.locator(sel).count()
            except Exception as e:
                n = f"err:{e.__class__.__name__}"
            print(f"    {n!s:>6}  {sel}")

        # Heuristic: dump first N message-like nodes
        print("\n  Heuristic: top-level chat children with tagName + classes:")
        info = page.evaluate(
            """
            () => {
              const containers = document.querySelectorAll(
                'chat-history, infinite-scroller, div.chat-history, main'
              );
              const out = [];
              containers.forEach(c => {
                const kids = c.children;
                for (let i = 0; i < Math.min(kids.length, 12); i++) {
                  const el = kids[i];
                  out.push({
                    container: c.tagName.toLowerCase(),
                    tag: el.tagName.toLowerCase(),
                    cls: (el.className + '').slice(0, 200),
                    attrs: Object.fromEntries(
                      Array.from(el.attributes)
                        .filter(a => /^(data-|aria-|role)/.test(a.name))
                        .map(a => [a.name, a.value])
                    ),
                    sample: (el.innerText || '').trim().slice(0, 80),
                  });
                }
              });
              return out;
            }
            """
        )
        for row in info:
            print(f"    [{row['container']}] <{row['tag']}> cls={row['cls']!r}")
            print(f"        attrs={row['attrs']}")
            print(f"        text={row['sample']!r}")

        # Save full conversation HTML
        convo_html = page.evaluate(
            """() => {
              const el = document.querySelector('chat-history, infinite-scroller, main');
              return el ? el.outerHTML.slice(0, 400000) : document.body.outerHTML.slice(0, 400000);
            }"""
        )
        convo_path = out_dir / "conversation_dump.html"
        convo_path.write_text(convo_html, encoding="utf-8")
        print(f"\n  Wrote conversation HTML snapshot -> {convo_path.resolve()}")
        print("\nPaste the 'Turn-selector match counts' + heuristic section back to Copilot.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
