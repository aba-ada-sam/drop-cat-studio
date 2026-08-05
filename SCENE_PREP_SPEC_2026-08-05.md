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

## Port note
Identical module serves the site's Sing worker (its rp_handler can call the
director via API; DINO/SAM run on the pod). Goes in the port pass AFTER the
local app is done, per Andrew's sequencing ruling 2026-08-05.
