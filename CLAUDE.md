# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

**Drop Cat Go Studio** is a unified AI video production app belonging to Andrew. It merges 5 separate tools (previously independent apps on different ports) into one FastAPI + vanilla JS web app. Single server on port 7860, no build step.

**Run it:** `launch.bat` (or `python app.py` directly) → http://127.0.0.1:7860

There are no tests, linting, or CI/CD configured. The app is tested manually through the UI.

---

## Architecture

```
app.py                  — FastAPI entry, lifespan, global routes, feature router registration
core/                   — Shared infrastructure (config, keys, logging, LLM, jobs, session, nsfw sanitizer, wildcards)
services/               — External service lifecycle (WanGP, ACE-Step, Forge, Ollama)
features/               — Feature modules, each with routes.py + domain logic
  fun_videos/           — Photo → AI video + music (WanGP + ACE-Step)
  video_bridges/        — AI transition clips between videos (WanGP, OpenCV fallback)
  sd_prompts/           — SD prompt generation + Forge integration + wildcard manager
  image2video/          — Ken Burns slideshow (pure ffmpeg, no AI)
  video_tools/          — Batch transforms + music mixer
static/                 — Vanilla JS frontend (ES modules, no framework, no build)
  js/shell/             — Cross-tab shell: gallery, presets, palette, shortcuts, toast, ai-intent
  js/components/        — Reusable components (region-editor for Forge Couple)
  js/tab-*.js           — Per-tab controllers, lazy-inited on first visit
```

### Critical pattern: circular import avoidance

`app.py` imports all feature routers at module level (lines 555-567). Features that need the LLM router or job manager **must use lazy getter functions**, never direct imports:

```python
# CORRECT — deferred to request time
from app import get_llm_router
llm_router = get_llm_router()

# WRONG — circular import at module load
from app import LLMRouter
```

The `sys.modules` fix at the top of `app.py` (line 12) ensures `from app import ...` and `from __main__ import ...` resolve to the same module object with shared `_g` globals dict.

### GPU job queue (`core/job_manager.py`)

Fun Videos and Bridges share a single GPU (WanGP). The job manager enforces sequential execution for GPU job types (`JOB_FUN_VIDEO`, `JOB_BRIDGE`) via a background worker thread with a deque. Non-GPU jobs (`JOB_I2V`, `JOB_VIDEO_TOOL`, `JOB_SD_PROMPT`) run immediately in their own threads.

Worker functions receive a `Job` object and must:
- Update `job.progress` (0-100) and `job.message` periodically
- Check `job.stop_event.is_set()` for cancellation
- Set `job.output` on success

GPU jobs have a configurable timeout (`gpu_job_timeout_seconds`, default 600s).

### LLM routing (`core/llm_router.py`)

All AI calls go through `LLMRouter.route()` or `LLMRouter.route_vision()`. The provider is read from config on each call (hot-switchable via Settings UI):
- **auto** (default): tries Anthropic key → OpenAI key → Ollama
- Three tiers: `TIER_FAST`, `TIER_BALANCED`, `TIER_POWER` — mapped to different models per provider
- Retry with exponential backoff; respects `Retry-After` on 429s; permanent errors fail immediately

### Config system (`core/config.py`)

Single `config.json` with 53+ namespaced keys (prefixes: `i2v_`, `fun_`, `bridge_`, `sd_`, `tools_`). Global keys shared across features. `DEFAULTS` dict is the canonical key registry — only keys present in `DEFAULTS` are accepted via the API.

Thread-safe via `RLock` (allows nested `load()` inside `save()`). File mtime caching avoids repeated disk reads. Type validation runs once on first load.

### Session tracking (`core/session.py`)

Every generated file is registered via `session.add_file()` so outputs from one tab appear in "From Session" pickers in other tabs. Sessions persist to `projects/{id}/session.json`. Capped at 200 files per session.

### Frontend (`static/js/`)

- **ES modules** loaded via `<script type="module">` — no bundler
- **Cache busting** — every import in `app.js` has `?v=YYYYMMDD[letter]` (e.g. `?v=20260419h`). Bump the letter whenever any module changes, or bulk-bump to a new day. All modules use the same stamp.
- **Tabs initialize lazily** — `app.js` calls each tab's `init(panel)` once on first visit, wrapped in try/catch with error banner
- **Job polling** — `api.js:pollJob()` polls `GET /api/jobs/{id}` every 1.5s with a max-poll safety cap (400 polls ≈ 10 min)
- **`session-updated` event** — dispatched by `pollJob` on job completion; session pickers auto-refresh if visible
- **`components.js`** — shared UI factory (`el()`, `toast()`, `createDropZone()`, `createSlider()`, etc.)
- **`handoff.js`** — cross-tab data passing (e.g., Fun Videos output → Bridges input)

### Shell layer (`static/js/shell/`)

Cross-cutting concerns owned by the shell, not per-tab:

