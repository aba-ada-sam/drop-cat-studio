# Overnight state -- helper/brains (a085855e), written 23:05 2026-08-04

## SELF-CHECK FIRST, EVERY WAKEUP (Andrew, 23:26, standing): do not get
## rabbit-holed by rabbit-hole analysis -- the management failure mode.
## Ask: did the last hour produce an artifact Andrew can watch or use
## (a video, a fix, a page)? If not, CUT the current analysis thread and
## return to producing. Diagnosing the diagnosis is how tonight's first
## half was lost. Meta-work gets one paragraph, never a workstream.

Andrew authorized an overnight autonomous run and went to bed. This doc is the
re-anchor point after any context compaction: read it INSTEAD of re-deriving
from the board scrollback. Both sibling sessions are DEAD (lipsync b0293762
killed ~22:45, 3060-plan 87c16f22 wound down clean ~23:02, wind-down post has
its resume steps).

## Decisions of record (do not re-litigate)
- 30 SECONDS is the format for everyone (Andrew 22:55). tiers.py ships it.
- end_anchor stays TRUE -- blind labels 6/6 (ledger entry, pushed).
- SYNC_ENFORCE=False -- sync floor is advisory until a scorer is recalibrated
  against human labels. Do NOT re-arm overnight.
- Ribbon gate is the only auto-reject. total_motion/mouth_sync_score may not
  gate or select-with-authority. Aperture metric v1 discarded (ROI drift).
- Site branch sing-continuity is DEPLOYED (evidence on DropCat board 22:47).
- Sing endpoint nx5ibws5im4vxx exists; smoke job c7cf7dc3-...-u1 queued on
  US-TX-3 capacity (worker throttled). Costs nothing queued.

## MANDATE EXPANDED 23:10 (Andrew's last words before leaving for 8-9 hours):
"You can gen more than one, whatever... use your intelligence to fix both
paths without more feedback from me." So: MULTIPLE renders authorized; the
goal is BOTH paths (local DCS/DCMVS + cloud site Sing) actually producing
good 30s lip-synced videos by morning. Spend cap for the cloud path stays the
~$3 he nodded to (smoke + one or two real 30s jobs + at most one CI-fix
retry). Morning deliverable: ONE review page with the best finished videos
from each path plus a plain accounting of what failed and why.

## Overnight job list, in order
1. E2E: ONE user-tier job (30s, 5x6s, best_of_n=3, lip_sync explicit true)
   through the real DCS app -- the wired path's first complete run. When it
   finishes: run artifact screens' summaries, build a review page next to the
   video (DCS_Review dir, do NOT auto-open at night), note every
   WOULD-HAVE-REFUSED / LOOK-AT-THIS-CLIP line verbatim for the morning.
2. energy-hardfail subagent (ae10d39f728aeb42d): when it returns, rule-6
   review the diff, run its tests + test_window_energy + test_pick_best_seed
   + test_recipe, commit if clean. Do NOT restart the app mid-render for it;
   it goes live at the next natural restart.
3. Smoke job: re-check status occasionally via the endpoint API (single
   status GETs, never a loop in a tool call). If it completes: verify the
   output mp4 (>2s, >50KB, has audio), save to DCS_Review, note cost. If it
   errors: capture log_tail, diagnose, ONE corrected resubmit max (~$0.15),
   then stop regardless.
4. Morning summary: ONE message when Andrew appears -- what finished, what
   failed, where the videos are. No PushNotification overnight; he is asleep.

## Hard boundaries overnight
- NO 3060/STUDY connections (lane parked; resume steps in 3060-plan's last post).
- NO site/warehouse connections (deploy done; functional verify needs a human).
- NO spend beyond the one smoke resubmit cap above.
- NO GPU job beyond the one E2E (+ its own internal takes).
- Localhost polling only; remote checks are single attempts.
- All boards quiet unless something actually lands.

## Where things live
- Videos/review pages: C:\Users\andre\Desktop\DCS_Review\
- Judge page :7932 (orphan process, KEEP ALIVE): 6 re-muxed clips await
  Andrew's labels; labels.json in dead lipsync's scratchpad, real labels
  backed up. 6 ablation labels are the calibration set of record.
- Recipe of record: C:\DropCat-Studio\RECIPE.json + tests/test_recipe.py.
- Ledger: LIPSYNC_LEDGER.md -- append notable overnight findings there.
