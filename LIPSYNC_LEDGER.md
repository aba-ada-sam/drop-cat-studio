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
- Model: LTX-2 Dev19B Distilled via WanGP worker :7899. THE LIVE INSTALL IS
  C:\pinokio\api\wan.git\app AT COMMIT 3ba3863 -- verified 2026-08-04 night by PID
  (netstat :7899 -> pid -> command line), NOT C:\WanGP-10952, which also exists on this
  box at a different commit (1437067) and is NOT what renders. The running worker script
  is C:\DropCat-Studio\services\wangp_worker.py (DCS's copy), launched by pinokio's
  miniconda python. Anyone reproducing this stack, 3060 included, must match THAT path.
- THREE files are locally modified in the live WanGP, not two: (1) models/ltx2/
  ltx2_handler.py audio passthrough, (2) models/ltx_video/configs/ltxv-13b-0.9.8-dev.yaml
  skip_block_list=[], and (3) defaults/ltx2_distilled.json, which still carries
  tea_cache:true + skip_steps_multiplier 1.75 + skip_steps_start_step_perc 0. The
  TeaCache REVERSAL entry below says the config was "restored as-found" -- that is
  FACTUALLY WRONG, git diff shows it still modified. The reversal's CONCLUSION is
  nevertheless correct and re-verified 2026-08-04 night: wgp.py:827 does
  `if not any_steps_skipping: skip_steps_cache_type = ""` and the ltx2 handler declares
  no tea_cache/skip_steps at all, so the setting is inert for LTX-2 no matter what the
  worker pushes (wangp_worker.py:196-198 does set cache type "tea"). Leave it alone, but
  COPY IT for parity -- a box cloning only "the 2 patches" is not running our stack.
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
2026-08-05 -- THE MORNING THE RECIPE GOT RATIFIED (newest first)
================================================================================
- ACTIVE (morning, helper) | PRODUCTION RECIPE HUMAN-RATIFIED, three verdicts in 90
  minutes, one variable each: v3 chain.py beats the DCS assembly ("much better") ->
  v4 frames-per-clip 241 fixes eye drift ("better consistency") -> v5b crossfade
  0.15s with overlapped audio solves seams ("I'd call that issue solved"). Full
  command in RECIPE.json "production". Seam lessons: dip-to-black REJECTED; the
  June 0.4s-dissolve rejection was about LENGTH, not dissolves -- 0.15s inside the
  anchor zone reads clean. Rollback map verdict stands: nothing reverted, all four
  failure mechanisms were May-era, exposed (not created) by the week's E2E work.
- ACTIVE (morning, helper) | MUSETALK STACKING DEAD ON ALL ROUTES (Andrew reversed
  his June 19 "Native + MuseTalk both" on today's evidence: native-only render
  approved, post-pass crashed the server 23:27, c134c63 creature finding). Both
  route defaults False; pipeline gates the post-pass off whenever native
  conditioning ran, even if explicitly requested. MuseTalk remains reachable for
  unconditioned jobs only.
- ACTIVE (morning, helper) | MAY-ERA TIMELINE BUGS FIXED FORWARD (2d5612a):
  _clip_start_times now uses sing_grid's assembled-position math (0.000ms drift
  across N=1..10, vs +3.00s by clip 12 on the old formula); requested clip
  duration WINS over the arc's everywhere (render, slice cut, energy window --
  one resolver, three call sites); window_delivery real end to end (tier jobs mux
  the rendered window, never loop short video across the full song).
- LESSON (morning, helper) | STREAM-EXISTS IS NOT CONTENT-CORRECT, twice in 12
  hours: the judge page served guide-audio clips ("has audio" checked, WHAT audio
  did not), then my dip build shipped all-black video ("has video stream" checked,
  pixels did not). Standing rule now: any artifact reaching Andrew's page gets a
  frame extracted and LOOKED AT, and audio identified (music vs guide), first.

