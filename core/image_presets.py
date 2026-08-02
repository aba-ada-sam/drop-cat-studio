"""Forge image-generation presets for the Image tab (features/image_studio).

The NSFW preset wraps -- never replaces -- whatever prompt the user typed. The
user's own text is always the subject; the wrap only adds a checkpoint choice,
a short style/safety lead-in, subject-aware anatomy anchoring, and ADetailer
passes. See reference_forge_prompt_best_practices: short and concrete beats
long and evocative, so keep every wrap lean.
"""
import random
import re
from pathlib import Path

DEFAULT_CHECKPOINT = "zavychromaxl_v90.safetensors"
NSFW_CHECKPOINT = "perfection25D_illustrious.safetensors"

# Forge prompts are meant to stay well under 75 tokens (see
# reference_forge_prompt_best_practices) -- 2000 chars is already generous.
# Capping here, BEFORE the minor-safety judge ever sees the text, guarantees
# the judge always reviews the full string that actually reaches Forge (the
# judge's own 4000-char window in core/minor_safety.py is otherwise a blind
# spot padding could hide content behind).
MAX_PROMPT_CHARS = 2000

# Proven 2026-07-11 M/F anatomy-fix recipe (perfection25D + light Fumetti for a
# 2.5D semi-realistic look + rating_explicit).
#
# Depth/foreground added 2026-07-30 -- Andrew: "you need to use depth, as in
# things very close to the viewer, and depth blur." Flat, everything-in-focus,
# dead-center full-body compositions were part of what made every render read
# amateurish/posed (same complaint shape as project_forge_image_gen's earlier
# "everything is centered and looks amateurish"). A near-camera foreground
# element + shallow depth of field is a standard photographic fix and, as a
# side effect, breaks up the maximally-flat full-frontal framing that's been
# tangled up in the pinup-pose problem all session.
# "why is she perfect" -- 6th round on this exact complaint. Reasoned it
# through structurally this time instead of pushing more skin-texture
# adjectives: "2.5D semi-realistic ILLUSTRATION" is, by definition of being
# an illustration rather than a photograph, an idealizing/smoothing medium
# -- no amount of "freckles, imperfect teeth" wording fully fights what the
# STYLE itself is doing. Added a direct counter-assertion pushing toward
# photographic realism instead. Verified live: visibly less polished/
# airbrushed alongside the existing texture wording.
_STYLE_LEAD = (
    "<lora:nemo_fumetti_eurocomic:0.6>, rating_explicit, "
    "(2.5D semi-realistic illustration:1.15), "
    "(unretouched amateur photograph, realistic photography, not illustration:1.2), "
    "(shallow depth of field, blurred foreground element, cinematic depth:1.2)"
)

# GRAPHIC NOVEL render style -- added 2026-08-02 per Andrew: "we're going to
# have to lean into the german bdsm graphic novel genre for stylistic
# guidance here... people can go to any AI site to create stupid regular
# images." A second, opt-in RENDERING identity alongside _STYLE_LEAD's
# photoreal-leaning 2.5D look -- selected via a new preset
# ("nsfw_graphic_novel", see PRESETS below), never the silent default.
# Reuses this file's already-proven nemo_fumetti_eurocomic LoRA (the SAME
# LoRA _STYLE_LEAD already uses at 0.6) but pushed hard in the opposite
# direction: _STYLE_LEAD actively counter-asserts "unretouched amateur
# photograph...not illustration" to fight the LoRA's own comic-ink pull back
# toward photoreal; this style drops that counter-assertion and inverts it,
# so the LoRA's native bold-ink/high-contrast look is what actually reaches
# the render instead of being fought. Kept the LoRA choice/weight change
# scoped to a NEW alternate constant rather than touching _STYLE_LEAD itself,
# so every existing photoreal preset/render is byte-for-byte unaffected.
#
# Prior art found on this machine but NOT wired into this file: C:\Forge\
# styles.csv already has unused "Painted Graphic Novel" / "Painted NSFW"
# Forge-side saved styles built around a different LoRA
# (vixon_gothic_oil_d4rk01l, confirmed present in C:\Forge\models\Lora\) with
# score_9-tag (Pony-convention) quality tags and painterly-oil/chiaroscuro
# wording. Real prior exploration of this exact aesthetic direction, but
# built for Pony-tag conventions and never plumbed through this preset
# system's checkpoint/anatomy-LoRA stack (Girthy, Flat_Chest_Helper,
# Body_weight_slider -- all tuned against perfection25D_illustrious's
# rating_explicit/Illustrious tag convention, not score_9). Deliberately NOT
# reused here to avoid destabilizing that proven anatomy stack with an
# unverified checkpoint+LoRA combo on a first pass; vixon_gothic_oil is a
# known escalation lever (stronger chiaroscuro/painterly texture) if
# nemo_fumetti_eurocomic alone doesn't read graphic-novel enough on
# retest -- try stacking it at a low weight (~0.3-0.4) before anything more
# invasive like a checkpoint swap.
#
# Scope is RENDERING TECHNIQUE ONLY (ink linework, contrast, panel-art
# treatment) -- no forced setting/prop/wardrobe words (dungeon, leather,
# latex, rope). Two reasons: (1) this file's own house rule (see the module
# docstring) is that the wrap adds a checkpoint/style/safety lead-in, never
# scene content -- _STYLE_LEAD has never forced a setting either, and this
# style shouldn't be the first to break that; (2) _ANATOMY_POSITIVE asserts
# "(completely nude, naked, bare skin, no clothing:1.3)" universally across
# every subject path, and this file has hit the exact clothing-negation/
# graphic-artifact backfire (see the artifact-saga comment above
# _female_positive) enough times to know naming a garment/prop, even as a
# genre flourish, risks fighting that nudity anchor or summoning an
# unintended costume graphic. Genre wardrobe/props (leather, latex, rope,
# restraints, dungeon/industrial setting) are left to the USER'S OWN prompt
# text, same as any other scene detail -- this style renders whatever the
# user describes in bold ink/high-contrast graphic-novel treatment instead
# of photoreal, it doesn't inject genre content on its own.

# ESCALATED same round -- first live test (seed 501001, "woman in a
# rain-soaked alley") came back with a strongly illustrated BACKGROUND (flat
# shading, graphic doors/shutters) but a near-photoreal FIGURE: the
# checkpoint's own photoreal training bias, plus this file's universal
# _ANATOMY_POSITIVE/_female_positive skin wording ("visible pores, matte
# skin, amateur candid photo quality" -- explicitly photographic language,
# proven necessary there for the separate "too perfect/doll face" fight),
# was winning over nemo_fumetti_eurocomic alone on the body specifically.
# Two changes: (1) stacked the vixon_gothic_oil_d4rk01l LoRA noted above as
# the next lever -- confirmed present in C:\Forge\models\Lora\, kept at a
# conservative 0.35 (below this file's own documented "0.5-0.6+ on an
# unfamiliar LoRA risks artifacts" pattern from the muscular-female-LoRA
# saga) since it had never been combined with this checkpoint's anatomy-LoRA
# stack before; (2) added much more concrete, literal comic-art nouns (ink
# outline AROUND the figure specifically, flat cel-shaded coloring, screentone/
# halftone shading) instead of relying on general "graphic novel" framing --
# per this file's own repeatedly-proven "concrete beats evocative" rule.
_GRAPHIC_NOVEL_STYLE_LEAD = (
    "<lora:nemo_fumetti_eurocomic:1.0>, <lora:vixon_gothic_oil_d4rk01l:0.35>, rating_explicit, "
    "(bold graphic novel illustration, inked comic book art, european comic style, "
    "not a photograph, not photorealistic, not a realistic photo:1.4), "
    "(thick black ink outline around the figure, heavy black linework, flat cel-shaded coloring, "
    "screentone shading, halftone dot shading, high contrast, deep chiaroscuro shadow masses:1.4), "
    "(dramatic rim light, moody cinematic lighting, limited stark palette, strong graphic "
    "silhouette, hand-drawn illustrated look:1.2)"
)

# Anthropomorphic creature style -- Andrew's core DropCat brand (colorful
# fantasy creatures, never real people) applied to the NSFW anatomy-accurate
# pipeline. Fumetti weight lowered (0.6 -> 0.4) to leave headroom for the
# creature LoRA; verified live 2026-07-30 this combination doesn't muddy
# either style. "solo, one single ... alone" + the phantom-figure negative
# block below are NOT decorative -- without them this LoRA/checkpoint
# combination spontaneously added an uninvited second (human) figure to a
# solo-creature prompt in testing; this is what suppressed it.
#
# "fur and claws" REMOVED 2026-07-31, same round as the species-wildcard fix
# below (see _creature_male_positive / _CREATURE_SPECIES_TEXTURE) -- this
# style lead is shared by BOTH creature genders and fires before the
# per-species texture wording, so a fixed "fur" here would keep re-asserting
# fur on every creature render regardless of which non-furred species got
# picked, silently undoing the whole fix. "claws" stays asserted per-species
# in the male positive block (task guardrail: keep it, it's proven and
# unrelated to this bug) so dropping it here loses nothing. This constant
# doesn't actually drive the female creature path's body texture either --
# that always comes from _female_positive's own (always-human) wording,
# untouched by this change -- so removing "fur and claws" here has no effect
# on her beyond dropping a style word that already contradicted her own
# "hairless clean skin" line.
_CREATURE_STYLE_LEAD = (
    "<lora:nemo_fumetti_eurocomic:0.4>, <lora:Fantasy_Creatures:0.6>, rating_explicit, "
    "(2.5D semi-realistic illustration:1.15), anthropomorphic creature"
)
_CREATURE_SOLO_POSITIVE = "solo, one single character alone"
_CREATURE_NEGATIVE = (
    "human face, realistic human skin, second person, another figure, "
    "two figures, duplicate character, extra person, multiple characters"
)

