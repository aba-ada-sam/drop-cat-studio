# Validation needed -- V2 Music Video "Ratified Engine" wrapper (2026-08-05 night build)

Built by wrapping engine/chain.py as a subprocess (features/song_video/
chain_runner.py + chain_routes.py + a rewired tab-music-video.js). No server
start, no GPU process, no live render was exercised as part of this build --
per instruction, tonight's v16 render owned the GPU. See the INCIDENT section
at the top; everything else is the normal pre-live-test punch list.

## INCIDENT (read this first)

While writing this feature I ran a FastAPI TestClient smoke check
(`with TestClient(appmod.app) as c:`) against V2's app.py to verify the new
routes respond correctly. That entered the app's real ASGI lifespan, which
runs `services.manager.kill_orphans_at_startup()` ->
`_kill_stale_gpu_processes()`. That function does a machine-wide WMIC scan
for ANY process with `wangp_worker.py` in its command line and taskkills
it -- it is NOT scoped by port or by which DCS install (V1 vs V2) started it.

Evidence (see the message already sent to `main` for full detail): this
appears to have killed the V1 WanGP worker (port 7899) that was actively
serving tonight's v16 render. v16_render_log.txt stops dead at 23:19 with no
error, chain60_v16.mp4 was never written, chain.py (PID 24216) is still alive
but hung on a SYN_SENT to the now-dead 7899, and nothing is listening on 7899
anymore. The timing (my test ran at 23:19) lines up exactly.

**Before any future live test of this feature (or any FastAPI TestClient /
pytest run against this app.py) on a box where WanGP/chain.py might be
running under ANY DCS install:** set `DCS_NO_GPU_EVICT=1` in the environment
first. `kill_orphans_at_startup()` already has this exact escape hatch (and
a `PYTEST_CURRENT_TEST` auto-guard for real pytest runs) -- a bare
`python -c` / TestClient script doesn't set either, so it sailed through.
Consider also scoping `_kill_stale_gpu_processes()`'s WMIC match to the
process's own working directory / a specific port, not just a filename
substring -- as written it can kill a sibling install's worker regardless of
port, which is worse than the "orphan" case the function's own docstring
describes (it already documents killing a live DCMVS render twice on
2026-08-04 for a related reason -- this is a third variant of the same
class of bug, not a new kind of mistake).

## Punch list for tomorrow

