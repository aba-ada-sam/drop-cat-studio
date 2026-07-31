# Image Studio -- handoff, 2026-07-30 evening

## What this is

New tab in DropCat Studio (the app behind the "Drop Cat Go Studio" desktop icon,
`C:\DropCat-Studio`, port 7860): a dedicated, manual Forge image-generation surface
(separate from the existing Chat tab) with an NSFW preset that's supposed to reliably
produce **anatomically correct** genitals -- the images feed Andrew's NSFW music
video pipeline (image -> Animate -> WanGP).

**Current status: genitals are still wrong. Andrew's exact words on the latest round:
"nope, neither of these is anatomically correct."** That's the open problem. Everything
else described below (checkpoint routing, gender/subject detection, regional mode,
safety gate, Animate integration) is shipped and verified working -- don't re-litigate
those unless something else breaks them.

## Read this first (process notes, learned the hard way today)

1. **Andrew cannot see images from tool calls.** Every render round must go on his
   actual screen via a real file. Use `evalview.py`:
   ```python
   import sys; sys.path.insert(0, r'C:\Users\andre\dropcatgo-generator')
   from evalview import show_eval
   show_eval('title', [(r'C:\path\to.png', 'label'), ...], cols=3)
   ```
   It base64-embeds images into a self-contained HTML and opens it. Also
   `Start-Process "<path>"` as a backup to be sure it's actually on screen. See
   `feedback_eval_display_evalview.md` / `feedback_html_review_page.md` in memory.
2. **Don't guess blind for multiple rounds.** When his feedback is vague ("no good",
   "garbage"), look at the image yourself very carefully first -- often the real defect
   is visible and concrete (an artifact, a wrong body part) rather than a vague taste
   miss. When you genuinely can't tell (anatomical correctness of male genitals, for
   instance), say so and ask rather than iterate blind again.
3. **Forge prompts must stay under ~75 tokens** (`reference_forge_prompt_best_practices`
   memory) -- longer prompts chunk into independent CLIP encodes that stop reinforcing
   each other. This SPECIFICALLY caused a regression today: stacking body-taste text
   onto the existing anatomy-anchor text pushed a male render's prompt long enough that
   it silently collapsed back into the "wrong gender" bug. Keep every prompt-builder
   constant lean; if you add something, cut something.
4. Test everything through the **real API** (`POST /api/image-studio/generate`), not
   just raw Forge calls -- the raw-call tests are fine for fast hypothesis-testing but
   the final verification must go through the actual endpoint.
5. Restart pattern: `curl -X POST http://127.0.0.1:7860/api/app/restart` then poll
   `http://127.0.0.1:7860/` for HTTP 200 (usually back in ~1-5s). Required after every
   `core/image_presets.py` edit.

## The core anatomy problem -- full timeline (don't repeat these experiments)

Checkpoint: `perfection25D_illustrious.safetensors`. It has a known, proven bias
(documented in `project_forge_couple_regional_2026-07-11` memory, predates this
session): asked for a man/male anatomy in a plain prompt, it defaults to a
female-presenting figure with a penis grafted on ("futa").

