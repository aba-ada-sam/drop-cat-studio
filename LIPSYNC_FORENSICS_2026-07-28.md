# Lip-sync forensics -- why the successful videos stopped (2026-07-28 night)

Andrew: "I've got so many successful lip sync videos, and now we can't manage to do that anymore."
He is right. Forensic sweep of outputs, job metadata, and git history:

## The one human-confirmed success

`C:\DCMVS-restored\_milestones\v1.0_lipsync-seed-selection_2026-05-31\proof\DEMO_cat_magazines_10s.mp4`
(2026-05-31, Andrew's verdict in MILESTONE.md: **"amazingly good lip sync"**)

Made by **DCMVS** (separate app, `C:\DCMVS-restored`) driving the **RTX 3060 satellite**
(192.168.86.49:7899, WanGP v10.952). Recipe:

- LTX-2 Dev19B Distilled, **960x544 native**, 249 frames / 24 fps, 8 steps, guidance 3.0
- `audio_prompt_type=A`, `audio_scale=0.6`, `input_video_strength=0.69`, `sliding_window_size=481`
- **best-of-3 seeds gated by sync_qc.py** (at 8 steps, WHERE audio motion lands is a per-seed
  coin flip -- mouth vs eyes; seed selection is what made it reliable, not any single param)
- Subject: photoreal cat-in-suit -- clear, front-facing mouth

## Why the current pipeline cannot reproduce it

Ranked causes (evidence in the report; agent sweep 2026-07-28 ~23:50):

1. **Wrong GPU.** DCMVS's own README: the RTX 5080 (Blackwell) could NOT lip-sync in this model
   in testing, even with exact SDPA attention. Studio's song_video routes to the local 5080.
   A `use_satellite` flag exists in `features/song_video/routes.py` but defaults False.
2. **Wrong resolution.** DCMVS proved 960x544; Studio force-generates 640x360 (audio-token
   overflow workaround) and only upscales cosmetically.
3. **WanGP version drift.** Satellite ran v10.952; the 5080 install is now v11.20.
   Known sensitivity: `audio_cfg_scale` must be >1.0 on v11.x, omit on v10.952.
4. **Subject style.** Every confirmed success used an animal/non-human face with a clear mouth.
   The 07-24 "total fail" subject (00063, stylized blue-skin humanoid) has no proven success
   in ANY era -- do not use it as the acceptance test.
5. MuseTalk was never the successful era: **all 12 historical `_ls` outputs sit in
   output\Inbox\Trash** -- tried and rejected repeatedly (paste-box).

## Shortest path back (PENDING ANDREW'S GO -- satellite is off per his 2026-06-19 directive)

Revive DCMVS directly: start the 3060 satellite worker
(`Z:\My Drive\1 Apache Directions\DCMVS Satellite\Open this to run DCMVS Satellite.bat`),
then drive `C:\DCMVS-restored\chain.py --seeds-per-clip 3` (or its GUI, app.py :7900;
config.json has the satellite worker_url option ready). Validate on a clear-mouthed animal
subject FIRST, then try harder subjects. Note: the 3060 cannot run Storyteller/KoboldCpp and
the satellite worker at the same time. Check `opencv-python-headless` is NOT reinstalled in
Studio's Python310 (silently breaks face-crop).

Staying inside Studio instead = flip `use_satellite` + solve 960x544 audio-overflow on v11.x.
More work, untested; do only after DCMVS proves the satellite still delivers.
