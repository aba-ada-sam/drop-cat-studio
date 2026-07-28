# DropCat Music-Video Lip-Sync — HANDOFF for the next instance

## SESSION 2 UPDATE (sid ae64e3bf, 2026-07-24 ~9:15pm EDT) -- READ THIS FIRST

Andrew's verdict after this session: **"total fail... stop."** BOTH local lip-sync paths were
tried end-to-end on the human-faced creature (00063-4076602478.png + the 15s test song) and BOTH
were rejected by Andrew on screen. Do NOT re-run these expecting a different result:

1. **Native LTX-2 audio conditioning** (what Session-1 handoff below calls "the solved mechanism"):
   mouth moves but **ZERO sync to the audio** (Andrew: "matches nothing"). I verified EVERY recipe
   piece was active this run -- 640x360, Demucs-isolated + VAD-gated vocals, `audio_prompt_type=A`,
   `audio_scale=0.6`, **`audio_cfg_scale=3.0`** (logged), face-crop framing, `best_of_n=3` sync-gate.
   Still zero match. **The Session-1 handoff's "native conditioning is solved" claim is WRONG.**

2. **MuseTalk** (the studio's REAL lip-sync feature, `features/lipsync/runner.py`; IS installed at
   C:\MuseTalk w/ v15 weights, IS patched for creature faces): produces **real word-level sync**
   (mouth opens on sung words, rests in gaps -- confirmed in pixels) BUT ships **a glitchy soft
   rectangular paste-box over the mouth + a quality drop**. Causes: MuseTalk re-renders the mouth at
   ~256px and pastes back (soft box, fundamental); face-parse blend degrades on the stylized BLUE
   skin; v15 IGNORES `bbox_shift` (can't tune it). Mitigations I tried: crf 18->14 (helped quality)
   and a **tight face close-up** (helped the box a lot). Andrew still: "total fail, worse than the
   last couple." NOTE: historical `_ls` (MuseTalk) outputs are mostly in output/Inbox/Trash =
   MuseTalk was tried and rejected before, for the same reason.

**RECOMMENDED NEXT DIRECTION (not yet started -- Andrew said stop first):** the HOSTED
image+audio -> singing-avatar route (Hedra-class), exactly as the "sing" instance's handoff
recommends: `C:\Users\andre\dropcatgo-games\dropcat-video-wan\HANDOFF.md` (written 2026-07-24 5:50pm,
"stop, change models, this isn't working"). It regenerates the whole face from image+audio, so it
structurally CANNOT have MuseTalk's paste-box. Test it on THIS creature first (many avatar APIs
reject non-human faces). Weigh cost/latency/creature-face-quality WITH Andrew before building.

### What I changed this session (all in the working tree, uncommitted; keep unless Andrew says otherwise)
- `features/song_video/pipeline.py` `_merge_video_audio_trim`: replaced the whole-video **loop** with
  a last-frame **freeze** for small gaps (loop only as a >half-song safety net). VERIFIED in pixels
  (frozen tail, full song covered, no restart). This is a genuine fix, unrelated to the sync problem.
- `features/lipsync/runner.py`: composite encode crf 18 -> 14 (quality).
- ENV FIX: your Python310 had BOTH `opencv-python 4.13` and `opencv-python-headless 5.0` installed,
  colliding into a cv2 with NO `CascadeClassifier` (broke face-crop silently). I removed headless and
  reinstalled `opencv-python==4.13.0.92`; `CascadeClassifier` now loads. (This is real fix regardless.)
- `features/song_video/routes.py`: I reverted my own clip-count experiment; the only non-comment diff
  there is a teammate's pre-existing `lip_sync` default True, NOT mine.
- Failed-attempt videos on the Desktop (safe to delete): REAL_lipsync_creature.mp4,
  SYNCFIX_lipsync_creature.mp4, MUSETALK_creature.mp4, MUSETALK_closeup.mp4.

---

Written 2026-07-24 ~8:05pm EDT. Session was the **local 5080 project** instance (not the RunPod session).
Andrew's verdict: *"Ok, we're getting some lip sync now. It doesn't match the audio, and the song
doesn't fit in the video length. Clip transitions are kind of shitty, but at least you figured out
lipsync…almost!"* — **STOP and hand off.**

---

## 1. STATUS: lip-sync MECHANISM is solved. QUALITY is not.