================================================================================
2026-08-04 -- THE BROKEN-RULER DAY (all entries same day, ordered newest first)
================================================================================
- ACTIVE (23:15, helper, overnight) | 60s FAILURE: CONDITIONING EXONERATED, SELECTION
  IS THE PRIME SUSPECT. Measured all six work2 slices of the rejected adam60_v2 run:
  every one carries real vocal energy (mean -28.6..-32.8 dBFS, peak -9.8..-13.9,
  matching the healthy stem profile -30.6/-9.8). So the "were the sung windows even
  conditioned" question closes: YES, all of them. With inputs clean and the same
  recipe/span producing blind-labeled GOOD takes, what distinguishes the failed cut is
  that its clips were CHOSEN from 4 takes each by mouth_sync_score -- the scorer since
  proven not to track Andrew's judgement (his approved takes score 0.001-0.134; drift/
  morph takes clear the floor). A broken ranker given 4 candidates per clip does not
  merely fail to help, it plausibly ANTI-SELECTS. Third eliminated cause tonight
  (VAD under-detect, muted excerpt, now conditioning); the overnight 30s renders keep
  per-take artifacts so the morning page can show Andrew alternatives, not just picks. -- Andrew judged all 12 clips on
  the shuffled no-names judge page (labels.json, lipsync's scratchpad): ALL THREE
  anchor-ON takes GOOD (s101, s202, s303), ALL THREE anchor-OFF takes BAD, all six 60s-
  run segments SKIP. Perfect 6/6 separation on the anchor condition. This SUPERSEDES the
  earlier relayed reading ("seed 101 is the only one that does not suck") -- side-by-side
  framing narrowed his verdict; blind and shuffled he approves every steady take.
  Consequences: (1) SE_END_ANCHOR fully vindicated, stays TRUE; (2) three usable takes
  exist on that span, not one; (3) the framing hypothesis SHARPENS rather than dies:
  A-cell s202/s303 measurably drift (tracker offsets -98,-35 / -120,+75) and are still
  GOOD -- mild drift is acceptable; the B-cell push-in + identity morph is what gets
  rejected. "Holds framing" was the wrong variable; "keeps identity/shot integrity" fits
  all 6. (4) CORRECTED same night by 3060-plan: my "audio present so not a mux defect"
  was STREAM-EXISTS-NOT-CONTENT-CORRECT -- the judge page served RAW PRE-MUX work2/
  intermediates whose aac track is the isolated 150Hz-high-passed vocal GUIDE (measured
  7-9dB quieter, low end gone), i.e. Andrew was asked to judge a music video with no
  music. The six skips are VOID as evidence. Fixed: six clips re-muxed against the
  assembled cut's healthy audio at verified 10s offsets (re-mux profile -20.8/-24.5
  matches the good clips), originals in work2/premux_backup/, void skip labels cleared
  so the page re-offers them. RULE (3060-plan's, best lesson of the night): VERIFY THE
  STIMULUS BEFORE THE PROTOCOL -- four careful blinding rules, zero checks that the
  thing being judged was fit to judge; ceremony made a broken stimulus look rigorous.
- ACTIVE (late night, helper, Andrew-delegated ruling) | SYNC-OR-DIE DISARMED: the gate
  was inverted against ground truth on first human contact. Of the six ablation takes
  Andrew approved exactly ONE (seed 101 anchor-ON): verdict=static rank 0.001 -- which
  the floor REFUSES to bank -- while all three takes that CLEAR the floor (synced,
  0.15-0.18) he rejected. His stated reason, verbatim, now the calibration label of
  record: "the mouth didn't open while words were being sung." Under the standing
  no-validation-no-gating rule the floor's refusal authority is suspended:
  SYNC_ENFORCE=False in pipeline.py, verdict+rank still measured, WOULD-HAVE-REFUSED
  logged per take so evidence accumulates, SyncFloorNotMet unreachable, enforcement
  path kept and covered by an explicitly re-armed test. The WINDOW-ENERGY conditioning
  gate (audio going IN, validated against a human-confirmed failure) is a different
  instrument -- still ARMED; distrust is not transitive across instruments. Re-arm only
  in the same commit as a scorer recalibrated against human-labeled takes.
- ACTIVE (late night, lipsync) | AN EYEBALL MASQUERADING AS A MEASUREMENT -- three
  sessions built a validation design on a fact none of us measured. We asserted the
  anchor-ON trio (A_baseline s101/s202/s303) was CAMERA-LOCKED in all three, so framing
  stability could not explain Andrew's labels. 3060-plan proposed it from a 320px-wide
  7-frame contact sheet, I refined it, brains RATIFIED it. IT IS FALSE: s202 and s303
  both reframe mid-clip (measured tracker offsets -98,-35 and -120,+75 on a 36px ROI;
  overlays at scratchpad/sheets/track_A_baseline_s*.png). Only s101 -- Andrew's pick --
  actually holds framing. SE_END_ANCHOR REDUCES drift, it does not ELIMINATE it, and we
  all inferred the stronger claim from the setting's name.
  CONSEQUENCE: the confound was never removed, and a SIMPLER HYPOTHESIS fits all six
  human labels -- Andrew accepts the clip that HOLDS ITS FRAMING and rejects every clip
  that REFRAMES (6 of 6). No mouth metric has any evidence behind it yet. We cannot
  separate "wants a steady shot" from "wants the mouth to move" with the data we have.
  RULE: a property that a validation design DEPENDS ON must be MEASURED on every clip
  and reported per clip, never asserted from a thumbnail or inferred from a setting's
  name. A contact sheet shows you a hypothesis; it does not test one.
- ACTIVE (late night, lipsync) | APERTURE-DURING-VOICED: BUILT, RUN, RESULT DISCARDED --
  and discarded by looking, not by scoring. scratchpad/aperture.py implements Andrew's
  verbatim spec ("the mouth didn't open while words were being sung") as region + open-
  ness + voiced-only, headline number = longest run of voiced frames with a closed mouth.
  It reported core-cell PASS (0.586 vs 0.403/0.575) and full-set FAIL (clip_002 at 0.725
  above the good clip). BOTH NUMBERS ARE VOID: rendering the ROI box onto the frames
  showed the tracker sliding off the face -- by mid-clip it sits on the alien's SHOULDER
  in s303 and on background beside the head in s202. The 0.586-vs-0.575 "pass" was noise
  between two different body parts. Cause: single-scale template matching cannot survive
  a push-in or a head turn; needs scale-invariant matching or per-frame head re-detection.
  METHOD NOTE WORTH KEEPING: the overlay check cost five seconds and caught a result that
  would otherwise have been posted as a validated pass. Any ROI-based metric must ship
  with a visual ROI-placement check that a human looks at BEFORE its numbers are believed.
  Also retired here: I had labelled all six 60s clips BAD by INHERITANCE from Andrew
  rejecting the assembled video -- inference-as-ground-truth, the exact error this
  ledger warned against hours earlier. clip_002 outscoring his pick may simply mean
  clip_002 is fine. Those clips now go to him individually via the judging page.
- ACTIVE (late night, lipsync) | JUDGING PAGE, the actual unblock: scratchpad/judge.py
  on :7932. 12 shuffled clips, Good/Bad/Skip, labels written per vote. NO scores and NO
  filenames reach the browser (the page gets only an integer id) because both would leak
  the hypothesis under test and bias the label. Framing-stable and framing-drifting clips
  are deliberately mixed so his labels separate the two live hypotheses. THE STANDING
  LESSON THIS ENCODES: every metric failure today came from calibrating against a
  reference we chose ourselves. Rulers are cheap, labels are expensive, and we kept
  building rulers. Buy labels first.
- REVERSED same night, BY ANDREW'S EYES | "total_motion is the discriminator" and "the
  end anchor is the motion killer" -- BOTH DEAD. He watched the six-clip A/B and ruled:
  seed 101 ANCHOR-ON is the only one that does not suck. That is the take my meter scored
  WORST (motion 0.52, verdict static, score 0.001); the three anchor-OFF takes I called
  SYNCED at motion 3.36-3.71 he rejects. CAUSE, found by pulling contact sheets and
  LOOKING instead of scoring: the anchor-OFF take PUSHES THE CAMERA IN, dollying from the
  source framing to a head-and-shoulders close-up, and the skull MORPHS on the way --
  rounder, larger, different facial structure, arguably a different creature by the end.
  The end anchor was never a motion damper. It HOLDS FRAMING AND IDENTITY, exactly as
  named. Removing it freed the CAMERA and the FACE, not the mouth. total_motion counts
  whole-frame pixel change, so a slow push-in plus a morphing head scores enormous while
  a steady shot with a moving mouth scores near zero -- I ranked camera drift and identity
  collapse as lip sync.
  THE ERROR UNDERNEATH, and it is the reusable lesson: I calibrated the 2-13 "golden band"
  against AWM00001 -- which THIS LEDGER ALREADY RECORDS AS A MULTI-SCENE CUT VIDEO. A cut
  video's frame-to-frame change is dominated by its cuts. I compared single-shot 10s clips
  to a montage and read the gap as a quality deficit, having personally written down the
  fact that made the reference invalid. RULE, strengthening the no-ungated-metric rule:
  a metric is NOT validated against a known-bad reference unless a HUMAN STATED why that
  reference is bad. Inferring the reason, then measuring the inference, is circular -- the
  24 rejected takes were rejected for "does not lip sync", and I ASSUMED low motion was
  the cause rather than a correlate. Also: a reference must match the FORM of what it
  scores (single shot vs cut montage) or the comparison is meaningless.
  CONSEQUENCES: SE_END_ANCHOR STAYS TRUE in both codebases. The four-damper entry below is
  dead as a diagnosis -- input_video_strength 0.69 and the "steady framing" prompt wording
  are IDENTITY/FRAMING HOLDS TOO, so loosening them would likely produce MORE of what
  Andrew just rejected. The 144f-vs-233f A/B is UNBLOCKED (the confound I claimed does not
  exist). chain.py CLI flags 10c2a80 stay (additive, default-preserving) but nothing should
  USE the off setting. STILL UNEXPLAINED and where to start next: he rejected the 60s cut
  for losing sync after ~10s while calling a single anchor-ON clip acceptable, so the fault
  is more likely ASSEMBLY, per-clip variance across a 6-clip run, or later-clip
  conditioning -- NOT the base render settings.
- SUPERSEDED, see the reversal directly above -- kept because the reasoning shows how a
  plausible metric became a false diagnosis | WE HAVE BEEN MEASURING THE WRONG VARIABLE.
  THE MOUTH IS NOT DESYNCED, IT IS BARELY MOVING. Evidence: the 60s production run (adam60_v2,
  6 clips x best-of-4) returned verdict=static on 24 of 24 takes, total_motion
  0.4-1.1. Golden AWM00001 scored on the SAME meter, grid and song: motion 2-13.
  The bands do not overlap. Meanwhile our mouth_sync_score ranks (0.016-0.220)
  straddle the golden's own 0.070 median -- which is exactly why the metric kept
  reporting "at or above golden" while Andrew kept saying, correctly, that it does
  not lip sync. A near-still mouth trivially "matches" the audio envelope; rank
  rewards correlation, not amplitude. This is the same invisibility class as the
  silent-guide bug: a still mouth scores clean.
  RULE: total_motion is now a REPORTED, GATED quantity alongside sync rank, and no
  take is deliverable on rank alone. Validated per the no-ungated-metric rule on
  both sides BEFORE gating -- known-good = golden AWM00001 (2-13), known-bad = the
  24 takes Andrew rejected (0.4-1.1). Non-overlapping, so the discriminator is real.
- ACTIVE (night, lipsync) | FOUR MOTION DAMPERS ARE STACKED IN chain.py, and the
  code documents two of them against itself. Found by reading for cause instead of
  re-rolling seeds. (1) SE_END_ANCHOR (chain.py:242, default True) pins each clip's
  END frame to the SAME pristine source still it STARTS from -- with SECTION_CLIPS=1
  every clip is bookended by two copies of one image. chain.py's own comment at
  235-241 concedes "the subject winds back to its neutral pose in the last 1-2s of
  each clip". No CLI flag exists to disable it, and main() never even threads the
  se_end_anchor kwarg into chain_video(), so the CLI cannot reach it. (2)
  input_video_strength defaults to 0.69 (worker fallback), commented at chain.py:256
  "too high starves the audio-driven mouth motion" -- and ACTION shots deliberately
  drop to 0.5 (ACTION_INPUT_STRENGTH, chain.py:288) precisely to get more movement,
  while SING clips, which need mouth movement most, keep the tighter value. (3) The
  sing prompt itself is a damper: PROMPT_DEFAULT / SUBJECT_PROMPT_TEMPLATE contain
  "centered and alone in the frame", "Stay in the original scene and setting" and
  "steady framing", applied to every sing clip with no flag to remove them. (4)
  motion_amplitude is pinned at its floor value 1 in core/wangp_models.py:93
  SAFE_DEFAULTS and reaches WanGP only through a generic setdefault loop -- no
  payload field, no CLI flag, no override path anywhere in either codebase.
  STATUS: ablation RUNNING (scratchpad/motion_ablation.py) -- one identical audio
  span, identical seed triple per cell, one-factor-at-a-time A-E over (1)(2)(3);
  (4) needs a code change to test and is deferred. Verdict lands as its own entry.
  Note this also CONFOUNDS the pending 144f-vs-233f clip-length A/B: with the end
  anchor on, a longer clip spends proportionally more of its length pinned between
  two copies of one still, so a length result measured today measures the anchor.
  Clip length gets re-measured only after the anchor question is settled.
- ACTIVE (night, Tier-2 WIRED, helper) | A SAFEGUARD THAT CANNOT FIRE, AND A SCREEN
  THAT INVERTED ITSELF -- both found by rule-6 on the wired path, both live in the
  integration for ~30 minutes before the review. (1) The window-energy check was a
  MEASURED NO-OP: every window went in with labelled_sung=None and no intervals, and
  all three disagreement branches require one or the other, so 12 windows and 5.95s of
  real measurement produced ZERO log lines while the energy data (0.36-0.86 voiced) was
  computed and discarded. The check written to catch THIS FILE'S 79-second
  undriven-mouth bug could not have detected it. Cause: _isolate_guide_vocals already
  computed voiced_intervals and threw them away. Verified after: 0 disagreements
  before, 4 after, same stem. (2) The artifact screen INVERTED where it mattered most:
  all-takes-infested returned None, and pipeline.py:1248 reads None as a dead render --
  restart WanGP, wait 90s, re-render ONCE WITH NO SCREEN. So it discarded N real takes
  and shipped an unscreened one. Rate is not hypothetical: 41pct of this session's own
  takes screen infested (37/90 sampled), so at best_of_n=3 a 12-clip job is
  better-than-even to trigger a needless worker restart -- this ledger's own "01_1: 4/4
  ribboned" predicted it. Now held back as a ranked last resort with a loud
  LOOK-AT-THIS-CLIP. (3) The worker's explanatory refusal never reached the user
  (video_generator logs it and returns None; the not-clip_path branch never set
  _last_error), so the new hard-fail was feeding MORE traffic into the standing
  "No clips generated" bucket -- the ~23pct unsolved item in STILL OPEN below is where
  specific diagnoses go to die. LESSON: an integration needs its own adversarial pass.
  Every module was individually correct and separately tested; all three defects lived
  in the WIRING between them, where no unit test looks. USEFUL NEGATIVE RESULT from the
  same pass: full-res screening costs ~2.3s/take, 3-4pct of render time -- screening in
  the best-of-N loop is cheap, and the expensive mistakes were the restart path and an
  early-accept condition that silently burned all N seeds whenever the screen was
  unavailable.
- REVERSED same night by its own red team (Tier-2 port, helper) | "MEASURE WINDOW
  ENERGY" IS NOT ENOUGH -- THE STATISTIC IS THE WHOLE RULE. The first cut of
  window_energy.py used the MEAN dBFS over a window, and a mean is not
  scale-invariant: at the REAL 19.7s production window size an ~8s sung passage
  surrounded by digital silence averages -42.6 dBFS, below the -40 floor, and is
  built as unconditioned filler. The gate written to stop the 79-second
  undriven-mouth bug REPRODUCED it, on the same song, at the same grid, and
  passed its own 12-assertion suite while doing so (the suite's only negative
  control was synthetic -91 dBFS silence, ~50 dB from the boundary, so any floor
  in a ~60 dB range passed). It was also self-contradicting: a strict SUBSET of
  a window classified the opposite way. FIX: the ledger's rule already said
  "~-40 dBFS / ~20pct voiced" -- the PERCENTAGE is load-bearing and had simply
  not been implemented. Now 0.5s probes, fraction above floor. SECOND FIX,
  equally important: the rule is ONE-DIRECTIONAL. The draft also enforced the
  reverse (below floor => do NOT condition), which is not in the ledger and let
  a measurement override a CORRECT human label while telling the operator the
  map was stale. Energy may only ever ADD conditioning, never remove it.
  LESSON, general: when a rule is written as "X above a threshold", the
  threshold gets all the scrutiny and the STATISTIC gets none -- ask what is
  being averaged, over what window, and whether the answer changes with window
  length. And a validation suite whose negative control is synthetic is not a
  validation suite.
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
- PRODUCT DECISION (late night, Andrew) | FORMAT IS NOW 60s ADMIN / 30s USERS, built from
  ~9.7s clips (233 frames). Full-song (210s) is retired: 17 windows spread the take budget
  so thin that most windows shipped their best CLEAN-but-static take, which is exactly what
  Andrew rejected twice. Clip length picked from measurement, not taste: grip holds below
  ~250 frames (4/4 synced at 249f vs 1/21 at 465-473f), tonight's two best in-file scores
  came from 233f clips (0.2185, 0.1753), and 201f also synced (0.1433) -- so anywhere in
  ~8-10s works and 233f sits under the cliff instead of on it. 30s = 3 clips, cheap enough
  to re-roll the whole video. ACCEPTANCE CHANGES WITH IT: sync-or-die on voiced windows
  (verdict synced AND rank floor), not "best available" -- see the helper's sync-or-die
  wiring. Site path cannot lip-sync at all yet: its render endpoint takes prompt+image
  only, no audio input, so 30s there means silent until an audio-capable endpoint exists.
- ACTIVE (late night) | ASSERT ENERGY WHERE THE CONDITIONING INPUT IS CREATED, NOT WHERE IT
  IS CONSUMED. Third instance of one invisibility class in one night: (a) the site's silero
  under-detect -> digital-silence conditioning, (b) my window map labelling sung windows
  instrumental, (c) my own 60s test excerpt, cut with
  `ffmpeg -i vid -vn -ss 59 -t 60 -af afade=t=out:st=59.7` -- output-seek preserves stream
  timestamps at 59+, so the fade fired ~1s in and MUTED THE REST. Slices measured -180 dBFS
  (digital silence); the model correctly produced a still mouth; chain.py's isolation looked
  broken (1pct voiced) while faithfully isolating silence; every artifact gate passed. A
  SILENT GUIDE ALWAYS SCORES CLEAN, because a still mouth genuinely matches no audio.
  RULE: every conditioning artifact is energy-checked at creation (voiced fraction + peak
  dBFS logged), a silent guide ABORTS the job loudly, and any hand-cut excerpt is verified
  before it is used -- source at 59-119s measured 99pct voiced, so the pipeline was never
  the problem. Cost: one wasted 3-clip run plus an hour of misdirected diagnosis.
- TOOLING (late night) | DEV DASHBOARD replaces the GPU-thermometer page:
  C:\Users\andre\GPUWatch\devdash.py (port 7931, "Start Dev Dashboard.bat"). Shows what a
  decision actually needs -- every seed's score/verdict/motion with the KEPT take marked,
  the run's settings pinned at top, silent-failure alerts (resolution clamp, worker
  eviction, step-0 deadlock, suspected silence, "kept best available"), and run history
  persisted to runs.json so a recipe change is judged against the previous run instead of
  memory. Andrew on v1: "I can't see a damn thing about what's doing what in any way that
  would inform a decision" -- utilization and temperature are not decisions.
