# Gemini Conversation Exporter (Windows)

Exports your full Gemini chat history from `gemini.google.com` to:

- `gemini_export/conversations.json` — structured data for downstream agents
- `gemini_export/markdown/*.md` — readable per-chat files
- `gemini_export/html/*.html` — raw HTML fidelity (code blocks, tables, etc.)

It works by **attaching Playwright to your already-logged-in Chrome** over the
DevTools Protocol. Google never sees a programmatic login attempt, so there are
no captchas, no "couldn't sign you in" blocks, and no 3rd-party extensions.

---

## 1. One-time setup

Open **PowerShell** in this folder (`c:\Users\San\GeminiExporter`) and run:

```powershell
py -3 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python -m playwright install chromium   # only needed if you want a fallback browser; CDP mode uses your real Chrome
```

> If `Activate.ps1` is blocked, run once:
> `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`

---

## 2. Launch Chrome in debug mode

**Fully quit Chrome first** (check the system tray — right-click the Chrome icon
→ Exit, or `Get-Process chrome | Stop-Process -Force`). Then, in PowerShell:

```powershell
# Pick whichever chrome.exe path exists on your machine:
$chrome = "C:\Program Files\Google\Chrome\Application\chrome.exe"
if (-not (Test-Path $chrome)) {
  $chrome = "C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"
}
$profile = "$PWD\.chrome-debug-profile"
& $chrome --remote-debugging-port=9222 --user-data-dir="$profile"
```

A new Chrome window opens. **Sign into your Google Workspace account once** and
navigate to <https://gemini.google.com/app>. That profile dir is reused on
subsequent runs, so you stay logged in.

Verify the debug port is live (in a second shell):

```powershell
Test-NetConnection 127.0.0.1 -Port 9222   # TcpTestSucceeded : True
```

> Alternative: reuse your **main** Chrome profile by pointing `--user-data-dir`
> at e.g. `"$env:LOCALAPPDATA\Google\Chrome\User Data"` — but you must have
> *every* Chrome window closed first or the flag is silently ignored.

---

## 3. Run the exporter

In a second PowerShell window (leaving Chrome running):

```powershell
cd c:\Users\San\GeminiExporter
.\.venv\Scripts\Activate.ps1

# Smoke test: only the 3 most-recent chats
python scrape_gemini.py --limit 3

# Full export
python scrape_gemini.py
```

Useful flags:

| Flag        | Default                     | Purpose                                |
| ----------- | --------------------------- | -------------------------------------- |
| `--cdp`     | `http://localhost:9222`     | DevTools endpoint                      |
| `--out`     | `./gemini_export`           | Output directory                       |
| `--limit N` | (all)                       | Cap number of conversations            |
| `--delay S` | `1.2`                       | Seconds between actions (rate-limit-friendly) |

---

## 4. What you get

```
gemini_export/
├── conversations.json          # [{id, title, url, scraped_at, turns:[{role,text,markdown,html}]}]
├── markdown/
│   ├── My first chat.md
│   └── ...
└── html/
    ├── My first chat.html
    └── ...
```

Feed `conversations.json` straight into Grok / Claude / another local agent.

---

## 4b. Aggregate everything into a single Markdown file

After exporting, combine all per-conversation Markdown files into one big
chronological history file (with table of contents):

```powershell
python aggregate_markdown.py                        # -> gemini_export/HISTORY.md
python aggregate_markdown.py --reverse              # oldest first
python aggregate_markdown.py --split-by-month       # one file per YYYY-MM in gemini_export/HISTORY/
python aggregate_markdown.py --out C:\path\out.md   # custom output path
```

Order follows `conversations.json` (Gemini's sidebar order = newest first).
Per-chat headings are demoted by one level so the combined document stays a
valid Markdown tree.

---

## 4c. Convert for import into another AI app

`convert_for_import.py` re-encodes `conversations.json` into the export
schemas used by other AI apps. This is what you upload to
<https://gemini.google/import-memory/> (or to ChatGPT / Claude / Grok import
flows) — Gemini's importer rejects the native scraper JSON because it isn't a
"supported AI app" format.

```powershell
python convert_for_import.py --format all          # writes openai/, grok/, claude/
python convert_for_import.py --format openai       # ChatGPT export schema
python convert_for_import.py --format claude       # Claude export schema
python convert_for_import.py --format grok         # Grok export schema
python convert_for_import.py --in gemini_export/conversations.json --out-dir gemini_export/converted
```

