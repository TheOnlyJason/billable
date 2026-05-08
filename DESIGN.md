# Billable — Design Document

> An AI agent that captures what you actually did during the day and turns it into a billable-time document.

This document is the source of truth for **why** the system is shaped the way it is. The `README.md` covers **how** to use it. If a decision is non-obvious, it should be explained here.

---

## 1. Vision

**One sentence:** Capture the artifacts you produce during a workday (code, docs, AI chats, meetings) and produce a per-day billable summary good enough to send to a client or boss without manual rewriting.

**Two-horizon plan:**

| Horizon | Audience | What it is |
| --- | --- | --- |
| **v1 (personal MVP)** | The author | A Python CLI that reads Cursor chats, Google Docs activity (edits + views), ActivityWatch focus blocks, and hotkey notes for a given day and emits a Markdown timesheet. |
| **v2+ (sellable product)** | Solo / small-firm lawyers, then consultants | A desktop agent + cloud backend with matter management, ABA codes, LEDES export, audit trail, and a review UI. |

The v1 architecture is deliberately shaped so that the v2 pivot is *additive* (new adapters, new renderers, new UI) rather than a rewrite. See §7 for what that costs us up-front and what we defer.

---

## 2. Why this exists

Billable professionals lose 10–25% of their actual hours to bad time capture. The two failure modes are:

1. **Forgetting** — work happened, no entry exists.
2. **Vague entries** — "worked on case" gets rejected by clients or under-bills the work.

Existing tools either:

