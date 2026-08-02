# Image Studio -- handoff, 2026-07-31 morning

## What this is

Same feature as `IMAGE_STUDIO_HANDOFF_2026-07-30.md` (read that first for the original
shipped feature list: checkpoint routing, gender/subject detection, regional mode, safety
gate, Animate pipeline -- all still shipped and stable, don't re-litigate). This doc covers
everything that happened in the ~10-hour session that picked up that handoff: the anatomy
work went through several major pivots and a lot of hard-won, non-obvious fixes. All work in
`C:\DropCat-Studio\core\image_presets.py` (plus one fix in `services\manager.py`).

**Current status:** male is in a good, Andrew-approved state (human, erect, no precum). Female
is much improved (real thickness, real skin texture, ethnic variety, no more duplicate-figure/
artifact bugs) but Andrew's last direct question was "are you not capable of creating imperfect
faces" -- the doll-face complaint has survived 6 rounds of escalation and is NOT solved. Two
other items are known-open: the seated-pose bug, and "brickhouse" consistency across seeds.

## Read this first (process notes, learned the hard way this session)

1. **Andrew cannot see images from tool calls.** Every render round must go on his actual
   screen via `evalview.py` (`sys.path.insert(0, r'C:\Users\andre\dropcatgo-generator')`,
   `from evalview import show_eval`). **`show_eval()` already calls `webbrowser.open()`
   internally -- do NOT also call `Start-Process` on the same file, it opens a duplicate tab**
   (Andrew called this out directly this session).
2. **THE #1 LESSON, LEARNED AND RE-LEARNED THREE TIMES THIS SESSION: never name a visual
   concept in the NEGATIVE prompt, not even to suppress it.** Diffusion models don't do
   logical negation -- naming "bikini" in the negative produced a bikini; naming "armor,
   harness, tattoo, graphic on skin" in the negative produced literal armor/graphic-splash
   artifacts on nearly every female render for almost an hour before this was caught. The fix
   is always the same: delete the negative term entirely, replace with a POSITIVE assertion
   of the opposite ("plain unmarked skin, solid even skin tone" instead of "no tattoo, no
   graphic"). This is now written into `reference_forge_prompt_best_practices` memory as rule
   1 with two dated incidents -- **read that memory before adding ANY negative prompt term**,
   don't re-learn this a fourth time.
3. **When a LoRA or wording change doesn't reproduce through the real API the way it did in
   an isolated raw Forge test, the isolated test was missing something the real assembled
   prompt has** (usually a shared/universal block). Don't trust an isolated win until it's
   re-verified through `/api/image-studio/generate`.
4. Restart pattern for `core/image_presets.py` edits: `POST /api/app/restart`, poll `/` for
   200. For edits to `services/manager.py` (the watchdog), that's a *different, longer-running*
   process -- app-restart does NOT reload it; you need `cscript //nologo launch-silent.vbs`
   after killing the old manager/app processes.
5. **The whole DCS app (or just the WanGP worker) dies under heavy Forge load, semi-regularly
   this session.** Recovery: `cscript //nologo "C:\DropCat-Studio\launch-silent.vbs"`, poll
   `/` for 200, then verify both `manager.pyw` and `app.py` show up in
   `Get-CimInstance Win32_Process -Filter "Name='python.exe' or Name='pythonw.exe'" |
   Where-Object { $_.CommandLine -match 'DropCat-Studio' }`. Also watch for TWO wangp_worker.py
   processes fighting over port 7899 (orphan from a previous crash) -- `Get-NetTCPConnection
   -LocalPort 7899 -State Listen` shows which PID actually owns it; kill the other one.

## Timeline -- major pivots (read in order, each corrects the previous)

1. **Genital ADetailer removed entirely.** It was logging "nothing detected" ~100% of the
   time for male ("penis, testicles" -- open-vocab YOLO-World has no real signal for these
   classes at any confidence, checked down to 0.05) and produced black-blob artifacts for
   female when it did fire. Anatomy detail now lives entirely in the base-generation prompt.
2. **Foot ADetailer tried (copied from the proven `DropCat-Overnight/dropcat_overnight.py`
   recipe) and removed** -- same investigation, 0% fire rate on this file's 1024x1024 framing
   despite working in that other repo. Lesson in
   `feedback_check_proven_recipes_before_reinventing` memory: a proven recipe from a sibling
   repo is a hypothesis to verify against THIS pipeline, not a guarantee.
3. **Hand ADetailer added and confirmed firing** (unlike genital/foot) -- `hand_yolov8n.pt`,
   a real trained closed-set model, not an open-vocab guess.
4. **Body/skin wording rebuilt from the proven DropCat-Overnight recipe** instead of ad-hoc
   paraphrasing -- see the same feedback memory above for why that mattered (Andrew: "we've
   been over this a thousand times, review git").
5. **MAJOR PIVOT: males were made creature-only** ("the dudes should be creatures," avoid
   realistic human anatomy) -- built a whole anthropomorphic-creature system (7 iterations to
   get a genuinely furred, bipedal, non-costume-looking result; animal-type ROTATION added
   since it was hardcoded to wolf every time; "muzzle" turned out to mean bondage-gear muzzle
   to this checkpoint, not animal snout -- reworded to "snout").
6. **MAJOR PIVOT BACK: "I made a mistake in requiring this to be an animal... open it back up
   to humans."** Reverted male to the human path (`_MALE_POSITIVE`/`_MALE_NEGATIVE`, kept
   intact through the creature detour specifically for this). Creature code (`_creature_male_positive`,
   `_CREATURE_TYPES` rotation, `_CREATURE_MALE_NEGATIVE`) is NOT deleted -- still reachable via
   the `creature` checkbox, which now behaves normally again (same as it always has for
   female) instead of being forced on for every male render.
7. **"lose the pre-cum and focus on the erect"** -- removed the precum/wet-tip clause from
   both male paths, kept the weighted `(erect penis:1.4), (defined glans, corona:1.4)`.
8. **Female: "too fat, I want strength more than pot belly"** -- had pushed
   `Body_weight_slider_ILXL` too hard (0.9) plus literal "overweight, big belly" wording.
   Corrected to a strength framing (`ILL_muscular_female` LoRA + "powerlifter, strongwoman"
   wording) -- but `ILL_muscular_female` turned out to cause graphic/tattoo-like artifacts at
   every weight tried (0.5, 0.3, 0.2) and was **dropped entirely**.
9. **Female appearance variety rebuilt** -- "avoid asian and brunette, tired of the same doll
   face." Old `_FEMALE_APPEARANCE` had an Asian-coded pairing ("olive skin, black straight
   hair") and half the entries used black/dark-brown hair. Rebuilt: no black/brown hair
   anywhere (blonde/red/gray/vivid colors -- vivid ones fit the core DropCat "colorful" brand),
   no Asian-coded combos, and each entry now varies FACIAL STRUCTURE (nose/jaw/cheekbones/lips)
   not just color, since color-only variation was why it still read as "the same face."
10. **"brickhouse not hourglass"** -- added his own word directly, plus "rectangular torso, no
    waist curve." Body-weight LoRA calibration turned into a multi-round saga (0.5 -> 0.9 too
    fat -> 0.5/0.6 too slim after dropping the muscular LoRA -> 0.75 brought BACK the artifact
    -> settled at 0.6 + pushed wording weight to 1.4, the safe ceiling).
11. **NEW BUG this round: duplicate/twin figures on SOLO female renders** (3 of 4 test
    renders, reproduced on a plain non-symmetric scene so not scene-triggered). Root cause:
    the thigh-gap fix wording said "inner thighs touching and PRESSED TOGETHER" -- "pressed
    together" is strongly couple/embrace-coded in this checkpoint's training data and, without
    an anchor to a single body, read as two people embracing. Fixed: reworded to explicitly
    anchor to "her own thighs," dropped "pressed together," added an explicit `solo, one
    woman alone, single figure` positive cue and a duplicate-figure negative (female never had
    one before -- only creature mode did, via `_CREATURE_SOLO_POSITIVE`).
12. **THE ARTIFACT SAGA (this is the important one to not repeat).** A white/red/orange
    graphic-splash or literal armor/tattoo mark kept appearing on female chests/torsos across
    many different LoRA-weight combinations. Spent a long time chasing it as a LoRA-stacking
    problem (tried dropping `ILL_mature_female`, reducing `Body_weight_slider_ILXL` weight,
    removing `ILL_muscular_female`) -- **wrong theory**. The actual cause: when the artifact
    first appeared, the negative prompt was given "tattoo, body paint, marking on skin, logo,
    symbol, text, writing, graphic on skin, harness, straps on skin, armor" to try to suppress
    it -- this is EXACTLY lesson #2 above (naming a concept in the negative cues it back in),
    just not recognized as the same bug in the moment. Proved it by isolating: same seeds,
    artifact present with that negative block, gone the instant it was removed and replaced
    with a positive `(plain unmarked skin, solid even skin tone, uniform skin surface:1.2)`.
    **Verified clean on the two previously-artifacted seeds after the fix.** `Body_weight_slider_ILXL`
    was never actually the culprit -- don't re-blame it if this recurs, check the negative
    prompt for named visual concepts first.

## UPDATE 2026-07-31 later morning (fresh instance picked this up)

**Item 1 below (imperfect faces) got a real, mechanism-level root-cause fix this round, not just
more wording** -- see memory `project_dropcat_image_studio_2026-07-30`'s 2026-07-31 continuation
section for the full writeup. Summary: found and fixed THREE places actively fighting the
imperfect-face goal (all the same "naming a concept in the negative backfires" bug already
proven twice: `_SAFETY_FLOOR_NEGATIVE` and `_FEMALE_NEGATIVE` both named "doll face/symmetrical
perfect face/flawless" directly in the negative; `_face_adetailer_unit`'s own `ad_prompt` said
"symmetric ... good proportions", directly contradicting the base prompt). BIGGER find: a raw
Forge call proved a THIRD, completely unconfigured ADetailer face unit (`face_yolov8n.pt`,
Forge-side persisted UI state, invisible from this repo) was running on every render with no
custom prompt, repainting the face using the raw bug-laden negative prompt -- now explicitly
disabled via `_AD_UNIT_3_DISABLE`. Also swapped `_face_adetailer_unit`'s own model from
`mediapipe_face_full` (confirmed, again, essentially never fires) to `face_yolov8n.pt` (proven
firing) so the carefully-worded ad_prompt actually reaches pixels; denoising bumped 0.42 -> 0.5.
**Verified live, real API, locked seed, 3-step before/after comparison**: visible, real
improvement in skin-texture imperfection (freckles much more prominent) once the face pass
started actually firing. Face bone structure/symmetry is still fairly conventional -- this is
genuine partial progress, NOT a full solve, said plainly to Andrew. Also did a bounded Civitai
search for a realistic-skin/plain-face LoRA per this handoff's own suggestion -- found real
candidates (notably an Illustrious-native "Faces & Nationalities+Realism" LoRA, more relevant to
the bone-structure half of the complaint than a skin-only LoRA would be) but did NOT install
anything without Andrew's taste sign-off first.

**Also surfaced (unrelated to this fix, pre-existing, NOT investigated further this round):** the
seated/spread-pose bug (item 2 below) reproduced again in this round's regression batch, and in
one case worse than previously documented -- a `subject=female` "farmers market" scene produced
an implied male sex partner (visible penis) despite `_FEMALE_NEGATIVE` explicitly excluding
"penis, male anatomy". Flagging honestly rather than only reporting the face-fix good news.
Regional multi-subject mode also confirmed still broken (collapses to one figure), unchanged.

## Still open, told to Andrew, NOT solved

1. **"Are you not capable of creating imperfect faces?"** (Andrew's exact words, last message
   before this handoff). 6 rounds of wording escalation (freckles, imperfect teeth, no makeup,
   tired eyes, uneven skin tone, visible pores, amateur photo quality -- all at 1.4, the safe
   weight ceiling) plus a structural attempt (countering "2.5D illustration" with "unretouched
   amateur photograph, realistic photography, not illustration" in `_STYLE_LEAD`) have produced
   real but incomplete improvement -- visible freckles and less glossy skin now, but face bone
   structure is still fairly conventionally symmetric/attractive. No LoRA for "plain/amateur/
   ugly" exists in `C:\forge\models\Lora\` (checked). **UPDATE 2026-07-31: see the update section
   above this list** -- root-caused a phantom 3rd ADetailer unit + 3 backfiring negative-prompt
   spots, real but partial improvement verified live. **Next untried lever**: escalate
   `ad_denoising_strength` further (now 0.5, was 0.42) now that the face pass reliably fires, or
   pitch Andrew the Illustrious-native faces/nationalities LoRA found in the Civitai search.
2. **Seated/kneeling nude scenes still collapse into a wide-open spread-leg pose** on this
   checkpoint, independent of body weight or wording -- hit repeatedly THIS session even on
   scenes that never asked for sitting ("walking through a farmers market" produced a seated
   pose twice). Long failed-attempt log in the comment history above `_female_positive` in
   image_presets.py. Treat any scene that could plausibly resolve to sitting/kneeling as a
   real risk, not an edge case.
3. **"Brickhouse" build is inconsistent across seeds/scenes** -- some renders read genuinely
   thick and blocky, others still lean toward toned/athletic even with identical prompt
   weights. Current config (`Body_weight_slider_ILXL:0.6` + wording at 1.4) is the best
   verified-clean state, but it's not deterministic.
4. **Regional multi-subject mode (`forge couple`) is still broken** -- confirmed collapsing to
   a single figure, pre-existing from before this session, not investigated further (out of
   scope of what Andrew's actually been reviewing, which is all solo renders).
5. **Creature-male torso fur is inconsistent** (from before the human-male reversion, now
   lower priority since male is human again, but the creature path is still reachable via the
   checkbox) -- fur reliably covers head/limbs/hands/feet, torso center sometimes still shows
   a bare-ish patch. Not revisited since the human reversion.

## Infra fix this session

`services/manager.py`'s stuck-WanGP-generation watchdog (120s no-progress timeout, already
well-designed) was gated behind `_wangp_worker_proc` being a live, tracked Python process
handle -- across several app-crash-and-relaunch cycles this session, that handle went stale/
None while a real, hung worker was still alive on port 7899, silently blinding the watchdog
(both its crash-restart AND stuck-check branches no-op when the handle is None). Andrew hit
this live (his own image-auto-generate blocked with "Video render in progress" while nothing
was actually rendering). Fixed by making the stuck-check run unconditionally (it's already
port/HTTP-based with a try/except, doesn't actually need the tracked handle). Reasoned through,
NOT stress-tested against a real deadlock post-fix. Also found and killed a duplicate orphan
wangp_worker.py process this session (harmless -- verify with `Get-NetTCPConnection -LocalPort
7899` which PID actually owns the port before killing the other).

## Files touched this session

- `core/image_presets.py` -- everything above.
- `services/manager.py` -- the watchdog stuck-check fix (see Infra fix above).

## Git / board state as of this handoff

- Everything above is UNCOMMITTED. Andrew has not asked for a commit at any point this
  session -- don't commit without him asking.
- Studio board (`CLAUDETEAM_WORLD=studio`): claim `imagestudio` should be released as part of
  this handoff (same board-tool text-matching quirk noted in the prior handoff may apply --
  harmless, just release again if it still shows active).
- Full blow-by-blow (including raw test results and exact wording tried/rejected at each step)
  is in memory `project_dropcat_image_studio_2026-07-30` -- read that alongside this doc,
  it has detail this doc compresses out.

See also `IMAGE_STUDIO_HANDOFF_2026-07-30.md` (the original feature handoff, still accurate
for everything NOT related to anatomy/taste), memory `feedback_check_proven_recipes_before_reinventing`,
memory `reference_forge_prompt_best_practices` (rule 1 + rule 8, both added this session).
