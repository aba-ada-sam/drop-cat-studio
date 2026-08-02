# Session Handoff -- 2026-08-02 evening

Covers a long multi-thread session: Dev13B motion fix, output migration, f2.mp4
lip-sync + outro sign-off system (built from scratch), image style preset, Muse
narrator fix, and a still-running NSFW sign-caption video loop. Read this before
touching features/outro/, features/lipsync/runner.py, or the f2 asset family.

## Fixes shipped (commits, in order)

- c995ef5 -- Multi-video jobs auto-promote LTX-2 Distilled -> Dev13B. Distilled's
  8-step schedule can't calibrate motion across chained clips (alternates
  near-static / chaotic -- the "Ken Burns bullshit" complaint). Dev13B's 25-step
  schedule holds steady, ~3x compute. Single-clip make-it still uses Distilled.
- 7673353 -- Lip-sync silent-gap fix (features/lipsync/runner.py,
  _composite_voiced / new _freeze_gaps). The old code fell back to the raw
  source video between sung phrases, assuming the source is naturally still
  when nothing's being sung. False for a WanGP-generated creature clip with its
  own baked mouth motion -- that motion read as talking to nothing, uncorrelated
  with the lyrics. Now freezes the base video at each gap's start frame instead.
  Verified two ways: standalone (frames 2.5s apart inside a gap are pixel-
  identical) and on a real MuseTalk job.
- de2183b -- core/inbox.py now points at C:\Users\andre\Desktop\DCS_Review
  instead of output/Inbox, and accepts audio (.mp3/.wav) as well as video.
  This is the permanent, automatic landing spot for every finished
  video/song from any pipeline (already wired into song_video, lipsync,
  fun_videos, generate-audio-only) -- Andrew asked for this to stop being a
  one-off manual copy.
- 5247912, 3f93e3d, 0b3e4ba, 195e623 -- features/outro/sting.py, built fresh
  this session (see below).

## features/outro/sting.py -- the "Drop / Cat / GO" sign-off system

Went through several complete redesigns based on live feedback -- read the
module docstring, it has the full history. Current design, final as of
195e623:

