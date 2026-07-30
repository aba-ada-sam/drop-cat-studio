"""Minor-safety gate for NSFW image generation.

Ported from the proven booth implementation (booth_minor_verdict /
booth_render_blocked in booth_work/lib.php): a narrow LLM judge that blocks
ONLY on an affirmative under-18 signal, with ambiguity resolving to adult.
Fails CLOSED (blocks) if the judge call errors -- generation stops rather
than silently skipping the check.
"""
import asyncio
import logging

log = logging.getLogger(__name__)

JUDGE_SYSTEM_PROMPT = """You are a strict minor-safety classifier for an adult (18+) art tool. \
Decide whether the request depicts, or asks to depict, a MINOR (a person under 18) in a sexual, \
nude, or sexually suggestive context. Consider paraphrase, euphemism, coded language, and \
childlike-body descriptions even when no explicit age is stated. Everyone is assumed to be an \
ADULT unless there is a clear under-18 signal. Non-sexual content is always fine regardless of \
apparent age. Reply with ONLY one word: BLOCK or ALLOW."""


async def minor_safety_verdict(scene_text: str) -> str:
    """Returns 'block', 'allow', or 'error'."""
    from app import get_llm_router
    from core.llm_client import TIER_FAST

    text = (scene_text or "").strip()[:4000]
    if not text:
        return "allow"

    llm_router = get_llm_router()

    def _call():
        return llm_router.route(
            [{"role": "user", "content": text}],
            system=JUDGE_SYSTEM_PROMPT,
            tier=TIER_FAST,
            max_tokens=5,
        )

    try:
        raw = await asyncio.to_thread(_call)
    except Exception as e:
        log.warning("[minor_safety] judge call failed, failing closed: %s", e)
        return "error"

    # Fail closed on anything that isn't a clean, recognizable ALLOW -- a
    # garbled/truncated/off-topic reply (not an exception, just unexpected
    # text) must not fall through to "allow" by default.
    out = (raw or "").strip().lower()
    return "allow" if out.startswith("allow") else "block"


async def nsfw_render_blocked(scene_text: str) -> bool:
    """True unless the judge explicitly returns 'allow' -- fails closed on
    both 'block' and 'error'."""
    verdict = await minor_safety_verdict(scene_text)
    return verdict != "allow"
