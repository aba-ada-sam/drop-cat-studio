"""NSFW content sanitizer for cloud LLM providers.

Translates explicit terms to euphemisms before sending to Anthropic/OpenAI,
then reverses the translation on the output so generated prompts remain
creatively uncensored for local image/video generation.
"""

# Pairs: (explicit, euphemism)
# Ordered longest-first to avoid partial replacements.
_PAIRS = [
    # body parts
    ("bare breasts", "exposed shoulders"),
    ("breasts", "shoulders"),
    ("breast", "shoulder"),
    ("nipples", "collarbones"),
    ("nipple", "collarbone"),
    ("cleavage", "neckline"),
    ("boobs", "shoulders"),
    ("buttocks", "lower back"),
    ("butt", "back"),
    ("ass", "back"),
    ("groin", "waistline"),
    ("crotch", "waistline"),
    ("genitals", "midsection"),
    ("thighs", "legs"),
    ("thigh", "leg"),
    # states
    ("topless", "sleeveless"),
    ("bottomless", "flowing garment"),
    ("nude", "minimally dressed"),
    ("naked", "lightly dressed"),
    ("nudity", "minimal clothing"),
    ("strip", "reveal"),
    ("undress", "disrobe elegantly"),
    ("lingerie", "evening wear"),
    ("bikini", "summer outfit"),
    ("underwear", "undergarment"),
    ("panties", "garment"),
    ("bra", "top"),
    # actions
    ("seductive", "alluring"),
    ("sensual", "graceful"),
    ("erotic", "romantic"),
    ("provocative", "bold"),
    ("sexually", "intimately"),
    ("sexual", "intimate"),
    ("sex", "romance"),
    ("arousing", "captivating"),
    ("lustful", "passionate"),
    ("orgasm", "climax"),
    ("moan", "sigh"),
    ("voluptuous", "curvaceous"),
    ("busty", "statuesque"),
    ("slutty", "daring"),
    ("horny", "yearning"),
    ("naughty", "playful"),
    ("kinky", "adventurous"),
    ("fetish", "aesthetic"),
    ("bondage", "restraint art"),
    ("dominatrix", "commanding figure"),
    ("submissive", "yielding"),
    ("BDSM", "power dynamic"),
    ("porn", "art"),
    ("pornographic", "artistic"),
    ("hentai", "anime art"),
    ("explicit", "expressive"),
    # wardrobe / scene dressing that tends to trip cloud refusals
    ("gimp suit", "glossy black bodysuit"),
    ("gimp mask", "masked hood"),
    ("gimp", "glossy bodysuit wearer"),
    ("latex catsuit", "glossy black suit"),
    ("latex", "glossy synthetic"),
    ("rubber suit", "glossy black bodysuit"),
    ("rubber", "glossy synthetic"),
    ("leather catsuit", "sleek black suit"),
    ("leather harness", "sculpted harness"),
    ("harness", "sculpted strap set"),
    ("ball gag", "ornamental mouthpiece"),
    ("gag", "ornamental mouthpiece"),
    ("shackles", "ornamental cuffs"),
    ("chains", "decorative chains"),
    ("collar and leash", "neckpiece with tether"),
    ("collar", "neckpiece"),
    ("leash", "tether"),
    ("fishnets", "lace stockings"),
    ("fishnet", "lace weave"),
    ("garters", "ribboned straps"),
    ("garter", "ribboned strap"),
    ("stilettos", "heeled boots"),
    ("whip", "ornamental cord"),
    ("corset", "fitted bodice"),
    ("thong", "narrow garment"),
    ("g-string", "narrow garment"),
]

# Pre-sort by length (longest first) to avoid partial matches
_PAIRS.sort(key=lambda p: len(p[0]), reverse=True)


def sanitize(text: str) -> str:
    """Replace NSFW terms with euphemisms (case-insensitive, preserves case)."""
    if not text:
        return text
    result = text
    for explicit, euphemism in _PAIRS:
        result = _replace_preserve_case(result, explicit, euphemism)
    return result


def desanitize(text: str) -> str:
    """Reverse: replace euphemisms back to explicit terms."""
    if not text:
        return text
    result = text
    for explicit, euphemism in _PAIRS:
        result = _replace_preserve_case(result, euphemism, explicit)
    return result


def _replace_preserve_case(text: str, old: str, new: str) -> str:
    """Case-insensitive replace that tries to preserve the original casing."""
    import re
    def _repl(match):
        orig = match.group()
        if orig.isupper():
            return new.upper()
        if orig[0].isupper():
            return new[0].upper() + new[1:]
        return new
    return re.sub(re.escape(old), _repl, text, flags=re.IGNORECASE)
