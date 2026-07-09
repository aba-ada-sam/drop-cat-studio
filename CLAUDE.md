# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

**Drop Cat Go Studio** is a unified AI video production app belonging to Andrew. It merges 5 separate tools into one FastAPI + vanilla JS web app. The server picks its own port (7860–7879), writes `.dcs-port`, and the browser reads that file. No build step.

**Run it:** `launch.bat` (or `python app.py` directly) → http://127.0.0.1:7860 (or whichever port was free)

**Design philosophy:** simpleton path first. The Express tab ("Create") is the zero-friction entry — drop image, describe idea, get video with AI music + lyrics. Advanced users can go deeper through the per-step tabs (Create Videos → Audio). Never add infrastructure complexity (service names, LLM provider controls) to the header or primary UI.

---

## Commands

```bash
# Run the app
python app.py

# Smoke tests (in-process FastAPI TestClient, no GPU/Ollama needed)
python tests/smoke.py

# Check JS for silent syntax errors (ES module SyntaxErrors kill all JS silently)
node --check static/js/app.js
node --check static/js/tab-fun-videos.js   # or any other module

# Check Python syntax
python -m py_compile features/fun_videos/routes.py
```

**Silent JS failure pattern:** If the splash shows raw HTML text ("Connecting to server..." not "Connecting…"), it means app.js never executed — an ES module import or syntax error killed the whole chain. Run `node --check` on every changed JS file. Common culprits: Unicode minus/dash characters instead of ASCII `-`, duplicate function declarations, bad import paths.

---

## Architecture

```
app.py                  — FastAPI entry, lifespan, global routes, feature router registration
core/                   — Shared infrastructure (config, keys, logging, LLM, jobs, session, nsfw sanitizer, wildcards)
services/               — External service lifecycle (WanGP, ACE-Step)
features/               — Feature modules, each with routes.py + domain logic
  fun_videos/           — Photo → AI video + music (WanGP + ACE-Step)
  video_bridges/        — AI transition clips between videos (WanGP, OpenCV fallback)
  image2video/          — Ken Burns slideshow (pure ffmpeg, no AI)
  video_tools/          — Batch transforms + music mixer
static/                 — Vanilla JS frontend (ES modules, no framework, no build)
  js/shell/             — Cross-tab shell: gallery, presets, palette, shortcuts, toast, ai-intent
  js/tab-*.js           — Per-tab controllers, lazy-inited on first visit
```

### Feature API prefixes

| Feature | Route prefix | GPU? |
|---------|-------------|------|
| Fun Videos | `/api/fun/*` | Yes (WanGP) |
| Video Bridges | `/api/bridges/*` | Yes (WanGP) |
| Image-to-Video | `/api/i2v/*` | No |
| Video Tools | `/api/tools/*` | AI upscale only (venv-upscale) |

### Video upscaling (`core/upscaler.py`)

- `POST /api/tools/upscale` -- batch upscale/optimize (UI: Video Tools > Upscale & Optimize). `GET` on the same path reports `{ai_available}` so the UI adapts silently -- never put install instructions in GUI text.
- Two methods: `ffmpeg` (lanczos, instant) and `ai` (Real-ESRGAN). `scale=1.0` = re-encode only (optimize file size).
- The AI pass runs as a **subprocess in `venv-upscale/`** (`tools/ai_upscale_frames.py`), a dedicated venv with CUDA torch 2.11.0+cu128 (mirrors Forge's proven combo; the app's own Python stays CPU-torch). Rebuild it with `tools/INSTALL_REALESRGAN.bat` -- which also re-applies the required basicsr patch (`degradations.py`: `functional_tensor` -> `functional`); a basicsr reinstall reverts that patch and AI silently falls back to lanczos.
- Perf reality (RTX 5080, 1080p -> 4K): ~0.8-1.0 s/frame; the bottleneck is PNG I/O, not the GPU (worker uses cv2 + PNG compression level 1 for this reason). AI mode suits short clips; a 4-min video is ~1.5 h and stages ~60-80 GB of frames in %TEMP%. Known future win: stream ffmpeg rawvideo pipes through the model instead of PNG round-trips.
- Model weights auto-download on first AI run; pin local copies in `models/RealESRGAN_x{2,4}plus.pth`.

