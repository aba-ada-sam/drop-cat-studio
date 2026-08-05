# Session Handoff -- helper (Studio board role) -- 2026-08-04, late night

Andrew flagged my context filling; stopping per lipsync's standing instruction
before attempting either bug fix. Everything below is pushed to origin/master
(verified: 0 unpushed commits at handoff time). Claim `helper` on the Studio
board and continue as this role.

## What this lane did tonight: the Tier-2 lip-sync port

Ported the proven lip-sync recipe (session scratchpad tooling) into DCS proper
as real, tested code, then wired it into the live render path. Full commit
chain, oldest first:

```
555236f  frame-grid math + energy-vs-label window gate (sing_grid.py, window_energy.py)
b3615a4  ledger: findings from the port (frame rules absent in DCS, 2-source drift)
87008b5  fix red-team findings in the port itself (1 HIGH, 5 MEDIUM)
03ee212  ledger: the mean-vs-fraction reversal (window_energy's own red-team HIGH)
2e3aa5b  WIRE the recipe into pipeline.py + hard-fail the 6 raw-mix fallback paths
6f16648  fix a regression I introduced: lip_sync defaults True, don't crash default jobs
5500721  fix the WIRED path's red-team findings (3 HIGH, 3 MEDIUM)
68632d4  ledger: FIXED5 v3 delivered (lipsync's work, not mine, landed same window)
fb87138  ledger: "an integration needs its own adversarial pass" (the wiring lesson)
555f824  drive _pick_best_seed for real against fixtures (closed the "never executed" gap)
74a579c  SYNC-OR-DIE acceptance + 60s admin / 30s user tiers (product pivot)
e199399  surface truncation when sync-or-die shortens a delivered video
```

Also on a SEPARATE repo/branch, pushed: `aba-ada-sam/dropcatgo-generator`
branch `sing-continuity-2026-08-04` (commit `0bac02e`) -- named invites +
private continuity memory, unrelated feature, different assignment earlier
tonight. Not part of this lane's story but flagging so it isn't lost: NOT
merged to master, deploy manifest is on the DropCat board (world=dropcat),
posted ~21:06.

## PROVEN vs ASSUMED

**Proven** (real fixtures, real audio, or a real partial live run):
- artifact_screens.py's ribbon/red detectors reproduce the eye-calibrated
  numbers from the session scratchpad EXACTLY (ribbon known-bad p95 1.067 vs
  recorded 1.07; red known-bad 0.157 vs 0.157; the red false-positive is
  pinned as a test on purpose).
- window_energy.py catches the real 4 mislabelled "interlude" windows
  (-29.6 to -32.0 dBFS) off the actual stem_vocals_hp.wav, and does NOT
  false-positive on the real intro/outro.
- sing_grid.py's frame math: exhaustive sweep 17..12000 frames, zero
  invariant violations, after two rounds of red-team-found overflow bugs
  were fixed.
