# Scene-prep pass -- per-run vision director + deterministic executors

## PRIME RULE (Andrew, 2026-08-05 ~2:18 PM): CHARACTER-GENERAL, NEVER
## CHARACTER-CODED. "Stop hardcoding around this character, we're going to be
## creating lots of different characters doing lots of different things."
## Identity is NEVER a generation problem: scene anchors are built by
## TRANSPLANT (matte the user's subject pixels, paste onto an EMPTY generated
## plate, harmonize with the face frozen, occlude with plate foreground) --
## the generator only ever renders empty plates, and the video model carries
## identity from the anchor by conditioning. Measured basis, same day: every
## generative identity channel failed on a novel character (prompt cousins,
## reference-only, IP-Adapter -- CLIP gist compresses away brow folds and
## irises; the checkpoint prior fills them wrong), while the transplant
## preserved the face exactly. Per-character LoRAs are demoted to optional
## admin polish -- never load-bearing, never required per user.

Andrew's ruling 2026-08-05 (~1:55 PM), after watching hand-placed masks fail to
generalize: "hardcoding coordinates and instructions for a single image isn't
going to work, you'll need some sort of API to help you make these decisions
for each run. I don't know if haiku can do it" -- it can, with the labor split
below. NOTHING pixel-precise ever comes from the LLM; nothing judgment-shaped
ever comes from the detectors.

## The pass (runs once per job, before any render)

1. DIRECTOR (haiku vision, one call, ~fractions of a cent): input = the
   uploaded photo (and each scene anchor image for multi-scene jobs).
   Output = strict JSON:
     subject_query        -- text query naming the singing subject ("purple alien")
     distractor_queries[] -- things to sweep ("dark object in the man's hands",
                             include the HANDS holding an object: pose implies
                             contents, masking the object alone regrows it --
                             learned 2026-08-05, three inpaint rounds)
     scene_description    -- truthful describe-the-image prompt text (the ledger
                             rule automated; feeds per-scene prompts)
     wants_occlusion      -- whether the plate should place foreground elements
                             IN FRONT of the subject (Andrew 2026-08-05:
                             "foreground in front of our subject makes it feel
                             like he's in the pic, not superimposed on top of
                             it" -- occlusion sells belonging; composites paste
                             the subject BETWEEN background and a foreground
                             layer for the same reason)
     background_cast[]    -- per background figure: minimal-motion direction
                             ("stands calmly, slowly nodding"); big moves only
                             where the energy map marks a high point (Andrew:
                             "hang out bouncing, hit the obvious high points")
2. GEOMETRY (local, deterministic, proven 2026-08-05 by hand):
     GroundingDINO: text query -> boxes (the sam ext's dino path works; its
       "installment failed" warning falls back to local dino and STILL WORKS)
     SAM ViT-B: box -> mask (checkpoint installed at
       extensions/sd-webui-segment-anything-altoids/models/sam/)
3. EXECUTORS:
     distractor sweep -- Forge img2img inpaint per distractor mask; fill prompt
       describes what is BEHIND (clothing, wall), never any holdable noun
     subject matte -- subject mask, dilate ~31px + blur 24, becomes the DOF
       keep-sharp zone; delivery gets gblur sigma 3.2 outside it (ratified)
     scene prompts -- scene_description per anchor image -> chain.py
       --scene-prompts (paired with --images, cycling A-B-A-B-A)
4. FAIL-SAFE: any stage detecting nothing = that stage changes nothing.
   A run with zero detections renders exactly as today.

## Why the split (do not collapse it)
- LLM boxes drift by tens of pixels; DINO boxes are tight. LLM judges.
- DINO cannot decide WHAT deserves sweeping or how a figure should behave.
- Precedent: the story-arc LLM already writes per-clip prompts blind; this is
  the same call pattern with eyes.

## Anchor strategy B -- FACE GRAFT (Andrew's face-zoom-mask idea, proven 2026-08-05 ~2:25 PM)
Strategy A (whole-body transplant) is pose-exact but pose-locked. Strategy B gets
generation's pose variety with pixel-true identity: generate the body/scene (any
gist tool -- likeness does not matter below the neck), DINO+SAM the head box on
BOTH source and generated image ("the creature's head"), scale the source head
cutout to the target box, paste feathered, then RING-ONLY harmonize.
Measured parameters (facegraft_final2): ring = dilate(31) minus erode(5) of the
paste mask, blur 5; denoise 0.45 inside the ring only, face interior at literal
zero. The first attempt eroded 21 and ATE THE EYE -- the ring must never reach
a facial feature. Director-owned selection rule: prefer generated bodies whose
HEAD POSE matches the source (a downward 3/4 gaze on a straight-on body reads
uncanny); crown/edge quality of the source matte sets the graft's weakest seam.

## Scene-sequence rules (Andrew, 2026-08-05 ~15:55-16:05)

Two rulings, verbatim intent:
1. ">3 clips: mix up the A,B,A,B for better effect so it's not so predictable.
   ABBA, AABA, ABAA are all legit -- we want to START AND END ON THE SAME
   CONTEXT." Bookend rule: first clip = scene A, last FULL clip = scene A
   (the tail stub then inherits A via the chain.py stub rule).
2. "DCGS + user-supplied music at a length requiring 6+ clips: add a clip C
   -- a NEW context with the same subject. ABCBCA and ACBBCA both acceptable."

Planner contract (chain.py sequence generator, feeds --images cycling):
- n_clips <= 3: A B A (the ratified v12 shape)
- 4-5 clips: bookend A...A, interior drawn from {A,B}, not strictly
  alternating (ABBA / AABA / ABAA class), seeded by the render seed so a
  given job is reproducible
- >= 6 clips AND user-supplied music (DCGS flow): three contexts {A,B,C},
  bookend A...A, interior varied (ABCBCA / ACBBCA class); C is one more
  transplant/graft anchor in a new setting -- the director owns picking the
  setting, same PRIME RULE (identity from user pixels, never generated)

## Vocal-aware seam planner (design, 2026-08-05 -- answers Andrew's
## "are you performing waveform analysis to select seam locations?" -- today: NO)

Today's seams are FIXED ARITHMETIC: stride = clip_len - crossfade (9.892s),
zero audio awareness. chain.py's --musical-seams mode exists but is mutually
exclusive with crossfade (uniform-length assumption -- an engineering
shortcut, not physics). The build:
1. Vocal spans already exist (silero, computed for the activity gate).
2. Place each seam inside a vocal GAP nearest the nominal stride point
   (search window nominal +/- ~2s); a cut lands BETWEEN phrases, never
   mid-word. Fallback when no gap exists in-window: the quietest 0.3s of
   the isolated stem in-window.
3. Clip lengths go VARIABLE (min_clip_frames .. 241-frame cap); crossfade
   overlap math per-seam, not per-uniform-stride.
4. The LAST clip is sized to end exactly at song end -- kills the orphan-stub
   class entirely (no more 0.65s tails; the 2.5s stub rule remains as a
   safety net).
5. Scene-sequence rules above apply AFTER seam placement (patterns index
   clips, seams index time).

## Port note
Identical module serves the site's Sing worker (its rp_handler can call the
director via API; DINO/SAM run on the pod). Goes in the port pass AFTER the
local app is done, per Andrew's sequencing ruling 2026-08-05. The site's
30s format = 4 clips max, so the site uses the {A,B} patterns only; scene C
is a DCGS long-format feature.