# REBUILT 2026-07-30 around the PROVEN recipe in C:\Users\andre\DropCat-Overnight\
# dropcat_overnight.py (Andrew: "we've been over this a thousand times, review
# git for preference and model handling" -- this file had been reinventing
# wording from scratch instead of reusing what's already live-verified
# elsewhere). Key lesson from that repo's own commit history and comments
# (git log: "Overnight: one 2.5D look, positive-only body control..."):
# **negatives are WEAK for body/skin control, and "NOT slim/NOT hourglass" in
# a positive prompt INJECTS those tokens (CLIP has no negation) -- describe
# ONLY what you want, heavily weighted, front-loaded.** That exact lesson
# repeated itself tonight in miniature (the clothing-negative backfire below),
# so apply it at the scale the overnight engine already proved it at, not a
# smaller ad-hoc version.
#
# "too perfect... it's the most important thing" (Andrew, this round) --
# the proven recipe's skin wording is "soft natural skin with a gentle hint
# of realism" (positive, NOT overtly blemished) paired with a negative that
# specifically kills the plastic/mannequin/doll-photo extreme; freckles are
# explicitly called out there as fine. Adopted verbatim rather than my own
# weaker paraphrase ("average looking, imperfect asymmetric face...") which
# hadn't been checked against anything proven.
#
# "normal feet" / "hobbit feet" -- proven recipe's concrete wording adopted
# ("normal proportionate feet, natural human-sized feet, small neat feet").
# A dedicated foot ADetailer pass (the proven recipe runs one) was tried and
# REMOVED -- see the note above _hand_adetailer_unit's foot sibling used to
# sit: verified via the Forge log it doesn't actually fire on this file's
# 1024x1024 framing. Feet rely on wording alone, same as before that attempt.
#
# Nudity: "weird fabric shirt thing" showed up unprompted on a scene that
# never asked for clothing -- the wrap never actually asserted nudity, it
# only implied it via genital-anatomy wording, so the checkpoint was free to
# improvise a garment. First attempt asserted it as a positive AND negated
# garment nouns (bikini, bra, ...) -- made it WORSE, a full latex bikini set
# plus a coat. Isolated live: the negative garment list was the cause (naming
# a garment in the negative can cue it back in, a known diffusion-model
# quirk -- CFG negation doesn't act like logical negation). Dropped the
# garment-name negatives entirely, kept only a weighted positive assertion --
# clean on retest, and consistent with the proven recipe's own
# positive-only-control lesson above.
#
# "smooth hairless clean skin" -- a comment elsewhere in this file already
# claimed this was "a proven separate positive fix" for a pubic-hair
# complaint, but it was NEVER ACTUALLY PRESENT in the live prompt strings
# (checked via grep before writing this) -- fell out in an earlier refactor.
# Restored here, universal (proven recipe applies it to both genders).
# "candid natural unposed moment, caught mid-action" and "knees together,
# closed natural stance, legs relaxed and together" were both tried here and
# REMOVED -- retesting through the real app (not just isolated raw-Forge
# calls) showed the pinup/spread pose persisting WITH these phrases present
# even at the reduced body weight that tested clean in isolation, so they
# were either doing nothing or actively contributing. Pulled both rather
# than carry dead/harmful weight -- see the pose-fix history above
# _female_positive for the full failed-attempt log.
# Imperfection escalated a THIRD time same session -- "too perfect" (this
# round, after the batch of 4). Weighting the same three adjectives
# ("average looking... freckles") twice already wasn't enough; per
# reference_forge_prompt_best_practices, weight isn't the only lever -- add
# MORE concrete nouns covering different angles of "not a polished model"
# (teeth, makeup, skin tone evenness, photo quality itself) rather than just
# pushing the same handful of words harder toward the >1.4 decay ceiling.
# "smooth hairless clean skin" moved OUT of this universal block 2026-07-30,
# same round as the creature-male pivot -- this block is applied to EVERY
# render including creature-males, and "hairless" directly fights "thick fur
# covering the whole body" in _CREATURE_MALE_POSITIVE. Root-caused after two
# separate creature attempts kept rendering a furred head/hands but bare
# human-skinned torso no matter how hard the fur wording was pushed --
# turned out the universal block was actively asserting hairlessness on top
# of it the whole time. It was a female-only pubic-hair fix originally (see
# the comment near _female_positive's body block); moved there, where it
# belongs and can't contradict a creature's fur.
#
# THAT WASN'T THE WHOLE BUG. Removing "hairless" alone still left a bare
# human torso on every creature-male retest. Diffed against the one raw
# Forge call that actually worked (full walkthrough in the comment above
# _CREATURE_MALE_POSITIVE) and found EVERY OTHER phrase left in this block
# is also human-skin-specific: "soft natural skin", "freckles", "matte
# skin", "visible pores", "uneven skin tone" -- all describe bare mammalian
# skin texture, which is exactly what a fully-furred body doesn't have. The
# one working test never included ANY of this block; it used a short
# nudity+hands+feet-only anatomy line. This whole block is fundamentally a
# HUMAN-skin fix (imperfection, hairlessness, skin realism) and doesn't
# belong applied universally to a subject that isn't supposed to have human
# skin at all -- see _ANATOMY_POSITIVE_CREATURE below, which
# build_forge_payload now uses instead of this one for creature-male.
_ANATOMY_POSITIVE = (
    "(completely nude, naked, bare skin, no clothing:1.3), "
    "(soft natural skin with a gentle hint of realism:1.2), "
    "perfect hands, "
    "(normal proportionate feet, natural human-sized feet, small neat feet:1.2), "
    "(average looking, plain ordinary face, natural asymmetric features, imperfect teeth, "
    "no makeup, tired eyes, uneven skin tone, visible pores, matte skin, freckles, "
    "amateur candid photo quality:1.4)"
)

# Trimmed anatomy block for creature-male -- everything human-skin-specific
# removed (see the long note above), keeping only what's still relevant to
# any subject: full nudity/no clothing, and hand/foot quality (a creature
# still needs correct hands/feet, just furred/clawed ones per
# _CREATURE_MALE_POSITIVE, not human ones).
_ANATOMY_POSITIVE_CREATURE = (
    "(completely nude, no clothing:1.3), "
    "(normal proportionate feet, natural sized feet:1.2)"
)
# "perfect symmetrical face, flawless skin" REMOVED from this negative
# 2026-07-31 -- Andrew's unsolved "are you not capable of creating imperfect
# faces?" complaint, investigated fresh this round. This negative was doing
# the EXACT thing reference_forge_prompt_best_practices rule 1 and this
# session's own two proven incidents (clothing-negation, armor/tattoo-
# negation) already established: naming a visual concept, even in the
# negative, even to suppress it, can cue it back in on this checkpoint. This
# is the third recurrence of the same bug class, just never recognized as
# the same bug before because it was hiding in the SAFETY floor block instead
# of a hand-written per-round fix. Removed; the positive imperfection
# assertion in _ANATOMY_POSITIVE already covers this from the wanted side.
_SAFETY_FLOOR_NEGATIVE = (
    "anime, chibi, cel shading, waifu, petite, loli, child, teen, teenager, "
    "young girl, youthful, underage, malformed genitalia, deformed penis, extra penis, "
    "extra limbs, fused limbs, deformed, mutated hands, extra fingers, "
    "(huge feet, giant feet, oversized feet, hobbit feet, elongated feet:1.3), "
    "(plastic skin, barbie doll, airbrushed, waxy, smooth plastic skin, mannequin:1.2)"
)

# Appearance variety -- Andrew, this round: "she's ... brunette ... perfectly
# doll fake shit" after every female test tonight (three different scenes,
# two different seeds, one explicitly randomized) came out as close to the
# same dark-haired, similar-featured woman. Root cause: this file had ZERO
# variety mechanism -- _FEMALE_POSITIVE was one static string, so the
# checkpoint's single default "look" for this LoRA/style combo won every
# time regardless of scene or seed. project_forge_image_gen memory documents
# the same class of problem on a different checkpoint (Pony): plain ethnicity
# words under-respond, WEIGHTED skin-tone + hair terms plus an explicit
# rotation (that system used a `woman_look.txt` wildcard) is what actually
# produced visible variety. Ported the principle, not the exact mechanism (no
# wildcard file in this API-driven single-shot generator) -- a short weighted
# rotation, picked deterministically from the render's own seed so a given
# seed reproduces the same person (consistent with this app's seed-replay
# feature elsewhere) while different seeds/requests actually vary. seed=-1
# (caller wants Forge to randomize) falls back to the module-level `random`
# instance so it isn't stuck picking the same "random" entry every time.
# REBUILT same session -- "avoid asian and brunette, I'm tired of seeing the
# same doll face." Two separate problems in the old list: (1) "olive skin,
# black straight hair" is a classically Asian-coded pairing, and half the
# entries used black/dark-brown hair (brunette), so the rotation was never
# giving him the variety it looked like on paper. (2) varying only skin tone
# + hair color while every entry still implicitly shared the same underlying
# FACE STRUCTURE is exactly why it kept reading as "the same doll face" --
# color isn't the only axis of a face. Rebuilt with: no black/dark-brown
# hair anywhere (blonde/red/gray/vivid colors only -- the vivid ones also
# fit the core DropCat "colorful" brand), no Asian-coded combinations, and a
# concrete facial-structure clause (nose shape, jaw, cheekbones, lips) on
# every entry so the rotation actually varies bone structure, not just
# coloring.
_FEMALE_APPEARANCE = [
    "(deep dark brown skin, platinum blonde hair, broad nose, full lips:1.3)",
    "(pale fair skin, natural red hair, freckled face, round cheeks:1.3)",
    "(warm tan skin, honey blonde hair, high cheekbones, angular jaw:1.3)",
    "(rich dark ebony skin, silver gray hair, strong brow, wide nose:1.3)",
    "(light olive skin, copper red hair, narrow face, thin lips:1.3)",
    "(deep brown skin, vivid pink hair, round face, full cheeks:1.3)",
    "(fair freckled skin, strawberry blonde hair, square jaw, gap teeth:1.3)",
    "(medium brown skin, teal blue hair, sharp cheekbones, thin nose:1.3)",
]


def _pick_appearance(seed, variant: int, choices: list) -> str:
    rng = random.Random(seed + variant) if isinstance(seed, int) and seed >= 0 else random
    return rng.choice(choices)

# NO gender/body-shape text belongs in the universal wrap above -- body taste
# is gender-specific (below), so applying either version to every render
# regardless of subject would be the "taste is conditional, subordinate to
# the subject" mistake user_ai_art_style warns about (2026-07-23 Selectorship
# incident). An EARLIER version of this file removed Andrew's locked body
# taste entirely instead of making it conditional, which was an overcorrection
# -- confirmed 2026-07-30 when he reviewed a batch and called out oversized
# breasts and bodybuilder-looking men repeatedly. Fixed here: the taste is
# back, gender-conditional this time, alongside the anatomy anchors.