### Critical pattern: circular import avoidance

`app.py` imports all feature routers at module level. Features that need the LLM router or job manager **must use lazy getter functions**, never direct imports:

```python
# CORRECT — deferred to request time
from app import get_llm_router
llm_router = get_llm_router()

# WRONG — circular import at module load
from app import LLMRouter
```

The `sys.modules` fix at the top of `app.py` ensures `from app import ...` and `from __main__ import ...` resolve to the same module object with shared `_g` globals dict.

### GPU job queue (`core/job_manager.py`)

Fun Videos and Bridges share a single GPU (WanGP). The job manager enforces sequential execution for GPU job types (`JOB_FUN_VIDEO`, `JOB_BRIDGE`) via a background worker thread with a deque. Non-GPU jobs (`JOB_I2V`, `JOB_VIDEO_TOOL`, `JOB_SD_PROMPT`) run immediately in their own threads.

Worker functions receive a `Job` object and must:
- Update `job.progress` (0-100) and `job.message` periodically
- Check `job.stop_event.is_set()` for cancellation
- Set `job.output` on success

```python
def my_worker(job: Job, input_path, param):
    job.update(status="running", progress=10, message="Starting…")
    for step in work_steps:
        if job.stop_event.is_set():
            return
        job.update(progress=step_pct, message=step_label)
    job.update(status="done", progress=100, output=output_path)
```

GPU jobs have a configurable timeout (`gpu_job_timeout_seconds`, default 600s). Between GPU jobs, `gc.collect()` + `torch.cuda.empty_cache()` free VRAM.

**`submit_with_prep` pattern** — multi-clip jobs split across two functions:

```python
job_manager.submit_with_prep(
    JOB_FUN_MULTI_VIDEO, run_multi_prep, run_multi_pipeline,
    photo_path, settings, label=label, timeout_seconds=timeout,
)
```

`run_multi_prep(job, photo_path, settings)` runs immediately in a background thread with **no GPU lock** — it does LLM calls (story arc, music direction, lyrics), and optionally ACE-Step audio-first generation. Results are written into `settings` with `_` prefixes (`_story_arc`, `_prepped_music_prompt`, `_prepped_lyrics`). When prep finishes, the job is automatically queued for the GPU phase.

`run_multi_pipeline(job, photo_path, settings)` is the GPU-locked worker — it pops `_`-prefixed keys from settings and skips any work that prep already did.

**Job timeout grace period:** When a GPU job times out, `stop_event.set()` is called and the job manager waits 30s for the thread to exit naturally. If still alive, WanGP is restarted (to force-close the blocked HTTP connection to `/generate`), then waits 15s more. If still alive, the next job starts anyway — risking VRAM contention. This is the expected path when WanGP deadlocks mid-step.

### LLM routing (`core/llm_router.py`)

All AI calls go through `LLMRouter.route()` or `LLMRouter.route_vision()`. The provider is read from config on each call (hot-switchable via Settings UI):
- Providers: `anthropic`, `openai`, `featherless` (uncensored cloud), `kobold` (uncensored local KoboldCpp), `auto`. Legacy `ollama` is aliased to `featherless` (Ollama was removed 2026-07-05).
- **auto**: tries Anthropic key → OpenAI key → (if `allow_uncensored_fallback`) the uncensored provider
- **Uncensored providers** (`featherless`/`kobold`) do their own vision and never refuse NSFW. `core/llm_router.UNCENSORED_PROVIDERS` + `is_uncensored(p)` are the canonical test — import them instead of hardcoding a provider name.
- Three tiers: `TIER_FAST = "fast"`, `TIER_BALANCED = "balanced"`, `TIER_POWER = "power"` — always pass the constant, never its name as a string literal
- Retry with exponential backoff; respects `Retry-After` on 429s; permanent errors fail immediately
- The uncensored backend is **off the GPU orchestrator**: Featherless is cloud, and a local KoboldCpp is an external server DCS doesn't manage — so LLM calls never evict WanGP/Forge/ACE-Step.

