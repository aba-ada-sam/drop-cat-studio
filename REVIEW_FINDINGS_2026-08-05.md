# GUI/UX Review Findings -- 2026-08-05 night (input to the V2 build)

Compiled from a 3-agent audit (flow trace, capability mine, full GUI review with
per-tab sub-audits). File:line refs are against the v1 tree at commit 080a914,
which this repo is cloned from. Items marked [V1-FIXED] were already fixed in v1
tonight and are inherited by this clone.

Coverage gap: Music Video + Video Bridges tabs and the deeper shell survey
(AI Manager bar, Gallery overlay, command palette, presets, admin page) were
NOT audited. Music Video is being replaced by the chain.py wrapper anyway.

## CRITICAL

- C1 Loop Folder ignores every visible setting. tab-fun-videos.js:1311-1395,
  fun_videos/routes.py:243-311, folder_loop.py:190-208. JS never sends
  multi_video (server defaults True); _submit_one sends raw JS key names
  (model, duration, steps) that multi_pipeline.py does not read (expects
  model_name, clip_duration, ...) so EVERY field silently falls back to
  hardcoded defaults. Fix: route through the same key-mapping as make-it,
  send explicit multi_video flag.
- C2 panel-image2video.js (Ken Burns slideshow, 137 lines) + features/
  image2video/routes.py (221 lines) fully built, fully unreachable (not in
  TAB_INIT, no rail button). Its handoff also targets video-tools whose
  TAB_HANDOFF is null, and its drag-to-reorder claim is false. Decision for
  Andrew: wire or delete. Do NOT silently wire it in v2.
- C3 [V1-FIXED] Quick Video idea handoff eaten by stale cached tab-express.js
  (?v= never bumped after Aug 2/3 fixes). Root cause of Andrew's complaint.
  V2 additionally makes the generate step VISIBLE (wizard, see build brief).
- C4 [V1-FIXED] Restart banner freezes forever on dead backend. V2 adds a
  proper "Server offline" overlay + "server unreachable" banner state
  (app.js:362-371, 1140-1220; 90s silent Restart wait).
- C5 Action / Action HD presets silently force motionless clips.
  tab-express.js:1338,1652: motion_style uses includes('dev13') which never
  matches any Express model -> always 'calm' -> "subject completely still,
  static shot" appended to action prompts. Correct logic already exists in
  _collectFolderLoopSettings() (isLtx ? 'calm' : 'dynamic'). Fix both sites.
- C6 Chat strips NSFW subject/creature controls silently.
  chat_studio/routes.py:294-299 hardcodes subject="auto", creature=False and
  never echoes resolved values; image_studio/routes.py:136,171-180 does this
  right. Fix: pass through + echo like Image Studio.
- C7 VRAM messaging contradicts itself 3 ways for the same default model
  (Express). tab-express.js:157-169 warns "needs ~30GB / you have ~16GB";
  single Create proceeds (gate disabled 2026-08-01 on purpose); Loop Folder
  hard-blocks HTTP 400 and points at the WRONG tab ("Create Videos settings").
  Fix: one consistent gate + correct tab reference.
- C8 Pause does not cover GPU-heavy Video Tools ops and the label implies it
  does. tab-queue.js:90-108,330 + job_manager.py GPU_JOB_TYPES: AI Upscale /
  RIFE start instantly during pause and can fight WanGP for VRAM; header
  reads "Running (last before pause)" for them. Fix: route JOB_VIDEO_TOOL
  through pause-aware path or scope the label/tooltip honestly.

## HIGH

- H9  Forge invisible to service monitoring (svc.get_status() only wangp/
  acestep/featherless; no splash check, no dot). Chat + Image Studio depend
  on it blind. Fix: reachability check in /api/services + dot + splash line.
- H10 Chat/Image Studio still images never session.add_file()'d -> invisible
  to Gallery (their video outputs DO register). Fix: add the add_file call.
- H11 "+ Add to Queue" always POSTs single-clip make-it even with Multi-video
  Story on. tab-fun-videos.js:1301-1308,1473-1485 vs 1521. Fix: mirror the
  main Generate button's branching.
