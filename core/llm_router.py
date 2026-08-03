"""LLM router -- dispatches AI calls to Anthropic, OpenAI, or an uncensored backend.

Provider is read from config at call time so it can be hot-switched in Settings.
The "uncensored" role (analyses NSFW images, never refuses) is served by
Featherless (cloud, default) or KoboldCpp (local) -- see core/llm_client.py.
Ollama was removed 2026-07-05; a legacy "ollama" provider value is aliased to
"featherless" so old configs keep working.
"""
import logging
import time

from core import config as cfg
from core.llm_client import TIER_FAST, TIER_BALANCED, TIER_POWER, _is_oom  # re-exported for features

log = logging.getLogger(__name__)

# Providers that run uncensored and do their OWN vision (send images directly,
# never refuse NSFW). Feature code imports this instead of hardcoding a name.
UNCENSORED_PROVIDERS = ("featherless", "kobold")


def is_uncensored(provider: str | None) -> bool:
    """True if `provider` is a local/cloud uncensored backend (vision-capable)."""
    return provider in UNCENSORED_PROVIDERS


# Legacy alias: Ollama was the old uncensored provider. Map it to Featherless.
_PROVIDER_ALIASES = {"ollama": "featherless"}

# Default model selections per provider (overridable via config ai_model_fast/balanced/power)
_ANTHROPIC_MODELS = {
    TIER_FAST:     "claude-haiku-4-5-20251001",
    TIER_BALANCED: "claude-sonnet-4-6",
    TIER_POWER:    "claude-sonnet-4-6",
}

_OPENAI_MODELS = {
    TIER_FAST:     "gpt-4o-mini",
    TIER_BALANCED: "gpt-4o",
    TIER_POWER:    "gpt-4o",
}


# Phrases that signal a cloud API (vision OR text) refused a request for
# content-policy reasons. When we see any of these in a response (and the
# response is short prose rather than the structured JSON we asked for), we
# transparently retry against the uncensored backend (Featherless/kobold).
_SAFETY_REFUSAL_MARKERS = (
    "contains nudity",
    "i can't process",
    "i cannot process",
    "i'm not able to process",
    "i am not able to process",
    "i can't help with",
    "i cannot help with",
    "i'm unable to",
    "i am unable to",
    "against my guidelines",
    "violates my guidelines",
    "anthropic's usage policies",
    "anthropic's policies",
    "openai's usage policies",
    "openai's policies",
    "content policy",
    "content policies",
    "safety guidelines",
    "explicit content",
    "sexually explicit",
    "i won't be able to describe",
    "i won't describe",
    "i cannot describe",
    # Contractions. The list above only had the expanded forms, so a real refusal
    # ("I can't describe this image as it contains explicit sexual content")
    # matched nothing: "explicit content" is not a substring of "explicit sexual
    # content", and the model says "can't", not "cannot". The refusal then flowed
    # through to the caller, which discarded it -- and every clip in that job
    # rendered with no subject anchor.
    "i can't describe",
    "i can't provide",
    "i cannot provide",
    "explicit sexual content",
    # Text-generation refusals (lyrics/brainstorm/prompt writing). The markers
    # above were all written for the vision-refusal case ("I can't describe/
    # process this image") and none of them match how Claude phrases a refusal
    # to WRITE something -- a real refusal ("I'm not able to write lyrics built
    # around that phrase... it promotes antisemitic conspiracy thinking") sailed
    # through as if it were valid output and got literally sung by ACE-Step.
    "i'm not able to write",
    "i am not able to write",
    "i'm not able to provide",
    "i am not able to provide",
    "i'm not going to help",
    "i am not going to help",
    "i won't write",
    "i will not write",
    "i can't write",
    "i cannot write",
    "i'm not comfortable",
    "i am not comfortable",
    "not something i can help with",
    "not something i'm able to help with",
    # "generate" is its own common refusal verb, distinct from write/process/
    # describe/provide above -- missed until a real refusal ("I can't generate
    # that content. I'd be happy to help with a different video concept...")
    # sailed through and got silently rehashed into an unrelated SFW idea.
    "i can't generate",
    "i cannot generate",
    "i'm not able to generate",
    "i am not able to generate",
)