- **`toast.js`** — global toast host + `apiFetch()` with error-log integration. Every fetch in shell/tab code should use `apiFetch()` so failures populate the error log.
- **`gallery.js`** — persistent cross-tab gallery. Pulls from `/api/gallery` (SQLite-backed). Tabs call `pushFromTab(tab, savedPath, prompt, seed, settings)` on generation success. Detail view has "Load Settings" (apply in-place) and "Branch & Tweak" (apply + jump to source tab). The gallery renders in `#split-gallery` inside `#gallery-overlay` — a full-screen overlay toggled by the Gallery header button. It is **never** a persistent side panel. Do not add a split-pane/side-column gallery back; it steals workspace.
- **`presets.js`** — save/load named preset bundles per tab. Backed by `/api/presets`. Presets surface in the command palette as "Preset: <name>". Save is Ctrl+S (uses native `prompt()` for name).
- **`command-palette.js`** — Ctrl+K. Fuzzy-matches registered items (tabs, actions, presets). If the active tab has an AI applier registered and the query doesn't match, shows `✦ Ask AI: "<query>"` as the last row. Empty palette surfaces last 5 AI queries as "Recent AI" for replay.
- **`shortcuts.js`** — global keyboard shortcut registry. Registered in `app.js` init. Respects input focus.
- **`ai-intent.js`** — palette-driven natural-language mutation. Each tab calls `registerTabAI(tabId, {getContext, applySettings})` at init time. Palette's Ask AI row calls `askAI(query)` which POSTs to `/api/ai-intent` and dispatches the result to the active tab's applier. Also exposes `applySettingsToTab()` for gallery "Load Settings".

### Smart wildcards (sd-prompts)

`/api/prompts/enhance` accepts `smart_wildcards: bool`. When true, the server passes the current wildcard catalog (inline + `sd_wildcards_dir/*.txt`) to the LLM so it can embed `__tokens__` in the composed prompt and optionally emit a `create_wildcards` JSON array to invent new ones when the user's idea explicitly asks. New wildcards are persisted flat to `sd_wildcards_dir/{name}.txt` with append+dedupe. System prompt enforces a STRICT TOKEN RULE — every emitted `__token__` must be in the catalog or in `create_wildcards`, because `wc_expand` leaves unknown tokens as literal text.

---

## External Services

| Service | Port | Purpose | Startup |
|---------|------|---------|---------|
| WanGP | 7899 | AI video generation | Set path in Settings → auto-starts |
| ACE-Step | 8019 | Music generation | Set path in Settings → auto-starts |
| Forge SD | 7861 | Stable Diffusion images | Must start separately with `--api` flag |
| Ollama | 11434 | Local LLM (prompt gen, vision) | Auto-started if `ollama` is on PATH |

Forge is at `C:\forge`. The app detects and attempts to auto-start it (injects `--api` flag). Services start in background daemon threads via `services/manager.py:startup_all()`, each wrapped in try/except with error logging.

---

## Config & Keys

- **Config:** `config.json` in project root (auto-created from `DEFAULTS`)
- **API keys precedence:** `config.json` (highest) → `C:\JSON Credentials\QB_WC_credentials.json` (fallback)
- **Key namespacing:** `i2v_*`, `fun_*`, `bridge_*`, `sd_*`, `tools_*`, plus globals

---

## Theme & Layout

**Circus theme** in `static/css/design-system.css`: dark crimson/gold palette (`#0d0606` bg, `#d4a017` gold, `#c41e3a` crimson, `#f0e6d0` cream text).

**Responsive breakpoints** prepared for Andrew's 49" ultrawide (5120x1440):
- `< 1100px` single column → `1100-1600px` sidebar + main → `> 2560px` 3-column with info panel → `> 4000px` ultrawide widths

---

## Local SQLite stores

- `gallery.db` — cross-tab generation history (url, prompt, model, seed, metadata JSON). Gitignored. Created on first POST to `/api/gallery`.
- `presets.db` — named setting bundles per tab. Gitignored. Created on first POST to `/api/presets`.

Schemas live inline in `app.py` via `CREATE TABLE IF NOT EXISTS`. No migration system; schema changes require deleting the file.

---

## Known Issues

1. **ffmpeg** must be on PATH — nearly all video features require it. The splash screen warns if missing.
2. **Forge** must be started separately with `--api` flag before SD Prompts image generation works. The watchdog in `services/manager.py` re-checks externally-launched Forge every 30s.
3. **WanGP first run** — model loading takes 2-3 minutes; splash screen shows "not running" until load completes. This is normal.
4. **First AI intent call** takes ~14s because Ollama cold-loads the model. Subsequent calls are ~3s. The palette shows a "Thinking…" spinner for the duration.

---

## Original Source Apps (reference, do not delete)

All at `C:\Users\andre\Desktop\AI Editors\`:
- `DropCat-Image-2-Video\` — Ken Burns (FastAPI)
- `DropCatGo-Fun-Videos_w_Audio\` — Photo→video+audio (FastAPI)
- `DropCatGo-SD-Prompts\` — SD prompts (Gradio)
- `DropCatGo-Video-BRIDGES\` — Transitions (FastAPI)
- `Github Video Editor\` — Infrastructure donor (LLM router, WanGP runtime, music mixer)
- `Video Reverser\` — Batch transforms (Tkinter)
