# Session state -- 2026-08-05 evening (written ~17:45, updated at close)

Written so a PC restart loses nothing. Plain-language summary first, then
the technical state. Previous anchor: SESSION_STATE_2026-08-04_overnight.md.

## Where things stand, in one paragraph

The local studio recipe is fully ratified through v12 (30s, alternating
scenes, per-scene depth blur -- Andrew: "that's the best") and opened to
long format (v13 = 60s). v13 exposed the last real defect: the take-picker
cannot tell a singing take from a frozen one, so back-half clips shipped
dead mouths. The fix -- a vision-judge selector -- is built, validated on
labeled takes, and its ratification render (v14) was in flight at write
time. Seam placement got the same treatment: seams now can land in vocal
gaps with variable clip lengths (smart seams, built + unit-tested, awaiting
its validation render v15). The site path is parked per Andrew until local
is done; its UX ruling is captured and committed. Cloud failover: Iceland
is dry, a datacenter probe mission is finding a serverless-capable DC.

## The engine and app (WHO IS WHO -- Andrew's folder question)

- C:\DropCat-Studio = THE APP (desktop link "Drop Cat Go Studio" -> :7860).
- C:\DropCat-Studio\engine = THE RENDER ENGINE (chain.py; its own git repo,
  dcmvs-lipsync). MOVED from C:\DCMVS-restored ~18:20 this evening (Andrew's
  folder-confusion fix): repo verified intact post-move (f75dc3e pushed),
  chain.py compiles + --help runs from the new path, the dead "Drop Cat
  Studio.vbs" retired into engine\_session_backups, references updated
  (wangp_models comment, regen_offender_clips docstring, RECIPE.json,
  memory). C:\DCMVS-restored NO LONGER EXISTS.

## Ratified recipe (RECIPE.json v3 is the source of truth)

30s AND 60s production: chain.py, 241-frame clips, crossfade 0.15 with
overlapped audio, SE end-anchor, isolated-vocal conditioning (HP150),
alternating scene anchors (transplant/graft composites, NEVER generative
identity), per-scene DOF finish (sigma 3.2, one DINO+SAM matte per scene,
time-gated maskedmerge). v11 calm-background-cast prompt wording.

## Built today, awaiting ratification renders (flags default OFF)

1. --judge-select (engine commit 4b45803): haiku labels 8 frames per take
   at the conditioning slice's vocal peak (per-scene head crop, DINO+SAM,
   anchor->video coord scaling); score = mouth-state transitions + 2*open
   fraction; artifact rejects. Validated: articulating takes 4.25-5.0,
   weak 2.25, v13's frozen clip 0.0. v14 = ratification render.
   KNOWN COST: ~2x render wall time when Forge (DINO/SAM) loads models
   while LTX holds VRAM -- optimization queued: precompute the per-scene
   head crops at PLAN time, before any rendering.
2. --smart-seams (engine commit a53882e): vocal-gap seam placement,
   variable clip lengths, last clip sized to the song end (stub class
   gone); assembler generalized to per-clip xfade offsets. Pair with
   --min-clip-frames 169. v15 = validation render.
3. --vocal-highpass (4b45803): HP80 is the measured oon/oom fix candidate
   (HP150 splits 4/29 sung spans; 3/4 heal at 80). A-B pair rendered but
   both takes glyphed (pinned seed hallucinated captions); re-roll with a
   fresh seed queued behind v14/v15.
4. Scene patterns (a53882e): A-bookends, ABBA/AABA class, no triples,
   scene C at 6+ clips with 3 anchors, deterministic per seed. Rides
   along with any multi-scene render; v15 shows it.

## DCGS app changes LIVE (restarted ~16:45, pid 36636 at the time)

Song provided -> length auto-fills from the song (editable). No song ->
user's length setting + ACE-Step writes the track (fun_videos client;
instrumental unless lyrics given). Music Video tab has the length control.
DCS repo commit 379d77d (pushed).