- DELIVERED (late night) | FIXED5 v3 shipped on review_0004.html: all 17 vocal-bearing
  windows now CONDITIONED renders (unconditioned singing time 79s -> 0s), 18-segment
  rigid grid frame-exact, tail probe -1 sharp, 20/20 windows + 17/17 seams eye-passed
  (9 adversarial inspector passes tonight; every flag senior-adjudicated). In-file sync:
  top windows 0.2185 / 0.1753 vs golden's median 0.07 on the same meter; means 0.05 vs
  golden 0.11 (3 of ours are proven meter-instability zeros). Remaining honest gaps:
  per-window word-lock varies with stem loudness; golden's edge is performance energy +
  multi-scene format (Andrew's format call, not a repair item).
- ACTIVE (late night) | FIFTH ARTIFACT CLASS -- PHANTOM LIMB: a huge hallucinated
  hand/forearm intruding from the frame edge (candI_06_0, rejected). Red screen caught
  it via flesh tones (p95 0.2496 vs known-bad 0.157); class added to eye-pass checklists.
- ACTIVE (late night) | SOURCE IMAGE IS THE FALSE-POSITIVE TIEBREAKER: an inspector
  flagged all four 02/04 windows for an identical 'strand + maroon blob' on the
  background man -- identical placement across 4 windows x 56 frames x independent
  seeds, which is impossible for generation slop and diagnostic of SOURCE CONTENT
  (it is the tool strap in awm_alien_source.png; matched crops confirmed). Rule: an
  artifact that repeats at the same coordinates across independently-seeded takes is
  scene content -- check the conditioning image before rejecting anything.
- REJECTED same night at 0:17 | FIXED5's real remaining flaw was the WINDOW MAP, not the
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

================================================================================
2026-08-04 ~22:05 -- THE PLAN (owned by helper, per Andrew's direct mandate)
================================================================================
Andrew's instruction, verbatim in intent: one session owns the cross-front plan and
actively guides the other two through it with regular check-ins, catching mistaken
understandings before they cost real time -- as has already happened repeatedly tonight
(TeaCache red herring, the broken artifact gate, FIXED4 ribbons, the muted-excerpt bug,
3060-plan's stale retirement citation). helper (a085855e) owns this from here. A future
session picking this up should re-derive from this section, not from re-reading the whole
board.

THREE FRONTS, ONE DEPENDENCY CHAIN:

FRONT 1 -- local studio (DCMVS-restored + DropCat-Studio convergence). Sequence:
1. lipsync: motion ablation (running ~18 min from 22:01) isolates which of 4 stacked
   dampers actually kills motion -- SE_END_ANCHOR, input_video_strength 0.69, restrictive
   prompt wording ("steady framing"/"centered and alone"), motion_amplitude floor=1.
2. lipsync: apply the ablation's answer to chain.py (already adding --no-se-end-anchor +
   --audio-cfg flags as of 22:03 -- helper does NOT touch chain.py's argparse until
   lipsync posts done, per their own conflict flag).
3. lipsync: THEN re-run 144f-vs-233f -- was blocked on this exact confound (a longer clip
   spends more of itself pinned near the end-anchor, so a number measured today would be
   measuring the anchor, not the length).
4. helper: RECIPE.json schema + loader, built now with TODAY's values as a no-op refactor
   (motion fields named but inert until steps 1-3 land), both DCS tiers.py and DCMVS
   chain.py reading from it, shipped behind parity tests.
5. helper: fold the ablation's real values into RECIPE.json once lipsync posts them,
   including a motion floor in the acceptance rule -- current SYNC-OR-DIE (rank>=0.12) has
   the same blind spot the motion finding just exposed, it never checks motion at all.
6. helper: energy-at-creation hard-fail (whole-stem + per-slice per lipsync's correction,
   abort not warn) -- independent of 1-5, building in parallel.
7. helper: live E2E of DCS's own wired path, once lipsync posts GPU FREE (~18 min from
   22:01) -- the one thing no fixture has proven yet.

FRONT 2 -- dropcatgo.com. NOT an engineering front right now: both branches
(sing-audio-conditioning 04c6ff3, sing-continuity-2026-08-04 0bac02e) are tested,
manifested, and sitting on pure go/no-go from Andrew. RunPod Sing endpoint needs his ~$3
spend nod. Nothing for sing-cloud to build until one of those lands.

FRONT 3 -- 3060 management. Sequence, and this is the part most at risk of a mistaken
understanding: 3060-plan is doing config-parity now (assigned 22:02) and should move to
measuring the resolution ceiling next -- but real benchmarking/dispatch on the 3060 should
WAIT for front 1's recipe to stabilize (motion fix + clip-length decision). Benchmarking
against a recipe that is about to change is wasted work and produces numbers nobody can
trust. Freeze-risk tolerance / keep-or-kill Forge / Storyteller interest are real open
questions but they block LATER phases (unattended overnight use), not config-parity or the
resolution measurement -- don't let those sit idle waiting on Andrew when they are not
actually blocked.

CHECK-IN MECHANISM: recurring board check every 5 min (session-only cron, job 7b3ff732),
watching both StudioTeam and DropCatTeam boards for stalls, unanswered questions, and
anything needing Andrew's call specifically. helper reports to Andrew only when something
changed, stalled, or needs his decision -- not on every board post.
