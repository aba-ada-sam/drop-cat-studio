# Lip-sync handoff -- 2026-07-29 night

**UPDATE, later same night: ROOT CAUSE FOUND AND FIXED (commit 87883d5).** Everything below
this point was accurate at the time it was written but describes an intermediate state, not
the end state. The real remaining bug: this pipeline only used LTX-2's native SE (start+end
keyframe) mode -- "every clip ends on the original frame" -- for chained clips (2+); clip 0
(and any non-chained clip) ran plain I2V instead, which is why a seed/prompt/audio combo that
reliably syncs on DCMVS scored static/unsynced through this pipeline even after the
audio_scale/resolution/input_video_strength fixes below. Confirmed via matched-payload A/B
testing (identical request, only end_image_path present/absent, reproducibly flips
static<->synced across multiple seeds and a from-scratch worker restart -- ruling out
worker-instance state, seed-family luck, or anything else as the cause). Fix: `pipeline.py`
now sets `clip_end_image = clip_start_image` for every lip-sync clip. Post-fix hit rate on a
small fresh sample: 2/4 seeds synced on first try, consistent with DCMVS's documented
per-seed coin flip -- `best_of_n` (already implemented) is what makes that reliable in
production, not a 100%-every-seed expectation.

Session closing at Andrew's word mid-fix. Read this before touching lip-sync again.

## What's PROVEN tonight (don't re-derive)

1. **The mouth-box artifact is real** (confirmed by pulling actual frames, not just the
   sync_qc score -- see `LIPSYNC_HANDOFF.md`'s sibling note: score measures timing
   correlation only, not picture quality).
2. **It is NOT a 5080/Blackwell limitation.** `093faaa`'s "5080 cannot audio-sync" is WRONG
   -- it cited DCMVS's README (written 05-31, day one, before the recipe was tuned) without
   cross-checking DCMVS's own milestone docs (`C:\DCMVS-restored\_milestones\v2.0` through
   `v2.9`, 06-01 to 06-04), which document WEEKS of "best yet, user-confirmed" results
   running locally on this same 5080 with WanGP **v11.20**.
3. **Reproduced the fix directly**: same subject/song/recipe (LTX-2 Dev19B Distilled,
   960x544, 8 steps, audio_scale 0.6, seed 777) run locally via `chain.py --worker
   http://127.0.0.1:7899` instead of the 3060 satellite (`.55:7899`, WanGP v10.952) --
   clean result, no box, sharp mouth/teeth detail, `is_synced=True`, and faster (1.7min vs
   3.1min). Proof video + REVIEW.html + before/after frames:
   `C:\Users\andre\AppData\Local\Temp\claude\C--Users-andre\e013438a-93e5-4960-8f33-8a72d0d4ee7a\scratchpad\review\`
   (session-temp, may not survive -- copy out if it matters long-term).

## What's ALSO wrong in Studio's own pipeline (separate from the satellite/box issue, found
while tracing why Studio's native lip_sync has been bad generally, not just tonight)

4. **`features/fun_videos/video_generator.py`'s `_generate_via_worker` never sends
   `audio_scale` or `audio_prompt_type` to WanGP at all** -- only `audio_source` (the file
   path). Confirmed in `C:\pinokio\api\wan.git\app\wgp.py:4394`: WanGP defaults
   `audio_scale` to **1.0** server-side when omitted. DCMVS's proven recipe uses **0.6**.
   Over-driving audio-conditioning strength is a plausible contributor to a
   blurry/over-attended mouth region independent of the satellite-vs-local GPU issue.
5. **Studio generates lip-sync clips at 640x360** (`features/song_video/pipeline.py:606-614`)
   instead of DCMVS's proven native **960x544**, then cosmetically upscales via ffmpeg
   lanczos afterward (`pipeline.py:1171`). This is a DELIBERATE workaround (comment explains
   580p+/1032x580 overflows LTX-2's audio-token budget and silently drops conditioning
   entirely) -- but 960x544 sits BELOW that overflow threshold (proven by tonight's local
   chain.py test running at exactly 960x544 successfully on this same worker) and is well
   above 640x360 in raw detail, with no upscale-blur needed afterward.

## IN-PROGRESS, NOT FINISHED -- do not assume this works yet

`features/fun_videos/video_generator.py`: added an `audio_scale: float | None = None` param
+ docstring to `generate_video()`'s signature ONLY. **It is not threaded through to the
WanGP payload in `_generate_via_worker` yet, and `pipeline.py`'s call site (`_gen_one`,
~line 996) does not pass it yet.** This is a harmless no-op addition as committed (unused
optional kwarg) -- do not assume audio_scale is actually being sent.

## Next steps, in order

1. Finish threading `audio_scale=0.6` from `generate_video()` -> `_generate_via_worker`'s
   payload dict (add `if audio_scale is not None: payload["audio_scale"] = audio_scale` near
   the existing `audio_source` handling, `video_generator.py` ~line 395) -> add
   `audio_prompt_type="A"` alongside it (also currently never sent) -> pipeline.py's
   `_gen_one` (~line 996-1016) pass `audio_scale=0.6` when `_lip_sync` is True.
2. Change `pipeline.py`'s lip-sync resolution block (lines 606-611) from `tw, th = 640, 360`
   to `tw, th = 960, 544` (drop the `ow, oh = 960, 544` line since that's now redundant --
   generate AND target become the same). Confirm the `th <= 360` upscale-trigger check at
   line 1171 correctly no-ops at th=544 (it should, no change needed there).
3. **Verify end-to-end through Studio itself** -- not just chain.py directly. Trigger a real
   single-clip lip-sync job via Studio's own `/api/song-video/generate` route (or its UI) and
   pull frames the same way tonight's proof was checked, before calling this done.
4. Red-team per board rule 6 before declaring it fixed to Andrew.
5. Only after that: revisit `best_of_n` batch-route wiring (`routes.py` `/batch/start` doesn't
   accept it, `batch_runner.py` doesn't pass it -- see board post 2026-07-29 ~21:50 from this
   session for the full gap list) and the frontend UI controls (`tab-music-video.js:246-248`
   has stub `satCheck`/`satWrap` objects, no `best_of_n` field at all).

## Explicitly NOT the plan anymore

Wiring/fixing the 3060 satellite dispatch (`SATELLITE_ENABLED=False`, stale `.49` relay IP,
missing upload-to-satellite step, untested download path -- full list was in this session's
research). Local dispatch already works and now looks BETTER than the satellite anyway --
don't spend time repairing satellite plumbing unless Andrew specifically wants
second-GPU parallelism back for throughput, not quality.

## Board

Posted to Studio board (`CLAUDETEAM_WORLD=studio`) under role `cpuguard`, session tag
`e013438a`: the root-cause finding + the correction to `093faaa`. Claim on "switch lip-sync
to local dispatch + proven recipe params" is being released with this handoff -- pick it back
up tomorrow.