We can now produce a music video where the creature's mouth genuinely moves to the vocals (verified in
pixels: mouth open @2.0s, closed @2.8s). Proof file: `C:\Users\andre\Desktop\REAL_lipsync_creature.mp4`
(source in `C:\DropCat-Studio\output\2026-07-24\songvid_00063-40766024_realpipe1\`).

But it is **not good yet** — three real defects below (Andrew's words).

## 2. THE MECHANISM (hard-won — the previous instance got it WRONG 4 times first; do not re-derive)

Lip-sync = **LTX-2 NATIVE audio conditioning** during generation (subject sings as frames diffuse).
NOT a MuseTalk post-pass. Andrew was right that "LTX-2 does the lip sync." Non-negotiables:

- **Generate at 640x360.** At 580p+ the audio tokens overflow LTX-2's context and WanGP SILENTLY drops
  audio conditioning = zero sync. Pipeline gens at 360p then upscales to ~580p. (`features/song_video/pipeline.py:505-530`)
- **Feed audio into generation** as per-clip `audio_source` (worker maps -> `audio_guide` + `audio_prompt_type="A"`,
  `audio_scale 0.6`, `audio_cfg_scale` MUST be >1.0). See `services/wangp_worker.py:222-245`.
- **Drive on the isolated + VAD-gated vocal stem** (`guide_vocals_gated.wav`) so it tracks WORDS not the beat.
- **MuseTalk is the WRONG tool** for stylized/creature faces — it silently no-ops (exit 0, ships a
  pixel-identical video). Don't use it here.
- **DON'T hand-reconstruct the pipeline.** Drive the real code (see §4). Every hand-assembly missed a step.
- **VERIFY in PIXELS, never from a "success" log.** Extract frames at different times; confirm the mouth
  changes state. A log saying SUCCESS means nothing.

(Also in memory: `project_dropcat_lipsync_mechanism`.)

## 3. OPEN ISSUES — what to fix next (priority order)

### (a) Lip-sync doesn't MATCH the audio (timing off)
- Biggest contributor WAS the loop (see (b)) -- now removed. The former looped tail had NO audio-matched
  mouth, so the back half was guaranteed out of sync. With looping replaced by a final-frame FREEZE, the
  back half is a still (no FAKE sync) instead of a mis-synced restart.
- Still open: per-clip audio-slice alignment. `run_song_prep` computes `_clip_start_times` with xfade-drift
  correction (`_SONG_XFADE_DUR=0.12`, `pipeline.py:389-407`). Verify each clip's audio slice matches where
  the clip actually lands in the final timeline. Native LTX-2 sync is energy-driven (loose), not phoneme-
  level -- there's a quality ceiling; don't expect Hedra-grade sync.

### (b) Song doesn't fit the video length -- ROOT CAUSE CORRECTED + FIX SHIPPED 2026-07-24 (sid ae64e3bf)
- The handoff's ORIGINAL hypothesis ("raise num_clips to cover the song") is WRONG and was NOT applied.
  Traced it: the clip planner uses clip_dur as a per-clip FLOOR so clips tile the SONG's timeline 1:1 for
  lip-sync. Raising num_clips beyond `floor(song_dur/clip_dur)` does NOT lengthen the video -- it walks the
  trailing clips' `_clip_start_times` PAST the end of the song, conditioning them on silence, and collapses
  their planned duration to a 4s sliver. So the feasibility clamp to floor() was actually CORRECT.
- The REAL defect: 2 clips DID correctly tile the 15s song (planned 7.5s each), but each GENERATED clip
  comes out ~0.5s short (frame rounding + 0.12s xfade overlap), so the concat was 13.88s and the merge
  LOOPED the whole video (`-stream_loop -1`, restarting from clip 1) to fill the ~2s gap.
- FIX SHIPPED (`features/song_video/pipeline.py` `_merge_video_audio_trim`): three fill modes --
  `none` (video >= song: trim surplus down), `freeze` (modest gap: HOLD the last frame via
  `tpad=stop_mode=clone`), `loop` (only if video < ~half the song = upstream under-generated; logs a
  loud warning). A held ending frame reads as intentional; the looped mis-synced tail is gone.
  Verified in PIXELS at the merge level (8s counter video + 10s song -> 10s out, frames at t=8.7/9.6
  still show the last frame "8", NOT a restart to "1"). routes.py left at its original (correct) clamp.
- VERIFIED END-TO-END 2026-07-24 20:32 on the real pipeline (2 clips, 15s song, clip_dur 7). Log:
  `merge: video=13.88s song=15.00s target=16.00s gap=2.12s fill=freeze` -- the EXACT case that used to
  log `loop=True` now FREEZES. Pixel checks on the output (songvid_ltx-2_203255.mp4, 16.0s):
  MAD(t=14.5 vs 15.8)=0.02/255 (tail frozen, identical) and MAD(t=1.0 vs 15.8)=30/255 (tail != start, so
  NOT a loop restart); mouth open @2.0s / closed @2.6s (lip-sync mechanism intact). Andrew judging overall
  quality. NOTE driver bug: `_lipsync_driver_handoff.py` copies the LARGEST match to Desktop, which grabbed
  a STALE 19:54 render -- fix it to pick the NEWEST (by mtime), not largest.

### (c) Clip transitions are "kind of shitty"
- xfade between clips is `_SONG_XFADE_DUR=0.12` (pipeline.py) + source-gravity chain continuity. With only
  2 clips the identity/scene can jump. Investigate `_concat_with_xfade` in `features/fun_videos/multi_pipeline.py`
  and the chain-anchor blend. More clips + tuned xfade + stronger identity hold should help.

### (d) cv2 CascadeClassifier missing (env bug, non-fatal but real)
- Log: `face crop failed (module 'cv2' has no attribute 'CascadeClassifier')`. The app's Python310 OpenCV
  is a build without the Haar cascades. `_build_face_crop` (`pipeline.py:664`) silently skips -> no face
  zoom. Native audio conditioning doesn't need it, but it matters for framing + the MuseTalk path. Fix:
  `pip install opencv-contrib-python` in the app's Python310 (or ship the haarcascade xml).

## 4. HOW TO REPRODUCE (drive the REAL pipeline — do not re-invent)

Working driver preserved at **`C:\DropCat-Studio\_lipsync_driver_handoff.py`** (copied from the session
scratchpad). It:
1. Inits the LLM router standalone: `import app; app._g["llm_client"]=LLMClient(); app._g["llm_router"]=LLMRouter(...)`.
2. Builds a mock `Job` with `.id`, `.meta={}`, `.update(**kw)`, `.stop_event=threading.Event()`.
3. Calls `run_song_prep(job, photo, settings)` then `run_song_pipeline(job, photo, settings)` with
   `settings["lip_sync"]=True`, `auto_lipsync=False`, model "LTX-2 Dev19B Distilled", steps 8, guidance 3.0.

Run with the app's Python310: `C:\Users\andre\AppData\Local\Programs\Python\Python310\python.exe`.
GPU: the `gpu_orchestrator` starts/uses the :7899 WanGP worker itself — stop any standalone worker first.
Worker cmd (if you need it manually): `C:\pinokio\bin\miniconda\python.exe C:\DropCat-Studio\services\wangp_worker.py --wangp-app C:\pinokio\api\wan.git\app --port 7899`.

**Better option than the standalone driver:** the DropCat Studio APP already does all of this correctly
end-to-end via `/api/song-video/generate` (upload-audio -> upload-image -> generate). If you can launch the
app (`launch-silent.vbs` -> `manager.pyw`, Python310), driving its API avoids the standalone LLM-router +
mock-job scaffolding entirely and is the truest reproduction.

## 5. OTHER WORK COMPLETED THIS SESSION (context, not lip-sync)

- **SFW/NSFW image decider** — BUILT + validated. `C:\DropCat-Studio\_nsfw_decider_handoff.py` (copied from
  scratchpad). Uses uncensored `Qwen/Qwen3-VL-32B-Instruct` on Featherless (needs the browser-y User-Agent
  or Featherless 403s). Routes SFW->hosted API, NSFW->RunPod. Validated: flagged 6/6 explicit renders NSFW,
  passed landscapes/creatures SFW. NOTE the design catch: **classify image OR prompt** — a SFW still (e.g.
  the 00238 mirror) can be driven explicit by the animation prompt. For the website routing.
- **Hub icon fixed** — `DropCatGo Studio.lnk` (was confusingly near-identical to `Drop Cat Go Studio.lnk`)
  RENAMED to `DropCat Hub.lnk`. It launches `C:\Users\andre\DropCat-Hub` = the LIVE :7910 Hub + Featherless
  lease broker + Overseer. **Do NOT archive that folder** — it's load-bearing.
- **DropCat-Studio default clip length** changed 5->6s (`fun_multi_clip_duration`, `config.py` + template +
  live config.json; also live `fun_multi_num_clips` 4->2) so a default 2-clip multi = 12s. Clamp ceiling in
  `features/fun_videos/routes.py:924` raised 5.0->6.0. (Andrew's request earlier in session.)

## 6. ANDREW PREFERENCES REINFORCED THIS SESSION
- To let him VIEW a file: **open it yourself** (`Start-Process`) + give the plain full path. Markdown/`file:///`
  links do NOT open in his VSCode. (memory: `feedback_always_clickable_local_path`)
- Don't claim "fixed/works" before he confirms, and NEVER from a log — verify in pixels. He caught 3 false
  "success" claims this session; rebuild that trust with verification, not assertions.

## 7. KEY PATHS
- Pipeline: `C:\DropCat-Studio\features\song_video\pipeline.py` (run_song_prep:339, run_song_pipeline:466,
  _build_face_crop:664, _do_song_gpu_phase:712)
- Worker audio API: `C:\DropCat-Studio\services\wangp_worker.py:200-245`
- Lipsync-res / 360p logic: `pipeline.py:505-534`
- MuseTalk runner (the wrong path for creatures): `C:\DropCat-Studio\features\lipsync\runner.py`
- Proof output: `C:\Users\andre\Desktop\REAL_lipsync_creature.mp4`
- Test song (has vocals): `C:\DropCat-Studio\output\2026-04-12\5b93aa4c_00102-24490_a6834dbc01ab\audio_20260412_154201.mp3`
- Test creature still: `C:\Users\andre\DropCatGo Extras\Trash\Galleries\critters human\00063-4076602478.png`
