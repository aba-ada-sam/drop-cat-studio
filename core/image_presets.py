"""Forge image-generation presets for the Image tab (features/image_studio).

The NSFW preset wraps -- never replaces -- whatever prompt the user typed. The
user's own text is always the subject; the wrap only adds a checkpoint choice,
a short style/safety lead-in, subject-aware anatomy anchoring, and ADetailer
passes. See reference_forge_prompt_best_practices: short and concrete beats
long and evocative, so keep every wrap lean.
"""
import re

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
_STYLE_LEAD = (
    "<lora:nemo_fumetti_eurocomic:0.6>, rating_explicit, "
    "(2.5D semi-realistic illustration:1.15)"
)

# Anthropomorphic creature style -- Andrew's core DropCat brand (colorful
# fantasy creatures, never real people) applied to the NSFW anatomy-accurate
# pipeline. Fumetti weight lowered (0.6 -> 0.4) to leave headroom for the
# creature LoRA; verified live 2026-07-30 this combination doesn't muddy
# either style. "solo, one single ... alone" + the phantom-figure negative
# block below are NOT decorative -- without them this LoRA/checkpoint
# combination spontaneously added an uninvited second (human) figure to a
# solo-creature prompt in testing; this is what suppressed it.
_CREATURE_STYLE_LEAD = (
    "<lora:nemo_fumetti_eurocomic:0.4>, <lora:Fantasy_Creatures:0.6>, rating_explicit, "
    "(2.5D semi-realistic illustration:1.15), anthropomorphic creature, fur and claws"
)
_CREATURE_SOLO_POSITIVE = "solo, one single character alone"
_CREATURE_NEGATIVE = (
    "human face, realistic human skin, second person, another figure, "
    "two figures, duplicate character, extra person, multiple characters"
)

# "average looking, natural imperfect skin" added 2026-07-30 -- Andrew:
# "somehow we need regular ugly normal imperfect people," said after my
# vague "distinctive natural face" attempt (on the woman only) did nothing.
# Applies to BOTH genders now, universally, not just women -- concrete this
# time (skin texture, not "distinctive"), per his own prompting rules.
_ANATOMY_POSITIVE = "perfect hands, normal feet, average looking, natural imperfect skin"
_SAFETY_FLOOR_NEGATIVE = (
    "anime, chibi, cel shading, waifu, petite, loli, child, teen, teenager, "
    "young girl, youthful, underage, malformed genitalia, deformed penis, extra penis, "
    "extra limbs, fused limbs, deformed, mutated hands, extra fingers"
)

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
# just assert a state).
_MALE_POSITIVE = (
    "a man, male body, flat chest, broad heavy-set build, "
    "erect penis, defined glans, testicles"
)
_MALE_NEGATIVE = "breasts, feminine body, bodybuilder, six pack abs"

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
_FEMALE_POSITIVE = (
    "<lora:Flat_Chest_Helper_V1:0.6>, a woman, female body, visible vulva, "
    "thick stout broad-shouldered build, small natural breasts"
)
_FEMALE_NEGATIVE = "penis, male anatomy, large breasts, huge breasts, fake breasts, implants"

# Genital ADetailer gets its OWN short prompt instead of inheriting the full
# scene prompt -- caught live 2026-07-30: a busy scene prompt (style LoRA +
# setting + body-taste text) fed into a tiny genital-region inpaint crop
# produced a garbled white-blob artifact directly over the anatomy, not a
# clean render. A focused prompt for just that crop is the documented fix
# (same principle as booth's ADetailer-tab-specific prompting). "hairless"
# moved here too -- it never took effect in the base render (confirmed by
# a same-seed diagnostic without this pass), a focused inpaint prompt is a
# more plausible place for it to actually land. Glans/corona named explicitly
# here too, for the same reason as _MALE_POSITIVE above.
_GENITAL_AD_PROMPT = {
    "male": "erect penis, defined glans, corona, testicles, detailed male anatomy",
    "female": "vulva, smooth hairless, detailed female anatomy",
    "multi": "vulva, penis, defined glans, testicles, detailed anatomy",
    "multi_male": "erect penis, defined glans, corona, testicles, detailed male anatomy",
    "multi_female": "vulva, smooth hairless, detailed female anatomy",
    "unspecified": "detailed anatomy",
}

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