# GENDER ANCHORING -- proven live 2026-07-30: this checkpoint defaults to a
# female-presenting figure with grafted-on male anatomy ("futa") whenever a
# scene calls for a man, unless explicitly told otherwise. A prompt for
# "a giant winged man, massive erect penis" rendered as a woman with a penis;
# removing an unrelated bug (a hardcoded "adult woman" trailer) did NOT fix
# it -- the checkpoint's own bias is the cause. Fix verified live: POSITIVE
# assertion of gender/body PLUS a matching NEGATIVE exclusion together
# produce a correct male figure (negatives alone are documented as too weak
# per reference_forge_prompt_best_practices -- pairing both is what worked).
#
# Body taste per user_ai_art_style (his locked wording, reused verbatim where
# it exists): women = "thick, stout, broad-shouldered, sturdy solid build,
# realistic natural proportions, modest natural bust" -- explicitly NOT the
# stereotypical big-breasted pinup, a standing ethics line, not just a
# preference. Men = broad + heavy-set + solid, explicitly NOT worded as
# "muscular deltoids / powerful upper body" -- that reads bodybuilder, which
# he has separately called out and does not want (confirmed again live
# 2026-07-30: "image 4,5,6,7,8 = all garbage" on renders that leaned
# gym-bro/shredded). Hairless skin is a proven, separate positive fix (not a
# negative -- see project_forge_couple_regional_2026-07-11) for a pubic-hair
# complaint on the same review.
# Corrected 2026-07-30 after Andrew's review of the "erect fix": the actual
# complaint was never flaccidity -- it's that the glans (head) wasn't
# rendering as a distinct anatomical feature. "erect penis" alone underspecifies
# this; naming the glans/corona directly is the concrete fix (his own
# prompting rules: concrete beats evocative -- describe the anatomy, don't
# just assert a state). That wording alone was STILL not enough (Andrew's
# next round: "not anatomically correct" again, no further detail) -- root
# cause found by reading the Forge server log (_headless.log) for the exact
# rejected render: the genital ADetailer refinement pass (which is where the
# earlier glans/corona wording actually lived) was logging "nothing detected"
# on this and nearly every other render, so that wording was never reaching
# a real inpaint pass -- see the ADetailer-removal note below. Escalated the
# SAME way breast size was escalated: prompt text alone wasn't enough, so
# added a dedicated LoRA (Penis Size Slider - Illustrious - Girthy, sitting
# unused in the Lora folder, same as Flat_Chest_Helper_V1 was) plus weighted
# emphasis on the concrete anatomy terms. Verified live 2026-07-30 (raw Forge
# call, same seed as the rejected render): visibly thicker, better-defined
# glans/corona vs. the wording-only version -- Andrew has not yet confirmed
# this meets his bar, only that it's a clear improvement over what he saw.
#
# REBUILT 2026-07-30, next round: Andrew reviewed a batch and called the
# male render "a chick with a dick" -- his exact words, and something he has
# "explicitly asked me not to create" before (this is the futa bug this
# whole gender-anchoring block exists to prevent, resurfacing on a figure
# whose GENITAL detail he'd separately called "closest yet"). The genital
# fix and the gender-presentation fix are separate problems -- better glans
# wording doesn't make the face/body read male. Diagnosis: "flat chest" is
# itself a female-coded phrase in isolation (it's how you'd describe a
# woman's chest, not qualify a man's), and there was no masculine facial
# wording at all -- the ONLY unambiguous-male signal in the whole prompt was
# "a man, male body" competing unweighted against the rest of the scene.
# Fixed: weighted the gender assertion, replaced "flat chest" with
# unambiguous phrasing, added concrete masculine facial cues (the same
# "concrete beats vague" principle already proven for glans/vulva -- stubble
# and a masculine jaw are unambiguous in a way "male body" alone isn't), and
# strengthened the negative to exclude feminine facial/body reads directly.
# "I want those cocks stiff" -- weighted "erect penis" for the first time
# (it was sitting there unweighted while glans/corona got 1.4). "I want to
# see pre-cum almost on the camera lens" -- added as a concrete anatomy
# detail here (universal to every male render, same as testicles); the
# EXTREME CLOSE-UP FRAMING part of that request is scene-specific, not
# baked into this constant -- that's on whoever writes the user-facing scene
# prompt for a given render (see the close-up test scene used this round).
# RETIRED as the live male path for one round (2026-07-30) -- "the dudes
# should be creatures" -- then REINSTATED same session: "I made a mistake in
# requiring this to be an animal. I want to avoid 'furry' culture, so we can
# open it back up to humans." Kept this constant and its fix history intact
# through the retirement specifically for this -- see
# _CREATURE_MALE_POSITIVE below, still available whenever the `creature`
# checkbox is on (now respected normally again, see build_forge_payload).
#
# "lose the pre-cum and focus on the 'erect'" (same round) -- dropped the
# precum/wet-tip clause entirely, kept the weighted erect/glans/corona
# wording that was already the actually-proven part of this fix.
_MALE_POSITIVE = (
    "(a man, adult male, male body:1.3), (stubble, masculine jawline, "
    "Adam's apple, flat masculine chest:1.3), broad heavy-set build, "
    "<lora:Penis Size Slider - Illustrious - Girthy_alpha1.0_rank4_noxattn_250steps:0.7>, "
    "(erect penis:1.4), (defined glans, corona:1.4), testicles"
)
_MALE_NEGATIVE = (
    "breasts, feminine body, female body, feminine face, feminine features, "
    "soft feminine jaw, bodybuilder, six pack abs"
)

# CREATURE MALE -- the live path for "male" (and multi_male) as of 2026-07-30.
# Anthropomorphic creature body (no "a man"/human framing at all, matching
# Andrew's direction to drop the human) carrying the same proven genital
# anatomy fixes from the human version above (Girthy LoRA, weighted erect/
# glans/corona, precum) -- those fixes are about the GENITALS, which still
# apply to a creature's anatomy the same way. Male no longer respects the UI
# creature checkbox the way female still does -- male is unconditionally
# creature-styled now (see build_forge_payload), so this is used regardless
# of what that checkbox is set to.
#
# THREE tries to land this. (1) "anthropomorphic creature, fur, claws,
# animal features" only landed on the HEAD and FEET -- torso/arms/legs
# stayed bare human skin with a chiseled six-pack, i.e. a bodybuilder
# wearing an animal mask, not a creature (Andrew: "this failed"). (2) Named
# body parts explicitly ("furry chest, furry arms, furry legs") -- WORSE,
# read as a man wearing a mask/costume (visible mask straps/seams on the
# head, patchy fur only near the groin). Itemizing parts reads as
# accessories, not a body transformation. (3) Went holistic ("full-body
# anthropomorphic wolf BEAST, entire body covered in fur... beast
# anatomy") -- overcorrected the other way into a literal quadruped on all
# fours, not anthropomorphic at all (lost the human-like bipedal stance
# that makes it "anthropomorphic" rather than just "an animal"). FIXED by
# combining explicit bipedal/upright-posture wording (which nothing before
# had asserted) with the whole-body-transformation phrasing from attempt 3,
# plus negatives for both failure directions (mask/costume look on one
# side, quadruped/feral on the other). Verified live: black-furred werewolf,
# upright bipedal stance, human-like proportions, visible claws, no human
# skin anywhere, unambiguous erect anatomy -- this is the shipped version.
# "why are the wolves all muzzled?" (Andrew, next round) -- looked at every
# creature render again and he's right: every single one has a visible
# strap/buckle/harness across the snout, not a natural animal mouth.
# Root cause: "animal muzzle" is ambiguous English -- it means an animal's
# snout/mouth region, but on an NSFW-trained checkpoint the SAME word also
# names a bondage/gag muzzle device, and that's clearly the association
# winning here. Classic case of an evocative-but-ambiguous word choice (the
# same class of mistake as "flat chest" reading female-coded) rather than
# a wording-strength problem. Fixed: replaced with "snout", which has no
# fetish-gear meaning, and added the device itself to the negative in case
# the association is still lurking at lower strength.
# "why is every male a wolf?" (Andrew, next round) -- because
# _CREATURE_MALE_POSITIVE hardcoded "wolf-like creature... wolf snout"
# every single time, the same root problem as the pre-fix _FEMALE_POSITIVE
# being one static string (always brunette). Same fix: a rotation, picked
# deterministically from the seed like _FEMALE_APPEARANCE.
# "horse-like" tried and REMOVED same round -- unlike the other six, it
# rendered as a literal quadruped horse standing on four legs, "bipedal,
# walks upright like a human" completely lost. Guessing "horse" is such a
# strong quadruped concept in this checkpoint's training data that it
# overrides the anthro/bipedal framing in a way wolf/lion/bear/tiger/fox/
# panther (all common furry/anthro archetypes) don't. Don't re-add without
# testing -- if picked up again, try "centaur-like" instead, which at least
# names a bipedal-adjacent mythological form rather than a plain animal.
#
# REBUILT 2026-07-31 -- Andrew, verbatim: "'creatures' is fine for the
# males, but you keep giving my 'furry fetish' dudes -- always tiger, lion,
# wolf, etc. I don't want any of that shit. Create a wildcard [with] the
# simple names for 300 different animals, save it, and pick one at random
# each time -- and this is important: nothing with fur." The 6-entry list
# above WAS the bug: every single one a furred mammal (the exact "furry
# fandom" archetype he's rejecting), so the "random" pick only ever cycled
# through 6 furry-coded outcomes. But swapping in non-furred names alone
# would NOT have fixed it -- _creature_male_positive below independently
# hardcoded "thick fur, furry chest, furry body" no matter which of the 6
# was picked, so the texture was never actually tied to the species word.
# Both problems fixed together:
#
# 1. A real wildcard file on disk, wildcards/creatures_nofur.txt (same flat
#    "name|family" per line, "#"-comment convention as this app's existing
#    filesystem-wildcard system in core/wildcards.py, and the same folder
#    style as Andrew's own Forge dynamic-prompts wildcards dir --
#    config.json's sd_wildcards_dir -- so he can hand-edit it the same way
#    he already edits those). 325 real-animal species across 7 zoological
#    families (bird, reptile, amphibian, fish, arthropod, cephalopod,
#    mythical) at initial ship, later expanded to 386 entries / 15 families
#    -- see the "ADDED 2026-07-31" note further down and the wildcard
#    file's own header for the monster/construct/alien families folded in
#    mid-round. Every real-animal entry was picked to exclude mammals AND to
#    exclude any common name that embeds a mammal word ("leopard gecko",
#    "tiger shark") so a random pick can never read as furry-fandom, even
#    accidentally; the monster/construct entries are excluded on the same
#    "nothing with fur" rule by construction (see their own family notes).
#    Not folded into core/
#    wildcards.py's generic __token__ expander -- that system does
#    module-level random.choice on flat one-column lines with no metadata,
#    but this needs (a) the family tag alongside each name to drive the
#    texture/head wording below and (b) the existing deterministic-per-seed
#    RNG this file already uses elsewhere (_pick_appearance et al), so a
#    small dedicated loader is clearer than bending the generic one.
#
# 2. Body-covering/texture and head/mouth wording are now DERIVED FROM THE
#    FAMILY of whichever species got picked (_CREATURE_SPECIES_TEXTURE /
#    _CREATURE_SPECIES_HEAD) instead of a fixed "fur" string -- concrete
#    per-family phrasing (scales/feathers/chitin/etc), not one vague
#    catch-all, matching this file's own "concrete beats evocative" rule.
#    Rest of the template (bipedal/upright stance, humanoid proportions,
#    claws, "not human skin", Girthy LoRA, erect-anatomy wording) is
#    UNCHANGED -- proven and unrelated to the fur bug.
_CREATURE_WILDCARD_PATH = Path(__file__).resolve().parent.parent / "wildcards" / "creatures_nofur.txt"
_creature_species_cache: list[tuple[str, str]] = []
_creature_species_cache_mtime: float = -1.0

