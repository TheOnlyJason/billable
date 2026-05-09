# Billable

> Capture what you actually did during the day and turn it into a billable timesheet you can send to your boss or client without rewriting it.

This README covers **how to install and run v1** (the personal MVP). For the architecture, data model, and product roadmap, see [`DESIGN.md`](./DESIGN.md).

---

## What v1 does

For a given day, it captures everything you did from four sources, summarizes it with an LLM, and emits a Markdown timesheet:

| Source | What it captures | Setup |
| --- | --- | --- |
| **Cursor** | Every chat prompt you sent — text, timestamp, workspace | Nothing — reads local SQLite |
| **Notes** | Manual timestamped notes from `billable note` (or a hotkey) | Nothing for the CLI; one-time hotkey binding for fluent use |
| **ActivityWatch** | Active-window focus blocks (with AFK time subtracted) — what was on your screen and for how long | Install [ActivityWatch](https://activitywatch.net) and leave it running |
| **Google Docs** | Edits AND views — including docs others shared with you that you opened to review | Set up Google OAuth (one-time, see below) |

Then:

1. Each event is normalized into a common shape and tagged with a `matter_id` from `config/projects.yaml`.
2. OpenAI clusters them into work sessions (stage 1, `gpt-4o-mini`) and writes one outcome-focused paragraph per matter (stage 2, `gpt-4o`).
3. The pipeline rolls up to one entry per (date × matter), rounded up to 0.25h.
4. A Markdown timesheet lands in `./out/YYYY-MM-DD.md` with an audit trail back to every source artifact.

You run it manually at end of day. No tray app, no scheduler, no UI yet.

---

## Requirements

- **Python 3.11+**
- **An OpenAI API key** with access to `gpt-4o-mini` (stage 1) and `gpt-4o` (stage 2)
- **A Google Cloud project** with the Drive Activity API and Google Docs API enabled, plus an OAuth client (desktop application) — *only if you want the Google Docs source*
- **Windows 10/11**, **macOS 12+**, or **Linux**. The Cursor adapter auto-detects the right per-OS storage path.

---

## Install

The Python core is identical across platforms; only the activate command and the hotkey helper differ.

### Windows (PowerShell)

```powershell
# From the repo root:
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e .
```

### macOS / Linux (bash / zsh)

```bash
# From the repo root:
python3 -m venv .venv
source .venv/bin/activate
pip install -e .

# Make the hotkey script executable (one time).
chmod +x scripts/billable-note.sh
```

---

## Configure

### 1. Environment

Copy `.env.example` to `.env` and fill in:

```
OPENAI_API_KEY=sk-...
GOOGLE_OAUTH_CLIENT_SECRETS=./config/google_oauth_client.json
```

### 2. Project / matter mapping

You have three options here, in increasing order of how much effort they take:

**A. Skip it.** You don't *have* to write any yaml — every Cursor workspace folder is auto-promoted to a matter on the fly (e.g. opening `acme-site` produces an `acme-site` / "Acme Site" matter without you doing anything). Browser-only work and free-form notes will land in Unclassified, and matter display names will just be the humanized folder name. Good enough for a single-developer test drive.

**B. Let the LLM write it for you.** After you've used Cursor for a few days:

```powershell
billable discover         # scans your machine, drafts config/projects.proposed.yaml
# review that file...
billable discover --apply  # overwrites config/projects.yaml (with .bak)
```

This produces a yaml with friendly display names inferred from your actual prompts — e.g. a `bot-1` workspace whose prompts mention Handshake AI becomes `display_name: "Handshake AI Automation Bot"`. Costs about $0.01 of LLM time and takes ~1 minute.

**C. Write it by hand.** Copy `config/projects.example.yaml` to `config/projects.yaml` and edit. The schema:

```yaml
projects:
  internal-billable-agent:
    display_name: "Internal R&D — Billable Agent"
    cursor_workspaces:
      - billable                # matches Cursor prompts in this workspace
    gdoc_folder_ids:
      - 1aBcD...                # Drive folder id (matches gdoc events)
    keywords:                   # case-insensitive substring match;
      - billable agent          # checks project_hint AND content_excerpt
      - timesheet ai            # so it also catches AW window titles
```

Resolution order at runtime:

1. `--matter` override on a `billable note` invocation (hard override).
2. Rules in `projects.yaml` order (first match wins).
3. **Auto-discovery fallback**: cursor / activitywatch events with a workspace `project_hint` synthesize a matter from the hint itself.
4. Otherwise: Unclassified.

### 3. Google OAuth (one-time)

1. In Google Cloud Console, create a project, enable **Drive Activity API** and **Google Docs API**.
2. Create an **OAuth 2.0 Client ID** of type **Desktop application**. Download the JSON.
3. Save it at the path you put in `GOOGLE_OAUTH_CLIENT_SECRETS`.
4. The first run will open a browser for consent and cache a token in `./cache/google_token.json`.

### 4. Billing config (optional)

`config/billing.yaml` controls increments and rounding. Defaults are sensible:

```yaml
increment_hours: 0.25
rounding: up           # up | nearest | down
daily_cutoff: "23:59"  # entries past this time roll into the next day
```

---

## Run

```powershell
billable run --date today
billable run --date 2026-05-07
billable run --range 2026-05-01..2026-05-07
billable discover                  # auto-propose a projects.yaml from your machine
```

Output lands in `./out/YYYY-MM-DD.md`. Raw events are cached in `./cache/events/YYYY-MM-DD.json` so you can re-run the LLM stages without re-pulling from Cursor / Google / ActivityWatch.

### Useful flags

| Flag | What it does |
| --- | --- |
| `--no-llm` | Skip the LLM stages; dump raw events as a debug Markdown file. |
| `--reuse-cache` | Use cached events instead of re-pulling from sources. |
| `--stage narrate` | Re-run only stage 2 (cheap when you're tweaking the narrative prompt). |
| `--source cursor` / `--source notes` / `--source activitywatch` / `--source gdocs` | Limit to one or more sources. Repeatable. |
| `--verbose` | INFO-level logs (per-adapter event counts, cache paths). |

## Hotkey notes

Add a one-line note at any time:

```bash
billable note "Reviewed the Smith brief, returned with comments."
billable note "Call with Acme product team" -m 45 --matter acme-website
```

Notes get appended to `./data/notes.jsonl` with the current timestamp and show up in the next `billable run`. The `--matter` flag pins the note to a specific project (bypassing keyword matching) — perfect for "I just spent 30 min reviewing X for client Y, count it as billable."

For friction-free capture, bind a hotkey. Three platform-specific paths ship in `scripts/`; pick whichever matches your OS. All three auto-detect the repo location, so they work no matter where you cloned the project.

### Windows — PowerShell (no installs)

Add the helper to your PowerShell profile:

```powershell
Add-Content -Path $PROFILE -Value '. "<ABSOLUTE-PATH-TO-REPO>\scripts\billable-note.ps1"'
```

Open a new PowerShell tab and `bnote` works:

```powershell
bnote                                # opens a small input dialog
bnote -Text "quick one" -Minutes 5    # logs directly, no dialog
```

For a global hotkey (any app, any time), create a Windows shortcut to:

```
powershell.exe -NoProfile -WindowStyle Hidden -Command "& { . '<repo>\scripts\billable-note.ps1'; bnote }"
```

Right-click the shortcut → **Properties** → **Shortcut key** → press your combo (e.g. `Ctrl+Alt+N`). Windows requires the shortcut to live on the Desktop or in `Start Menu\Programs` for the global hotkey to fire.

### Windows — AutoHotkey (best UX)

Install [AutoHotkey v2](https://www.autohotkey.com), then double-click `scripts\billable-note.ahk`. You get:

- `Win+Shift+N` — quick note prompt
- `Win+Shift+M` — full prompt (text + minutes + matter)

Drop a shortcut to the `.ahk` in `shell:startup` to auto-launch on login.

### macOS — Shortcuts.app (no installs)

The script `scripts/billable-note.sh` already handles the dialog via AppleScript. Wire it to a hotkey:

1. Open **Shortcuts.app** (built into macOS).
2. New Shortcut → add a **Run Shell Script** action.
3. Paste the absolute path:
   ```bash
   /absolute/path/to/billable/scripts/billable-note.sh
   ```
4. Set "Pass Input" to **none**.
5. In the shortcut's details panel (right side), assign a **Keyboard Shortcut** (e.g. `⌃⌥N`).

Now `Ctrl+Option+N` (or whatever you bound) anywhere on macOS pops a native AppleScript dialog. You can also call `./scripts/billable-note.sh "quick note"` directly from any terminal to skip the dialog.

### Linux — desktop keyboard settings

The same `scripts/billable-note.sh` works. Wire it via your DE:

- **GNOME:** Settings → Keyboard → View and Customize Shortcuts → Custom Shortcuts → bind to `/absolute/path/to/scripts/billable-note.sh`
- **KDE:** System Settings → Shortcuts → Custom Shortcuts → New → Global Shortcut → Command/URL
- **WMs (i3/sway/sxhkd):** add the absolute path to your keybinding config

The script auto-detects the right dialog backend (zenity, kdialog, rofi, or AppleScript on macOS).

## ActivityWatch (active-window capture)

To capture *everything you had on your screen* (PDFs, Word docs, browser tabs, anything), install [ActivityWatch](https://activitywatch.net) (Windows / macOS / Linux) and leave it running in the background. The adapter discovers it automatically at `http://localhost:5600`. Tunables in `.env`:

| Variable | Default | What it does |
| --- | --- | --- |
| `BILLABLE_AW_URL` | `http://localhost:5600` | ActivityWatch REST endpoint |
| `BILLABLE_AW_MIN_FOCUS_MIN` | `2` | Drop focus blocks shorter than N minutes (filters tab-flick noise) |

The adapter automatically subtracts AFK time, so a 2-hour Chrome session with a 30-min lunch in the middle becomes 1.5 hours of active focus, not 2.

---

## Roadmap (short version)

See [`DESIGN.md`](./DESIGN.md) §7 for the full picture. The next things on the list, when v1 starts feeling limiting:

- `git` adapter (commit messages + diffs)
- Calendar adapter (Google Calendar / Outlook)
- Browser-history adapter (Chrome/Edge SQLite)
- `.docx` and Google Docs renderers
- A small review UI to edit entries before export

---

## Privacy notes

- Cursor chat transcripts and Google Docs excerpts are sent to the OpenAI API for summarization. If your work is sensitive, do not run v1 against it. Local-LLM and BYO-key support are on the roadmap (see `DESIGN.md` §3, §7).
- Adapters strip obvious secrets (API keys, tokens, password-shaped strings) from `content_excerpt` before anything leaves your machine. This is best-effort, not a guarantee.
- Raw events in `./cache/` and rendered timesheets in `./out/` are local files. Both directories are gitignored.

---

## Repository layout

See [`DESIGN.md`](./DESIGN.md) §10.
