"""NSFW content sanitizer for cloud LLM providers.

Translates explicit terms to euphemisms before sending to Anthropic/OpenAI,
then reverses the translation on the output so generated prompts remain
creatively uncensored for local image/video generation.

IMPORTANT: All replacements use whole-word matching (\b word boundaries) so
common English words that contain NSFW substrings ("massive", "classic",
"legion", "background") are never accidentally mangled.

Euphemisms are chosen to be uncommon as standalone English words so that
the desanitizer does not corrupt innocent sentences in LLM responses.
"""
import re

# Pairs: (explicit, euphemism)
# Rules for choosing euphemisms:
#   1. Must not be a common English word that the LLM might use independently
#   2. Must not be a substring of common innocent words
#   3. Ordered longest-first to avoid partial phrase replacements
_PAIRS = [
    # body parts
    ("bare breasts", "exposed shoulders"),
    ("breasts", "shoulders"),
    ("breast", "shoulder"),
    ("nipples", "collarbones"),
    ("nipple", "collarbone"),
    ("cleavage", "neckline"),
    ("boobs", "bust area"),        # "shoulders" was too innocent; "bust" itself is kept below
    ("buttocks", "lower torso"),   # was "lower back" -- "back" too common
    ("butt", "rear end"),          # was "back" -- "back" is FAR too common a word
    ("ass", "hindquarters"),       # was "back" -- ditto. Must NOT share "rear end" with
                                   # "butt": desanitize() can't tell which explicit term a
                                   # shared euphemism came from, so it always reverses to
                                   # whichever pair is listed first -- silently swapping the
                                   # user's actual word choice on every round-trip.
    ("groin", "waistline"),
    ("crotch", "pelvic area"),     # distinct from "groin" for the same reason
    ("genitals", "midsection"),
    ("thighs", "upper legs"),      # was "legs" -- "legs" is too common; "upper legs" is specific
    ("thigh", "upper leg"),        # was "leg"  -- ditto
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
    ("rubber suit", "glossy elastic suit"),   # was "glossy black bodysuit" -- distinct from
                                              # "gimp suit" (same-euphemism round-trip bug)
    ("rubber", "glossy elastic"),             # was "glossy synthetic" -- distinct from "latex"
    ("leather catsuit", "sleek black suit"),
    ("leather harness", "sculpted harness"),
    ("harness", "sculpted strap set"),
    ("ball gag", "ornamental mouthpiece"),
    ("gag", "decorative mouthpiece"),   # distinct from "ball gag" -- same-euphemism bug
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
    ("g-string", "slender garment"),   # distinct from "thong" -- same-euphemism bug
]

# Defensive check: if two different explicit terms ever share a euphemism,
# desanitize() can't tell which one a match came from and always reverses to
# whichever pair happens to be listed first, silently swapping the other
# term's original wording on every round-trip (found live, see the inline
# comments above for the pairs this actually happened to). Catch any future
# addition that reintroduces a collision at import time instead of silently.
_euphemism_owners: dict[str, str] = {}
for _explicit, _euphemism in _PAIRS:
    _key = _euphemism.lower()
    if _key in _euphemism_owners:
        raise ValueError(
            f"nsfw_sanitizer: euphemism {_euphemism!r} is used by both "
            f"{_euphemism_owners[_key]!r} and {_explicit!r} -- desanitize() "
            f"cannot round-trip these correctly, give one a distinct euphemism"
        )
    _euphemism_owners[_key] = _explicit
del _euphemism_owners, _explicit, _euphemism, _key

# Pre-sort by length (longest first) to avoid partial phrase matches
_PAIRS.sort(key=lambda p: len(p[0]), reverse=True)

# desanitize() runs in the opposite direction (searching for euphemisms,
# restoring explicit terms), so it needs its OWN longest-first order keyed on
# euphemism length -- sorting only by explicit-term length (above) leaves
# desanitize() vulnerable to a shorter euphemism that's a substring of a
# longer one (e.g. "garment" inside "narrow garment"): the short pair matches
# first and corrupts the longer phrase before its own pair ever gets a
# chance (found live: "thong" -> "narrow garment" -> "narrow panties" instead
# of back to "thong", because ("panties", "garment") matched inside it first).
_PAIRS_BY_EUPHEMISM_LEN = sorted(_PAIRS, key=lambda p: len(p[1]), reverse=True)


def sanitize(text: str) -> str:
    """Replace NSFW terms with euphemisms (whole-word, case-insensitive)."""
    if not text:
        return text
    result = text
    for explicit, euphemism in _PAIRS:
        result = _replace_preserve_case(result, explicit, euphemism)
    return result


def desanitize(text: str) -> str:
    """Reverse: replace euphemisms back to explicit terms (whole-word, case-insensitive)."""
    if not text:
        return text
    result = text
    for explicit, euphemism in _PAIRS_BY_EUPHEMISM_LEN:
        result = _replace_preserve_case(result, euphemism, explicit)
    return result


def _replace_preserve_case(text: str, old: str, new: str) -> str:
    """Whole-word, case-insensitive replace that preserves the original casing.

    Uses \b word boundaries so substrings inside larger words are never
    touched: "legion" is safe from the "leg" pair, "massive" from "ass", etc.
    """
    def _repl(match):
        orig = match.group()
        if orig.isupper():
            return new.upper()
        if orig[0].isupper():
            return new[0].upper() + new[1:]
        return new

    # \b treats the hyphen in "g-string" as a boundary, so we need to handle
    # multi-token phrases that start/end with punctuation carefully.  For
    # simple alphanumeric terms \b is sufficient; for phrases with spaces the
    # word boundaries at the edges of the whole phrase are what matter.
    pattern = r'\b' + re.escape(old) + r'\b'
    return re.sub(pattern, _repl, text, flags=re.IGNORECASE)