- Track *time* but not *content* (Toggl, Harvest, RescueTime) — you still write the narrative.
- Track *content* but only inside one silo (GitHub activity, Notion analytics, Clio's built-in tools) — they miss the cross-tool reality of how work actually happens.
- Are AI-first but generic ("summarize my screen time") — output is not billing-grade.

The wedge is **artifact-grounded narratives**: read the things you actually produced, infer the work from them, and write entries in the voice the recipient expects.

---

## 3. Architecture (target)

```
┌─────────────────────────────────────────────────────────┐
│                    Capture Adapters                      │
│  (each implements the same interface, drop-in pluggable) │
├─────────────────────────────────────────────────────────┤
│  CursorAdapter        → chat transcripts + workspace    │
│  GoogleDocsAdapter    → edits via Drive Activity API    │
│                       + views via viewedByMeTime        │
│  NotesAdapter         → hotkey-triggered manual notes   │
│  ActivityWatchAdapter → focus blocks (window + AFK)     │
│  GitAdapter           → commits + diffs          (later) │
│  CalendarAdapter      → meetings                 (later) │
└──────────────────────┬──────────────────────────────────┘
                       │ normalized Event objects
                       ▼
┌─────────────────────────────────────────────────────────┐
│                   Normalization Layer                    │
│  All adapters emit the same Event shape (see §4).        │
└──────────────────────┬──────────────────────────────────┘
                       ▼
┌─────────────────────────────────────────────────────────┐
│                  Project / Matter Mapper                 │
│  YAML config: keywords / repos / folders / doc-IDs      │
│  → matter. Returns "unclassified" instead of guessing.   │
└──────────────────────┬──────────────────────────────────┘
                       ▼
┌─────────────────────────────────────────────────────────┐
│              LLM Stage 1 — Session Clustering           │
│  Groups events into coherent work sessions per matter.   │
│  Cheap model. Outputs structured JSON.                   │
└──────────────────────┬──────────────────────────────────┘
                       ▼
┌─────────────────────────────────────────────────────────┐
│              LLM Stage 2 — Narrative Generator          │
│  Writes outcome-focused billable descriptions.           │
│  Better model. Switchable prompt: "general" vs "legal".  │
└──────────────────────┬──────────────────────────────────┘
                       ▼
┌─────────────────────────────────────────────────────────┐
│                    Daily Rollup                          │
│  One entry per (date × matter). Round to billing         │
│  increment (default 0.25h, configurable to 0.1h).        │
└──────────────────────┬──────────────────────────────────┘
                       ▼
┌─────────────────────────────────────────────────────────┐
│                   Output Renderers                       │
│  MarkdownRenderer  (v1)                                  │
│  DocxRenderer / GoogleDocsRenderer            (later)    │
│  LEDES1998BRenderer / ClioCSVRenderer         (v2 legal) │
└─────────────────────────────────────────────────────────┘
```

**Two-stage LLM, on purpose.** Stage 1 ("what happened") is messy inference over noisy events and benefits from a cheap, fast model. Stage 2 ("write it for the boss") is short, high-leverage prose where quality matters. Splitting them lets us swap models per stage and re-run stage 2 cheaply when prompts change.

---

## 4. Data model

The data model is the most important piece of the design. Get it right now and the v1 → v2 path is mostly additive.

```python
@dataclass
class Event:
    timestamp: datetime
    duration: timedelta | None     # None when unknown (e.g. a single doc edit)
    source: str                    # "cursor" | "gdocs" | "git" | ...
    project_hint: str | None       # raw signal: repo name, doc title, workspace folder
    artifact_ref: str              # stable id: commit sha, doc id, chat id
    content_excerpt: str           # what the LLM reads (sanitized, truncated)
    raw: dict                      # adapter-specific payload (kept for audit)

@dataclass
class Session:
    start: datetime
    end: datetime
    matter_id: str | None          # resolved by the mapper; None = unclassified
    events: list[Event]
    category: str                  # "planning" | "building" | "research" | "meeting" | "admin"

@dataclass
class Entry:
    date: date
    matter_id: str
    description: str               # the narrative the boss/client reads
    hours: Decimal                 # already rounded to billing increment
    task_code: str | None          # ABA L210, etc. (None for non-legal)
    activity_code: str | None      # ABA A103, etc. (None for non-legal)
    sources: list[str]             # artifact_refs — the audit trail
```

**Why these specific fields:**

- `artifact_ref` + `sources: list[str]` give us a hard audit trail back to the original artifact. Required for legal e-billing; also useful for "why does this entry say what it says?"
- `task_code` / `activity_code` are nullable now and turn on later. No schema change when we add legal mode.
- `raw: dict` on `Event` means we never lose information — we can re-derive sessions and entries with a new prompt without re-capturing.
- `matter_id` (not "project_name") because the v2 audience already uses that vocabulary, and renaming later is more painful than starting with the right word.

---

## 5. v1 scope (personal MVP)

**What v1 does, end-to-end:**

1. `billable run --date today`
2. Captures from four sources in parallel:
   - **Cursor** chat prompts from local SQLite (`%APPDATA%\Cursor\User\workspaceStorage\<hash>\state.vscdb`)
   - **Google Docs** edits (Drive Activity API) **and** views (Drive `viewedByMeTime`)
   - **Notes** — timestamped manual entries from `data/notes.jsonl` (written by the `billable note` CLI or hotkey-bound script)
   - **ActivityWatch** — focus blocks from local REST API (window watcher minus AFK), with sub-`min_focus_minutes` blocks dropped as noise
3. Normalizes everything into `Event` objects.
4. Resolves each event's `matter_id` via `config/projects.yaml` (notes can override via `raw['matter_id']`).
5. Sends events to OpenAI in two stages (cluster → narrate).
6. Rolls up to one entry per (date × matter), rounded up to 0.25h.
7. Writes a Markdown file to `./out/YYYY-MM-DD.md`.

**What v1 deliberately does not do:**

- No git adapter, no calendar adapter — *defer until you actually feel the pain*.
- No `.docx` or Google Docs output — Markdown only. Renderers are pluggable; add later.
- No web UI, no tray app, no scheduling — run it manually at end of day.
- No multi-user, no auth, no cloud backend.
- No local LLM — OpenAI only (with provider abstraction so swapping is one file).
- No automatic project classification — if the mapper can't classify an event, it gets `matter_id = None` and the LLM is told to flag it for human review.

**Decisions made for v1:**

| Decision | Choice | Rationale |
| --- | --- | --- |
| Language | Python | Best ecosystem for SQLite + Google APIs + OpenAI, fast to iterate. |
| Capture sources | Cursor + Google Docs | User confirmed these are the only two that matter today. |
| LLM provider | OpenAI | User has an `OPENAI_API_KEY`. Behind a `LLMClient` interface so swapping is trivial. |
| Output format | Markdown | Simplest, easy to eyeball, easy to convert later. |
| Billing increment | 0.25h, round up | Sensible consultant default; configurable. |
| Granularity | Per-day entries | User-stated requirement. |
| Storage | Filesystem (`./out/` for output, `./cache/` for raw events) | No DB needed at v1 scale. Event-sourced layout makes the v2 DB import trivial. |
| Project mapping | Hand-edited `config/projects.yaml` | Auto-classification is a v2 feature. |
| Sanitization | Regex-based secret stripping at the adapter boundary | Done at v1 even though it's just for the author — it's a feature we'll advertise later. |

---

## 6. Open product-level questions (deferred)

These were raised but not answered. They don't gate v1 — but they will gate v2 decisions, and we should answer them before investing in any product-shaped work.

- **Side-project SaaS or fundable startup?** Affects how aggressively to push on legal compliance and self-hosted from day 1.
- **Lawyer design partner?** A practicing lawyer who'll pilot is worth months of guesswork.
- **Tauri vs Electron** for the desktop agent? Tauri is leaner and a better security story for legal; Electron ships faster.
- **Google Workspace Marketplace listing?** Right distribution for the Docs angle, 4–8 week review.

When answered, drop the answers in §6 and update §7's roadmap.

---

## 7. v2+ roadmap (sketch, not commitment)

Ordered by what unlocks the next milestone, not by difficulty.

**M1 — First paid pilot (general professional, not legal):**

- Provider abstraction → finalize: OpenAI, Anthropic, Azure OpenAI.
- Add `git` adapter (+ Outlook/Google Calendar adapter).
- Add browser-history adapter (catches doc-review time on the web, not just in Drive).
- `.docx` renderer.
- A small review UI (web, single user) to edit entries before export.
- BYO API key option (so customers carry their own LLM cost / contract).
- Public privacy/security page; no-training contracts with LLM providers.

**M2 — First law-firm pilot:**

- Matter management UI (add/edit/archive, billing rates, conflict-check fields).
- ABA UTBMS task & activity code autocomplete; LLM suggests one with confidence.
- LEDES 1998B export.
- Privilege flag on entries (changes how content is sent to LLM).
- Audit log surfaces `sources: list[str]` per entry.
- DPA + BAA-equivalent templates ready.
- SOC 2 Type I in progress (Vanta/Drata).

**M3 — Scaling:**

- Desktop agent (Tauri) replaces the Python CLI for capture. Same adapter interface; agent does local sanitization, posts events to backend over TLS.
- Cloud backend (multi-tenant) hosts LLM orchestration, OAuth, storage, renderers.
- SSO, region-pinned data residency, optional self-hosted deployment.
- SOC 2 Type II.

**Things to actively *not* build until forced:**

- Usage-based pricing (billable professionals hate variable bills).
- Mobile app (capture happens on the workstation).
- Slack/Teams integrations (low signal for what was actually *produced*).

---

## 8. Pricing direction (placeholder)

| Tier | Price | What it includes |
| --- | --- | --- |
| Pro | $29–49/user/mo | Our cloud LLM, 1 user, basic exports |
| Business | $79–129/user/mo | BYO key option, team features, LEDES export, audit log |
| Enterprise | Custom | Self-hosted/VPC, SSO, SOC 2 docs, dedicated support |

Margin assumption: LLM cost per Pro user is < $5/mo with prompt caching at typical billable-professional usage. Validate with v1 telemetry once shipped.

---

## 9. Trust & compliance roadmap

Ordered by when each becomes blocking, not by ease.

1. **Day 1 (v1):** TLS everywhere (n/a for local CLI), encryption at rest (n/a), no-training contracts with LLM provider, regex-based secret sanitization at the capture boundary.
2. **First 10 paying customers:** DPA template, basic SOC 2 readiness via Vanta.
3. **First law-firm customer:** SOC 2 Type I, BAA-equivalent, US-only data residency by default.
4. **Scaling:** SOC 2 Type II, ISO 27001 if EU customers ask, self-hosted deployment option.
5. **Healthcare/finance side market:** HIPAA BAA, then GLBA where relevant.

---

## 10. Repository layout

```
billable/
├── README.md                  # v1 user-facing
├── DESIGN.md                  # this file
├── pyproject.toml
├── .env.example
├── .gitignore
├── config/
│   ├── projects.example.yaml  # commit this
│   ├── projects.yaml          # gitignored — the real config
│   └── billing.yaml           # increments, rounding, daily cutoff
├── prompts/
│   ├── cluster.md             # stage 1 prompt
│   └── narrate.md             # stage 2 prompt
├── scripts/
│   ├── billable-note.ahk      # AutoHotkey hotkey binding (Win+Shift+N)
│   └── billable-note.ps1      # PowerShell function alternative
├── src/billable/
│   ├── __init__.py
│   ├── cli.py                 # `billable` entry point (run, note, version)
│   ├── core/
│   │   ├── events.py          # Event, Session, Entry
│   │   ├── pipeline.py        # orchestration
│   │   ├── mapper.py          # project/matter resolution (raw override aware)
│   │   └── sanitize.py        # secret stripping
│   ├── adapters/
│   │   ├── base.py            # CaptureAdapter abstract class
│   │   ├── cursor.py          # local SQLite reader
│   │   ├── google_docs.py     # edits + views
│   │   ├── notes.py           # data/notes.jsonl reader
│   │   └── activitywatch.py   # localhost:5600 REST client
│   ├── notes/
│   │   └── store.py           # JSONL append/read for hotkey notes
│   ├── llm/
│   │   ├── client.py          # LLMClient interface + OpenAI impl
│   │   ├── cluster.py         # stage 1
│   │   └── narrate.py         # stage 2
│   ├── rollup/
│   │   └── daily.py           # per-day aggregation + rounding
│   └── renderers/
│       ├── base.py
│       └── markdown.py
├── cache/                     # gitignored — raw event JSON per day, OAuth token
├── out/                       # gitignored — rendered timesheets
├── data/                      # gitignored — user-generated notes (preserve!)
└── tests/                     # 91 passing
```

---

## 11. Glossary

- **Matter** — a unit of billable work for a single client. In legal, this is a specific case or transaction. For the personal use case, it maps to "project."
- **Entry** — one row on the final timesheet: a date, a matter, a description, hours.
- **Session** — an LLM-inferred coherent block of work, *before* rollup. Multiple sessions on the same matter on the same day collapse into one entry.
- **Adapter** — a class that pulls activity from one source and emits normalized `Event` objects.
- **Renderer** — a class that takes `Entry` objects and writes them as a document in some format.
- **ABA UTBMS codes** — Uniform Task-Based Management System; the standard task/activity codes US legal e-billing requires.
- **LEDES 1998B** — the standard machine-readable invoice format for legal e-billing.
