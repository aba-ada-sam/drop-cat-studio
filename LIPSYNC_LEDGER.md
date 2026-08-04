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
  artifact-clean AND verdict=synced. Gates before ranking: yellow-glyph excess <=0.02%,
  transient-white (vs clip's own static bright mask) <=2.0%, luma mean drift <=25% of
  source / peak <1.6x source.

================================================================================
2026-08-04 -- THE BROKEN-RULER DAY (all entries same day, ordered newest first)
================================================================================
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

================================================================================
HISTORICAL LEDGER (from git archaeology of DCMVS, DropCat-Studio, dropcatgo-generator)
================================================================================
(being appended from the 2026-08-04 research sweep -- see board posts of that evening)