- Not a separate appended card. append_outro(input_video_path,
  variant_key, output_path=None) composites directly onto the source video's
  own real tail footage (extending with a frozen last frame first if there
  isn't enough silent runway after the audio ends).
- No wordmark, no brand name spelled out. Just "Drop" -> "Cat" -> "GO" in
  sequence, deliberately unexplained ("if they can't figure it out, all the
  better" -- Andrew's words).
- A flat icon IS integrated (_make_flat_icon): solid-color cat mark
  (circle head + two ears + punched-out eye), PIL-drawn, zero gradient/stroke/
  bevel. This is deliberate -- flat fill structurally cannot read as the 90s-
  CGI chrome/bevel spinning-logo look Andrew explicitly rejected (see
  Desktop/f2_dropcatgo_emblem_v4.mp4 for what NOT to do -- an external,
  non-DCS 3D render, not from this codebase).
- Bass-tone hit per word (_bass_hit): synthesized sub-bass thump (rising
  55/65/78Hz across the 3 words) via ffmpeg's own sine source, no sample
  assets, mixed in via amix delayed to each word's onset.
- Audio fades out, doesn't hard-cut (_AUDIO_FADE_S = 0.8).
- Dead-air tail was cut roughly in half (195e623) after Andrew flagged
  3.7s of near-silence at the end of a 32.2s video -- min_buffer 0.35->0.10,
  every variant's pad/gap tightened.
- 4 variants: minimal_fade, ink_stamp, circus_glow (most on-brand,
  crimson/gold), neo_mono. render_samples() renders all 4 against one
  source clip for side-by-side review.

### ffmpeg gotchas discovered building this (documented in the module docstring too)

- overlay in this ffmpeg build (8.1 gyan.dev essentials) does NOT
  alpha-blend against a synthetic "-f lavfi -i color=c=...@alpha" source --
  silently paints opaque RGB instead. A real RGBA PNG file works correctly.
- drawbox/drawtext blend their own alpha correctly against real video
  content directly -- no separate transparent layer needed for those.
- scale's w/h expressions don't support enable= in this build; eq,
  drawbox, gblur, rotate do.
- Container format.duration metadata can overstate true decodable video
  length by 1+ second -- anchor all timing to measured/probed durations, never
  the container's claimed duration.
- ffmpeg input-index bugs: don't infer -i input indices from
  len(cmd) // 2 -- the ffmpeg/-y preamble throws the arithmetic off by
  one. Use an explicit incrementing counter instead (bit both the icon-overlay
  and bass-mix code in this session before being fixed).

## Known-bad / open items

- features/outro/yarn_burst.py (new file, another session's work): "yarn
  bursting from the mouth" effect, 3 compositing iterations so far (pasta/
  meatballs -> marbles/confetti -> dot-cluster). Andrew's own assessment of the
  latest: still doesn't convincingly read as yarn. Not resolved -- either needs
  a 4th design pass or should be dropped from the final deliverable.
- A separate WanGP-generation attempt at the same effect failed entirely:
  submitting /api/fun/make-it with an explicit "yarn explodes out of the
  mouth" prompt got silently overridden -- the pipeline auto-appended "subject
  completely still, static shot, fixed camera, gentle atmospheric motion" to
  the prompt (visible in the job's stored video_prompt), which directly
  fights any explosion/motion request. Cost ~15 min of GPU for a static
  near-identical frame. Not fixed -- worth finding where that qualifier
  string gets injected (likely a "calm"/scene-hold style default leaking into
  an explicitly dynamic single-clip request) if this effect is revisited via
  WanGP instead of compositing.
- Lip-sync region targeting (separate from the gap-freeze fix): a
  teammate's sync_qc heatmap tool flagged audio-correlated motion landing on
  the eyes rather than the mouth on the f2 creature clip, across all 3 passes
  checked. Own history notes call this a "known seed-lottery failure." I have
  direct visual counter-evidence (frame-by-frame mouth shape genuinely varies
  and correctly freezes during gaps), so I'm skeptical the metric is decisive
  here, but flagging since it's unresolved either way. Real fix if it IS a
  real problem: WanGP best-of-N reseed (pipeline.py already has
  _pick_best_seed/best_of_n) -- needs the original source photo, which
  isn't recoverable for the legacy Desktop/f2_dropcatgo_emblem_v4.mp4 file.
- Output migration to D: (C:\DropCat-Studio\output -> junction pointing
  at D:\ColdArchives\DropCat-Studio-Output) is still blocked. One empty
  leftover folder (output\2026-05-01) resists deletion -- tried direct
  delete, .NET delete, cmd rmdir, a Restart-Manager handle scan (found no
  owning process), ACL/ownership reset, a Search-Indexer restart. All failed
  identically. This has now blocked at least two separate sessions across
  different restarts -- smells like a genuine stale NTFS handle that needs a
  reboot to clear, not something worth more troubleshooting time.
- features/song_video/pipeline.py clip-vanishing bug: during a watchdog-
  triggered WanGP deadlock recovery, two already-completed, previously-
  verified-on-disk clip files vanished entirely (confirmed gone, not moved) by
  the time the merge step ran. Root cause not pinned down -- ruled out: this
  session's own folder-migration commands (timing doesn't fit), the app's
  periodic job cleanup (memory-only, never touches disk), a manual
  /api/output/delete call (none in the log). Best guess: a lingering zombie
  WanGP worker process from an earlier deadlock cycle (4 stale workers got
  killed in one watchdog recovery that same afternoon) racing a rename-based
  save. Genuinely unresolved.

## Other work this session (separate threads, less detail needed)

- core/image_presets.py (60352c4): new opt-in nsfw_graphic_novel preset,
  selectable in the existing Image Studio dropdown, doesn't touch/default-
  change existing presets. Found-but-not-fixed: multi-subject "forge couple"
  regional mode collapses two people into one blended figure -- pre-existing,
  reproduces on the old preset too.
- Storyteller repo (DropCatStudio.ps1, commits 56294dc + 4270231,
  separate repo from DropCat-Studio): extended the existing ChatArc-Judge
  continuity mechanism to also catch narrator-voice drift (companion slipping
  from first-person into third-person narration), and made sure the fix
  actually runs on autopilot turns too, not just manual ones.
- A _forever_signloop.py automation script (outside the repo, at
  C:\Users\andre\Desktop\DCG\) is running: generates a starter image via
  DCS's own Forge pipeline (subject holding a sign with an AI-written caption,
  NOT via OpenAI's API -- deliberately avoided, see below), feeds it through
  video + song + lip-sync, loops with variation. Was mid-build of a face-
  detection + Grounding-DINO/SAM inpaint-repair step (for when the starter
  image comes out faceless) -- status not confirmed finished as of this
  writing.
- Explicitly declined: using OpenAI's image API to generate a seed image
  for the NSFW pipeline. Their usage policy prohibits using their generation
  as an input/seed for downstream sexual content, regardless of how tame the
  OpenAI-side output is -- that's the specific circumvention pattern they
  disallow. Used DCS's own Forge pipeline instead.

## Where things physically live

- Final review outputs: C:\Users\andre\Desktop\DCS_Review\ (now permanent/
  automatic via the inbox fix -- also has a f2_final\ subfolder from another
  session's outro variant renders).
- Best current f2 lip-sync source: output\2026-08-02\f2_lipsync_184054.mp4
  (has the gap-freeze fix; earlier passes f2_lipsync_180516.mp4 etc. do not).
- Outro sample renders: output\outro_samples\.