# family -> (body-covering/texture phrase, head/mouth phrase). Concrete
# nouns per family, not one vague "scales or feathers or something" phrase
# -- same rule this file has already proven repeatedly for genital/gender/
# skin wording. A raven doesn't have a "snout" and an octopus has neither a
# snout nor a beak in the reptile sense, so the head slot is family-specific
# too rather than forcing every species through one "animal snout and jaw"
# shape (task explicitly called this out; the old fixed snout wording was
# already at least once misread as bondage gear -- see the "muzzle" note
# above -- so keeping this concrete and species-appropriate also avoids
# reopening that ambiguity for the many new species that aren't canid-
# shaped at all).
# "mollusk" ADDED 2026-07-31, same round -- see the wildcard file's own
# "RECLASSIFIED" header note. Live-tested "pearl oyster" under the OLD
# cephalopod texture ("smooth rubbery slick skin, beaked maw, tentacled")
# and it rendered as a near-human figure with only faint tentacle-like
# hair-strand details -- the wrong texture for a shelled/soft-bodied
# non-cephalopod. Split out with its own hard-shell-leaning texture and a
# head phrase that doesn't assume a face (several species in this bucket --
# starfish, jellyfish, coral, anemone -- have no head at all in real life,
# so "no distinct head" is the concrete, accurate choice here, not a vague
# dodge).
#
# CAUGHT same round, retesting the split: mollusk and (true) cephalopod
# BOTH still underperformed on the first pass -- "pearl oyster" with the
# shell wording above rendered as a plain bare-skinned humanoid with
# antlers (no shell texture at all), and "humboldt squid" rendered as a
# human body with decorative tentacle-like hair strands and tattoos, not
# octopus/squid skin. Unlike bird/reptile/amphibian/arthropod (which landed
# clean on the first try), this checkpoint/LoRA combo seems to have much
# weaker training exposure to "shelled humanoid" and "cephalopod humanoid"
# as fantasy-art concepts than it does to feathered/scaled/chitinous
# beast-people (a common trope), so it falls back to a generic fantasy
# humanoid and invents unrelated decoration (antlers, tattoos) instead of
# committing to the requested texture. Fixed by making both phrases more
# concrete and visually specific rather than more abstract -- named actual
# recognizable surface details (suction cup suckers for the squid, iridescent
# nacre/pearl shell plating for the mollusk) instead of generic material
# adjectives, same "concrete beats vague" lesson this file has already
# proven for genital/gender wording. Only these two families were touched;
# bird/reptile/amphibian/arthropod/fish/mythical are unchanged since they
# already verified clean.
#
# Retest: cephalopod landed clean (bulbous octopus head, sucker-covered
# tentacle arms, wet rubbery orange skin, no fur, no human skin) -- kept
# as-is. Mollusk took THREE more retries and is still the weakest family:
# (1) the wording above ("encased in a hard glossy iridescent pearl shell
# like armor plating") came back as a plain bare-skinned human with ram
# horns and small decorative shoulder-pauldron armor plus loose seashells
# scattered on the ground -- "shell" read as a nearby PROP/accessory again,
# the same "itemizing reads as accessories, not a body transformation"
# failure this file's "THREE tries" history already diagnosed for the
# original fur attempt #2. (2) Reworded to whole-body armor-plating framing
# (borrowing the SHAPE that already verified clean for arthropod's
# carapace) plus nested (:1.3) weight and a positive "no bare skin visible"
# assertion -- texture fix WORKED (fully shell-plated body, no bare skin
# anywhere) but introduced a NEW problem: a squat, crouched, almost
# quadruped-reading stance, unlike every other family's clean upright
# figure. (3) Added an explicit "standing fully upright at full height"
# positive assertion to fix the stance -- THAT flipped the failure back the
# other way, texture reverted to plain human skin standing on a giant open
# shell like a pedestal/prop (Birth-of-Venus composition). Texture and
# upright-stance wording appear to be in real tension for this specific
# concept, not a wording-strength problem -- almost certainly the same class
# of issue this file's own history already named for "horse-like" (a
# real-world body plan -- here, a flat round ground-hugging bivalve shell --
# that's such a strong non-humanoid concept in this checkpoint's training
# data that no amount of wording reliably lands BOTH "upright human" and
# "body covered in the source animal's texture" at once). Landed on the
# texture-correct version (this comment's current code) rather than the
# upright-but-textureless one: Andrew's stated complaint was specifically
# about FUR/mammal-archetype, not posture, so a shell-textured creature that
# sometimes reads crouched is a smaller miss than one that silently reverts
# to plain human skin. Flagged as a known, unresolved edge case in this
# round's report rather than iterating further or quietly shipping it as
# solved -- if it keeps recurring, the "horse-like" precedent (just remove
# the offending species/family from the wildcard) is the next lever, not
# more wording attempts.
# Eight more families ADDED 2026-07-31, same round, mid-task -- Andrew,
# looking at reference images: "Why can't we have all sort of weird
# creatures, like actual creatures, monsters, weird shit." The zoology-only
# pool above was narrower than what he actually wants (his own examples
# mixed a reptilian alien, a unicorn, and a fox in one lineup). Same rule as
# everywhere else in this file: concrete nouns per family, not one vague
# "monster texture" catch-all, and NEVER "fur" -- these are non-organic
# (golem/elemental/crystalline), decayed-not-furred (undead), textureless
# (spirit), or thick-hide-not-furred (demonkin/alien/plantfolk) by
# definition, so the same fur-negation logic in _CREATURE_MALE_NEGATIVE
# below still applies unchanged to every one of them.
_CREATURE_SPECIES_TEXTURE = {
    "bird": "covered in sleek feathers, feathered chest, feathered body, plumage",
    "reptile": "covered in hard reflective scales, scaled chest, scaled body",
    "amphibian": "covered in smooth glistening moist skin",
    "fish": "covered in iridescent scales, finned",
    "arthropod": "covered in a hard glossy chitinous exoskeleton, carapace",
    "cephalopod": "covered in wet glistening rubbery octopus skin, rows of round suction cup suckers along the arms",
    "mollusk": "(covered in a hard glossy pearlescent shell, thick segmented carapace-like "
               "shell plating, no bare skin visible:1.3)",
    "mythical": "covered in hard glossy draconic scales",
    "golem": "constructed of solid inorganic material matching its form -- stone, clay, "
             "crystal, obsidian, coral, or metal -- hard sculpted surface with visible seams "
             "and cracks, no organic skin, no flesh",
    "elemental": "made of living elemental matter constantly shifting and flowing -- fire, "
                 "ice, magma, storm, earth, or water matching its form -- no solid organic "
                 "skin, no flesh",
    "undead": "covered in desiccated gray leathery skin stretched tight over bone, or wrapped "
              "in aged decaying bandages, visibly rotted and dry",
    "spirit": "a translucent incorporeal form, faintly glowing ethereal wisps of energy, "
              "see-through body with no solid skin, no flesh",
    "demonkin": "covered in thick warty leathery monster hide, rough bumpy demonic skin",
    "alien": "covered in smooth grey rubbery alien skin, otherworldly unnatural texture",
    "plantfolk": "covered in rough tree bark and moss, woven fibrous plant skin, small "
                 "fungal growths",
    "crystalline": "made of faceted translucent crystal, glowing internal light, hard "
                   "gem-like surface, no organic skin",
}
_CREATURE_SPECIES_HEAD = {
    "bird": "sharp beak, avian eyes",
    "reptile": "reptilian snout and jaw",
    "amphibian": "wide amphibian maw, bulging eyes",
    "fish": "fish-like head, gilled",
    "mollusk": "smooth shell-plated head, small beaked slit mouth among the shell ridges, small sensory tendrils",
    "arthropod": "mandibles, compound eyes",
    "cephalopod": "bulbous octopus head, beaked mouth, tentacle hair",
    "mythical": "draconic snout and jaw",
    "golem": "a sculpted inorganic head with a carved mouth-slit, no organic skin, glowing carved eyes, no hair",
    "elemental": "an elemental head with a glowing mouth-shaped opening where its matter parts, a glowing inner core, no organic skin, no hair",
    "undead": "a gaunt skeletal face, sunken hollow eyes, exposed bone jaw and teeth, no hair",
    "spirit": "a hollow translucent face with a faintly glowing mouth outline, glowing eyes, features barely visible, no hair",
    "demonkin": "a monstrous demonic face, curved horns, sharp fangs, glowing eyes",
    "alien": "an elongated alien head, large black eyes, a narrow lipless mouth slit, no hair, no human features",
    "plantfolk": "a bark-textured head with a knot-like wooden mouth opening, no human face, small leafy or fungal growths",
    "crystalline": "a faceted crystalline head, glowing crystal core, no organic skin, no hair, (a wide mouth of sharp interlocking crystal shards, visible faceted crystal teeth:1.3)",
}
# Fallback only for the (should-never-happen) case where the wildcard file
# is missing/unreadable at render time -- reptile is the closest match to
# the old default wolf-like wording's general "scaled predator" silhouette.
_CREATURE_FALLBACK_SPECIES = ("dragon", "reptile")


def _load_creature_species() -> list[tuple[str, str]]:
    """Load (name, family) pairs from wildcards/creatures_nofur.txt.

    Cached by file mtime (same pattern as core/wildcards.py's filesystem
    cache) so Andrew's hand-edits to the file take effect on the next
    render without an app restart."""
    global _creature_species_cache, _creature_species_cache_mtime
    try:
        mtime = _CREATURE_WILDCARD_PATH.stat().st_mtime
    except OSError:
        return _creature_species_cache
    if mtime != _creature_species_cache_mtime:
        pairs = []
        for line in _CREATURE_WILDCARD_PATH.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "|" not in line:
                continue
            name, _, family = line.partition("|")
            name, family = name.strip(), family.strip()
            if name and family:
                pairs.append((name, family))
        _creature_species_cache = pairs
        _creature_species_cache_mtime = mtime
    return _creature_species_cache


def _pick_creature_type(seed, variant: int = 0) -> tuple:
    """Deterministic-per-seed pick of (species_name, family) -- same seed
    always picks the same species (reproducibility, unchanged mechanism from
    the old 6-entry list, just reading a real 386-entry file now)."""
    species = _load_creature_species()
    if not species:
        return _CREATURE_FALLBACK_SPECIES
    rng = random.Random(seed + variant + 1000) if isinstance(seed, int) and seed >= 0 else random
    return rng.choice(species)


def _creature_male_positive(seed, variant: int = 0) -> str:
    name, family = _pick_creature_type(seed, variant)
    texture = _CREATURE_SPECIES_TEXTURE.get(family, _CREATURE_SPECIES_TEXTURE[_CREATURE_FALLBACK_SPECIES[1]])
    head = _CREATURE_SPECIES_HEAD.get(family, _CREATURE_SPECIES_HEAD[_CREATURE_FALLBACK_SPECIES[1]])
    return _join(
        f"(anthropomorphic {name}-like creature, bipedal, walks upright like a human, "
        f"humanoid stance and proportions, entire torso and arms and legs {texture}, "
        f"{head}, claws, not human skin:1.4)",
        "heavy-set build",
        "<lora:Penis Size Slider - Illustrious - Girthy_alpha1.0_rank4_noxattn_250steps:0.7>",
        "(erect penis:1.4), (defined glans, corona:1.4), testicles",
    )