`core/llm_client.py` also exports `parse_json_response(text)` — strips markdown code fences and extracts the outermost JSON object/array from an LLM response. Use this instead of writing raw `re.search` for JSON extraction.

### Config system (`core/config.py`)

Single `config.json` with 53+ namespaced keys (prefixes: `i2v_`, `fun_`, `bridge_`, `sd_`, `tools_`). Global keys shared across features. `DEFAULTS` dict is the canonical key registry — only keys present in `DEFAULTS` are accepted via the API.

Thread-safe via `RLock` (allows nested `load()` inside `save()`). File mtime caching avoids repeated disk reads.

### Session tracking (`core/session.py`)

Every generated file is registered via `session.add_file()` so outputs from one tab appear in "From Session" pickers in other tabs. Sessions persist to `projects/{id}/session.json`. Capped at 200 files per session.

### Frontend (`static/js/`)

- **ES modules** loaded via `<script type="module">` — no bundler
- **Cache busting** — every import in `app.js` has `?v=YYYYMMDD[letter]` (e.g. `?v=20260419h`). Bump the letter whenever any module changes, or bulk-bump to a new day. **All modules must use the same stamp** — if `app.js` imports `tab-express.js?v=20260419h` but `tab-express.js` was updated to `?v=20260419j`, the browser loads the old `app.js` whose import still points to the old stamp, and the new file is never fetched.
- **Tabs initialize lazily** — `app.js` calls each tab's `init(panel)` once on first visit, wrapped in try/catch with error banner
- **Job polling** — `api.js:pollJob()` polls `GET /api/jobs/{id}` every 1.5s with a max-poll safety cap (400 polls ≈ 10 min)
- **`session-updated` event** — dispatched by `pollJob` on job completion; session pickers auto-refresh if visible
- **`components.js`** — shared UI factory (`el()`, `toast()`, `createDropZone()`, `createSlider()`, etc.)
- **`handoff.js`** — cross-tab data passing (e.g., Fun Videos output → Bridges input)

### Express tab (`tab-express.js`) — inline polling model

The Express tab polls its own job and renders progress + output inline. Non-loop single-run jobs must follow this pattern: show a progress bar on submit, call `pollJob()`, update the bar on each tick, render the video player on done, show the error in red on failure. **Do not fire-and-forget.** The user has no other place to see what is happening unless the Queue tab is open.

### Shell layer (`static/js/shell/`)

Cross-cutting concerns owned by the shell, not per-tab:

- **`toast.js`** — global toast host + `apiFetch()` with error-log integration. Every fetch in shell/tab code should use `apiFetch()` so failures populate the error log.
- **`gallery.js`** — persistent cross-tab gallery. Pulls from `/api/gallery` (SQLite-backed). Tabs call `pushFromTab(tab, savedPath, prompt, seed, settings)` on generation success. Detail view has "Load Settings" (apply in-place) and "Branch & Tweak" (apply + jump to source tab). The gallery renders in `#split-gallery` inside `#gallery-overlay` — a full-screen overlay toggled by the **Gallery** rail button (bottom of left rail). It is **never** a persistent side panel. Do not add a split-pane/side-column gallery back; it steals workspace.
- **`presets.js`** — save/load named preset bundles per tab. Backed by `/api/presets`. Presets surface in the command palette as "Preset: <name>". Save is Ctrl+S (uses native `prompt()` for name).
- **`command-palette.js`** — Ctrl+K. Fuzzy-matches registered items (tabs, actions, presets). If the active tab has an AI applier registered and the query doesn't match, shows `✦ Ask AI: "<query>"` as the last row. Empty palette surfaces last 5 AI queries as "Recent AI" for replay.
- **`shortcuts.js`** — global keyboard shortcut registry. Registered in `app.js` init. Respects input focus.
- **`ai-intent.js`** — palette-driven natural-language mutation. Each tab calls `registerTabAI(tabId, {getContext, applySettings})` at init time. Palette's Ask AI row calls `askAI(query)` which POSTs to `/api/ai-intent` and dispatches the result to the active tab's applier. Also exposes `applySettingsToTab()` for gallery "Load Settings".

### Header (`static/index.html` — `#app-header`)

