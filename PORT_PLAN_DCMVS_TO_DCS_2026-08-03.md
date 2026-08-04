# PORT PLAN: make DCS song_video match DCMVS (the system of record) -- 2026-08-03 night

Decision (Andrew + manager session, 2026-08-03 ~22:00): DCMVS (C:\DCMVS-restored) is the
music-video system of record. AWM00001.mp4 came from it, NOT from DCS (no DCS job record
matches). DCS's song_video adapts to DCMVS's proven configuration -- never the reverse.
Full extracted spec: see the 22:14 board post series (Studio board) and the agent report
this file distills. Execution is GATED on the manager's control-experiment verdict
(cat_in_suit + magazines proof pair through today's stack -- isolates subject-vs-env).

## Tier 1 -- sync-critical payload/structure deltas (port first)

1. NO CHAINING for lip-sync clips: every sing clip starts AND ends on the PRISTINE source
   image (DCMVS SECTION_CLIPS=1 + SE_END_ANCHOR=True; "the breakthrough", v2.4). DCS
   instead chains frames with reanchor_every=2 (pipeline.py ~940-1010) -- DCMVS's own
   milestones call chained output "not watchable" (drift, degradation). Port: when
   _lip_sync, start_image = end_image = prepped source for every clip; drop chain-frame
   logic on that path. (DCS 87883d5 already sets end=start; the START image must stop
   chaining too.)
2. Resolution 960x544 (emits 960x512) @ 24fps, steps 8, guidance 3.0, audio_scale 0.6,
   input_video_strength omitted (worker default 0.69). DCS currently: 640x360-or-override,
   guidance clamped to 3.0 (32c1ad7 -- keep). 83881b3's "960x544 hangs" was TDR-window
   evidence; DCMVS proves 960x544 daily. Port: lip_sync default resolution 960x544.
3. audio_cfg_scale AND audio_guidance_scale (same value, both keys, 3.0) must be sent
   explicitly; 1.0=off, <1.0 = ANTI-sync. Verify DCS's video_generator sends them (worker
   log shows cfg=3.0 arriving -- confirm it is per-payload, not worker default).
4. vae_config=1 forced every call (never auto): auto at h>480 lands 256px VAE tiles =
   visible mouth-area squares that still PASS sync scoring. Verify DCS worker path forces
   it; if payload-controllable, send it.
5. sliding_window_size (481) + sliding_window_overlap (17) must exceed clip video_length
   or WanGP silently sub-windows and destroys audio<->video cross-attention.
6. Clip length: up to 481 frames (~20s) is the audio-conditioned ceiling; MIN 241 (~10s)
   for musical clips. DCS's 6s clips are legal but DCMVS ships 10-20s; frame counts must
   quantize to 8k+1.
7. Surface the worker's SILENT audio-strip fallback (rejected audio_prompt_type=A ->
   same job retried with no audio, no error): DCS must detect + at minimum log/flag a
   clip that silently lost conditioning.

## Tier 2 -- audio prep (DCS's demucs chain is close but not equal)

8. Isolation: DCMVS uses WanGP's bundled BS-Roformer (env python ->
   preprocessing.extract_vocals.get_vocals), fail-soft to full mix. DCS uses Demucs via
   MuseTalk venv. Evaluate swapping or keep Demucs if control run says env is fine.
9. Highpass: DOUBLE-cascaded highpass=f=150:poles=2 (DCS: single).
10. Gating: RMS-hysteresis vocal-activity spans (enter 0.16*peak, stay 0.09*peak, floor
    170, min 0.30s, merge 0.50s) as PRIMARY; silence/ratio detectors are fallback only.
    DCS uses silero VAD -- likely fine, but adopt the two guards regardless:
    - soft trapezoid mute edges, 0.18s ramps (hard gate = mouth pops open/closed);
    - over-mute circuit breaker: if mutes cover >85% of the song, skip gating entirely.
11. Slices: cut from the GATED conditioning track, pcm_s16le 44100 stereo, duration
    exactly frames/fps. Final output always remuxes the ORIGINAL song.
12. Per-slice RMS log + SILENT marker below -35dB mean; a gated-silent slice renders
    UNCONDITIONED (mouth rests by design) instead of conditioned-on-silence (mouth agape
    -- what Andrew watched all night). [Manager directive earlier tonight; consistent
    with DCMVS's design intent via activity spans.]

## Tier 3 -- selection/QC + assembly

13. Ranking: mouth_sync_score soft motion factor -- DONE both repos (DCS 86abbee, DCMVS
    d94c55b). DCS _pick_best_seed already mirrors generate_best_clip (early-accept on
    is_synced, keep best otherwise); ADD honest flagging: persist per-clip
    kept-score/flagged into job.meta so a no-winner clip is visible, not silent.
14. DCMVS's SHIPPED default is single-take + manual per-clip regenerate (v2.8) with
    drift-guarded retakes (REGEN_TAKES=3, drift reject 1.6x, firmer hold 0.73). DCS
    equivalent (queue-modal "regenerate clip N") is a bigger feature -- park for Andrew's
    call; best-of-3 stays DCS's default meanwhile.
15. Seams: variable clip lengths landing on vocal rests/beats (plan_segments), hard cuts,
    concat FILTER (never -c copy) with frame-count assertion. DCS's fixed 6s tiling +
    xfade differs; port after Tier 1-2 prove out.
16. Prompts: negative prompts are INERT on distilled LTX-2 (CFG forced 1.0); identity is
    held by subject-anchored POSITIVE prompt + "the only subject in the shot, no other
    people and no other faces"; structure Subject -> Action -> Camera -> Lighting.

## Non-code facts that govern everything

- Per-seed mouth/eyes coin flip is real; fixed seeds do NOT transfer across WanGP version
  x image x song. Random seeds + selection/regen is the design.
- Subject suitability is load-bearing: clear, centered, front-facing, non-human face with
  real mouth geometry. Limited-mouth subjects (pumpkin, owl-beak) under-animate at ANY
  setting (v2.6). If the cat control syncs and the alien does not, this is the answer.
- Test on SUNG windows only; verify slice RMS > -35dB before drawing conclusions
  (the 0-6s intro of Adam_Friends is instrumental -- it poisoned every clip-1 test
  on 2026-08-03 until caught).

## Status ledger (2026-08-03 ~22:20)

- DONE tonight in DCS: guidance clamp (32c1ad7), lip_sync-only resolution override
  (530e962), scorer soft-factor (86abbee), default-path rollback (c134c63), librosa cap
  (87917c7). DONE in DCMVS: scorer port (d94c55b).
- GATED: Tier 1-2 implementation awaits the manager's cat-control verdict (env vs
  subject). One bounded DCS verification render authorized after the port.
- DCS queue is PAUSED for the night's GPU serialization -- unpause when GPU work ends.