# Genital ADetailer -- open-vocab YOLO-World detector, cached locally
# (models--Bingsu--yolo-world-mirror). Harmless when nothing of the named
# class is present in frame (ADetailer just no-ops), so it's safe to run
# unconditionally on every NSFW render rather than gate it on subject.
_GENITAL_CLASSES = {
    "male": "penis, testicles",
    "female": "vulva",
    "multi": "penis, testicles, vulva",
    "multi_male": "penis, testicles",
    "multi_female": "vulva",
    "unspecified": "penis, testicles, vulva",
}

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
        "description": "perfection25D_illustrious + nemo_fumetti_eurocomic 2.5D recipe, subject-aware anatomy anchoring, face+genital ADetailer, regional mode for multi-subject scenes.",
        "checkpoint": NSFW_CHECKPOINT,
        "nsfw": True,
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


def _face_adetailer_unit(subject: str) -> dict:
    return {"ad_model": "mediapipe_face_full", "ad_mask_k": 2 if subject == "multi" else 1}


def _genital_adetailer_unit(subject: str) -> dict:
    return {
        "ad_model": "yolov8x-worldv2.pt",
        "ad_model_classes": _GENITAL_CLASSES[subject],
        "ad_mask_k": 2 if subject == "multi" else 1,
        "ad_prompt": _GENITAL_AD_PROMPT[subject],
    }


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
    # creature flag is a no-op for multi-subject renders until this is
    # solved properly -- creature_applied in the return value reports
    # whether it actually took effect, so callers aren't silently misled.
    creature_applied = creature and resolved not in _REGIONAL_SUBJECTS
    style_lead = _CREATURE_STYLE_LEAD if creature_applied else _STYLE_LEAD

    if resolved in _REGIONAL_SUBJECTS:
        columns = {
            "multi": (_MALE_POSITIVE, _FEMALE_POSITIVE),
            "multi_male": (_MALE_POSITIVE, _MALE_POSITIVE),
            "multi_female": (_FEMALE_POSITIVE, _FEMALE_POSITIVE),
        }[resolved]
        # No solo/anti-duplicate cues here -- regional mode WANTS two figures.
        global_line = _join(style_lead, prompt, _ANATOMY_POSITIVE)
        final_prompt = "\n".join([global_line, columns[0], columns[1]])
        final_negative = _join(_SAFETY_FLOOR_NEGATIVE, negative_prompt)
        alwayson_scripts = {
            "ADetailer": {"args": [_face_adetailer_unit("multi"), _genital_adetailer_unit(resolved)]},
            "forge couple": {"args": list(_COUPLE_ARGS_TEMPLATE)},
        }
    else:
        gender_positive = {"male": _MALE_POSITIVE, "female": _FEMALE_POSITIVE, "unspecified": ""}[resolved]
        gender_negative = {"male": _MALE_NEGATIVE, "female": _FEMALE_NEGATIVE, "unspecified": ""}[resolved]
        solo_positive = _CREATURE_SOLO_POSITIVE if creature_applied else ""
        creature_negative = _CREATURE_NEGATIVE if creature_applied else ""
        final_prompt = _join(style_lead, prompt, gender_positive, solo_positive, _ANATOMY_POSITIVE)
        final_negative = _join(_SAFETY_FLOOR_NEGATIVE, gender_negative, creature_negative, negative_prompt)
        alwayson_scripts = {
            "ADetailer": {"args": [_face_adetailer_unit(resolved), _genital_adetailer_unit(resolved)]},
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