- H12 Seed 0 silently becomes random: parseInt(v) || -1. tab-fun-videos.js:
  1452. Fix: Number.isFinite check.
- H13 Queue's "Save & Restart" never recovers (no poll/reload; header buttons
  do it correctly). tab-queue.js:164-197 vs app.js:1105-1230. Fix: reuse the
  poll-then-reload pattern.
- H14 Swallowed AI-brainstorm failures on blank idea/lyric auto-fill (bare
  catch, no toast, no fallback) in tab-fun-videos.js + tab-express.js:1288,
  1317-1318,1614-1615. Fix: toast + fallback text.
- H15 GPU pill only tracks WanGP/ACE-Step (app.js:1236-1274) -> reads idle
  while Forge renders. Fix: include Forge or caveat honestly.

## MEDIUM

- M16 "Lip Sync" checkbox is a no-op in single-clip mode (pipeline.py never
  reads lip_sync; only multi_pipeline.py does). tab-fun-videos.js:1061-1065.
- M17 Multi-video Clips length readout wrong: server clamps clip duration
  4-6s (routes.py:941) but JS multiplies slider value (tab-fun-videos.js:
  1240-1257). 15s x 4 clips shows ~60s, renders 16-24s.
- M18 Create Videos never checks /api/services or /api/gpu/status before
  submit -> minutes of fake progress on cold WanGP.
- M19 Loop Folder status polling swallows errors -> status card freezes on
  last good message.
- M20 Chat and Image Studio: two parallel generate+animate implementations,
  zero shared state or handoff.
- M21 provider_used (which LLM answered; cloud vs uncensored fallback)
  discarded client-side in Chat; no Featherless health check there either.
- M22 "Style" (Chat) vs "Preset" (Image Studio) for the identical catalog;
  Chat's "Current (no style override)" is opaque.
- M23 Image Studio failures: silent:true, never reach central error log;
  Chat: toast only, no inline. Same failure class, two presentations.
- M24 Lip Sync tool auto-mounts an empty bordered divider on queue detail
  when MuseTalk absent (silent no-op gap; Upscale/Smooth explain inline).
- M25 Queue job dismiss ignores r.ok=false -> can permanently hide a job the
  server still considers running.
- M26 Video Tools: instant ffmpeg steps vs GPU-queued "Generate & Mix Music"
  visually identical; no "runs now vs waits in line" distinction.
- M27 Studio Home links 2 of 9 tools ("every creation type" claim stale).
  tab-pipeline.js:16-41 STEPS.
- M28 Three inconsistent pipeline narratives: Help modal (Make Videos ->
  Video Bridges), Home (Create Videos -> Video Tools), app.js:48-50
  breadcrumb collapsed to one dead entry.
- M29 Express: disabling Extend Scene hides the Lip Sync checkbox but leaves
  _lipSync=true in state -> next Create silently submits lip_sync:true.

## LOW

- L30 Express loopBtn ("Loop") created display:none, never wired. Dead code.
- L31 Queue "Finished" header counts cancelled as failed (nFailed = status
  !== 'done') while per-card chip says Cancelled.
- L32 No Reveal-in-Explorer/Delete in Queue tab (only Gallery); good 423/403
  handling exists but is unreachable from Queue.
- L33 Upscale/RIFE capability probes fail OPEN on fetch error -> engine
  offered that is not installed; failure surfaces mid-job.
- L34 Image Studio Subject/Creature controls invisible until an NSFW preset
  chosen; no hint they exist.
- L35 tab-adobe.js (346 lines) + features/adobe_agent/routes.py unreachable
  (not in TAB_INIT, no rail button). RULED BY ANDREW 2026-08-05: he does not
  know what it is -> DELETE both in V2.

## INCIDENT 2026-08-05 23:19 -- V2 test killed V1's live render

A build agent constructed FastAPI TestClient(app) to smoke-test new routes.
The context-manager form runs the REAL lifespan -> kill_orphans_at_startup ->
_kill_stale_gpu_processes, which matched ANY wangp_worker.py process on the
machine and killed V1's worker mid-render. chain.py hung on SYN_SENT to the
dead socket; ~7 minutes of GPU work lost. The DCS_NO_GPU_EVICT /
PYTEST_CURRENT_TEST guard did not fire -- it lives in the CALLER, and a bare
TestClient sets neither variable.

