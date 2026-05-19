"""LLM router -- dispatches AI calls to Ollama, Anthropic, or OpenAI.

Provider is read from config at call time so it can be hot-switched in Settings.
Default is Ollama (local, no API key needed).
"""
import logging
import time

from core import config as cfg
from core.llm_client import TIER_FAST, TIER_BALANCED, TIER_POWER  # re-exported for features

log = logging.getLogger(__name__)

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


# Phrases that signal a cloud vision API refused an image for content-policy
# reasons. When we see any of these in a response (and the response is short
# prose rather than the structured JSON we asked for), we transparently retry
# against Ollama, which doesn't filter user content.
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
)


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
        """client: LLMClient instance (Ollama) -- used when provider=ollama."""
        self._client = client
        self._stats = {"ok": 0, "errors": 0, "retries": 0}

    def _provider(self, force: str | None = None) -> str:
        """Resolve which LLM provider to use for this call.

        Ollama is opt-in: callers may still pass force='ollama' (e.g. a privacy-
        conscious path), but in auto-mode Ollama is only chosen when no cloud
        key is configured AND the user has explicitly enabled the fallback in
        Settings. Otherwise we surface a hard error so the user knows to
        configure a cloud key or enable Ollama -- no silent local routing.
        """
        if force in ("anthropic", "openai", "ollama"):
            return force
        p = cfg.get("llm_provider") or "auto"
        if p != "auto":
            return p
        # Auto: cloud first, in preference order
        from core.keys import get_key
        if get_key("anthropic"):
            return "anthropic"
        if get_key("openai"):
            return "openai"
        # No cloud key. Only fall back to Ollama if user has opted in.
        if cfg.get("allow_ollama_fallback"):
            return "ollama"
        raise RuntimeError(
            "No cloud LLM key configured and Ollama fallback is disabled. "
            "Add an Anthropic or OpenAI key in Settings, or enable "
            "'Allow Ollama fallback' to use local models."
        )

    def route(
        self,
        messages: list,
        tier: str = TIER_BALANCED,
        max_tokens: int = 1024,
        est_tokens: int = 500,
        system: str = "",
        force_provider: str | None = None,
    ) -> str:
        provider = self._provider(force_provider)
        if provider == "anthropic":
            return self._call_with_retry(lambda: self._anthropic_chat(messages, tier, max_tokens, system), provider=provider)
        if provider == "openai":
            return self._call_with_retry(lambda: self._openai_chat(messages, tier, max_tokens, system), provider=provider)
        return self._call_with_retry(
            lambda: self._client.chat("ollama", messages, tier=tier, max_tokens=max_tokens, system=system),
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
            result = self._call_with_retry(
                lambda: self._anthropic_vision(prompt, images_b64, tier, max_tokens, system),
                provider=provider,
            )
        elif provider == "openai":
            result = self._call_with_retry(
                lambda: self._openai_vision(prompt, images_b64, tier, max_tokens, system),
                provider=provider,
            )
        else:
            return self._call_with_retry(
                lambda: self._client.chat_with_images("ollama", prompt, images_b64, tier=tier,
                                                     max_tokens=max_tokens, system=system,
                                                     format_json=format_json),
                provider=provider,
            )

        # Cloud safety-refusal fallback: Anthropic and OpenAI refuse images
        # they classify as containing nudity / sensitive content with a plain-
        # English refusal instead of a structured error. Detect that and
        # transparently retry the SAME prompt against Ollama (local, no
        # content filter). This restores the pre-Ollama-opt-in behaviour for
        # NSFW/artistic photos without making Ollama the default for SFW
        # calls. Caller never needs to know which provider answered.
        if _looks_like_safety_refusal(result):
            try:
                from services.manager import ollama_alive
                ollama_up = ollama_alive()
            except Exception:
                ollama_up = False
            if ollama_up:
                log.info("[router] %s refused the image as NSFW -- retrying via Ollama (local, no content filter)",
                         provider)
                try:
                    return self._client.chat_with_images(
                        "ollama", prompt, images_b64, tier=tier,
                        max_tokens=max_tokens, system=system, format_json=format_json,
                    )
                except Exception as e:
                    log.warning("[router] Ollama fallback failed after %s refusal: %s", provider, e)
                    # Return the original refusal so the caller's UI can show it
            else:
                log.info("[router] %s refused the image and Ollama is not available -- "
                         "user will see the refusal text", provider)
        return result

    def stats(self) -> dict:
        return dict(self._stats)

    # -- Anthropic -------------------------------------------------------------

    def _anthropic_chat(self, messages, tier, max_tokens, system):
        import anthropic
        from core.keys import get_key
        from core.nsfw_sanitizer import sanitize, desanitize
        client = anthropic.Anthropic(api_key=get_key("anthropic"))
        model = cfg.get(f"ai_model_{tier}") or _ANTHROPIC_MODELS.get(tier, _ANTHROPIC_MODELS[TIER_BALANCED])
        safe_msgs = [
            {**m, "content": sanitize(m["content"]) if isinstance(m.get("content"), str) else m.get("content")}
            for m in messages
        ]
        kwargs = dict(model=model, max_tokens=max_tokens, messages=safe_msgs)
        if system:
            kwargs["system"] = sanitize(system)
        resp = client.messages.create(**kwargs)
        if not resp.content:
            raise ValueError(f"Anthropic returned empty response (stop_reason={resp.stop_reason!r}) -- possible content policy refusal")
        return desanitize(resp.content[0].text)

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

    def _openai_chat(self, messages, tier, max_tokens, system):
        from openai import OpenAI
        from core.keys import get_key
        from core.nsfw_sanitizer import sanitize, desanitize
        client = OpenAI(api_key=get_key("openai"))
        model = cfg.get(f"ai_model_{tier}") or _OPENAI_MODELS.get(tier, _OPENAI_MODELS[TIER_BALANCED])
        safe_msgs = [
            {**m, "content": sanitize(m["content"]) if isinstance(m.get("content"), str) else m.get("content")}
            for m in messages
        ]
        all_messages = []
        if system:
            all_messages.append({"role": "system", "content": sanitize(system)})
        all_messages.extend(safe_msgs)
        resp = client.chat.completions.create(
            model=model,
            messages=all_messages,
            max_tokens=max_tokens,
        )
        if not resp.choices or resp.choices[0].message.content is None:
            reason = getattr(resp.choices[0], "finish_reason", "unknown") if resp.choices else "no choices"
            raise ValueError(f"OpenAI returned empty response (finish_reason={reason!r}) -- possible content policy refusal")
        return desanitize(resp.choices[0].message.content)

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
                # Never retry timeouts for Ollama -- the model is already running; a
                # second queued call just adds to the backlog and makes things worse.
                # (Cloud providers are different: they can accept parallel requests.)
                # Use the *actual* provider for this call (force_provider-aware) rather
                # than self._provider(), which only sees the auto-resolved value and
                # wrongly let force_provider="ollama" calls retry through 3x backoff.
                is_timeout = ("timeout" in exc_str or "timed out" in exc_str
                              or "ReadTimeout" in exc_type or "ConnectTimeout" in exc_type)
                if is_timeout and actual_provider == "ollama":
                    log.warning("Ollama timeout (no retry -- Ollama is busy): %s", exc)
                    raise
                # Never retry OOM -- the model doesn't fit in RAM; retrying 3x just
                # wastes ~15 seconds and makes the error message more confusing.
                # LLMClient.chat() already attempted a vision-model fallback before
                # this exception propagated up, so we are truly out of options.
                from core.llm_client import _is_ollama_oom
                if _is_ollama_oom(exc) or "requires more memory than available" in exc_str:
                    log.error("Ollama OOM (no retry): %s", exc)
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