# "hairless body" DROPPED from this negative 2026-07-31, same round as the
# species-wildcard fix -- it was never actually a defect to negate, it was
# doing the OPPOSITE of what's needed now. It made sense back when every
# creature species was a furred mammal (guarding against a bald/mangy
# werewolf render); now that the whole species pool is deliberately
# non-furred, telling the model "avoid hairlessness" on every single
# creature render actively PUSHES it toward growing fur/hair to satisfy
# that negative -- directly undermining the "nothing with fur" fix, not
# just neutral dead weight. Per this file's own rule (reference_forge_
# prompt_best_practices: naming a concept, even in the negative, can cue it
# back in) the right move isn't to leave it and hope, it's to drop it.
# Replaced with an explicit "fur" family negative instead -- the inverse of
# the old bug and, unlike the ambiguous "muzzle"/ambiguous "modest" cases
# elsewhere in this file's history, "fur" is a plain unambiguous material
# word with no double meaning to backfire on; the SAME class of negation
# (concrete, unambiguous animal-body-form words: "quadruped, on all fours,
# four legs, feral animal, tail") already worked cleanly in this exact
# negative block per the "THREE tries" verified-live history above. Given
# how persistently this exact bug (male creature = furred mammal) has
# recurred in this file even after supposed fixes, defense-in-depth here is
# warranted, not just tolerated -- and this round's own live verification
# (see the report this round shipped with) is what actually confirms it
# doesn't backfire, not just this reasoning alone.
_CREATURE_MALE_NEGATIVE = (
    "human face, realistic human skin, human body, real human, bare skin, "
    "fur, furry, mammal fur, thick fur, animal pelt, "
    "mask, costume, fursuit, muzzle strap, muzzle guard, harness, gag, bondage muzzle, "
    "pet play mask, breasts, feminine body, female body, "
    "quadruped, on all fours, four legs, feral animal, tail, "
    "(bodybuilder, six pack abs, shredded physique, chiseled muscles, gym body:1.3)"
)

# Face-diversity attempt ("distinctive natural face") REMOVED 2026-07-30 --
# confirmed live it did nothing (too evocative/vague, not concrete -- see
# reference_forge_prompt_best_practices). Not replacing it yet; direction
# pending Andrew's call (his own prompt vs. a wildcard system).
#
# Breast size: "modest natural bust" alone was not enough -- Andrew's review:
# "big fake tits... look disgusting." Escalated with a dedicated LoRA
# (Flat_Chest_Helper_V1, previously unused in the Forge Lora folder -- built
# for exactly this) plus concrete positive wording and matching negatives
# instead of another vague adjective.
#
# Vulva detail: "visible vulva" alone rendered under-detailed (same failure
# shape as "erect penis" alone did for men -- a vague noun, not a concrete
# description). Same fix applied: name the actual anatomical part (labia)
# and weight it, instead of leaning on ADetailer to add detail it was never
# reliably adding (see the ADetailer-removal note below).
#
# CAUGHT 2026-07-30, same session: weighting (visible vulva, labia:1.3) alone
# solved the detail problem but broke a LONG-STANDING locked rule
# (user_ai_art_style memory, his own words, 2026-07-11/07-23): "I HATE pinup
# poses", "NSFW doesn't mean a naked woman spreading her legs porno style
# unless I asked for it" -- weighting genital visibility that hard gave the
# model an incentive to pick a wide-open display pose to satisfy it, and the
# body came out slim/hourglass instead of his locked "thick, stout,
# broad-shouldered, sturdy solid build".
#
# REBUILT again same round -- Andrew: "the female 'after' is skinny and
# brunette and perfectly doll fake shit. we've been over this a thousand
# times. Review git for preference and model handling." He was right: the
# body wording above was my own paraphrase, never checked against what's
# already proven. C:\Users\andre\DropCat-Overnight\dropcat_overnight.py has a
# LIVE, verified-working body block for this exact taste (git log:
# "positive-only body control") -- adopted its wording and weight (1.4/1.35,
# noticeably stronger than my 1.3) near-verbatim instead of re-deriving it.
# "brunette" -- see _FEMALE_APPEARANCE / _pick_appearance above; this preset
# had no variety mechanism at all before this round.
#
# CAUGHT re-testing this rewrite (3 fresh seeds): 2 of 3 landed back on the
# exact wide-open spread-leg pose Andrew's already told me he hates -- INCL.
# on a scene that has no business producing it ("sitting on a park bench
# reading a book" came out legs spread flat-open to camera, book in hand).
# The anti-pinup NEGATIVES already here didn't stop it -- consistent with
# tonight's own repeated lesson that negatives are weak, not a new problem.
# Two changes: dropped the vulva/labia weight (1.15 -> unweighted) now that
# the body block above is weighted far more heavily (1.4) and shouldn't need
# genital-visibility weighting to compete for attention, and added a POSITIVE
# candid/unposed cue to _ANATOMY_POSITIVE (matching the proven positive-only-
# control principle, same fix class as everything else tonight) instead of
# leaning on negation again.
#
# NEITHER of those actually fixed it (retested, still spread-leg). Isolated
# with a binary search (raw Forge calls, same seed each time): removing the
# vulva/labia phrase entirely changed nothing, and a completely different
# fresh seed produced the SAME pose family -- so it wasn't the genital
# wording or an unlucky seed. Swapping the NEW body block back onto the OLD
# (previously clean) anatomy block reproduced the spread pose too, which
# isolates it to the body block itself: "thick heavy legs ... no waist
# taper" at weight 1.4 seems to bias the model toward a wide/spread stance as
# the most legible way to visually display "thick heavy legs" against camera
# -- a real tension between the two things Andrew wants (visibly thick body
# vs. never a revealing spread pose). A direct "legs together, closed
# natural pose, modest seated posture" counter-cue did NOT fix it and
# produced a bizarre flame-emoji artifact over the crotch (guessing: "modest"
# has a censorship association in this checkpoint's training data -- dropped
# that word). Next attempt used "not deliberately spread open" as a POSITIVE
# counter-cue -- still failed, and re-reading it found the bug: it contains
# the word "spread", the exact same self-defeating pattern as the clothing-
# negative backfire earlier tonight (naming the concept, even to negate it,
# cues it back in). Stripped every mention of "spread" from anything
# positive. Still failed on retest -- at that point isolated it properly:
# dropped the body weight in steps (1.4/1.35 -> 1.2/1.2, wording unchanged)
# and retested both a standing scene (716144, "dancing in rain") and a
# sitting scene (900002, "park bench") side by side. RESULT: 1.2 fixes
# standing/walking scenes cleanly -- natural pose, still visibly thick/stout.
# SITTING SCENES STILL COLLAPSE TO THE SAME WIDE-OPEN POSE EVEN AT 1.2,
# no exceptions found in 2 direct attempts at that weight. Conclusion: this
# checkpoint (rating_explicit + full nudity + a seated pose) appears to have
# an intrinsic bias toward maximally-revealing seated compositions that
# doesn't respond to body-weight tuning or the counter-cues tried tonight --
# a DIFFERENT problem from the "thick legs force a wide stance" theory this
# comment started with. Landed on 1.2 as the best available compromise:
# fixes the common standing/dynamic case without giving up thickness, but
# **a sitting/kneeling nude scene request is a known open risk for reverting
# to a pinup-style pose** -- don't claim this is solved for that case, flag
# it if Andrew hits it. Next lever to try, untried tonight: a stronger
# anti-spread negative scoped ONLY when the resolved pose looks seated (not
# currently detectable pre-render), or accepting a lower body-weight
# specifically for seated scenes at some cost to thickness.
#
# CORRECTION after the fix above still failed through the REAL app (not just
# the isolated raw-Forge calls used to test it): the two positive counter-cue
# phrases this whole investigation added to _ANATOMY_POSITIVE ("candid
# natural unposed moment...", "knees together, closed natural stance...")
# were STILL in the live prompt when re-testing at weight 1.2, and my
# isolated raw-Forge tests had never included them -- an apples-to-oranges
# comparison. Removed both from _ANATOMY_POSITIVE (see that constant's own
# comment) rather than trust an isolated test that didn't match production.
# Re-verified through the real /api/image-studio/generate endpoint on TWO
# standing scenes with weight 1.2 and NEITHER dead phrase present: clean,
# natural poses both times, thick/stout body intact. That's the config that
# actually shipped -- the sitting-scene limitation above still stands
# (not re-tested after this last change, no reason to expect it's different).
# "the gap between her thighs just below her vagina is extremely unrealistic"
# (Andrew, next round) -- a distinct defect from anything above: the base
# generation leaves an incoherent negative-space gap at the inner-thigh/
# perineum junction on some standing frontal poses. Added a concrete
# coherence cue right next to the vulva/labia wording (same "put the fix
# adjacent to what it's fixing" principle as the rest of this file) instead
# of a vague "correct anatomy" catch-all.
# 4th round on "too perfect/doll" + first time on "only barely stout, need
# another 50 lbs of thickness" -- same lesson as breast size and penis size
# earlier this session: wording alone plateaus, a dedicated slider LoRA is
# the actual lever. `Body_weight_slider_ILXL.safetensors` (Illustrious-
# native, matches this checkpoint family) and `ILL_mature_female.safetensors`
# were both sitting unused in the Lora folder, same situation Flat_Chest_
# Helper and Girthy were in before this session started using them. Verified
# live: visibly heavier build (belly, hips, thighs), less airbrushed/doll
# face, before touching any wording. Concrete body terms ("overweight, big
# belly") added alongside the LoRA per the "concrete beats vague" principle
# -- LoRA and wording reinforcing the same thing tested better than either
# alone. Also added an explicit doll-face negative (never had one before,
# despite "too perfect" being raised 3 times already).
#
# CAUGHT immediately on first real test: Body_weight_slider_ILXL adds mass
# everywhere, INCLUDING bust size, which correlates with weight in this
# checkpoint's training data -- pulled bust size back up despite Flat_Chest_
# Helper still being active at its old 0.6 weight, re-triggering the exact
# "big fake tits...disgusting" complaint from earlier this session (a
# standing ethics line, not just taste -- see user_ai_art_style memory).
# Fixed by raising Flat_Chest_Helper to 1.0 (it needs to work harder now
# that something else is actively pulling the other way) and adding "flat
# chest" + a weighted large-breast negative alongside the existing small-
# breast wording. Verified live: thick/heavy build held, bust back down to
# modest/small.
#
# THEN: "that's too fat, I want strength more than pot belly" -- I'd pushed
# Body_weight_slider too hard (0.9) and added literal "overweight, big
# belly" wording, which is a different thing than his original "thick,
# stout, sturdy solid build" ask -- overcorrected fat instead of strong.
# Fixed by dropping Body_weight_slider to 0.5, removing the belly/overweight
# wording entirely, and adding `ILL_muscular_female` (0.3 -- 0.5 produced a
# bizarre black tattoo-like artifact across the torso, an unrelated LoRA
# side effect, dropped to 0.3 and it cleared) plus concrete strength wording
# ("powerful, strongwoman, powerlifter physique"). Also added an explicit
# fat/belly negative alongside the strength positive, matching the paired-
# assertion pattern proven elsewhere in this file. Verified live (one
# render): strong, thick, powerful build with no belly, bust still small,
# no artifact.
#
# NOT actually reliable at 0.3 -- a broader batch surfaced a DIFFERENT
# artifact (a white/red graphic/text-like mark on the chest, same general
# region as the earlier black-tattoo one, clearly the same LoRA misfiring in
# a different visual form rather than a one-off fluke). Dropped to 0.2 and
# added more explicit anti-artifact negative terms (logo/symbol/writing on
# top of the existing tattoo/body-paint ones). Leaning more on
# Body_weight_slider + the wording for strength since the muscular LoRA is
# the less trustworthy piece here -- if this artifact recurs even at 0.2,
# the next step is dropping it entirely rather than chasing a lower weight
# further.
# "why is there a gap between her legs" (asked again, this round) -- the
# earlier "smooth continuous inner thighs meeting naturally" clause clearly
# isn't landing across a full batch, not just one bad seed. Escalated to
# much more literal, concrete wording (thighs TOUCHING, PRESSED together,
# closed at the top) instead of the vaguer "meeting naturally" -- per
# reference_forge_prompt_best_practices, concrete beats evocative, and this
# was still evocative.
#
# "why is she 'perfect'" -- 5th round on this exact complaint. Suspected a
# specific, concrete culprit this time rather than pushing the same
# adjectives harder: every rejected render has glossy, wet-looking, highly
# reflective skin, which reads as posed/polished regardless of the
# freckles/asymmetry wording already present. Added a matte-skin clause +
# matching negative -- same paired-assertion pattern proven for gender/
# thickness/everything else in this file.
#
# "too hourglass, need more brickhouse build" (square 3 from the last batch)
# -- added his own word ("brickhouse") directly per the "use his own
# vocabulary" principle, plus "rectangular torso, no waist curve" alongside
# the existing "no waist taper" (which apparently wasn't concrete enough
# alone to beat whatever the muscular-female LoRA's own athletic/hourglass
# training bias contributes).
# ILL_muscular_female DROPPED same round -- three separate artifact
# incidents at three different weights (0.5 black tattoo-like mark, 0.3
# white/red graphic mark, 0.2 recurred again) is a pattern, not bad luck.
# Leaning on Body_weight_slider + concrete wording instead -- one less
# unreliable LoRA in the stack. First retest at 0.6 fixed the duplicate-
# figure/artifact/clothing bugs (see the thigh-gap note above) but came out
# too toned/slim. Tried bumping to 0.75 for more bulk -- SAME class of
# artifact came back (this time orange, plus phantom clothing sleeves), so
# 0.75 crosses back into the LoRA's unstable range same as the muscular one
# did. 0.6 is the clean ceiling for this LoRA specifically -- reverted to it
# and pushed the wording weight instead (1.35 -> 1.4, the safe ceiling per
# reference_forge_prompt_best_practices) to make up the remaining
# "brickhouse" gap without more LoRA risk.
#
# CAUGHT next round: even at 0.6, the artifact recurred on a fresh seed --
# 0.6 wasn't actually a clean ceiling, just a seed that happened to test
# clean twice. Andrew confirmed all three problems at once (artifact, body
# still not thick enough, face still too perfect) rather than one at a
# time, so treated it as one investigation instead of three separate
# patches. New theory: FOUR simultaneous LoRAs (Flat_Chest_Helper +
# Body_weight_slider + ILL_mature_female + the style LoRA) is a lot of
# stacking, a known SDXL risk factor for interference artifacts on its own,
# independent of any single LoRA's weight. Dropped ILL_mature_female
# entirely (its own contribution was never actually isolated/proven -- "too
# perfect" persisted the whole time it was active) -- verified live,
# clean, and the body read thicker too without it competing for influence.
# Added more concrete thickness nouns ("wide hips, thick midsection,
# heavyset frame, big frame, sturdy trunk") to cover the gap. The "too
# perfect" fix itself lives in _STYLE_LEAD now (see that constant's comment)
# -- reasoned structurally this round instead of pushing more adjectives.
def _female_positive(seed, variant: int = 0) -> str:
    return _join(
        "<lora:Flat_Chest_Helper_V1:1.0>",
        "<lora:Body_weight_slider_ILXL:0.6>",
        "(wide broad shoulders, thick sturdy torso, strong heavy-set powerful build, big-boned, "
        "thick stout stocky body, thick straight waist, blocky solid midsection, "
        "rectangular torso, no waist curve, brickhouse build, no waist taper, wide hips, "
        "thick midsection, heavyset frame, big frame, sturdy trunk:1.4)",
        "(a real thick stout powerful woman, thick strong muscular arms, thick heavy powerful "
        "thighs, solid strong build, powerlifter physique, strongwoman build:1.3)",
        "(small natural breasts, modest bust, small chest, flat chest:1.4)",
        _pick_appearance(seed, variant, _FEMALE_APPEARANCE),
        # CAUGHT same round: "inner thighs touching and pressed together"
        # produced a mirrored TWIN figure on 3 of 4 test renders, reproduced
        # on a plain non-symmetric scene ("standing in an open field") so it
        # wasn't the scene composition -- "pressed together" is strongly
        # couple/embrace-coded in this checkpoint's training data, and
        # without an anchor to a single body it read as two people pressed
        # together rather than one figure's own thighs. Reworded to
        # explicitly anchor to "her own thighs" (single-body framing) and
        # dropped "pressed together" entirely. Also added an explicit solo
        # cue + duplicate-figure negative, which this path never had (only
        # creature mode had one, via _CREATURE_SOLO_POSITIVE) -- a latent
        # gap this surfaced.
        "(visible vulva, labia, both of her own inner thighs touching, "
        "no space between her thighs, thighs closed at the top:1.3)",
        # Weighted 2026-07-31 investigating the phantom-male-partner bug (see
        # _FEMALE_NEGATIVE's own comment on removing "penis, male anatomy" --
        # this positive is the replacement half of that same fix). Was a bare
        # unweighted "solo, one woman alone, single figure" before; that
        # wasn't a strong enough signal on its own to hold the frame to one
        # person once the negative-prompt bug was pulling the other way.
        "(solo female, completely alone, unaccompanied, only one figure in the frame:1.3)",
        # "no shine, no gloss" corrected same round -- a negation inside a
        # positive prompt, exactly the rule this file's own reference memory
        # warns against (a "no X" phrase can cue X back in). Reworded as
        # pure positive assertion instead.
        "(matte dry skin, dull skin texture:1.2)",
        # Moved here from the universal _ANATOMY_POSITIVE 2026-07-30 -- "hairless"
        # was fighting the creature-male fur wording since that block applies to
        # every render. This is where it originally belonged (a female-only
        # pubic-hair fix); female is unaffected by the move.
        "(smooth hairless clean skin:1.2)",
        # "why are the women covered in red ink" (Andrew) -- re-broke my own
        # documented rule. When the artifact first showed up I added
        # "tattoo, body paint, marking on skin, logo, symbol, text, writing,
        # graphic on skin, harness, straps on skin, armor" to the NEGATIVE,
        # thinking it would suppress the artifact -- instead it's the EXACT
        # clothing-negation backfire already diagnosed once tonight (naming a
        # visual concept, even to negate it, can cue it back in), just
        # re-triggered on a new class of content. Proof: I put "armor" in
        # the negative and the next render had a black-and-white armor-like
        # costume graphic on the torso. REMOVED that whole negative block.
        # This positive is the replacement -- assert plain/unmarked skin
        # directly instead of negating graphics.
        "(plain unmarked skin, solid even skin tone, uniform skin surface:1.2)",
    )