## Cloud / site path (parked until local done -- Andrew's sequencing)

- dcg-sing (TX-3, nx5ibws5im4vxx): workersMax=0 (slot loaned to -b).
- dcg-sing-b (EUR-IS-1, d4fc54gn2vzctt): volume i837q1w1r3 populated
  (~41.6GB incl Gemma-3-12B), but EUR-IS-1 serverless is DRY -- zero
  workers ever placed across 12 GPU types incl H100/H200. Smoke job
  ba9fff89-...-u2 harmlessly queued on it.
- dc-failover agent mission IN FLIGHT: probe serverless placement across
  candidate DCs with tiny throwaway workers, replicate the volume to the
  first DC that places, restore workersMax=1, report. Budget inside the
  remaining ~$2.46 of Andrew's $3 cap. Quota is exactly 5/5.
- SING_UX_SPEC_2026-08-05.md committed (c6efbcd, dropcat-video-wan,
  branch sing-audio-conditioning): upload-vs-generate choice, 30s note,
  backend optimal-30s picker.

## Repos at last push

- DropCat-Studio: 379d77d (pushed)
- DCMVS-restored (dcmvs-lipsync): a53882e (PUSH PENDING at write time --
  done at close-out)
- dropcat-video-wan: c6efbcd on sing-audio-conditioning (pushed)

## Late-evening delta (~20:20)

v14 FAILED Andrew's eye on B clips -> judge v1 was blind (crop divergence,
see ledger) -> judge v2 (per-take crops + sync contrast) built, validated,
and v15 rendered with the FULL new stack: judge v2 + smart seams (7 variable
clips at vocal gaps) + scene pattern A-B-B-A-A-B-A + 4 seeds. Pixel check
7/7 clips articulate. Local page review_0805_12.html; mobile decision page
at the site /decide/v15/ + Telegram link (Andrew is out; his reply rides
the Buddy chat -> board pipe). oon CLOSED by Andrew (HP150 stays). Engine
commits through 2dac2d0/f9e8876; scene_prep director module built+validated
(1839b76); parity tests 19/19 (aaffedf). CLOUD, final for the night (~21:00): 14 DCs probed, ZERO genuine 48GB
serverless placements (fleet-wide crunch; the one transient signal was a
5090 artifact -- a card the image cannot run, measured tonight: dies at
WanGP launch on 32GB, twice; 5090 REMOVED from the endpoint GPU list).
Pod smoke on Iceland proved boot+checkpoint+handler AND the external
deadline-kill discipline, but no render (only 5090 pods there by then).
Total mission spend ~$1.05 of the $3 cap; zero pods; both volumes intact
(~$7/mo each, shrink planned). ANDREW'S PICK PENDING: per-job pods in a
48GB-stock DC (survey = step 1 if picked) vs Novita vs wait out the
crunch. Also pending: his v15 verdict (phone page /decide/v15).

## The queue (if resuming cold)

1. v14 done? -> per-scene DOF (scratchpad dof_v14.sh) -> extract frames,
   LOOK (especially clips 4-6 mouths) -> fresh page review_0805_10.html.
2. Folder move (see above) in the GPU gap; validate: next render runs
   from C:\DropCat-Studio\engine.
3. oon A-B re-roll: same two commands as before but --seed fresh value
   (both runs SAME seed; 4242 glyphed).
4. v15: 60s smart-seams + patterns render:
   --smart-seams --min-clip-frames 169 --judge-select (if v14 ratifies).
5. DC probe results -> resubmit smoke on the new endpoint.
6. Ledger/RECIPE updates per Andrew's verdicts, close-out ritual.

## Open items on the ledger

'oon/oom' residual span (one span stays split even at HP80), seam-planner
ratification, judge-selector ratification, plan-time crop optimization,
scene-C anchor production (director builds it, SCENE_PREP_SPEC).