The header contains three zones:
- **Left:** logo + app name
- **Center:** the **AI Manager** bar (`#manager`) — see below. (The Text/Image/Sound/Video AI pills that used to live here were relocated to `#pills-dock`, a slim centered bar just above the activity-log footer. The pills are wired purely by element id, so they kept working after the move.)
- **Right:** `#ai-badge` (shows effective AI provider: "✦ AI: Anthropic" / "✦ AI: Local" / "✦ AI") + Settings gear.

**Do not add provider-switch controls to the header.** LLM provider selection lives in Settings only. The badge is read-only status + click-to-configure.

### AI Manager (`static/js/shell/manager.js` + `features/manager/routes.py`)

An autonomous in-app agent that operates the app FOR the user. Type a plain-English goal in the header bar; the Manager drives the **real UI** to accomplish it — navigating tabs, filling controls, clicking buttons, watching the queue — narrating as it goes. Default behaviour is fully autonomous (no per-step confirmation; a **Stop** button is the only brake), per Andrew's request.

**The browser owns the agent loop; the server is a thin LLM proxy.** Each step `manager.js` snapshots the live screen (`readScreen()` — tags every visible interactable element with a transient `data-mgr-ref`, captures controls/buttons/visible-text), POSTs `{goal, screen, history, chat}` to `/api/manager/think`, and gets back the single next action. It executes that action against the DOM, re-snapshots, and loops (cap `MAX_STEPS = 48`; identical-action-3× guard).