FIXED (v2-gui 3ff0b78): _kill_stale_gpu_processes now honors the env guard in
the function itself AND only matches this checkout's own absolute worker path
(ACE-Step scoped to its configured root). V2 can only evict V2's workers.
STILL OPEN: the same unscoped function exists in V1's tree (v1 is frozen; if
Andrew wants, port the same 20-line fix). Standing rule issued to all build
agents: never construct TestClient against app.py; static validation only.

## OPEN ITEM -- cross-install GPU visibility (found by the chain builder)

core/gpu_orchestrator sees only ITS OWN install's worker. V2's chain start
endpoint refuses when V2 is rendering but CANNOT see a V1 render in flight, so
starting a V2 render while V1 renders would put two jobs on one 16GB card.
Needs a cross-install signal (a lock file both installs honor, or probing both
worker ports) before V2 is used alongside a live V1 render. Red team should
attack this.

## VERIFIED POSITIVES (do not regress)

- Forge-down handling in Chat/Image Studio: clean 503 "Forge is not running
  on :7861 -- start it from its own GUI" propagates to both UIs.
- Video Tools step progress + Cancel kills the real subprocess.
- Most Create Videos / Express controls call real endpoints with proper
  error handling (Enhance, Create Story, Refine, Suggest Music, Redo Audio,
  Audio Sync, model dropdown, aspect chips, v2v continuation).

## ARCHITECTURE (from the capability mine)

- Everything ratified 2026-08-05 (recipe v3) lives in engine/chain.py, CLI
  only: 241f clips, 0.15 crossfade, --images/--scene-prompts multi-scene,
  --smart-seams --min-clip-frames 169, --judge-select, tail-stub rule.
  Ratified example command: review/render_v16_detached.ps1:10.
- features/song_video/pipeline.py is a PARALLEL OLDER implementation with
  two known live bugs (clip start-time drift ~0.28s/clip pipeline.py:538-549
  fixed in sing_grid.py but unwired; arc-duration override pipeline.py:
  1506-1508). RECIPE.json is validated by recipe.py but NOT consumed at
  runtime. tiers.py has zero callers.
- V2 verdict: wrap chain.py via subprocess for the Music Video path (option
  a from the miner). Do not port piece-by-piece into pipeline.py.
- Per-scene DOF finish (sigma 3.2, frame-exact single-pass maskedmerge) is
  ratified but NOT EVEN SCRIPTED -- hand-run ffmpeg each delivery. Segmented
  trim+concat is BANNED (0.25s drift). Scripting it is in scope for v2 but
  must copy the ratified method exactly (commit 0cc1ade in engine repo).

## LANDMINES (must not violate)

- fps stays 24 (25 = 4% A/V drift bug, recipe.py:72-75 enforces).
- sync_enforce / SYNC-OR-DIE floor stays DISARMED (recipe.py:60-66 blocks
  re-arming; floor proven inverted vs Andrew's blind labels).
- mouth_sync_score / total_motion are whole-frame unmasked -- never gate or
  auto-select on them.
- Generative identity for scene anchors REJECTED (PRIME RULE: identity =
  user's pixels; transplant/graft composites only).
- 0.4s crossfade REJECTED; dip-to-black REJECTED; dance/choreography
  background-cast wording REJECTED 3x; minimal-motion direction only.
- MuseTalk: RULED BY ANDREW 2026-08-05 ~22:55 -- REMOVE from everything in
  V2 (supersedes the June default-ON order). Native audio-conditioned sync
  in chain.py is the proven mechanism and stays. Strip: auto_lipsync route
  default + param plumbing, the queue-detail Lip Sync tool mount (M24), the
  no-op Lip Sync checkbox (M16, M29), musetalk_dir/musetalk_python config.
  v1 stays untouched (frozen).
- Audio-conditioning grip collapses past ~250 frames (hard content ceiling).
- GPU: single RTX 5080 16GB; v16 render owns it tonight -- NO live GPU calls
  until it completes. LTX-2 rounds resolution down to 64-multiples.