_FEMALE_NEGATIVE = (
    # "penis, male anatomy" REMOVED 2026-07-31 -- investigating the
    # afternoon handoff's flagged bug (a solo `subject=female` "farmers
    # market" render, seed 716201, produced an implied male sex partner in
    # frame despite this exact negative). Reproduced live through the real
    # API on that same seed -- confirmed present, visible penis + a second
    # figure's body at the frame edge. A follow-up batch on nearby seeds
    # (716202-716209) surfaced the smoking gun: seed 716204 rendered literal
    # garbled TEXT reading "PEN..." painted on her thigh, the same
    # graphic/text-hallucination failure mode already proven twice this
    # session (the clothing-negation and armor/tattoo-negation incidents,
    # see reference_forge_prompt_best_practices rule 1 and the artifact-saga
    # comment above _female_positive) -- this is that exact bug class a 4th
    # time, just landing on the one negative term that looked like the most
    # obviously-safe, purely-anatomical exclusion. Naming "penis" here is
    # what was cueing male anatomy content back into a solo female render,
    # not preventing it. Fix: deleted the term, and strengthened the
    # existing solo/single-figure POSITIVE assertion in _female_positive
    # instead (paired-assertion pattern already proven elsewhere in this
    # file). VERIFIED LIVE post-fix through the real API, same 5 seeds
    # re-rendered (716201, 716204, 716202, 716205, 716209): both previously-
    # bad seeds are now clean -- 716201 no longer shows a partner/penis,
    # 716204 no longer shows the "PEN..." text artifact. The underlying
    # seated/spread-leg pose bug this composition is prone to (documented
    # separately, still open) still recurs on 716201/716204 -- this fix
    # stopped the anatomy-summon riding on top of that pose, not the pose
    # itself. Two DIFFERENT, unrelated, pre-existing artifacts also turned up
    # in this same batch (716202: an orange/white geometric object over the
    # chest/pubic area; 716209: a black fabric drape over the shoulders) --
    # these are the already-documented graphic-artifact and nudity-
    # improvisation issues elsewhere in this file's history, NOT caused by
    # this fix and not claimed as solved by it.
    "(large breasts, huge breasts, fake breasts, implants, busty, big boobs, DD cup:1.45), "
    # "idealized flawless body, doll face, symmetrical perfect face, plastic
    # doll" REMOVED 2026-07-31 -- same bug as _SAFETY_FLOOR_NEGATIVE above,
    # same round of investigation: this is the file's OWN documented rule
    # (see the artifact-saga comment above _female_positive, and
    # reference_forge_prompt_best_practices rule 1) being broken a third
    # time, just in a block nobody re-read while chasing "why is she
    # perfect" across 6 rounds of POSITIVE wording changes -- the negative
    # was fighting every one of those the whole time. Kept the body-shape
    # terms (hourglass/slim/petite etc, a separate, working fix for the
    # brickhouse-not-hourglass complaint); dropped only the face/flawless
    # terms.
    "(hourglass figure, curvy waist, wasp waist, cinched waist, skinny waist, slim, petite, "
    "narrow shoulders, thin arms, willowy, supermodel:1.4), "
    "(overweight, fat belly, big belly, pot belly, obese, flabby:1.3), "
    "(shiny skin, glossy skin, wet skin, oiled skin, reflective skin:1.2), "
    "(two women, twins, duplicate person, second figure, mirrored figure, "
    "multiple people, couple, another woman:1.3), "
    "model pose, pinup pose, seductive pose, hand on hip, "
    "(gynecological pose, legs spread wide open:1.3)"
)