def _looks_like_refusal_exception(exc: Exception) -> bool:
    """True if a raised exception is Anthropic/OpenAI declining the request
    outright (empty content / blocked stop_reason) rather than a real API
    error. _anthropic_chat/_anthropic_vision/_openai_chat/_openai_vision all
    raise ValueError with this exact phrase for that case -- see below. Unlike
    _looks_like_safety_refusal() (which reads a returned string), this reads
    a raised exception, because a hard content-policy block never reaches the
    "return the text" path at all."""
    return "possible content policy refusal" in str(exc).lower()


def _looks_like_safety_refusal(text: str) -> bool:
    """Heuristic: did a cloud vision call return a refusal instead of an answer?

    Refusals tend to be SHORT (under ~600 chars) and contain a stock phrase.
    Long responses that happen to mention 'content policy' in the body of a
    valid answer don't match.
    """
    if not text:
        return False
    if len(text) > 700:
        return False
    lowered = text.lower()
    return any(marker in lowered for marker in _SAFETY_REFUSAL_MARKERS)


class LLMRouter:
    """Routes AI calls to the configured provider with retry.

    Provider is read from config on each call so Settings changes apply immediately.

    Usage (thread-safe):
        router = LLMRouter(llm_client)
        text = router.route(messages, tier=TIER_BALANCED, max_tokens=512)
        text = router.route_vision(prompt, images_b64, tier=TIER_POWER)
    """

    def __init__(self, client):
        """client: LLMClient instance -- used for the uncensored providers
        (featherless / kobold)."""
        self._client = client
        self._stats = {"ok": 0, "errors": 0, "retries": 0}

    def _provider(self, force: str | None = None) -> str:
        """Resolve which LLM provider to use for this call.

        The uncensored provider (Featherless cloud, or KoboldCpp local) is opt-in:
        callers may pass force='featherless'/'kobold' (e.g. an NSFW-safe path), but
        in auto-mode it is only chosen when no cloud key is configured AND the user
        has explicitly enabled the fallback in Settings. Otherwise we surface a hard
        error so the user knows to configure a cloud key or enable the fallback.
        """
        force = _PROVIDER_ALIASES.get(force, force)
        if force in ("anthropic", "openai", "featherless", "kobold"):
            return force
        p = _PROVIDER_ALIASES.get(cfg.get("llm_provider"), cfg.get("llm_provider")) or "auto"
        if p != "auto":
            return p
        # Auto: cloud first, in preference order
        from core.keys import get_key
        if get_key("anthropic"):
            return "anthropic"
        if get_key("openai"):
            return "openai"
        # No Anthropic/OpenAI key. Fall back to the uncensored provider if the
        # user opted in. Default fallback target is Featherless (cloud).
        if cfg.get("allow_uncensored_fallback"):
            return cfg.get("uncensored_provider") or "featherless"
        raise RuntimeError(
            "No cloud LLM key configured and the uncensored fallback is disabled. "
            "Add an Anthropic or OpenAI key in Settings, or enable "
            "'Allow uncensored fallback' to use Featherless / local KoboldCpp."
        )

    def _resolve_refusal_fallback(self, provider: str) -> str | None:
        """`provider`'s response looked like a safety refusal -- return the
        uncensored fallback provider (Featherless by default) if it's up,
        else None so the caller just returns the original refusal text."""
        fb = cfg.get("uncensored_provider") or "featherless"
        try:
            fb_up = self._client.is_available(fb)
        except Exception:
            fb_up = False
        if fb_up:
            log.info("[router] %s refused -- retrying via %s (no content filter)", provider, fb)
            return fb
        log.info("[router] %s refused and %s is not available -- user will see the refusal text",
                 provider, fb)
        return None

    def route(
        self,
        messages: list,
        tier: str = TIER_BALANCED,
        max_tokens: int = 1024,
        est_tokens: int = 500,
        system: str = "",
        force_provider: str | None = None,
        skip_sanitize: bool = False,
    ) -> str:
        provider = self._provider(force_provider)
        if provider in ("anthropic", "openai"):
            chat_fn = self._anthropic_chat if provider == "anthropic" else self._openai_chat
            try:
                result = self._call_with_retry(
                    lambda: chat_fn(messages, tier, max_tokens, system, skip_sanitize=skip_sanitize),
                    provider=provider,
                )
            except Exception as e:
                # Hard content-policy block (empty response, no refusal text to read) --
                # same recovery as the "refused with prose" case below, just triggered
                # from the exception instead of the return value.
                if _looks_like_refusal_exception(e):
                    fb = self._resolve_refusal_fallback(provider)
                    if fb:
                        return self._client.chat(fb, messages, tier=tier, max_tokens=max_tokens, system=system)
                raise
            # Same cloud safety-refusal fallback as route_vision(), extended to
            # text-only calls -- a plain-English refusal (e.g. an NSFW idea/lyric
            # brainstorm) used to have nowhere to go on the text path even though
            # photo analysis already recovered transparently via Featherless.
            if _looks_like_safety_refusal(result):
                fb = self._resolve_refusal_fallback(provider)
                if fb:
                    try:
                        return self._client.chat(fb, messages, tier=tier, max_tokens=max_tokens, system=system)
                    except Exception as e:
                        log.warning("[router] %s fallback failed after %s refusal: %s", fb, provider, e)
                        # Return the original refusal so the caller's UI can show it
            return result
        # uncensored backend (featherless / kobold)
        return self._call_with_retry(
            lambda: self._client.chat(provider, messages, tier=tier, max_tokens=max_tokens, system=system),
            provider=provider,
        )

    def route_vision(
        self,
        prompt: str,
        images_b64: list[str],
        tier: str = TIER_BALANCED,
        max_tokens: int = 2048,
        est_tokens: int = 800,
        system: str = "",
        force_provider: str | None = None,
        format_json: bool = False,
    ) -> str:
        provider = self._provider(force_provider)
        if provider == "anthropic":
            vision_fn = lambda: self._anthropic_vision(prompt, images_b64, tier, max_tokens, system)
        elif provider == "openai":
            vision_fn = lambda: self._openai_vision(prompt, images_b64, tier, max_tokens, system)
        else:
            # uncensored backend (featherless / kobold) does its own vision
            return self._call_with_retry(
                lambda: self._client.chat_with_images(provider, prompt, images_b64, tier=tier,
                                                     max_tokens=max_tokens, system=system,
                                                     format_json=format_json),
                provider=provider,
            )
        try:
            result = self._call_with_retry(vision_fn, provider=provider)
        except Exception as e:
            # Hard content-policy block (empty response, no refusal text to read) --
            # same recovery as the "refused with prose" case below, just triggered
            # from the exception instead of the return value.
            if _looks_like_refusal_exception(e):
                fb = self._resolve_refusal_fallback(provider)
                if fb:
                    return self._client.chat_with_images(
                        fb, prompt, images_b64, tier=tier,
                        max_tokens=max_tokens, system=system, format_json=format_json,
                    )
            raise

        # Cloud safety-refusal fallback: Anthropic and OpenAI refuse images
        # they classify as containing nudity / sensitive content with a plain-
        # English refusal instead of a structured error. Detect that and
        # transparently retry the SAME prompt against the uncensored backend
        # (Featherless -- no content filter). This restores NSFW/artistic photo
        # analysis without making the uncensored provider the default for SFW
        # calls. Caller never needs to know which provider answered.
        if _looks_like_safety_refusal(result):
            fb = self._resolve_refusal_fallback(provider)
            if fb:
                try:
                    return self._client.chat_with_images(
                        fb, prompt, images_b64, tier=tier,
                        max_tokens=max_tokens, system=system, format_json=format_json,
                    )
                except Exception as e:
                    log.warning("[router] %s fallback failed after %s refusal: %s", fb, provider, e)
                    # Return the original refusal so the caller's UI can show it
        return result

    def stats(self) -> dict:
        return dict(self._stats)

    # -- Anthropic -------------------------------------------------------------

    def _anthropic_chat(self, messages, tier, max_tokens, system, skip_sanitize=False):
        import anthropic
        from core.keys import get_key
        from core.nsfw_sanitizer import sanitize, desanitize
        _san = (lambda t: t) if skip_sanitize else sanitize
        _desan = (lambda t: t) if skip_sanitize else desanitize
        client = anthropic.Anthropic(api_key=get_key("anthropic"))
        model = cfg.get(f"ai_model_{tier}") or _ANTHROPIC_MODELS.get(tier, _ANTHROPIC_MODELS[TIER_BALANCED])
        safe_msgs = [
            {**m, "content": _san(m["content"]) if isinstance(m.get("content"), str) else m.get("content")}
            for m in messages
        ]
        kwargs = dict(model=model, max_tokens=max_tokens, messages=safe_msgs)
        if system:
            kwargs["system"] = _san(system)
        resp = client.messages.create(**kwargs)
        if not resp.content:
            raise ValueError(f"Anthropic returned empty response (stop_reason={resp.stop_reason!r}) -- possible content policy refusal")
        return _desan(resp.content[0].text)

    def _anthropic_vision(self, prompt, images_b64, tier, max_tokens, system):
        import anthropic
        from core.keys import get_key
        from core.nsfw_sanitizer import sanitize, desanitize
        client = anthropic.Anthropic(api_key=get_key("anthropic"))
        model = cfg.get(f"ai_model_{tier}") or _ANTHROPIC_MODELS.get(tier, _ANTHROPIC_MODELS[TIER_BALANCED])
        content = []
        for img in images_b64[:5]:  # Anthropic allows up to 5 images per message
            content.append({
                "type": "image",
                "source": {"type": "base64", "media_type": "image/jpeg", "data": img},
            })
        content.append({"type": "text", "text": sanitize(prompt)})
        kwargs = dict(
            model=model,
            max_tokens=max_tokens,
            messages=[{"role": "user", "content": content}],
        )
        if system:
            kwargs["system"] = sanitize(system)
        resp = client.messages.create(**kwargs)
        if not resp.content:
            raise ValueError(f"Anthropic returned empty response (stop_reason={resp.stop_reason!r}) -- possible content policy refusal")
        return desanitize(resp.content[0].text)

    # -- OpenAI ----------------------------------------------------------------

    def _openai_chat(self, messages, tier, max_tokens, system, skip_sanitize=False):
        from openai import OpenAI
        from core.keys import get_key
        from core.nsfw_sanitizer import sanitize, desanitize
        _san = (lambda t: t) if skip_sanitize else sanitize
        _desan = (lambda t: t) if skip_sanitize else desanitize
        client = OpenAI(api_key=get_key("openai"))
        model = cfg.get(f"ai_model_{tier}") or _OPENAI_MODELS.get(tier, _OPENAI_MODELS[TIER_BALANCED])
        safe_msgs = [
            {**m, "content": _san(m["content"]) if isinstance(m.get("content"), str) else m.get("content")}
            for m in messages
        ]
        all_messages = []
        if system:
            all_messages.append({"role": "system", "content": _san(system)})
        all_messages.extend(safe_msgs)
        resp = client.chat.completions.create(
            model=model,
            messages=all_messages,
            max_tokens=max_tokens,
        )
        if not resp.choices or resp.choices[0].message.content is None:
            reason = getattr(resp.choices[0], "finish_reason", "unknown") if resp.choices else "no choices"
            raise ValueError(f"OpenAI returned empty response (finish_reason={reason!r}) -- possible content policy refusal")
        return _desan(resp.choices[0].message.content)

    def _openai_vision(self, prompt, images_b64, tier, max_tokens, system):
        from openai import OpenAI
        from core.keys import get_key
        from core.nsfw_sanitizer import sanitize, desanitize
        client = OpenAI(api_key=get_key("openai"))
        model = cfg.get(f"ai_model_{tier}") or _OPENAI_MODELS.get(tier, _OPENAI_MODELS[TIER_BALANCED])
        content = [{"type": "text", "text": sanitize(prompt)}]
        for img in images_b64[:4]:
            content.append({
                "type": "image_url",
                "image_url": {"url": f"data:image/jpeg;base64,{img}", "detail": "low"},
            })
        all_messages = []
        if system:
            all_messages.append({"role": "system", "content": sanitize(system)})
        all_messages.append({"role": "user", "content": content})
        resp = client.chat.completions.create(
            model=model,
            messages=all_messages,
            max_tokens=max_tokens,
        )
        if not resp.choices or resp.choices[0].message.content is None:
            reason = getattr(resp.choices[0], "finish_reason", "unknown") if resp.choices else "no choices"
            raise ValueError(f"OpenAI returned empty response (finish_reason={reason!r}) -- possible content policy refusal")
        return desanitize(resp.choices[0].message.content)

    # -- Retry -----------------------------------------------------------------

    def _call_with_retry(self, fn, max_attempts: int = 3, provider: str | None = None) -> str:
        last_exc = None
        actual_provider = provider or self._provider()
        for attempt in range(max_attempts):
            try:
                result = fn()
                self._stats["ok"] += 1
                return result
            except Exception as exc:
                last_exc = exc
                self._stats["errors"] += 1
                exc_str  = str(exc).lower()
                exc_type = type(exc).__name__
                # Never retry permanent errors (wrong key, model not found).
                if any(t in exc_type for t in ("AuthenticationError", "NotFoundError",
                                               "PermissionDeniedError", "InvalidRequestError",
                                               "BadRequestError")):
                    log.error("LLM permanent error (will not retry): %s", exc)
                    raise
                # Never retry a content-policy block -- the same prompt against the
                # same provider refuses identically every time. Raise immediately so
                # route()/route_vision() can fall back to Featherless right away
                # instead of burning 3 attempts' worth of backoff on a dead end.
                if _looks_like_refusal_exception(exc):
                    log.info("%s declined the request (no retry -- falling back): %s", actual_provider, exc)
                    raise
                # Never retry timeouts for a LOCAL KoboldCpp -- the model is already
                # running; a second queued call just adds to the backlog. (Cloud
                # providers -- anthropic/openai/featherless -- can accept parallel
                # requests, so they still retry.) Use the *actual* provider for this
                # call (force_provider-aware) rather than self._provider().
                is_timeout = ("timeout" in exc_str or "timed out" in exc_str
                              or "ReadTimeout" in exc_type or "ConnectTimeout" in exc_type)
                if is_timeout and actual_provider == "kobold":
                    log.warning("KoboldCpp timeout (no retry -- it is busy): %s", exc)
                    raise
                # Never retry OOM -- a local model doesn't fit in VRAM; retrying 3x
                # just wastes time and makes the error message more confusing.
                if _is_oom(exc) or "requires more memory than available" in exc_str:
                    log.error("Local LLM OOM (no retry): %s", exc)
                    raise
                if attempt < max_attempts - 1:
                    self._stats["retries"] += 1
                    wait = 2 ** attempt
                    if "RateLimitError" in exc_type:
                        retry_after = getattr(getattr(exc, "response", None), "headers", {}).get("retry-after")
                        if retry_after:
                            try:
                                wait = min(int(retry_after), 120)
                            except (ValueError, TypeError):
                                pass
                    log.warning(
                        "LLM call failed (attempt %d/%d), retrying in %ds: %s",
                        attempt + 1, max_attempts, wait, exc,
                    )
                    time.sleep(wait)
        raise RuntimeError(
            f"LLM ({actual_provider}) failed after {max_attempts} attempts: {last_exc}"
        ) from last_exc
