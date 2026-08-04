# LIP-SYNC KNOWLEDGE LEDGER

One line per lesson, dated, with status. This file exists because on 2026-08-04 a full
day was spent re-deriving lessons already recorded in DCMVS milestones and this repo's
own history, while a never-validated homemade metric rejected good renders and sent the
investigation chasing phantom causes. Andrew: "Continue learning and posting knowledge
to a place where you won't lose it." This is that place for the lip-sync lane.

RULES OF THE LEDGER
- New entries go at the top of their section, dated, one sentence, with evidence ref.
- A reversed lesson is NOT deleted -- it gets STATUS: REVERSED and a pointer to what
  reversed it. Reversals are the most valuable entries here.
- Any new metric/gate gets a ledger entry stating what known-good and known-bad
  references it was validated against BEFORE first use. No validation, no gating.

================================================================================
CURRENT RECIPE (the one true set -- change only with a ledger entry + evidence)
================================================================================
- Model: LTX-2 Dev19B Distilled via WanGP worker :7899 (commit 3ba3863 + 2 load-bearing
  local patches: ltx2_handler.py audio passthrough, ltxv yaml skip_block_list=[]).
- Resolution: request 960x544 -> RENDERS AS 960x512 (LTX-2 ceil-rounds to 64-multiples,
  ltx2.py:1152). 1024x576 is exact 16:9 and 64-clean on both axes -- A/B PENDING.