**Fixed today, verified working, don't re-break:**
- Gender collapse itself -- paired POSITIVE assertion + matching NEGATIVE exclusion
  (`_MALE_POSITIVE`/`_MALE_NEGATIVE`, `_FEMALE_POSITIVE`/`_FEMALE_NEGATIVE` in
  `core/image_presets.py`) reliably produces the CORRECT GENDER now, solo and in
  multi-subject (Forge-Couple regional) mode, including same-gender pairs
  ("two men" etc, `detect_subject()`'s plural/count-word logic).
- Breast size -- was "big fake tits... disgusting" per Andrew. Fixed by adding
  `<lora:Flat_Chest_Helper_V1:0.6>` (was sitting unused in the Forge Lora folder)
  plus concrete wording ("small natural breasts") and negatives ("fake breasts,
  implants"). Andrew did not call this out as still-broken in the last round --
  treat as resolved unless he says otherwise.
- Genital ADetailer artifact (v1) -- a white paper/leaf-shaped blob over the vulva
  on a STATIC pose. Root-caused by diagnostic (same seed, ADetailer genital pass
  disabled -> clean). Fixed by giving the genital ADetailer unit its OWN short
  `ad_prompt` (`_GENITAL_AD_PROMPT` dict) instead of inheriting the full busy scene
  prompt -- same principle as the booth pipeline's per-tab ADetailer prompting.
- Seed clamp bug (unrelated but found along the way) -- was capped at 2^31-1
  (signed int32), but Forge's own `random.randrange(4294967294)` (unsigned) can
  return higher values, so reproducing an exact seed silently gave a different
  image. Fixed in `features/image_studio/routes.py` to `4294967294`.

**Still broken / attempted and NOT confirmed fixed:**
- **The actual anatomical correctness of the genitals themselves.** Andrew's most
  specific complaint: "it's not that their cocks are flaccid, it's that they don't
  have proper 'heads'" (glans). Tried: adding "defined glans" / "corona" to both
  `_MALE_POSITIVE` and `_GENITAL_AD_PROMPT["male"]`. Result: Andrew's verdict on the
  next round was still "not anatomically correct" for BOTH genders, with no further
  detail on what specifically is still wrong. **Do not assume the glans wording
  attempt worked -- it was not confirmed, and may have been rejected outright.**
- Erect vs. flaccid is inconsistent across renders even with "erect" explicitly in
  both the user's own prompt AND `_MALE_POSITIVE`. Not solved. Possibly a CFG/weight
  issue (`(erect penis:1.3)`-style weighting untried), possibly needs a dedicated
  LoRA.
- Genital ADetailer artifact (v2) -- recurred in a DIFFERENT form (a black blob
  instead of white) on a DYNAMIC/action pose (woman dancing), immediately after the
  v1 fix (dedicated ad_prompt) seemed to have worked on static poses. Pattern
  suggests the failure may be pose-angle-dependent (the YOLO-World "vulva" detector
  or the inpaint mask may not handle an awkward crop well on action poses) rather
  than purely a prompt-content issue. Untried: lowering `ad_denoising_strength`
  (Forge default 0.4) for the genital unit, raising `ad_confidence`, or checking
  the actual detected mask/bbox on a failing case before assuming the fix.
- **Never actually tried:** `Penis Size Slider - Illustrious - Girthy` LoRA
  (`C:\forge\models\Lora\Penis Size Slider - Illustrious - Girthy_...safetensors`,
  confirmed SDXL-base compatible, weight-driven per its filename convention like the
  other slider LoRAs in that folder). This was identified as a candidate lever early
  in the session and never actually added to `_MALE_POSITIVE`. Given breast size
  needed LoRA escalation (prompt text alone wasn't enough), the same may be true for
  penis shape/head -- **this is the most promising untried lever, start here.**

## Suggested next step

Given two rounds of guessing at the glans/head wording didn't satisfy Andrew and he
hasn't given more specifics than "not anatomically correct" -- the efficient move is
probably to **ask him directly what specifically is wrong now** (shape? proportion?
missing detail entirely? something about the ADetailer inpaint quality?) rather than
keep iterating blind a third time, UNLESS the untried Girthy LoRA produces an obvious,
confident improvement on the first try (worth one clean attempt before asking, since
it's a genuinely new, unexplored lever, not a repeat of something already tried).
Whatever you try, verify via `evalview.py` before claiming anything is fixed.

## Also still open, lower priority

- Face/"Barbie doll" stereotype direction. Andrew: "somehow we need regular ugly
  normal imperfect people" -- added `average looking, natural imperfect skin` to the
  universal `_ANATOMY_POSITIVE` (applies to both genders). Not yet re-verified against
  his specific "ugly normal imperfect" bar after the anatomy-focused rounds took over;
  worth a fresh look once anatomy itself is settled.
- Creature + multi-subject mode is disabled on purpose (produces blended/intersex
  anatomy when tried, see code comments in `core/image_presets.py` around
  `_REGIONAL_SUBJECTS` / `creature_applied`) -- don't re-enable without solving that
  properly first. Solo creature mode works fine and is unaffected.

## Files touched this session

- `core/image_presets.py` -- the preset/prompt-building logic, all the anatomy fixes
  above live here.
- `core/minor_safety.py`, `core/animate_bridge.py` -- unrelated to the anatomy issue,
  shipped and stable, don't need touching.
- `features/image_studio/routes.py` -- the API route; seed clamp fix is here.
- `static/js/tab-image-studio.js`, `static/index.html`, `static/js/app.js` -- the UI
  (prompt/negative/preset/subject/creature controls). Stable, no changes needed for
  the anatomy work.
- `features/chat_studio/routes.py`, `app.py` -- touched earlier in the session
  (unrelated refactor + router mount), stable.

## Git / board state as of this handoff

- Base Image Studio feature: committed `648f3ef`.
- Everything since (creature integration + all anatomy/taste fix rounds, scoped to
  `core/image_presets.py`, `features/image_studio/routes.py`,
  `static/js/tab-image-studio.js` only) is being committed as part of this handoff --
  check `git log` for a commit around this handoff doc's timestamp.
- Do NOT touch or commit `core/llm_router.py`, `features/fun_videos/*.py`,
  `static/js/tab-express.js` if you see them dirty -- that's a different, concurrent
  session's WIP, not part of this feature.
- Studio board (`CLAUDETEAM_WORLD=studio`): claim `imagestudio` should be released as
  part of this handoff. If you see it still showing active, it's a board-tool text-
  matching quirk (the release call didn't exactly match the original claim text) --
  harmless, just release it again yourself or ignore it.
