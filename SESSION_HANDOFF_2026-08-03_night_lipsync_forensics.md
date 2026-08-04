# Session handoff -- 2026-08-03 night -- lip-sync forensics + DCMVS pivot

Session b0293762 (role dcs-lipsync on the Studio board), working under the manager
session a085855e per Andrew's coordination order. Read PORT_PLAN_DCMVS_TO_DCS_2026-08-03.md
alongside this. Andrew is asleep; overnight orders: keep working, coordinate via board,
morning deliverable = watchable synced video on the manager's HTML review page.

## The night's arc, compressed

1. Andrew: DCS "lost the ability to make lip-sync videos like AWM00001.mp4."
2. Hours of DCS-pipeline fixes (all committed, all real, none sufficient):
   87917c7 librosa cap, 7829855 face-crop widening (ROLLED BACK by c134c63 -- it
   silently re-framed DEFAULT-path videos; default path now byte-identical to
   pre-08-03), 530e962 lip_sync-only resolution override, 32c1ad7 lip_sync-only
   guidance clamp 7.5->3.0, 86abbee sync_qc motion soft-factor (real bug: the 1.2
   motion floor zeroed ALL best-of-N candidates, picker was blind; ported to DCMVS
   as d94c55b).
3. PROVENANCE BREAK: AWM00001.mp4 was never made by DCS -- no job record matches.
   It came from DCMVS (C:\DCMVS-restored). Andrew's standing instruction "go back
   to the code that worked" meant DCMVS. It is now the SYSTEM OF RECORD; DCS
   adapts to it (see PORT_PLAN, memory project_dcmvs_system_of_record_2026-08-03).
4. Test-design traps that poisoned early conclusions (do not repeat):
   - Adam_Friends 0-6s is INSTRUMENTAL; any clip-1 test conditioned on gated
     silence = mouth-agape artifact. Verify slice RMS > -35dB before concluding.
   - uploads/0f5e25e0_00218-2156486707.png is a seed-number FILENAME COLLISION
     with AWM00001's actual source (a purple alien, big frontal face). Use
     uploads/awm_alien_source.png (extracted from AWM00001 frame 1).
   - MuseTalk is the WRONG engine for creatures: fundamental 256px human-tone
     paste-box (LIPSYNC_HANDOFF.md 07-24), not a regression.
5. ENV DRIFT ENDGAME (state when this doc was written):
   - Cat control (frozen May proof pair, seed 777) FAILS identically on today's
     stack: uncorrelated c=0.116 syncY 0.58, reproduced bit-stable pre/post
     sliding-window "fix" -- vs May's synced c=0.186.
   - Scorer instrument VALIDATED: the frozen May DEMO re-scores c=0.186 today.
   - ELIMINATED as causes: WanGP repo drift (reflog: single clone entry 04-06,
     HEAD never moved), wgp settings (ltx2_distilled_settings.json has
     sliding_window_size 481 since May 3 -- NOTE: my ee38f88 "smoking gun" was
     WRONG, setdefault no-op, commit kept as harmless parity), wgp_config
     (vae_config=1 on disk), wan env python stack (newest mtime Jun 6, torch
     2.7.1+cu128), wrapper audio-field mapping (both wrappers correct),
     native-conditioning deadlock + 960x544 hang (TDR-window diagnoses, did not
     reproduce on the reinstalled driver).
   - LAST VARIABLE STANDING: the NVIDIA driver reinstall after the 08-02 TDR
     cluster. Same-seed-different-output across a driver change fits.
   - DECISIVE TEST (manager's lane, ~$1 of the $20 overnight wallet): rented pod
     with pinned versions rendering the frozen pair. Pod syncs => local env
     confirmed broken; cloud is the render path; local driver remediation is a
     daytime task (needs Andrew's UAC). Pod fails => theory wrong, regroup.

## Operational state

- DCS app: pid from the 22:2x respawn, HEAD at ee38f88, queue PAUSED on purpose
  (unpause when GPU serialization ends: POST /api/jobs/resume).
- WanGP worker :7899: alive post-restart, loaded model, DCS-spawned. Guard duty:
  eviction watch (server.log tail) + 60s liveness monitor armed in session
  b0293762 (die with the session). Eviction anatomy: fires only after 30 straight
  non-busy minutes; direct-driven renders do NOT reset DCS's acquire clock (bit
  us once at 22:19 -- respawn via POST /api/services/start/wangp, which also
  resets the clock).
- Red-team on the 5 DCS commits: PASSED (manager's independent agent). Two
  non-urgent findings parked: >15min songs still degrade beat alignment
  (audio_analyzer full-duration math); cosmetic log drift.
- War Room GUI (C:\Users\andre\StudioTeam\warroom.pyw, pythonw): blue-border
  status window Andrew asked for -- DCS jobs + Studio board feed, 4s refresh.
- Andrew's standing asks tonight: never show him local paths, always ONE html
  review page, opened for him (Start-Process); timestamp every reply; 5-min board
  check cadence until consensus stop.

## If you are a fresh session picking this up

1. team.py status on the STUDIO board (CLAUDETEAM_WORLD=studio) -- the manager
   session may still be driving; do not collide with the pod lane.
2. Read the board from 21:13 onward for the full manager/dcs-lipsync thread.
3. Do NOT re-litigate: MuseTalk-for-creatures, the deadlock rule, sliding-window
   theory, subject-suitability of the alien (cat control failing exonerated it).
4. The DCS port plan (Tier 1-3) is written and GATED on the pod verdict.