- fps: 24. (WanGP _ARCH_SPECS ltx2_19B = 24. DCS core/wangp_models still registers 25 --
  KNOWN BUG, 4% a/v drift, fix pending. Site's sing_pipeline already corrected to 24.)
- Frames: 8k+1 only (241, 249...); SING CLIPS <= ~250 frames -- audio grip collapses on
  long clips (measured 4/4 synced at 249f vs 1/21 at 465-473f on 2026-08-04; direction
  solid, exact ratio measured with the pre-fix scorer). 481 renders fine but doesn't sync.
- Steps 8 (locked by distilled). guidance_scale: FORCED to 1.0 by the handler -- the 3.0
  we pass is DISCARDED (harmless). audio_cfg/audio_guidance 3.0 DOES pass through
  (worker mirrors guidance; must be >1.0 or conditioning is neutralized).
- audio_scale 0.6. SE anchoring: start_image AND end_image = pristine source. NO chaining.
- CONDITIONING AUDIO: isolated vocal stem (BS-Roformer via chain.py isolate_vocals) ->
  150Hz high-pass (chain.py highpass_audio) -> phrase gating (silero, soft 0.18s ramps;
  SILENT slice for zero-phrase windows; over-mute breaker >85% only for bad separation).
  NEVER the raw mix -- see 2026-08-04 entry below.
- Selection: best-of-N (3-4) per sing clip, early-accept on first take that is
  artifact-clean AND verdict=synced. Gates before ranking: RIBBON (full-res bright-mask
  flicker, p95 < 0.30 AND max < 1.0 -- see ribbondet v3 entry), yellow-glyph excess
  <=0.02%, transient-white (vs clip's own static bright mask) <=2.0%, luma mean drift
  <=25% of source / peak <1.6x source. The transient-white gate alone is BLIND to thin
  ribbons (4x downsample) -- it never substitutes for the ribbon gate.

================================================================================
2026-08-04 -- THE BROKEN-RULER DAY (all entries same day, ordered newest first)
================================================================================
- ACTIVE (night, Tier-2 port, helper) | DCS PROPER HAS NEITHER FRAME RULE. Measured
  while porting, both live in the shipped code: (a) there is NO 8k+1 quantization
  anywhere in DCS -- video_generator.py:255-257 rounds to the nearest ODD frame,
  a different convention, so a 9.317s beat-planned clip renders 223f, not a valid
  LTX-2 count; (b) there is NO conditioning-grip cap -- the only cap is max_sec 19
  (455f) and the song pipeline clamps clips to 12s = 289f, so EVERY sing clip in
  DCS can legally render past the ~250f ceiling, with the silent failure mode
  (renders clean, does not sync). Both are now implemented in
  features/song_video/sing_grid.py with pure-arithmetic proofs
  (tests/test_sing_grid.py, 35 assertions, no GPU). These were the two traps this
  session hit live today from the other direction.
- ACTIVE (night, Tier-2 port, helper) | THE DRIFT HAS TWO INDEPENDENT SOURCES IN
  DCS, not one. Beyond the trim-vs-slice mismatch already ledgered for the site:
  (1) prep lays conditioning-slice start times on (planned_duration - 0.12
  song-xfade, pipeline.py:480-487) while each clip actually advances the timeline
  by (duration - 0.28 boundary trim, :1207/1210) and then loses the xfade again
  -- ~0.28s x clip_index, reproduced as arithmetic at 3.08s by clip 12
  (test_sing_grid.py group C); (2) slice start times come from the beat plan's
  clip_durations (:482) but render AND slice LENGTH come from the LLM arc's
  duration (:912/:1064, default 7.0) -- two lists that are never reconciled, so
  the drift exists even if the trim were fixed. Fixing only the trim would leave
  a residual nobody would think to look for.
- ACTIVE (night, Tier-2 port, helper) | A PERMANENTLY-RED SUITE IS A BROKEN
  ALARM: tests/smoke.py asserted LTX-2 vram_min_gb 8-12 and had been FAILING on a
  healthy tree ever since 8a45f7c (08-02) raised the floor to 30GB for a 20.07GB
  model file -- the test enshrined the value the ledger had already corrected.
  A suite that is always red is how a REAL failure gets waved through as "the
  usual one". Assertion updated to the documented floor; suite 20/20 green.
  General rule: when a known-wrong value is corrected, grep the tests for it in
  the same commit.
- ACTIVE (night, Tier-2 port, helper) | DETECTORS PORTED WITH THEIR FALSE
  POSITIVE PINNED AS A TEST. features/song_video/artifact_screens.py reproduces
  the recorded calibrations exactly (ribbon known-bad p95 1.067 vs recorded 1.07,
  clean 0.240 vs 0.24; red known-bad p95 0.157 vs 0.157), and
  tests/test_artifact_screens.py asserts the RED FALSE POSITIVE on purpose --
  eye-clean final_07_0 max 0.260 >= known-bad 0.245 -- so any future attempt to
  promote red from screen to gate fails a test that explains why it must not.
  Fixture warning found while calibrating: quarantine/final_03_0.mp4 is
  BYTE-IDENTICAL (same md5) to the promoted final_03_0.mp4, so the quarantine
  folder's label is NOT reliable as known-bad; only quarantine/final_01_1.mp4 is
  a distinct file. Same trap the ledger already names ("never infer an asset by
  filename pattern").
- ACTIVE (night, Tier-2 port, helper) | THE WINDOW RULE IS NOW MECHANICAL IN DCS:
  features/song_video/window_energy.py measures per-window energy on the isolated
  stem; any window above -40 dBFS must be conditioned regardless of map or VAD
  label, disagreements log loudly (or raise, in strict mode for unattended
  paths), and UNMEASURABLE audio fails TOWARD conditioning -- None is "no
  information", never "silent". Validated against the real bug, not a synthetic
  one: the four windows this session shipped as unconditioned filler re-measure
  02=-31.0, 04=-32.0, 06=-30.1, 08=-29.6 dBFS and all four are caught, while the
  genuinely silent intro (-90.3) is still accepted -- it discriminates rather
  than flagging everything, which is the only thing that makes a gate worth
  having. Before this, DCS measured voicedness ONCE over the whole song before
  slicing; nothing ever checked a window against the song.
- REJECTED same night at 0:17 | FIXED5's real remaining flaw is the WINDOW MAP, not the
  windows: 'interlude' windows 02/04/06/08 measure 55-68pct voiced on the stem (08 is the
  DENSEST-vocal window in the song) but were built as unconditioned filler in every cut
  to date -- 79s / 38pct of runtime sings with an undriven mouth. Every gate verified
  windows against the map; nothing verified the map against the song. RULE (hardened per
  sing-cloud's reciprocal audit, their 04c6ff3): the check is MECHANICAL, not a habit --
  measure per-window energy on the ISOLATED stem; any window above the floor (~-40 dBFS /
  ~20pct voiced) MUST be a conditioned render no matter what any map or VAD label says;
  a label-vs-energy disagreement fails loudly. The site's sibling failure (silero
  under-detects -> digital-silence conditioning that SCORES FINE because the mouth
  matches the silence it was handed) is the same invisibility class -- neither gives any
  signal until human ears hit it. Fix here: split each 473f interlude into two 236f
  halves on the rigid grid, condition each (interlude_hunt.py, running).
- CALIBRATION (night) | GOLDEN AWM00001 measured on the same meter, same song, same grid:
  ranks average ~0.10 (our banked windows are IN that band), only 7/20 windows verdict
  'synced' -- but motion runs 2-13 vs our 0.3-1.3, and it is a MULTI-SCENE cut-and-
  crossfade video (mirror alien, singing cats...), not a single tableau. What reads as
  'real lip sync' is performance energy + scene variety + sync where vocals are dense,
  not per-frame viseme lock. Consequences: (a) the rank formula's motion-dampening
  selects statues -- do not optimize takes solely by it; (b) the 'a face singing, clear
  mouth, facing camera' prompt invites a static portrait -- an energy-prompt A/B is the
  next cheap lever; (c) matching AWM00001 ultimately means DCMVS multi-scene, which is a
  format decision for Andrew, not a repair of this file.
- DELIVERED (night) | FIXED5 shipped on review_0003.html: FIXED3 base + 6 replacements
  + pod-master tail on an exact 5040-frame grid. 16/16 windows + 9/9 seams eye-passed
  (5 adversarial subagent passes, every flag re-checked senior); full numeric gauntlet
  green (probe -1/0, yellow 0.0000%, white 1.62%, luma flat, no spikes). Disclosed,
  not hidden: w06 baked wall glyph (also in master), w07_0 one-frame prop flicker,
  modest sync on quiet-backing windows.
- ACTIVE (night) | FOURTH ARTIFACT CLASS -- CARVED/STAMPED TEXT: ~15s of embossed
  gibberish lettering on the desk edge across FIXED3's calm window 10 (and closing
  fog in 11) -- dark text is INVISIBLE to every brightness/color detector; only the
  full-video eye pass found it. It shipped in ALL prior cuts. Calm/instrumental
  windows are NOT safe to skip: the music-video prior decorates them with title/
  credit furniture precisely because nothing else is happening.
- ACTIVE (night) | CLEAR EYE-DUMP DIRS BETWEEN GAUNTLET RUNS: leftover detector-worst
  frames from run 1 sat beside run 2's dumps and produced false DEFECTIVE verdicts on
  already-replaced windows (same filenames as the real defects). 20 minutes of forensic
  disambiguation via file mtimes + re-extraction. gauntlet_f5.py now rmtree's the dump
  dir first. General rule: any evidence directory an inspector reads must be born empty.
- NOTE (night) | sync_qc's sync_y is UNSTABLE on weak-motion clips: the same pixels
  re-encoded flipped sync_y 0.71 -> 0.26 and crushed rank 0.1027 -> 0.0002 through the
  position gaussian. In-file regression ranks are only meaningful on motion-rich
  windows; the assembly-correctness proof is the frame-match probe + pixel-identity
  check, never the rank.
- ACTIVE (late evening) | BOOST VERDICT -- LOUDER GUIDES RAISE SYNC *AND* SLOP TOGETHER:
  flat-gaining quiet slices to -20 dBFS lifted reachable sync ranks ~9-75x on the two
  phrase-bearing 09 windows (0.17-0.18 vs 0.02-0.10 unboosted) but high-rank takes kept
  arriving slop-dirty; across 18 boosted takes only one clean upgrade banked (09_1
  0.0190). Boost is a real lever for quiet windows, not a free one -- pair it with the
  full gate stack and more takes. Intro-type unvoiced windows (01_0) do not respond.
- ACTIVE (late evening) | RED SCREEN VALIDATED IN ACTION: a rank-0.1802 take passed
  ribbon+yellow+white+luma and was caught ONLY by the red screen + eye (red graffiti
  glyphs/drips, p95 0.135 vs 0.124 on verified-clean skin motion). The margin between
  clean and infested red scores is too thin for an automatic gate -- worst-frame eye
  review stays mandatory before banking any take.
- NOTE (late evening) | chain.py checked for the site's content-vs-render cap trap:
  NOT present -- chain.py:754 derives boundaries so audio-slice == video ==
  num_frames/fps by construction. Its long-clip 481f default remains the separate,
  already-ledgered sync killer.
- ACTIVE (evening) | SITE VARIANT OF THE GRID BUG, WORSE (sing-cloud audit, commit
  832d94c on sing-audio-conditioning, undeployed): trim_boundaries cut 0.28s off every
  clip AFTER render while conditioning slices were cut at absolute song positions ->
  0.28s x clip_index cumulative drift (5.3s at the 20-clip ceiling), plus _mux_final
  looping the video to cover the shortfall. Two transferable lessons: (a) HEADROOM THEN
  TRIM beats pad-with-clones for fresh renders (clone-pad can freeze a mouth mid-word;
  Studio's FIXED5 pad is safe only because SE anchoring parks the final frames on the
  pristine pose); (b) CONTENT budget != RENDER cap: adding render headroom while
  splitting on the render cap pushes clips past the 249f conditioning-grip ceiling --
  renders fine, silently stops syncing. Split on a 236f CONTENT budget. Check chain.py
  for the same trap during the Tier-2 port.
- ACTIVE (evening) | ASSEMBLY MUST BE A RIGID FRAME GRID -- short splices killed the
  sync mechanically: FIXED4 inserted 233-frame renders (8k+1 rule) into ~236-frame
  slots with no padding, shrinking the video 7 frames total; every splice makes all
  later video play EARLIER against the untouched soundtrack (~0.3s by the tail =
  visible lip-lead everywhere downstream, on takes that were individually synced).
  align_probe.py (frame-matching vs pod master in the shared pod windows) measured
  FIXED3 rigid (-1/0 frames throughout) and confirmed FIXED4's drift. RULE: every
  segment is cut/padded to its slot's exact integer frame count (tpad stop_mode=clone;
  SE anchoring makes a 3-frame tail hold invisible) and counts must sum to the master
  total BEFORE concat. Never trust -t to fix a SHORT clip -- it only trims long ones.
  Same bug class threatens ANY per-clip stitcher (site sing pipeline flagged X-SILO).
- ACTIVE (evening) | THIRD ARTIFACT CLASS -- DARK-RED STRANDS: red ribbon/confetti
  streaks + edge fog + glyph-on-clothing, found by eye in a window BOTH bright-flicker
  and yellow gates passed (FIXED4 09_0). Red-flicker metric (R-max(G,B)>45 & R>90,
  XOR between frames) separates it (bad p95 0.157 vs good 0.04-0.07) but fires on
  legit moving skin (0.124-0.26 on a verified-clean take), so it is a SCREEN not a
  gate: rank worst frames, send them to the eye. The eye stays the gate for red.
- ACTIVE (evening) | WINDOW 01_0 (song intro) IS BARELY VOICED: phrase gating hit the
  ungated-breaker (>85% would mute) and +7.7dB boost still produced 0 synced takes in
  6 -- there is nothing to lip-sync there. A clean, alive-but-quiet mouth is CORRECT
  content for the intro; do not burn takes chasing "synced" verdicts on it.
- ACTIVE (evening, pending boost A/B) | QUIET GUIDES DON'T DRIVE THE MOUTH: the two
  windows that refuse to sync are the two quietest slices in the song (01_0 peak
  -27.7 dBFS frame-RMS, 09_1 -28.9 -> 0-1 synced of 6 takes each) while the windows
  that synced first-try are louder (03_1 -23.3, 07_0 -25.2). One-variable test
  running: flat gain to -20 dBFS on the conditioning slice only (mux audio untouched).
- ACTIVE (evening) | SYNC AND SLOP RISE TOGETHER: high-energy takes that move the mouth
  also ribbon more (01_1: 4/4 takes ribboned, the 2 synced ones among them; 09_1's only
  synced take was also its dirtiest). Best-of-N with the full gate stack is how you keep
  one without the other -- there is no settings knob that trades them.
- REVERSED (evening) | "sage2-on-Blackwell attention kernel causes the ribbons" --
  disproven by the sdpa re-roll itself: with wgp_config attention pinned to sdpa,
  12/16 takes STILL ribboned (01_0 4/6, 09_1 4/6, 01_1 4/4). Ribbons are stochastic
  per-seed music-video-prior slop on ANY attention backend; window content sets the
  odds. wgp_config.json left at "sdpa" (no measured slowdown, ~62-70s per 201f take);
  reverting to "auto" is safe if ever wanted.
- ACTIVE (evening) | RIBBON DETECTOR v3 (scratchpad ribbondet.py): thin near-white
  strands that REDRAW every frame; metric = full-res bright mask (min(R,G,B)>190)
  XOR'd between consecutive frames, % changed. Eye-calibrated both ends on FIXED4:
  infested p95 1.07 vs clean p95 0.24. Window call: p95>0.50 infested, <0.30 clean,
  else eye-check. Built AFTER FIXED4 shipped with 4 infested windows that the
  transient-white gate scored "below baseline" -- downsampled detectors cannot see
  1-3px flicker. No delivery without ribbondet + full-res eye pass on EVERY window.
- ACTIVE (evening) | OVER-RETRACTION LESSON: the round-4 bright-streak metric was
  retracted globally because its CROSS-scene comparisons were garbage (golden file
  scored worse than a rejected one) -- but its WITHIN-scene flags on the morning
  windows were real ribbons. A metric that fails across scenes may still be right
  within one; check both scopes before discarding the signal entirely.
- ACTIVE | Artifacts are LTX-2's music-video prior leaking under weak conditioning:
  eye-confirmed a hallucinated TITLE CARD (white screen, fake logo text) in a peak-luma
  reject; the yellow 48-55s glyphs in FIXED3 are lyric-text furniture. Strong vocal
  guides suppress artifacts AND sync failure together (17/17 isolated-guide takes clean).
- ACTIVE | Regen tooling conditioned on the RAW SONG: slices measured +6.9dB
  bass-over-mid (isolated vocals cannot be bass-dominant); isolate_vocals was never
  called (DCMVS_WANGP_ROOT unset + tool never wired it). Isolation + 150Hz HP flips the
  guide to -1.1dB; sync rank jumped 10-26x on the test window, matching DCMVS v2.0's
  recorded fix (beat band 0.256 -> 0.092). Design rule going forward: the render path
  must not ACCEPT a raw-mix wav -- prep produces a stem artifact, renderers take only that.
- REVERSED same day | "TeaCache in defaults/ltx2_distilled.json is the arc factory" --
  code trace + identical render times prove the tea gate never fires for LTX-2
  (wgp.py:827 zeroes it; ltx2 handler declares no tea_cache); config restored as-found.
- REVERSED same day | "65% arc lottery / degraded worker / thermal" -- all artifacts of
  a never-validated bright-streak metric that scored golden AWM00001 at 70.7, WORSE than
  the humanly-rejected FIXED3 (18.3), and rejected eye-verified-clean takes at 14.56.
  Actual artifact rate in the delivered cut: 2 windows out of 11 (yellow 48.0-55.5s,
  white 118.0-123.8s). Lesson: no new gate may reject work before being validated
  against a known-good and a known-bad reference.
- ACTIVE | Luma gate (mean within 25% of source, peak <1.6x) added eb44188 -- NOT from
  history, invented same day; validated against a dark failure (mean 62) and the
  title-card washout (peak 222.7); known-goods pass at ~90.
- ACTIVE | DCS worker-start can wedge silently inside gpu.acquire (no log, no error,
  status stays "stopped") AND the boot auto-start races an API start (healthy worker
  killed as "stale orphan" 2s after binding). Recovery ladder: POST /api/app/restart ->
  watchdog respawn -> start -> require :7899/health twice 6s apart before rendering.
- ACTIVE | fps bug: DCS fun_videos/video_generator.py:141 registers LTX-2 Distilled at
  fps 25; WanGP's arch spec is 24. Site corrected it independently; DCS fix pending.
- ACTIVE | Resolution rounding: LTX-2 ceil-rounds W and H to 64-multiples silently
  (ltx2.py:1152-1158); 960x544 -> 960x512 actual. Source photo is 1280x720 (exact 16:9);
  1024x576 would be exact-16:9 AND 64-clean -- A/B queued.
- ACTIVE | Best-of-N restored for hero videos: DCMVS v1.0 documented the per-seed
  mouth/eyes/forehead coin flip (one clip, four seeds: 0.77/0.31/0.19/0.03); v2.8
  retired best-of-N for batch throughput -- a tradeoff, not a refutation.
- ACTIVE (added ~17:10) | THE PROMPT MUST DESCRIBE THE CONDITIONING IMAGE, NEVER
  CONTRADICT IT. Matched-seed A/B, isolated wav, window 01_1: short prompt "a face
  singing, clear mouth, facing camera" -> 1/3 synced, 0.1433; DCMVS's long subject
  template -> 0/3, best 0.0005. The SAME seed scored 0.1433 synced vs 0.0005 static
  (~280x). Cause: the template claims "the only subject in the shot, no other people"
  while this source image has a man at the bench -- text guidance fights the image and
  audio grip loses. DCMVS's template worked because ITS subjects really were alone in
  frame (v2.3 "subject-anchored prompting" was a fix FOR ITS scenes, not a universal).
  Default: short generic sing prompt; add scene detail only if it is TRUE of the image.

================================================================================
HISTORICAL LEDGER -- DropCat-Studio (916 commits mined 2026-08-04; condensed)
================================================================================
THE OSCILLATION CHAIN (the single most important pattern -- read before "fixing" sync):
  MuseTalk-first f167363 (05-28) -> native-first abb7583 (06-19, the audio_guide-not-
  audio_source field fix) -> MuseTalk-first again 71c2320 (06-27, "native follows the
  beat not the words") -> native fixed for real 1920968/87883d5 (07-29/30) -> native
  disabled for deadlock 2e1a063 (08-02) -> native restored 5d835ab (08-04). Each flip
  had evidence at the time; most were later reframed. Any future flip needs a ledger
  entry naming what NEW evidence justifies it.

RESOLUTION FLIP-FLOP: 640x360 (7d5a69e, "580p drops audio tokens") -> native 580p
  (43d08b1, "360p chained = pox/lesions") -> 360p+keyframes (b247a12) -> 960x544 proven
  (1920968) -> blocked as "hangs card" (83881b3, 08-02) -> unblocked, hang was the TDR
  driver window (530e962) -> default (5d835ab). STATUS: 960x544 active; the "580p+
  drops audio tokens" claim was NEVER re-verified at 960x544 -- it demonstrably syncs.

RECURRING BUG CLASS -- settings silently diverging between planning and render:
  01370d1/09d4ce0 (05-25, scoping), 64bccfd (08-03, guide-slice length not clamped like
  the render), 029a4e7 (08-02, a default fixed at ONE call site while three others kept
  the old value -- grep every call site), 1920968 (gate logic believed 360p while the
  render was already 960x544). The canary test exists to catch this whole class.

KEY SINGLE LESSONS (still active):
- 87883d5 (07-30): SE anchor must be on EVERY clip -- but was only half the fix;
  5d835ab (08-04) added the other half: no chaining, start image = pristine source.
- 86abbee (08-03): sync_qc MIN_TOTAL_MOTION=1.2 hard floor zeroed every candidate on
  low-motion takes, blinding best-of-N; now a soft factor (identical scores >=1.2).
- 8a45f7c (08-02): Dev19B VRAM floor is 30GB not 10GB (20.07GB file); enforcement gate
  deliberately NOT wired -- mmgp offload makes flat floors false-positive on this card.
- 2a581bc (07-29): GPU services must raise on CPU fallback, never silently grind.
- f266be4 (05-25): only ltx2_distilled accepts audio conditioning; ltxv_13B silently
  renders zero clips on audio_prompt_type=A.
- e4a832b (05-29): the two MuseTalk engine patches (landmark face box for creatures,
  contiguous %08d frame writes) live in features/lipsync/musetalk_patches/ -- a Pinokio
  reinstall wipes C:\MuseTalk hand-patches; restore from there.

TEST-DESIGN TRAPS (paid for in full days; doc-only until this ledger):
- Verify slice RMS > -35dB before concluding anything -- a benchmark song's 0-6s is
  instrumental; conditioning on gated silence produces mouth-agape that reads as failure.
- Never infer an asset by filename pattern -- a seed-number collision put the WRONG
  source image under test for hours (AWM00001's real source = extracted frame 1).
- VERIFY IN PIXELS. A numeric "synced" can carry a mouth-box, washout, or title card.
- Fixed proof seed: 777. Frozen May proof pair re-scores c=0.186 today (scorer stable).
- AWM00001 was made by DCMVS (C:\DCMVS-restored), NOT DCS -- no DCS job record matches.
  DCMVS is system of record; DCS adapts to it, never the reverse.

CLOSED 2026-08-04 (were open at archaeology time):
- Driver theory DEAD: sync proven locally on 610.88 with gated guides + payload fixes;
  the 08-02 deadlock/hang cluster sat inside the TDR window. 610.47 rollback package =
  fallback archive only (Desktop/DCS_Review/driver_remediation, STAND DOWN note).
- ee38f88 sliding-window "smoking gun" was a no-op (settings file had 481 since May 3);
  kept as parity documentation. The commit message reads as a fix -- it is not one.
- app.py gpu.acquire lock race: STILL UNPATCHED and bit twice on 08-04 (13:31 wedge sat
  2.5h silent; 16:08 boot-vs-API double start killed a healthy worker as "stale orphan").
  Recovery ladder is in the CURRENT RECIPE section above. Needs a real design pass.

STILL OPEN (inherited from archaeology):
- MuseTalk on creatures: "fundamental paste-box" (07-24, 08-03) vs "clean on fruit/
  animal faces after alignment patches" (71c2320) -- never reconciled; moot while the
  native path holds, but do not re-adopt MuseTalk without settling it.
- The 08-02 eyes-vs-mouth heatmap disagreement (scorer vs human pixels) -- unresolved;
  today's wrong-region(eyes/upper) verdicts make the scorer's side more credible.
- ~23% "No clips generated" WanGP failure rate (15/66 jobs, 07-28..08-02) -- standing
  reliability gap, no root cause on record.
- PORT_PLAN Tiers 2 (audio-prep parity in DCS proper) and 3 (seams/regen UI): NOT
  implemented; today's isolation work lives in session tooling, not DCS code yet.

================================================================================
HISTORICAL LEDGER -- dropcatgo-generator (site) + DCMVS (mined 2026-08-04; condensed)
================================================================================
DCMVS (6 commits, all knowledge in _milestones/*/MILESTONE.md + FINDINGS docs):
- v1.0 (05-31): per-seed mouth/eyes coin flip documented with numbers (one clip, four
  seeds: 0.77/0.31/0.19/0.03); sync_qc + --seeds-per-clip born. 960x544/249f/8 steps.
- v2.0 (06-01): vocal isolation + 150Hz HP, beat-band 0.256->0.092 -- sync watchable.
  5080 parity proven (the "5080 can't sync" README line was day-one, never updated).
- v2.4/v2.5 (06-02/03): SE keyframes make hard cuts invisible; 481f=20s is the audio-
  conditioned ceiling (30s request silently clamps); full 3:31 song held together.
- v2.7 (06-04): vocal-activity gate with hysteresis, muting 30%->21%.
- v2.8 (06-04): best-of-N RETIRED for render-once + surgical regen -- a THROUGHPUT
  tradeoff for overnight batches, not a refutation; restored 08-04 for hero videos.
- v2.9 (06-04): "generation stays 960x512@24 (model rounds 544->512)"; retry regen with
  drift guard (REGEN_TAKES=3, input_strength 0.73); don't render lower to save time.

SITE (dropcatgo-generator) tracked history:
- 4dc58bf (07-28) SETTLED, do not re-litigate: Andrew A/B-judged 4 same-seed chains;
  the shipped baseline BEAT the ported Studio per-clip knobs (cfg-drop, subject-anchor
  each added fringing by clip 3). Remaining gap vs Studio = story-arc direction layer,
  not per-clip knobs.
- 3e305cf (07-28): chain "pox/boils" = anchoring on the blurred TAIL frame; anchor at
  the 88% mark + trim tail + lock camera. (Same lesson family as DCS 43d08b1.)
- Vendor video endpoint truths (dc_video.php header): width/height/steps honored,
  guidance IGNORED (cfg is live), always center-crops to 16:9, clip length FIXED
  ~5.16s, NO audio input -- native sing cannot run there, period.
- c253fba (08-03): BYO-song is a PORT of Studio's upload->isolate->lip-sync, not
  greenfield.

SITE dropcat-video-wan/ (dcg-sing) -- CORRECTION 08-04 (helper): this is its OWN git
repo (aba-ada-sam/dropcat-video-wan), nested inside dropcatgo-generator and gitignored
by the PARENT -- so it has real history of its own, but `git log` in the parent never
shows it and a session working the parent repo can miss it entirely. The knowledge-loss
risk below stands for anyone reading only the parent repo:
- BUILD_RUNBOOK "RECIPE STATUS: CONFIRMED" (08-04): clean pod (L40S) reproduced native
  LTX-2 audio-conditioning end-to-end; the 08-02 "deadlock -> LTX-2 is video-only"
  ruling was one box's TDR-era driver, NOT the technique. **The tracked handoff docs
  still said video-only/spike-LatentSync -- corrected 08-04 evening (helper assigned);
  without that, the next session re-spikes an answered question.**
- Same runbook found the SAME raw-mix bug independently: DCMVS_WANGP_ROOT's Windows
  default doesn't exist on a Linux pod -> isolate_vocals fail-softs -> full-mix
  conditioning -> instrument-following. Their fix: HARD-FAIL when isolation is missing.
  Adopt everywhere: the render path must not accept a raw mix (see 08-04 design rule).
- Their load-bearing constants: fps 24 (corrected from 25 against WanGP source),
  SING_MAX_FRAMES 249, sliding_window 481, guidance clamp 3.0, best-of-3. All agree
  with this ledger except SING_WIDTH/HEIGHT=640x360 -- see next entry.
- OPS RULE (paid ~$8 to learn): every pod self-terminates via on-boot
  (sleep <budget_s> && shutdown -h now) & disown -- sized to the $ cap, not the task.

THE RESOLUTION WAR, DISSOLVED (hypothesis, A/B pending): site says "above ~580p audio
tokens overflow" (measured at requested 1032x580); studio/DCMVS prove "960x544" syncs.
Both are about REQUESTED sizes. LTX-2 ceil-rounds to 64-multiples: "960x544" really
renders 960x512 (fits budget); "1032x580" really renders 1088x640 -- 33% more pixels
(plausibly the real overflow). If the A/B confirms, the rule is: think in ROUNDED
pixels; 960x512 actual is proven; 1024x576 (exact, no rounding) is the candidate step up.

PRODUCTION FACTS (site, live today): DCV_CLIPS=4 flat for everyone incl. admin; ~5.16s
vendor clips, 4.74c each, ~7min wall per video; $3/day/person, $8/day sitewide, $6/day
video sub-cap; 1 video in flight per person. A user tier for Andrew's 30s-of-10s-clips
sing product lives naturally as invites.max_clips next to budget_cents (resolved once
in dcv_submit, two consumption sites) -- but sing itself needs the dcg-sing worker
(Dockerfile.sing never built, network volume unprovisioned), NOT the vendor endpoint.