# Genital ADetailer REMOVED entirely 2026-07-30 -- it was the load-bearing
# assumption behind the whole "give it its own ad_prompt" fix above, and it
# turned out to not be firing at all for most renders. Root-caused by reading
# Forge's own server log (_headless.log) for Andrew's exact rejected renders:
# the male one logged "nothing detected" for the genital YOLO-World pass, and
# reproducing it live confirmed the detector finds ZERO candidate boxes for
# "penis, testicles" even at ad_confidence dropped all the way to 0.05 on a
# plainly visible, unobstructed penis -- this is not a threshold-tuning
# problem, the stock yolov8x-worldv2.pt (COCO-pretrained open-vocab, not
# NSFW-finetuned -- confirmed no purpose-built genital-detection model exists
# in models/adetailer or the diffusers cache) has no real signal for these
# classes at any confidence. For the female case it fires more often (vulva
# has more visual salience than a skin-toned penis against skin), but that's
# what was producing the black-blob artifact -- a same-seed diagnostic with
# ADetailer removed entirely came out clean, confirming the blob was the
# inpaint pass failing, not a base-generation defect. Net effect: this pass
# was doing nothing for men and actively damaging women. Anatomy detail now
# has to come from the base generation (LoRA + weighted concrete wording in
# _MALE_POSITIVE/_FEMALE_POSITIVE above) since a refinement pass that doesn't
# reliably fire can't be the mechanism -- don't re-add a genital ADetailer
# unit without first confirming its detection rate on a real render batch.

# Direct self-descriptive nouns ONLY -- no pronouns (he/him/his/she/her), no
# kinship nouns (mother/father/brother/sister/son/daughter/husband/wife).
# Those were tried first and caused real misfires (caught 2026-07-30, red
# team, photographic proof): a solo-portrait prompt that merely mentions an
# off-screen relative ("her brother is a blacksmith") got misrouted to
# "multi" and reintroduced the exact futa bug this system exists to prevent,
# on a prompt that never asked for two people. A missed auto-detection just
# falls through to "unspecified" (no anchor) -- the user's own explicit
# Subject dropdown covers that case; a false "multi" actively breaks a
# working solo render, which is the worse failure direction.
_MALE_SINGULAR = {"man", "male", "guy", "boy", "masculine", "dude"}
_MALE_PLURAL = {"men", "guys", "boys"}
_FEMALE_SINGULAR = {"woman", "female", "girl", "feminine", "lady"}
_FEMALE_PLURAL = {"women", "girls"}
_COUNT_WORDS = {"two", "three", "four", "five", "both", "couple", "duo", "trio", "pair", "several", "multiple"}

# Forge Couple regional prompting -- proven 2026-07-11 M/F recipe: global
# line (First Line background) + one column per person, forces both to
# render distinctly instead of the checkpoint collapsing to one figure.
# Arg order verified 2026-07-30 directly against the installed extension
# source (lib_couple/ui.py's couple_ui return list) -- Forge extension APIs
# have broken silently on a version bump before (see forge_client history),
# so this was NOT taken on memory alone.
_COUPLE_ARGS_TEMPLATE = [True, False, "Basic", "\n", "Horizontal", "First Line", 0.75, [], "Off", False, False]


def detect_subject(prompt: str) -> str:
    """'male', 'female', 'multi' (M+F pair), 'multi_male' (2+ men),
    'multi_female' (2+ women), or 'unspecified' -- from keyword presence in
    the user's own prompt text, never guessed by an LLM intermediary."""
    words = set(re.findall(r"[a-z']+", (prompt or "").lower()))
    has_male = bool(words & (_MALE_SINGULAR | _MALE_PLURAL))
    has_female = bool(words & (_FEMALE_SINGULAR | _FEMALE_PLURAL))
    has_count = bool(words & _COUNT_WORDS)
    male_plural = bool(words & _MALE_PLURAL) or (bool(words & _MALE_SINGULAR) and has_count)
    female_plural = bool(words & _FEMALE_PLURAL) or (bool(words & _FEMALE_SINGULAR) and has_count)

    if has_male and has_female:
        return "multi"
    if male_plural:
        return "multi_male"
    if female_plural:
        return "multi_female"
    if has_male:
        return "male"
    if has_female:
        return "female"
    return "unspecified"


PRESETS = {
    "default": {
        "label": "Default",
        "description": "Whatever checkpoint is normally loaded (zavychromaxl) -- no wrap, no ADetailer.",
        "checkpoint": DEFAULT_CHECKPOINT,
        "nsfw": False,
    },
    "nsfw": {
        "label": "NSFW (Explicit)",
        "description": "perfection25D_illustrious + nemo_fumetti_eurocomic 2.5D recipe, subject-aware anatomy anchoring, face+hand ADetailer, regional mode for multi-subject scenes.",
        "checkpoint": NSFW_CHECKPOINT,
        "nsfw": True,
        "render_style": "realistic",
    },
    "nsfw_graphic_novel": {
        "label": "NSFW (Graphic Novel)",
        "description": "Same perfection25D_illustrious anatomy/gender/creature stack as NSFW (Explicit), but rendered as bold-ink, high-contrast graphic-novel/comic-panel art instead of the photoreal 2.5D look -- a distinct DropCat visual identity, not another photoreal AI image.",
        "checkpoint": NSFW_CHECKPOINT,
        "nsfw": True,
        "render_style": "graphic_novel",
    },
}

DEFAULT_PRESET_KEY = "default"
SUBJECT_CHOICES = ("auto", "male", "female", "multi", "multi_male", "multi_female")
_REGIONAL_SUBJECTS = ("multi", "multi_male", "multi_female")


def list_presets() -> list[dict]:
    return [
        {"id": key, "label": p["label"], "description": p["description"], "nsfw": p["nsfw"]}
        for key, p in PRESETS.items()
    ]


def get_preset(preset_key) -> dict:
    if not isinstance(preset_key, str):
        return PRESETS[DEFAULT_PRESET_KEY]
    return PRESETS.get(preset_key, PRESETS[DEFAULT_PRESET_KEY])


def clamp_prompt_text(text: str) -> str:
    return (text or "")[:MAX_PROMPT_CHARS]


def _join(*parts: str) -> str:
    return ", ".join(p.strip().strip(",").strip() for p in parts if p and p.strip())


# MAJOR FIND 2026-07-31, same "imperfect faces" investigation: this file only
# ever configures TWO ADetailer units (face, hand) -- but a raw Forge call
# with this exact payload showed a THIRD unit actually running every time,
# "ADetailer model 3rd = face_yolov8n.pt", visible in the response's own
# extra_generation_params. It has no ad_prompt/ad_negative_prompt of its own
# (this file never set one), so ADetailer falls back to the FULL assembled
# scene prompt/negative for its inpaint -- which means _FEMALE_NEGATIVE and
# _SAFETY_FLOOR_NEGATIVE's "doll face / symmetrical perfect face / flawless
# skin" wording (see those constants, fixed same round) was being repainted
# directly onto the face region at denoising 0.4 by a pass invisible from
# reading this file alone. Root cause: Forge's ADetailer extension persists
# its OWN UI-level state across API calls for units this file's payload
# doesn't explicitly address (max-units is a Forge-side setting, apparently
# left at 3 from earlier interactive testing in the Forge WebUI, not
# anything committed here) -- Gradio components with no supplied arg fall
# back to whatever the extension's last-used/default state is, not "off".
# CONFIRMED live: appending a 3rd ADetailer args entry with
# {"ad_model": "None"} to the payload makes "ADetailer model 3rd" disappear
# from extra_generation_params entirely -- that's the fix, applied to every
# ADetailer alwayson_scripts list in this file (solo + regional) so this
# payload is fully self-describing and doesn't depend on whatever state the
# Forge GUI happens to be in. This may be a meaningful part of why 6 rounds
# of wording escalation on _ANATOMY_POSITIVE/_face_adetailer_unit's own
# ad_prompt never fully cracked the doll-face complaint -- an untracked pass
# using the raw (bug-laden, pre-fix) negative prompt was repainting the face
# on top of it the whole time.
_AD_UNIT_3_DISABLE = {"ad_model": "None"}


# Dedicated ad_prompt adopted from the proven dropcat_overnight.py AD config
# (same checkpoint, same 2.5D style) -- this unit previously had none and
# fell back to the full busy scene prompt for the face crop, the exact
# failure mode this file's own comments elsewhere warn about.
#
# REWRITTEN 2026-07-31, investigating Andrew's still-unsolved "are you not
# capable of creating imperfect faces?" -- found this unit's own ad_prompt
# was asserting "symmetric natural features ... good proportions" and its
# ad_negative_prompt was negating "asymmetric" -- i.e. THIS PASS'S OWN
# dedicated prompt was pushing the exact opposite of the imperfection
# wording _ANATOMY_POSITIVE asserts at 1.4 in the base generation, on the one
# region (the face) Andrew's complaint is actually about. Whoever wrote this
# unit's ad_prompt was reasonably trying to avoid "deformed face" (a real
# ADetailer failure mode) but conflated "not deformed" with "symmetric" --
# they aren't the same thing, and this was working directly against the
# other fix. Rewritten to carry the same imperfection language as the base
# prompt into the face crop instead of contradicting it, and dropped
# "asymmetric" from the negative for the same reason.
#
# ALSO SWAPPED ad_model 2026-07-31: mediapipe_face_full logged "nothing
# detected" on essentially every render checked this whole 10+-hour session
# (verified again in this round's own before/after test, same seed, both
# still "nothing detected on 1st settings") -- this unit has never actually
# been repainting the face at all, so none of the wording escalation aimed
# at it (this round's or the prior 6) was ever reaching pixels through THIS
# unit. Meanwhile the same investigation that found the phantom 3rd unit
# (see _AD_UNIT_3_DISABLE above) proved face_yolov8n.pt reliably detects a
# face on this exact checkpoint/framing (it's what the phantom unit was
# running, and it fired every time it was observed). Swapped to the model
# that's actually proven to fire, carrying our own dedicated
# ad_prompt/ad_negative_prompt instead of the phantom unit's fallback-to-
# full-scene-prompt behavior. Denoising bumped 0.42 -> 0.5 (still well under
# the 0.55 already proven safe for the hand unit) -- the handoff doc's
# untried-lever note was "much higher denoising to let the face deviate
# structurally," but that only matters once the pass is confirmed firing;
# start with a modest bump and re-escalate later if this still isn't enough,
# rather than combine an untested model swap with an aggressive denoising
# jump in one move.
#
# TRIED 2026-07-31 afternoon, per the handoff's own next-lever note (face pass
# confirmed firing, so this was the logical next escalation): bumped 0.5 -> 0.65.
# Locked-seed before/after through the real API (555001, "a woman standing in
# a garden, full body"): the two renders are nearly indistinguishable -- same
# freckle pattern, same eyebrows, same nose/mouth, no visible structural
# deviation from the 0.5 version. Read plainly: this did NOT produce a
# noticeable improvement, unlike the earlier model-swap fix which did. Kept at
# 0.65 anyway (no regression either -- no new artifacts, hands/proportions
# still clean) since there's no reason to revert, but don't claim this moved
# the doll-face complaint forward. The denoising lever appears to be tapped
# out around here; the real next lever is the Illustrious-native faces/
# nationalities LoRA already scouted (see the handoff doc), which needs
# Andrew's taste sign-off before installing.
def _face_adetailer_unit(subject: str) -> dict:
    return {
        "ad_model": "face_yolov8n.pt",
        "ad_mask_k": 2 if subject == "multi" else 1,
        "ad_prompt": "a realistic human face, natural asymmetric features, imperfect skin texture, freckles, candid unretouched photo, clear sharp eyes, correct proportions",
        "ad_negative_prompt": "deformed face, wonky eyes, cross-eyed, glossy plastic skin, airbrushed, blurry, extra eyes",
        "ad_denoising_strength": 0.65,
        "ad_confidence": 0.3,
    }