Each format is written to its own subfolder under `gemini_export/converted/`
as a single `conversations.json`. The script then re-reads the file and
prints `OK` only if the on-disk conversation count matches the input —
a built-in cross-check that nothing was silently dropped.

The converter also cleans the data on the way out:

- **Titles**: placeholder titles like `chat_abcd1234` or the Chinese UI label
  `你說了` are replaced by a snippet of the first user prompt.
- **Bodies**: UI labels Gemini injects into the DOM (`你說了`,
  `Gemini 說了`, `顯示思路`, `Sources`, `Show thinking`, …) are stripped —
  including when they appear as Markdown headings (`## Gemini 說了`).
- **Duplicates**: consecutive same-role turns with identical text (a known
  side-effect of Gemini's nested `<user-query>` DOM) are collapsed.

### Self-test

A companion smoke test validates every output:

```powershell
python test_gemini_conversion.py             # run converter, then validate
python test_gemini_conversion.py --skip-run  # only validate existing files
```

It checks each target file for: parseability, expected top-level shape,
matching conversation count, matching cleaned message count, non-blank
titles, absence of UI-noise lines in message bodies, and normalised
sender values.

If you only want to refresh titles on an existing scrape without re-running
the full export, use the scraper's retitle mode (it visits each conversation
URL just long enough to read the tab title):

```powershell
python scrape_gemini.py --retitle
```

---

## 4d. Convert a **Grok export** for import elsewhere

If you've downloaded your Grok data (`prod-grok-backend.json`), drop it into
`grok_export/` and run `convert_grok_for_import.py` to convert it into the
same four target schemas plus a clean re-emit of Grok's own format.

```powershell
# Default: reads grok_export/prod-grok-backend.json
# writes  grok_export/converted/{openai,claude,grok,gemini}/conversations.json
python convert_grok_for_import.py

# Single target
python convert_grok_for_import.py --format openai
python convert_grok_for_import.py --format claude
python convert_grok_for_import.py --format grok      # cleaned Grok schema (for Grok-4 re-import)
python convert_grok_for_import.py --format gemini    # ChatGPT shape, for Gemini import-memory

# Explicit paths
python convert_grok_for_import.py --in C:\path\to\export.json --out-dir .\grok_export\converted
```

Key behaviour:

- **Real timestamps preserved** — Grok stores per-message `create_time` as
  `{"$date": {"$numberLong": "<ms epoch>"}}`. Those are converted into each
  target schema's native timestamp shape (no synthetic 1-sec spacing needed).
- **Sender casing normalized** — Grok mixes `"human"`, `"assistant"`, and
  `"ASSISTANT"` in the same file. All become `human` / `assistant`.
- **Same cleaning as the Gemini converter** — title rescue, UI-noise
  stripping, consecutive-duplicate collapse (via shared helpers in
  `convert_for_import.py`).
- **Cross-check** — every output file is re-read after writing and the
  conversation count must match the input. Conversion fails loudly otherwise.

### Self-test

A smoke test script validates every output:

```powershell
python test_grok_conversion.py             # run converter, then validate
python test_grok_conversion.py --skip-run  # only validate existing files
```

It checks each target file for: parseability, expected top-level shape,
matching conversation count, matching total message count, non-blank titles,
absence of UI-noise lines in message bodies, and normalised sender values.

---

## 5. Troubleshooting

- **"could not connect"** — Chrome wasn't launched with `--remote-debugging-port=9222`,
  or another Chrome instance is squatting on the profile. Quit *all* Chrome
  processes (`Get-Process chrome | Stop-Process`) and relaunch.
- **"no conversations in the sidebar"** — Gemini occasionally renames DOM
  classes. Open DevTools on a conversation, right-click a sidebar item →
  *Inspect*, copy a stable selector, and add it to the top of
  [scrape_gemini.py](scrape_gemini.py) in the `SEL_CONVO_ITEMS` list.
- **Some chats look short** — long conversations may need a bigger
  `--delay` so the virtualized scroller has time to mount older turns. Try
  `--delay 2.5`.
- **Rate limiting** — increase `--delay` and run with `--limit` in batches.

---

## 6. Security notes

- The script never sees your password — it only talks to a local
  `localhost:9222` socket exposed by your own Chrome.
- The DevTools port is **not** firewall-exposed by default, but close the
  debug Chrome window when you're done so nothing on your machine can attach
  to that session.
- All output stays on disk in this folder. Nothing is uploaded.