- **The wiring itself partially ran LIVE tonight** (job e73c61802173, stopped
  early to hand the GPU to lipsync's DCMVS run, NOT a bug): confirmed log
  line `[song-video] Lip sync: conditioning on isolated vocals
  (guide_vocals_gated.wav)` -- so isolation, high-pass, and phrase-gating all
  ran for real, returned the gated stem (not raw mix), through the rewritten
  hard-fail function including its new tuple return. Also confirmed
  "pre-extracting 20 audio slices" and "best-of-2 seed selection ON" fired.
  **NOT yet proven live**: window-energy disagreements on a real plan, the
  artifact screens inside `_pick_best_seed`, the SYNC-OR-DIE path, trim/
  concat/mux, or a tier config end to end. This is the single most valuable
  next step -- see "What I'd do next".

**Assumed / fixture-only** (no live GPU exercise):
- SYNC-OR-DIE (`SyncFloorNotMet`, `require_sync`) -- driven against known-
  verdict fixture mp4s via a fake `gen_fn`, never against a real WanGP take.
- The 60s/30s tier configs -- their arithmetic is proven (import-time
  assertions + tests/test_tiers.py), but no job has ever actually run at
  either shape.
- The truncation-visibility fix (`job.meta["truncated_reason"]`) -- plumbing
  only, never observed firing on a real job.

Four rule-6 passes tonight found real HIGH-severity bugs in supposedly-tested
code (see LIPSYNC_LEDGER.md 2026-08-04 section, especially the "statistic is
the whole rule" and "integration needs its own adversarial pass" entries).
Read the ledger before trusting anything here at face value -- that is the
whole reason it exists.

## BUG A -- non-lipsync song jobs deadlock on this card (NOT INVESTIGATED, mine to fix, unclaimed work)

Reported by lipsync from Andrew's own studio log, 21:09-21:10:
```
[song-video] effective render params: 1032x580 ... lip_sync=False
Step 0/8   (repeating every ~2s, forever)
```
This is the classic step-0 VRAM deadlock. 1032x580 ceil-rounds to 1088x640 at
the LTX-2 64-multiple boundary (33% more pixels than 960x544 -> 960x512,
which is proven to fit and sync), and 1088x640 does not fit this card's VRAM.
This is EXACTLY the failure the wangp_worker.py refusal message I wrote
tonight (commit 5500721) already predicts in its error text -- I just never
noticed the plain (non-lip-sync) path never got the resolution clamp the
lip_sync path forces.

**Where to look**: `features/song_video/pipeline.py`, search for where
resolution is resolved before the render call (`tw, th` / `resolution` /
`"effective render params"` -- that log line is your anchor). The lip_sync
branch forces 960x544 somewhere near `_want_face_framing` / the isolation
call; the plain branch does not go through that. I have NOT located the exact
line -- ran out of context before tracing it.

**Fix direction** (lipsync's framing, agree with it): derive a max-safe
resolution from detected VRAM for ALL song-video paths, not just lip_sync,
and log the clamp so a future 1032x580-style request never silently reaches
the worker at all.

**Repro**: submit a song-video job with `lip_sync` absent/false at default
resolution on this card; watch `logs/dropcat.log` for the `effective render
params` line and Step 0/8 not advancing.

## BUG B -- MY test suite kills in-flight renders (NOT INVESTIGATED past the grep below, mine to fix)

Worse than A: **this is actively dangerous to run on a shared GPU box.**
`tests/smoke.py` boots the real FastAPI app in-process (documented in its own
header). App startup runs orphan-GPU-worker eviction unconditionally:

```
services/manager.py:756:    log.info("Evicting any orphan GPU workers from prior session...")
```
(followed by "Killing frozen WanGP on port 7899" nearby -- I grepped the log
line, not yet the kill logic itself.)

lipsync's log shows this firing at 20:33/20:46/20:50/21:02/21:07/21:09
tonight -- every time I ran `python tests/smoke.py` (or any of my six new
suites, if they share the app-boot fixture) to verify a change. The 21:02
firing killed their DCMVS render mid-clip. **I ran this suite repeatedly all
night without knowing it did this** -- I was treating it as a safe, isolated
CPU-only check because my OWN new suites (test_sing_grid.py etc.) genuinely
are pure-Python with no app boot. I did not check whether `smoke.py` shares
that property before running it against a box someone else's GPU work was
live on. That is the mistake, plainly, not a code defect I introduced --
`services/manager.py:756`'s eviction-on-boot predates tonight.

**Where to look**: `services/manager.py` around line 756 and whatever calls
it at app startup (search for where `Killing frozen WanGP` fires, and
whatever function contains `evict` in that file). Then `tests/smoke.py`'s
FastAPI TestClient setup (top of the file, per its own header comment) to
confirm it really does trigger a full app boot including this path.

**Fix direction** (lipsync's framing, agree with it):
1. Startup eviction must not run when the app is under test -- an env flag
   TestClient setup can set (`smoke.py`'s fixture is the natural place).
2. Independent of (1): eviction must never kill a worker that is ACTIVELY
   serving a job -- check the GPU lock/queue state before evicting, evict
   only true orphans (no owning job, no recent activity). This second half
   matters even outside tests -- any DCS restart during someone else's render
   has the same exposure today.
Both need a regression test once fixed.

**Until this is fixed**: do not run `tests/smoke.py` (or confirm whether any
of my six new suites import anything that boots the app -- I believe they
don't, they're pure functions + subprocess ffmpeg, but VERIFY before trusting
that belief) while a GPU render might be in flight. Check `gpu_queue_length`
via `/api/jobs` or ask on the board first.

## What I'd do next, in order

1. **Fix Bug B first** -- it is actively unsafe to keep testing anything in
   this repo (mine or otherwise) until it's fixed, and it's the smaller of
   the two.
2. **Fix Bug A** -- trace the resolution-resolution code path in
   `_do_song_gpu_phase` / wherever `tw, th` get set before `_gen_one`, compare
   against how the lip_sync branch forces 960x544, apply the same clamp
   unconditionally, log it.
3. **The live E2E, now genuinely worth doing**: once A and B are fixed, run
   ONE real job at the `user` tier (5 clips, 30s, smaller GPU budget than
   admin) through `features/song_video/routes.py` `/generate` with
   `lip_sync=true` explicit (route defaults it false -- see tiers.py's own
   comment on why `job_payload()` sets it explicitly). Watch for: window-
   energy disagreement lines actually firing, a ribbon screen actually
   rejecting or accepting a take, and -- the one that matters most --
   whether `SyncFloorNotMet` ever actually fires on a real hard window
   (if it never does in practice, the floor of 0.12 may be too permissive;
   if it fires on nearly everything, too strict -- only a real run tells you
   which).
4. Inputs for that run: `C:\DropCat-Studio\uploads\awm_alien_source.png` +
   any `*_Adam_Friends.mpeg` under the same uploads folder (210s song, tier
   code only uses the first 30s/60s of it via `clip_duration`/`num_clips`
   overrides in `tiers.job_payload`).

## Where things live

- Fixture mp4s used for calibration all night:
  `C:\Users\andre\AppData\Local\Temp\claude\C--Users-andre\b0293762-ce80-418c-84f3-73221223aaf5\scratchpad\`
  (this is the SIBLING session's scratchpad, not mine -- it may not survive
  past their session; if these tests start failing with "SKIP: fixture
  missing", that's why, not a code regression).
- Six new test suites, all `python tests/test_NAME.py`, no GPU: 
  `test_artifact_screens.py`, `test_sing_grid.py`, `test_window_energy.py`,
  `test_guide_hardfail.py`, `test_pick_best_seed.py`, `test_tiers.py`.
  120 assertions total, all green as of `e199399`.
- `LIPSYNC_LEDGER.md` is the spec of record -- read the CURRENT RECIPE section
  and the full 2026-08-04 dated section before changing anything in
  `features/song_video/`.
- Studio board: `C:\Users\andre\StudioTeam\board.jsonl`,
  `CLAUDETEAM_WORLD=studio`. lipsync (sid b0293762) is senior/owns
  Andrew-facing deliverables; I report as `helper`.

## Verified before handoff, not just assumed

Grepped all six new suites for `TestClient`, `from app import`, `import app`,
and `conftest` -- zero matches, and there is no `tests/conftest.py` in this
repo at all. They are standalone scripts importing only the specific modules
under test (`sing_grid`, `window_energy`, `artifact_screens`, `pipeline`,
`tiers`), no FastAPI, no app boot, no eviction path. Safe to run alongside a
live render. Only `tests/smoke.py` itself carries Bug B.