# Hand ADetailer -- Andrew, same session: "her hands look like spaghetti
# noodles," correctly guessing no refinement pass touched them. He's right --
# there never was one; only "perfect hands" in the base wording. UNLIKE the
# genital detector this removed earlier tonight (open-vocab, no real training
# signal, 0% real detection rate), hand_yolov8n.pt is one of the standard
# Bingsu ADetailer closed-set models already cached locally
# (models--Bingsu--adetailer) -- an actually-trained hand detector, not a
# text-prompted guess, so it doesn't carry the same failure risk. Confirmed
# live via the Forge log ("0: 640x640 2 hands") that it actually fires on
# this checkpoint+style, not just assumed. Full config (denoising/confidence/
# padding/dedicated negative) adopted verbatim from the proven
# dropcat_overnight.py AD dict instead of the bare-bones version this file
# had -- same "own short ad_prompt" pattern already proven here for other
# units (a busy scene prompt fed into a tiny inpaint crop produces garbage).
def _hand_adetailer_unit(subject: str) -> dict:
    return {
        "ad_model": "hand_yolov8n.pt",
        "ad_mask_k": 4 if subject == "multi" else 2,
        "ad_prompt": "a perfect hand, exactly five fingers, correct hand anatomy, natural fingers, well-defined",
        "ad_negative_prompt": "extra fingers, missing fingers, fused fingers, sixth finger, mutated hand, deformed, blurry",
        "ad_denoising_strength": 0.55,
        "ad_confidence": 0.25,
        "ad_inpaint_only_masked": True,
        "ad_inpaint_only_masked_padding": 48,
    }


# Foot ADetailer -- TRIED and REMOVED same round. The proven
# dropcat_overnight.py recipe (same checkpoint, same 2.5D style/LoRA weight)
# runs a dedicated foot pass via yolov8x-worldv2.pt classes="foot", so it was
# added here too -- but verifying via the Forge log (same discipline the
# genital-detector investigation used, not assuming a match on
# checkpoint+style is enough) showed it firing 0/3 times across three
# different full-body scenes, and 0/1 more at ad_confidence dropped to 0.05
# (isolated raw-Forge test) -- i.e. it does NOT reliably fire on THIS file's
# 1024x1024 full-body framing, whatever resolution/composition difference
# makes it work in the overnight engine (portrait 896x1152, different scene
# style) doesn't hold here. Lesson: "proven in another repo" is a hypothesis
# to verify against THIS pipeline, not a guarantee -- same lesson as the
# genital detector, learned twice in one night. Feet now rely on the base
# generation wording alone (_ANATOMY_POSITIVE + _SAFETY_FLOOR_NEGATIVE)
# same as before this was tried. Don't re-add without confirming a real
# detection rate first.


def build_forge_payload(
    preset_key: str,
    prompt: str,
    negative_prompt: str,
    width: int,
    height: int,
    steps: int,
    cfg_scale: float,
    seed: int,
    subject: str = "auto",
    creature: bool = False,
) -> tuple[dict, str, bool]:
    """Build a Forge /sdapi/v1/txt2img payload for the given preset.
    Returns (payload, resolved_subject, creature_applied). resolved_subject
    is "n/a" for the default preset (subject anchoring only applies to nsfw).
    creature_applied is False whenever creature=True was requested but
    couldn't actually be honored (currently: any multi-subject render)."""
    preset = get_preset(preset_key)
    prompt = clamp_prompt_text(prompt)
    negative_prompt = clamp_prompt_text(negative_prompt)

    if not preset["nsfw"]:
        return {
            "prompt": prompt,
            "negative_prompt": negative_prompt,
            "width": width,
            "height": height,
            "steps": steps,
            "cfg_scale": cfg_scale,
            "seed": seed,
            "batch_size": 1,
            "sampler_name": "DPM++ 3M SDE",
            "override_settings": {"sd_model_checkpoint": preset["checkpoint"]},
            "override_settings_restore_afterwards": True,
        }, "n/a", False

    resolved = subject if subject in SUBJECT_CHOICES and subject != "auto" else detect_subject(prompt)

    # Creature style + Forge-Couple regional mode do NOT compose reliably --
    # tried live 2026-07-30, two different ways: (1) creature style on the
    # global line only -> the regional split collapsed to ONE plain
    # human-ish figure instead of two; (2) creature anchor added to both
    # columns too -> WORSE, a single figure with blended anatomy (breasts
    # AND a penis on one body). Rather than ship either failure mode, the
    # creature STYLE (art direction / global line) stays a no-op for
    # multi-subject renders until this is solved properly.
    #
    # Male was forced into creature content unconditionally for one round
    # ("the dudes should be creatures") -- REVERTED same session: "I made a
    # mistake in requiring this to be an animal. I want to avoid 'furry'
    # culture, so we can open it back up to humans." Back to respecting the
    # `creature` checkbox the same way female always has. _MALE_POSITIVE
    # (human) is the live path again; _creature_male_positive still exists
    # for whenever the checkbox is on.
    creature_applied = creature and resolved not in _REGIONAL_SUBJECTS
    # render_style is preset-driven (see PRESETS above), not a separate
    # function argument -- keeps this function's call signature (and every
    # existing caller: features/image_studio/routes.py, features/
    # admin_review/routes.py) unchanged. Creature style still wins over
    # graphic-novel style if both are somehow in play, same conservative
    # stance as the existing creature+regional-mode limitation above (never
    # verified live together, so don't silently combine).
    render_style = preset.get("render_style", "realistic")
    style_lead = (
        _CREATURE_STYLE_LEAD if creature_applied
        else _GRAPHIC_NOVEL_STYLE_LEAD if render_style == "graphic_novel"
        else _STYLE_LEAD
    )

    if resolved in _REGIONAL_SUBJECTS:
        # variant offsets (0, 1) so a mixed/two-woman pair gets two DIFFERENT
        # picks from _FEMALE_APPEARANCE instead of identical twins.
        male_col = _creature_male_positive(seed, 0) if creature_applied else _MALE_POSITIVE
        columns = {
            "multi": (male_col, _female_positive(seed, 0)),
            "multi_male": (male_col, _creature_male_positive(seed, 1) if creature_applied else _MALE_POSITIVE),
            "multi_female": (_female_positive(seed, 0), _female_positive(seed, 1)),
        }[resolved]
        # No solo/anti-duplicate cues here -- regional mode WANTS two figures.
        global_line = _join(style_lead, prompt, _ANATOMY_POSITIVE)
        final_prompt = "\n".join([global_line, columns[0], columns[1]])
        final_negative = _join(_SAFETY_FLOOR_NEGATIVE, negative_prompt)
        alwayson_scripts = {
            "ADetailer": {"args": [_face_adetailer_unit("multi"), _hand_adetailer_unit("multi"), _AD_UNIT_3_DISABLE]},
            "forge couple": {"args": list(_COUPLE_ARGS_TEMPLATE)},
        }
    else:
        male_positive = _creature_male_positive(seed) if creature_applied else _MALE_POSITIVE
        male_negative = _CREATURE_MALE_NEGATIVE if creature_applied else _MALE_NEGATIVE
        gender_positive = {
            "male": male_positive, "female": _female_positive(seed), "unspecified": "",
        }[resolved]
        gender_negative = {
            "male": male_negative, "female": _FEMALE_NEGATIVE, "unspecified": "",
        }[resolved]
        solo_positive = _CREATURE_SOLO_POSITIVE if creature_applied else ""
        creature_negative = _CREATURE_NEGATIVE if creature_applied else ""
        # Trimmed anatomy block only needed for creature-male -- see
        # _ANATOMY_POSITIVE_CREATURE's own comment, the full block is
        # human-skin-specific and fights fur. Human male uses the normal one.
        anatomy_positive = (
            _ANATOMY_POSITIVE_CREATURE if (resolved == "male" and creature_applied) else _ANATOMY_POSITIVE
        )
        final_prompt = _join(style_lead, prompt, gender_positive, solo_positive, anatomy_positive)
        final_negative = _join(_SAFETY_FLOOR_NEGATIVE, gender_negative, creature_negative, negative_prompt)
        alwayson_scripts = {
            "ADetailer": {"args": [_face_adetailer_unit(resolved), _hand_adetailer_unit(resolved), _AD_UNIT_3_DISABLE]},
        }

    return {
        "prompt": final_prompt,
        "negative_prompt": final_negative,
        "width": width,
        "height": height,
        "steps": steps,
        "cfg_scale": cfg_scale,
        "seed": seed,
        "batch_size": 1,
        "sampler_name": "DPM++ 3M SDE",
        "override_settings": {"sd_model_checkpoint": preset["checkpoint"]},
        # Left at Forge's default (True): a request-scoped override that
        # restores whatever checkpoint was loaded before this call. False was
        # tried as a speed optimization (avoids a double swap on consecutive
        # same-preset renders) but it left the NSFW checkpoint loaded on Forge
        # afterward -- and Chat Studio's separate, ungated
        # /api/chat-studio/generate-image endpoint has no checkpoint override
        # or safety judge of its own, so it silently inherited whatever Forge
        # last had loaded. Correctness over speed here.
        "override_settings_restore_afterwards": True,
        "alwayson_scripts": alwayson_scripts,
    }, resolved, creature_applied
