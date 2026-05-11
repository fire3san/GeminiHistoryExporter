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