1. **First smoke render.** Start V2's app.py normally (not via TestClient),
   confirm GPU is idle (V1 AND V2 -- see gap #3 below), upload a short song +
   one anchor image, click Start Ratified Render, verify:
   - `features/song_video/chain_runner.build_command()` produces a working
     invocation of engine/chain.py (validated statically against
     render_v16_detached.ps1's flags -- see below -- but never executed).
   - Progress panel actually updates (phase, clip i/N, take i/N, log tail)
     in near-real-time. The `-u` (unbuffered) flag on the python invocation
     is the fix for chain.py's `log=print` block-buffering under a pipe --
     confirm it actually produces line-by-line output rather than big
     delayed chunks.
   - The regex progress parser (`chain_runner._RE_CLIP_HEADER` /
     `_RE_SEED_LINE` / `_RE_CLIP_DONE`, chain_runner.py ~118-121) was tested
     against hand-copied strings from chain.py's source (see below), not
     against real captured stdout. Confirm real output matches -- clip_kind
     casing, spacing, the ETA suffix format, etc.
   - Final output plays back correctly at `/output/chain/chain_<id>.mp4` (or
     wherever the UI's `_outputPathToUrl()` resolves it).

2. **Cancel behavior.** Start a render, click Cancel mid-clip, confirm:
   - `chain_runner._kill_tree()`'s `taskkill /F /T /PID <pid>` actually
     kills chain.py AND any ffmpeg child it spawned (concat/mux), not just
     the python process.
   - The WanGP worker survives the cancel (only chain.py's process tree
     should die, not the worker on :7897) so a second render can start
     immediately after.
   - Status correctly lands on `cancelled`, not `error`.

3. **GPU busy-check gap (by design, needs a decision).** `chain_routes.py`'s
   `/start` only checks `core.gpu_orchestrator.gpu.is_wangp_rendering()`,
   which queries V2's OWN worker at :7897 (via
   `services.manager.WANGP_WORKER_PORT`). It has NO visibility into V1's
   worker on :7899. Since both share the same physical RTX 5080 16GB, a
   chain job started from V2 while V1 is mid-render (or vice versa) can
   still collide on VRAM even though each app's own orchestrator reports
   "idle". There is no existing cross-install GPU coordination mechanism to
   hook into. Needs a decision: a shared lock file? A port-agnostic "is
   ANYTHING using the GPU" check? Flagging rather than inventing one
   tonight.

4. **ACE-Step "generate" handoff.** `chain_runner._resolve_song_sync()`
   reuses `features/song_video/routes.py`'s no-song ACE-Step fallback
   (audio_generator.generate_audio) verbatim, plus an explicit
   `gpu.acquire("acestep", ...)` call the original route doesn't make (the
   original relies on `generate_audio`'s own direct `start_acestep()` call,
   which does NOT go through the orchestrator's eviction guard). Confirm
   live that: (a) generation actually produces a usable file, (b) the
   orchestrator handoff to "wangp" afterward doesn't double-evict or race,
   (c) a GPUBusyError during either acquire surfaces as a clean job "error"
   status in the UI rather than an unhandled crash.

5. **DOF finish is a stub, not a gap to silently work around.**
   `features/song_video/dof_finish.py` raises `DofFinishNotImplemented` --
   searched engine/ (`git log --all --oneline`, 11 commits) and review/
   (only render_v16_detached.ps1 exists there) for the ratified frame-exact
   mask-video + single-pass maskedmerge command; found only prose
   (LIPSYNC_LEDGER.md ~21:30 2026-08-05, RECIPE.json's "dof_finish" string)
   and the STATIC mask-image builder (scene_prep.py's
   `dof_mask_from_subject`), never the apply-to-video command itself. The
   UI's "Apply DOF finish" checkbox is present but disabled with a tooltip
   pointing here. Someone with access to that session's actual shell
   history (or Andrew re-deriving it fresh, human-judged) needs to recover
   the literal command before this can be implemented -- do not paper over
   it with an invented ffmpeg filter chain (explicit instruction, and the
   segmented trim+concat alternative is explicitly BANNED for the ~0.25s
   A/V drift it caused on v13/v15).

6. **Chain jobs are invisible to the Queue tab.** By design (chain.py talks
   to the WanGP worker directly over HTTP, bypassing
   `core/job_manager.py`'s GPU_JOB_TYPES queue entirely), a running chain
   job shows progress ONLY in the Music Video tab's own panel -- not in the
   rail's job feed or the Queue tab. Worth a UX pass once this path is
   proven: at minimum, register a lightweight non-GPU marker job in
   job_manager purely for Queue-tab visibility, without routing actual GPU
   work through it.

7. **Static command-shape check already done (no execution):**
   `chain_runner.build_command()` was verified programmatically (Python
   REPL, no subprocess launch) to produce, for 2 images + 2 scene prompts:
   ```
   python -u <engine>/chain.py --image <img0> --song <song> --output <out>
     --worker http://127.0.0.1:7897 --seeds-per-clip 4 --frames-per-clip 241
     --crossfade 0.15 --min-clip-frames 169 --smart-seams --judge-select
     --images <img0>,<img1> --scene-prompts "<promptA>||<promptB>"
   ```
   This matches review/render_v16_detached.ps1's ratified command byte-for-
   byte on every ratified flag, with `--worker` correctly pulled from
   `services.manager.WANGP_WORKER_PORT` (7897) rather than chain.py's own
   default (7899, which is V1's port). Still needs an actual live run to
   confirm chain.py accepts it end-to-end from this exact invocation
   context (cwd=engine/, `-u` flag, encoding="utf-8" errors="replace").

8. **RECIPE.json fallback path untested live.** `chain_runner._recipe_defaults()`
   prefers `features.song_video.recipe.load()` and falls back to literal
   ratified constants (241 / 0.15) on any exception. The success path was
   exercised (tests/test_recipe.py passes, 19/19, unchanged by this work);
   the fallback path (recipe.py raising) was not -- low risk since the
   values are hardcoded correctly either way, but worth a quick check that
   the warning log fires as expected if you want to confirm it.
