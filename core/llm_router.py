"""LLM router — dispatches AI calls to Ollama, Anthropic, or OpenAI.

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


class LLMRouter:
    """Routes AI calls to the configured provider with retry.

    Provider is read from config on each call so Settings changes apply immediately.

    Usage (thread-safe):
        router = LLMRouter(llm_client)
        text = router.route(messages, tier=TIER_BALANCED, max_tokens=512)
        text = router.route_vision(prompt, images_b64, tier=TIER_POWER)
    """

    def __init__(self, client):
        """client: LLMClient instance (Ollama) — used when provider=ollama."""
        self._client = client
        self._stats = {"ok": 0, "errors": 0, "retries": 0}

    def _provider(self, force: str | None = None) -> str:
        if force in ("anthropic", "openai", "ollama"):
            return force
        p = cfg.get("llm_provider") or "auto"
        if p != "auto":
            return p
        # Auto: prefer cloud APIs when keys are present (much faster than local Ollama)
        from core.keys import get_key
        if get_key("anthropic"):
            return "anthropic"
        if get_key("openai"):
            return "openai"
        return "ollama"

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
            return self._call_with_retry(lambda: self._anthropic_chat(messages, tier, max_tokens, system))
        if provider == "openai":
            return self._call_with_retry(lambda: self._openai_chat(messages, tier, max_tokens, system))
        return self._call_with_retry(
            lambda: self._client.chat("ollama", messages, tier=tier, max_tokens=max_tokens, system=system)
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
    ) -> str:
        provider = self._provider(force_provider)
        if provider == "anthropic":
            return self._call_with_retry(lambda: self._anthropic_vision(prompt, images_b64, tier, max_tokens, system))
        if provider == "openai":
            return self._call_with_retry(lambda: self._openai_vision(prompt, images_b64, tier, max_tokens, system))
        return self._call_with_retry(
            lambda: self._client.chat_with_images("ollama", prompt, images_b64, tier=tier, max_tokens=max_tokens, system=system)
        )

    def stats(self) -> dict:
        return dict(self._stats)

    # ── Anthropic ─────────────────────────────────────────────────────────────

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
        return desanitize(resp.content[0].text)

    # ── OpenAI ────────────────────────────────────────────────────────────────

    def _openai_chat(self, messages, tier, max_tokens, system):
        from openai import OpenAI
        from core.keys import get_key
        client = OpenAI(api_key=get_key("openai"))
        all_messages = []
        if system:
            all_messages.append({"role": "system", "content": system})
        all_messages.extend(messages)
        resp = client.chat.completions.create(
            model=_OPENAI_MODELS[tier],
            messages=all_messages,
            max_tokens=max_tokens,
        )
        return resp.choices[0].message.content

    def _openai_vision(self, prompt, images_b64, tier, max_tokens, system):
        from openai import OpenAI
        from core.keys import get_key
        client = OpenAI(api_key=get_key("openai"))
        content = [{"type": "text", "text": prompt}]
        for img in images_b64[:4]:
            content.append({
                "type": "image_url",
                "image_url": {"url": f"data:image/jpeg;base64,{img}", "detail": "low"},
            })
        all_messages = []
        if system:
            all_messages.append({"role": "system", "content": system})
        all_messages.append({"role": "user", "content": content})
        resp = client.chat.completions.create(
            model=_OPENAI_MODELS[tier],
            messages=all_messages,
            max_tokens=max_tokens,
        )
        return resp.choices[0].message.content

    # ── Retry ─────────────────────────────────────────────────────────────────

    def _call_with_retry(self, fn, max_attempts: int = 3) -> str:
        last_exc = None
        for attempt in range(max_attempts):
            try:
                result = fn()
                self._stats["ok"] += 1
                return result
            except Exception as exc:
                last_exc = exc
                self._stats["errors"] += 1
                # BUG-07: don't retry permanent errors (wrong key, model not found).
                # These will never succeed — fail immediately instead of wasting 6s.
                exc_type = type(exc).__name__
                if any(t in exc_type for t in ("AuthenticationError", "NotFoundError",
                                               "PermissionDeniedError", "InvalidRequestError")):
                    log.error("LLM permanent error (will not retry): %s", exc)
                    raise
                if attempt < max_attempts - 1:
                    self._stats["retries"] += 1
                    # Respect Retry-After header on rate limit errors
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
            f"LLM ({self._provider()}) failed after {max_attempts} attempts: {last_exc}"
        ) from last_exc