Action vocabulary (LLM emits `{"thought","action":{...}}`): `navigate` (clicks a rail button by `data-tab`, or `#btn-gallery-rail`), `read_screen`, `set_field` (ref + value; handles input/textarea/select/checkbox/radio/range; file pickers can't be set — it tells the user to drop the file), `click` (ref), `say` (narrate, loop continues), `ask` (pause for the user — resolved via a Promise wired to `#mgr-ask`), `done`.

- **Brain:** `route(..., tier=TIER_BALANCED, force_provider="anthropic")` with graceful fallback to the configured provider if the Anthropic key is missing/fails. The system prompt embeds `APP_MAP` (tab ids + what each does) so the agent knows where to go; the live screen read supplies the actual controls.
- **No DOM control on the server** — keeping clicks client-side means the user watches real navigation/clicks, and it works regardless of how a tab built its controls (custom chip `<button>`s are introspected like any other button).
- Conversation persists to `localStorage['dropcat_manager_chat']` (last 24 msgs) for continuity across goals.
- Route registered under `/api/manager/*`. Cache stamp for `manager.js` import bumps with the rest.

### Multi-clip pipeline (`features/fun_videos/multi_pipeline.py`)

The multi-clip pipeline has three internal phases within `run_multi_pipeline`:

**Phase 0 — Audio-first (conditional):** Generates ACE-Step music *before* clips so clip boundaries can snap to beats. Skipped if `gpu.current == "wangp"` — evicting a warm WanGP worker to run ACE-Step then reloading it costs 3-8 minutes with no benefit. Only runs when WanGP would cold-start anyway (first job of session or ACE-Step was already the GPU holder). If Phase 0 fails for any reason, falls back to post-clip audio transparently.

**Phase 1 — Clip generation:** N sequential WanGP clips, each starting from the last frame of the previous (`_chain_anchor` extracts the PNG). Re-anchoring periodically resets the chain start to the source photo to break compounding drift: LTX defaults to every 2 clips, Wan every 3, user-configurable via `reanchor_every` in settings (`0` = never). Scene-hold mode always sets `reanchor_every=0` because re-anchoring a static shot triggers LTX's background-fill heuristics, producing rain/debris artifacts.

**Scene-hold extension (LTX + calm only):** When `is_ltx and resolved_style == "calm"`, `_generate_story_arc` bypasses the LLM and returns N clips with varied per-clip environmental effects (light shift, steam, shadow creep, curtain stir, etc.) from a hardcoded list. `"gentle"` motion style was removed — it produced invisible motion at 8 steps. Any non-calm style now goes through the full LLM story arc path.

**Auto-pick model routing** (`features/fun_videos/routes.py:_get_pick_to_model`): Express auto-pick uses LTX-2 Dev13B for action/action_hd/story_action buckets (40 steps, deliberate physical motion, 10GB VRAM min) and LTX-2 Distilled for calm/long_story (8 steps, scene-hold, 10GB min). Wan I2V is NOT in auto-pick — confirmed deadlock on RTX 5080 (15.9GB): WanGP caps budget at 80% = 13GB, Wan I2V needs 15.87GB, first denoising step exceeds budget and hangs indefinitely. `vram_min_gb` set to 20 for Wan I2V 480P, 24 for 720P.

**Phase 3 — Audio:** If Phase 0 already produced audio, uses it directly. Otherwise calls `gpu.acquire("acestep")` to evict WanGP and generate post-clip. Always computes `total_dur = probe_duration(concat_path)` before both branches so gallery metadata has the correct duration.

<!-- Infinite Zoom feature removed 2026-07-05 along with Forge (it depended on
Forge img2img). Image generation is done in Forge's own GUI now, outside DCS. -->

### Audio analyzer (`features/fun_videos/audio_analyzer.py`)

Audio analysis for lyric/energy context (NOT timing). Key functions:
- `transcribe_audio(path)` — faster-whisper tiny/int8 on CPU, returns `[{start, end, text}]`
- `detect_audio_events(path)` — librosa: returns `{bpm, beat_times, energy_peaks, sections, duration}`. Energy peaks filtered to 75th percentile, min 2s apart.
- `build_clip_audio_context(...)` — per-clip lyric/energy context dicts for prompt refinement.

Beat-sync was removed entirely (2026-05-28): no clip-timing warping, no boundary snapping, no post-generation retime UI/`/api/sync`. Clips generate at their natural timing. Lip sync is independent (audio WAV passed to LTX-2 as conditioning) and unaffected. Do not reintroduce beat-snapping or speed-ramp-to-beat code.

## External Services

| Service | Port | Purpose | Startup |
|---------|------|---------|---------|
| WanGP | 7899 | AI video generation | Set path in Settings → auto-starts |
| ACE-Step | 8020 | Music generation | Deferred — only starts when music is needed (keeps VRAM free) |

Services start in background daemon threads via `services/manager.py:startup_all()`, each wrapped in try/except with error logging. ACE-Step is deferred to avoid VRAM contention with WanGP. (Image generation — Forge — and the LLM/vision backend — Featherless/KoboldCpp — are **not** DCS-managed services: Forge was removed 2026-07-05 and is used via its own GUI; the uncensored LLM is cloud or an external local KoboldCpp.)

### WanGP worker (`services/wangp_worker.py`)

The worker runs as a persistent subprocess on port 7899. DCS communicates via HTTP (`POST /generate`, `GET /status`, `GET /health`). Key behaviour:

- **After DCS restarts**, the old worker stays alive on 7899 but DCS loses its stdout pipe. The `[wangp-worker]` log lines only appear while DCS owns the pipe from the original `subprocess.Popen`. Restart the worker via `POST /api/services/restart/wangp` to get a fresh pipe and fresh error capture.
- **Error capture:** `process_tasks_cli` in WanGP returns `False` when `generate_video` raises internally. The actual error is printed to worker stdout but only visible if the drain thread is active. The worker monkey-patches `builtins.print` during `process_tasks_cli` to capture `[ERROR]` / traceback lines and exposes them in the `/status` error field — so the real WanGP error propagates back to the job failure message.
- **SAFE_DEFAULTS** in `core/wangp_models.py` is the single source of truth for required WanGP input keys. When WanGP is updated (Pinokio pulls), new keys may appear in `models/_settings.json`; add them to `SAFE_DEFAULTS` if WanGP raises `KeyError` on them.
- **VRAM profile** in `C:\pinokio\api\wan.git\app\wgp_config.json`: keep `profile / video_profile / image_profile = 3` (LowRAM_HighVRAM_Medium). Profile 3 is confirmed fast for **LTX-2** (~1.6s/step on RTX 5080). For **Wan I2V 14B**, profile 3 does NOT prevent the Step 0 deadlock on 15.9 GB cards -- WanGP caps its budget at 80% of VRAM (13 GB) and Wan I2V's working memory during the first denoising step exceeds that limit regardless of profile. Wan I2V requires 20 GB+ VRAM to run reliably. Profile 4 is correct for 6-8 GB cards only.
- **`compile: ""` (off)** in `wgp_config.json`. Empirically tested 2026-05-11 on LTX-2 2.0 Distilled fp8 with profile 3 (model fully resident, no streaming -- the precondition that made it safe to try). Result: compile DID work (no hang, no crash), but the actual speedup was ~12% per step (1.7s -> 1.5s) instead of the documented 20-40%, AND the first clip of each worker session paid ~100s of compile warmup. The torch.fx logs showed several `[12/N_1]` symbolic-shape recompile guards firing during warmup, meaning the dynamic input shapes (variable frame counts, image dimensions) prevent a clean compile -- inductor keeps re-tracing. Math: break-even at 3+ clips per worker session, net-negative for 1-clip jobs. Not worth the UX hit for typical use. **If a future LTX model has more static shapes (or torch.compile gets a fix for dynamic recompiles on Blackwell), re-test by flipping to `"transformer"` and watching for the same `[N/M_1]` recompile guards in the log.**
- **`vae_config: 1`** in the same file: forces the largest VAE tile size (256/32) which is designed for >= 24GB cards but works on 16GB after int8 quantization frees up headroom. Saves ~5-8s of VAE decode per clip. If WanGP OOMs during the decode phase, drop back to `0` (auto by VRAM) or `2` (medium tiles).
- **`attention_mode: "auto"`** auto-picks sage2 on sm_120 (RTX 5080). Do NOT switch to `xformers` or `sdpa` -- both are slower. Don't enable `sage3` (manual install, quality risk).

### GPU orchestrator (`core/gpu_orchestrator.py`)

Single coordinator for which service owns the GPU at any moment. WanGP (8-13GB) and ACE-Step (6-8GB) cannot coexist on 16GB VRAM; loading two at once forces one into CPU offloading mode (catastrophic slowdown). (Image gen and the LLM are no longer GPU services here — see the services note above.)

Usage from pipelines:
```python
from core.gpu_orchestrator import gpu
gpu.acquire("wangp", reason="multi-clip 5 clips")    # evicts anything else, ensures wangp alive
# ... do GPU work ...
gpu.acquire("acestep", reason="music gen")           # evicts wangp, starts acestep
```

The orchestrator owns eviction policy: WanGP/ACE-Step are killed via `stop_service`. A held service stays loaded across same-service calls -- only different-service `acquire` triggers eviction.

Endpoints: `GET /api/gpu/status` returns `{current, history[]}`. `POST /api/gpu/release` force-evicts everything.

The pre-orchestrator pattern of scattered `unload_checkpoint()` + `stop_service("acestep")` + `start_acestep` calls in each pipeline has been removed. Don't add them back; route through the orchestrator.

**Startup registration:** After `start_wangp_worker()` succeeds at boot, `startup_all()` sets `gpu._current = "wangp"` directly so the orchestrator knows WanGP owns the GPU. Without this, `gpu.current` stays `None` despite WanGP being loaded, and the first multi-clip job's Phase 0 audio-first would evict the freshly-loaded worker and force a reload.

**Watchdog deadlock recovery:** When the stuck-step watchdog fires (`_WANGP_STUCK_SECS = 300`), it calls `_kill_by_port(WANGP_WORKER_PORT, wait_release=True)` **before** `gpu.acquire("wangp")`. This is necessary because `_wangp_worker_proc` may point to a newer process started by a previous recovery attempt, leaving the original stuck process (which holds VRAM) unkilled. Port-kill catches all processes on 7899 regardless of which one the reference tracks.

**OPERATIONAL RULE -- DO NOT RESTART WANGP MID-SESSION.** Restarting `/api/services/restart/wangp` while a user job is in flight kills the worker the job is talking to; DCS-side polling never realizes its request was orphaned and the job sits at "Step 0/8" forever. Config changes that require a WanGP restart (compile, profile, vae_config in `wgp_config.json`) should be staged and applied between sessions, not while the user has jobs queued or running. The `compile` experiment on 2026-05-11 made this lesson very expensive in real time.

### Per-model step floors

`/api/fun/make-it` and `/api/fun/make-it-multi` apply a server-side floor on `steps` AFTER auto-pick has chosen the actual model. The UI slider value was tuned for whatever model was visible in the dropdown, but auto-pick can swap models, so the floor protects against e.g. sending 4 steps to Wan I2V (which produces a blob below 20). See `_MODEL_MIN_STEPS_SINGLE` / `_MODEL_MIN_STEPS` in `features/fun_videos/routes.py`. Bumps are logged as `[make-it] step floor: ui=4 -> 20 for Wan2.1-I2V-14B-480P`.

### The uncensored provider is opt-in (not the auto fallback)

`llm_router._provider()` in `auto` mode resolves Anthropic -> OpenAI -> (opt-in) uncensored provider -> error. Featherless/KoboldCpp are NEVER chosen automatically. The user must either explicitly set `llm_provider = "featherless"`/`"kobold"` in Settings, or check **Allow the uncensored provider as a fallback** (`allow_uncensored_fallback = true`; target set by `uncensored_provider`). The fallback flag only kicks in when no Anthropic/OpenAI key is configured.

NSFW-safe vision: `route_vision()` runs on the configured provider, but when Anthropic/OpenAI return a content-policy refusal on an image, the router transparently retries the SAME request on the uncensored provider (`uncensored_provider`, default Featherless) if it is available. So explicit/artistic photos still get analysed without making the uncensored provider the default for SFW work.

### `_resolve_path` caveat (`features/fun_videos/routes.py`)

`_resolve_path(raw)` only resolves URL-style paths starting with `/output/...` into absolute filesystem paths. It does **not** handle `/uploads/...` paths. The upload endpoint returns the absolute filesystem path in `f.path` — always send that absolute path (not the URL) as `photo_path` in make-it requests.

---

## Config & Keys

- **Config:** `config.json` in project root (auto-created from `DEFAULTS`)
- **API keys precedence:** `config.json` (highest) → `C:\JSON Credentials\QB_WC_credentials.json` (fallback)
- **Key lookup aliases in credentials file:** `anthropic_key` or `anthropic_api_key`; `openai_key`, `openai_api_key`, or `open_ai_key`
- **Default provider:** `"auto"` — resolves to Anthropic if key set, else OpenAI if key set, else Ollama
- **Key namespacing:** `i2v_*`, `fun_*`, `bridge_*`, `sd_*`, `tools_*`, plus globals

---

## Theme & Layout

**Circus theme** in `static/css/design-system.css`: dark crimson/gold palette (`#0d0606` bg, `#d4a017` gold, `#c41e3a` crimson, `#f0e6d0` cream text).

Fix CSS directly — never build theme-switching UI or provider-switch controls in the header.

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
2. **WanGP first run** — model loading takes 2-3 minutes; splash screen shows "not running" until load completes. This is normal.
3. **First AI intent call** may take a few seconds while the uncensored/cloud LLM responds. The palette shows a "Thinking…" spinner for the duration.
5. **`launch.bat` `%~dp0` trailing backslash** — `%~dp0` expands to `C:\DropCat-Studio\` (trailing `\`). Wrapping it in quotes as `"%~dp0"` makes `\"` an escaped quote, breaking the argument. Strip it: `set "_X=%~dp0"` then `if "%_X:~-1%"=="\" set "_X=%_X:~0,-1%"` before using in commands like `git -C`.

---

## Original Source Apps (reference, do not delete)

All at `C:\Users\andre\Desktop\AI Editors\`:
- `DropCat-Image-2-Video\` — Ken Burns (FastAPI)
- `DropCatGo-Fun-Videos_w_Audio\` — Photo→video+audio (FastAPI)
- `DropCatGo-SD-Prompts\` — SD prompts (Gradio)
- `DropCatGo-Video-BRIDGES\` — Transitions (FastAPI)
- `Github Video Editor\` — Infrastructure donor (LLM router, WanGP runtime, music mixer)
- `Video Reverser\` — Batch transforms (Tkinter)
