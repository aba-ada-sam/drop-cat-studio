# Image Studio -- handoff, 2026-07-31 afternoon (start here)

This is the entry point. Two longer docs have the full history -- read them in this order,
but this doc alone should be enough to know what to do next:

1. `IMAGE_STUDIO_HANDOFF_2026-07-30.md` -- the original shipped feature (checkpoint routing,
   gender detection, regional mode, safety gate, Animate pipeline). Still accurate, don't
   re-litigate any of it.
2. `IMAGE_STUDIO_HANDOFF_2026-07-31.md` -- the ~10-hour anatomy/taste session plus one
   continuation round this morning. Long (213 lines), has every failed attempt in detail so
   you don't repeat them. Read it before changing anatomy/body/face wording.

All code is in `C:\DropCat-Studio\core\image_presets.py` (plus one fix in
`services\manager.py`, see that doc's "Infra fix" section). **Everything is still
UNCOMMITTED** -- Andrew has never asked for a commit across two full sessions now, don't
commit without him asking.

## Where things actually stand right now

- **Male**: good, Andrew-approved (human, erect, defined glans/corona, no precum, no
  bodybuilder look). Not actively being worked on.
- **Female**: real, verified progress on thickness, skin texture, ethnic/facial variety, and
  several real bugs (duplicate figures, graphic artifacts) are fixed with confirmed root
  causes. Face imperfection got a genuine mechanism-level fix this morning (see below) --
  better, not fully solved.
- **UPDATE 2026-07-31 afternoon: the phantom-partner bug below IS INVESTIGATED AND FIXED.**
  Root cause: naming "penis, male anatomy" in `_FEMALE_NEGATIVE` was the exact rule-1 bug
  (naming a concept in the negative cues it back in) this file has already proven three times
  (clothing, armor/tattoo, doll-face) -- a 4th recurrence, this time on the one negative term
  that looked like the safest, most purely-anatomical exclusion. Smoking-gun evidence: a
  follow-up seed (716204) rendered literal garbled TEXT reading "PEN..." on her thigh -- the
  checkpoint drawing the actual negated word as a graphic. Fix: deleted the term from
  `_FEMALE_NEGATIVE`, strengthened the existing solo/single-figure assertion in
  `_female_positive` from an unweighted phrase to `(solo female, completely alone,
  unaccompanied, only one figure in the frame:1.3)`. VERIFIED LIVE through the real API: both
  previously-bad seeds (716201, 716204) are clean post-fix, no partner/penis/text-artifact, on
  top of 3 more seeds from the same bad batch and 2 fresh regression seeds. The underlying
  seated/spread-leg pose bug (see priority 3 below) still recurs on those same seeds -- this
  fix stopped the anatomy-summon riding on top of it, not the pose itself; don't conflate the
  two. Male path untouched, spot-checked clean (no regression). Full detail in
  `core/image_presets.py`'s `_FEMALE_NEGATIVE` comment.
- Original note for context (superseded by the fix above, kept for history): during routine
  regression testing this morning, a `subject=female` solo render (scene: "farmers market")
  produced an implied male sex partner in frame despite `_FEMALE_NEGATIVE` explicitly
  excluding "penis, male anatomy".

## Priority order for whoever picks this up

1. ~~Investigate the phantom-partner bug above.~~ **DONE, see the UPDATE note above.** Not the
   regional-collapse mechanism -- that code path never runs for solo subject=female. Was the
   rule-1 negative-prompt bug, fixed and verified. **RED-TEAMED 2026-07-31 afternoon**: an
   independent agent (no context from the fixing session) read the code cold and ran 12
   adversarial renders deliberately targeting the riskiest compositions (kneeling/seated/close-
   up/lying/squatting, plus a fresh seed on the original scene and a deliberate "gynecological
   exam table" scene) -- zero recurrence of implied male anatomy or a second figure, zero
   duplicate-twin-female regression. Verdict: fix holds. Per Andrew's rule-6 adversarial gate,
   this item is genuinely closed, not just self-reported.
2. ~~Continue the face-imperfection push~~ **Denoising lever TRIED 2026-07-31 afternoon, did
   NOT help.** Bumped `ad_denoising_strength` 0.5 -> 0.65 (the one untried lever from this
   morning that didn't need Andrew's sign-off), tested live on locked seed 555001 through the
   real API -- the before/after renders are nearly indistinguishable, no visible structural
   change to the face. Kept at 0.65 (no regression, no reason to revert) but this did NOT move
   the doll-face complaint forward, unlike the earlier ADetailer-model-swap fix which did. The
   denoising lever looks tapped out. **Only remaining lever is the Illustrious-native "Faces &
   Nationalities+Realism" LoRA** found on Civitai this morning -- NOT installed, needs Andrew's
   one-paragraph taste sign-off first (creative-decisions rule), don't install unilaterally.
   This item is now blocked on that decision, not on more prompt/ADetailer tuning.
3. **Seated/kneeling pose bug** (the original, milder form) -- still unsolved after many
   attempts, full failed-attempt log in the code comments above `_female_positive`. Don't
   re-try anything already listed there.
4. **"Brickhouse" build consistency** -- reads right on some seeds, too toned/athletic on
   others, same prompt weights. Not chased further yet.
5. **Graphic/body-paint artifact still recurs, unrelated to the phantom-partner fix above** --
   surfaced twice more during this round's regression batch (an orange/white geometric object
   over the chest/pubic area on one seed, a red splash/body-paint mark on another, both fresh
   seeds never in the original artifact-saga log). Not investigated -- flagging that the
   "plain unmarked skin" positive fix from the earlier saga (see `IMAGE_STUDIO_HANDOFF_2026-07-31.md`)
   isn't a complete fix, just noted honestly rather than chased further this round.

## Critical process reminders (proven the hard way, twice each)

- **Never name a visual concept in the negative prompt, not even to suppress it.** This
  checkpoint doesn't do logical negation -- naming "bikini," then later "armor/harness/
  tattoo," in the negative is what SUMMONED those exact things onto renders, not what
  suppressed them. Caught and fixed three separate times this session already (rule 1 in
  memory `reference_forge_prompt_best_practices` -- read it before adding any negative term).
  If a new artifact shows up, check the negative prompt for a matching noun before blaming a
  LoRA.
- **`evalview.py`'s `show_eval()` already opens the browser itself.** Do not also call
  `Start-Process` on the same file -- Andrew has called out the duplicate tab twice.
- **Test through the real API** (`POST http://127.0.0.1:7860/api/image-studio/generate`), not
  just raw Forge calls -- isolated raw-call fixes have twice failed to reproduce through the
  real app because the real assembled prompt includes a shared block the isolated test didn't
  have.
- **Look at the actual PNG yourself (Read tool) before showing Andrew anything or claiming a
  fix worked.** Every bug in this doc (duplicates, artifacts, the phantom partner) was only
  caught this way, not by reasoning about the prompt text.
- Restart after editing `core/image_presets.py`: `POST /api/app/restart`, poll `/` for 200.
  Editing `services/manager.py` needs a full relaunch instead: `cscript //nologo
  "C:\DropCat-Studio\launch-silent.vbs"`, then verify both `manager.pyw` and `app.py` are
  running (`Get-CimInstance Win32_Process -Filter "Name='python.exe' or Name='pythonw.exe'" |
  Where-Object { $_.CommandLine -match 'DropCat-Studio' }`). Also check
  `Get-NetTCPConnection -LocalPort 7899 -State Listen` occasionally -- orphaned duplicate
  `wangp_worker.py` processes have shown up a few times, harmless but worth cleaning up.

## New: PIN-gated admin review loop (2026-07-31 afternoon, Andrew's request)

`http://127.0.0.1:7860/admin` (also reachable via Tailscale at this box's tailnet IP for
phone use) -- a standalone, keypad-gated page, NOT linked from the tab rail. Generates 4
images from a scene/subject/preset, click one to select + optional feedback text, Submit
generates the next round (close-seed variants of the pick, feedback folded into the
prompt) -- loops indefinitely. Code: `features/admin_review/routes.py`,
`static/admin_review.html`, wired into `app.py` (search "admin_review"). PIN lives in
`admin_pin.json` (repo root, gitignored-by-convention -- don't commit it). Independently
security-reviewed same session: one real finding (generated images were served through the
app-wide, unauthenticated `/output/*` route, which would have defeated the PIN gate the
moment this left localhost) -- fixed by serving admin-review images through the router's
own authenticated `/api/admin-review/image/...` route instead, verified live (401 without
the session cookie, 404 on a path-traversal attempt, 200 with a valid cookie). Also added a
global brute-force cap alongside the per-IP one, and a request-body size cap. Reuses
`core/image_presets.build_forge_payload` -- no changes to the anatomy/taste prompt logic
itself, this is a review harness around it.

**Upgraded same session, per Andrew's ask ("can the admin talk to the program based on 4
presented images?")**: the feedback box is now genuinely vision-mediated, not string
concatenation. Typed messages + the 4 images currently on screen go to
`core/llm_router.route_vision()` (the SAME mechanism `features/chat_studio` already uses for
NSFW-safe vision -- Anthropic/OpenAI tried first, transparent fallback to Featherless/Kobold on
a content-policy refusal, per this repo's own `CLAUDE.md` "LLM routing" section). The vision
LLM returns a rewritten scene prompt, an optional short negative-prompt addition, and an
optional pick of which image (1-4) to anchor the next batch to -- verified live end to end
("I like image 2 best, change the setting to a rainy city street at night, standing not
sitting" correctly anchored to image 2's seed and rewrote the prompt; a follow-up "give her red
hair instead" correctly anchored to the prior pick and changed only the hair). On any vision-
call failure it falls back to the OLD raw-text-append behavior rather than silently dropping
the admin's message -- `llm_used: false` in the response tells the client this happened. System
prompt (`ADMIN_CHAT_SYSTEM_PROMPT` in `features/admin_review/routes.py`) reuses chat_studio's
proven prompt-engineering rules verbatim, plus an EXTRA rule specific to this session's own
proven lesson: never let the assistant put a body-shape/pose/anatomy concept in the negative
prompt to suppress it (the same rule-1 bug, taught to the assistant explicitly so it doesn't
reintroduce what this session spent hours fixing).

**Observed limitation, not a new bug, worth knowing:** the "anchor variant" batch (anchor seed,
+1, +2, +3) can land on different entries of the existing seed-keyed `_FEMALE_APPEARANCE`
rotation for each of the 4 -- so one render in a batch can show a different hair color than the
other 3 even when the prompt explicitly states a color, because the rotation's own weighted
clause competes with the free-text prompt. Seen live: one image out of 4 came out silver-haired
in a "give her red hair" round. Not chased further this round -- flagging for whoever picks up
appearance-variety work next, since it's a real interaction between this feature's seed offsets
and existing code, not something to quietly ignore.

## Board / coordination

Studio board (`CLAUDETEAM_WORLD=studio`), role name `imagestudio` used consistently across
both sessions so far -- check `team.py status` before claiming, the claim from this morning's
continuation round may still show active and should be released/re-claimed as appropriate.
