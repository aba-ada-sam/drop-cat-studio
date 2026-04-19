"""LLM client backed by local Ollama — no API keys required.

Replaces the Anthropic/OpenAI client. All AI calls route through the local
Ollama daemon (http://localhost:11434 by default). Models are selected by
tier; defaults match the qwen3-vl vision models already installed.
"""
import base64
import io
import json
import logging
import re
from pathlib import Path

log = logging.getLogger(__name__)

# AI model tiers — tasks mapped to speed/quality
TIER_FAST     = "fast"      # quick responses, lighter model
TIER_BALANCED = "balanced"  # all-round quality
TIER_POWER    = "power"     # deep analysis, best model

DEFAULT_OLLAMA_MODELS = {
    TIER_FAST:     "gemma4:e4b",
    TIER_BALANCED: "gemma4:e4b",
    TIER_POWER:    "gemma4:26b",
}


def _disable_thinking(system: str, model: str) -> str:
    """Append /no_think for qwen3 models to prevent thinking mode from
    consuming the entire token budget on structured-output requests."""
    if "qwen3" in model.lower():
        return system.rstrip() + "\n\n/no_think"
    return system


def _extract_content(resp) -> str:
    """Pull text content from an Ollama response, handling qwen3 thinking edge cases."""
    content = resp.message.content or ""
    # Strip inline <think> blocks that some Ollama versions leave in content
    if "<think>" in content:
        content = re.sub(r"<think>[\s\S]*?</think>", "", content).strip()
    # If content is empty after stripping, fall back to thinking field (Ollama 0.9+)
    if not content.strip():
        thinking = getattr(resp.message, "thinking", None) or ""
        if thinking:
            log.warning("Response content empty, extracting from thinking field (len=%d)", len(thinking))
            content = re.sub(r"<think>[\s\S]*?</think>", "", thinking).strip()
    return content


class LLMClient:
    """Unified AI client backed by local Ollama with vision support.

    Usage:
        client = LLMClient(host="http://localhost:11434")
        text = client.chat("ollama", messages, tier=TIER_BALANCED)
        text = client.chat_with_images("ollama", prompt, images_b64, tier=TIER_POWER)
    """

    def __init__(
        self,
        host: str = "http://localhost:11434",
        fast_model: str = DEFAULT_OLLAMA_MODELS[TIER_FAST],
        balanced_model: str = DEFAULT_OLLAMA_MODELS[TIER_BALANCED],
        power_model: str = DEFAULT_OLLAMA_MODELS[TIER_POWER],
        vision_model: str = "qwen3-vl:8b",   # always used for image analysis
    ):
        self._host = host
        self._models = {
            TIER_FAST:     fast_model,
            TIER_BALANCED: balanced_model,
            TIER_POWER:    power_model,
        }
        self._vision_model = vision_model
        self._client = None

    def _get_client(self):
        if self._client is None:
            import httpx
            import ollama
            self._client = ollama.Client(
                host=self._host,
                timeout=httpx.Timeout(connect=10, read=180, write=30, pool=10),
            )
        return self._client

    def update_config(
        self,
        host: str = "",
        fast_model: str = "",
        balanced_model: str = "",
        power_model: str = "",
        vision_model: str = "",
    ):
        """Update Ollama host or model selections at runtime."""
        if host and host != self._host:
            self._host = host
            self._client = None  # force reconnect
        if fast_model:
            self._models[TIER_FAST] = fast_model
        if balanced_model:
            self._models[TIER_BALANCED] = balanced_model
        if power_model:
            self._models[TIER_POWER] = power_model
        if vision_model:
            self._vision_model = vision_model

    def has_provider(self, provider: str) -> bool:
        """Return True if Ollama is reachable (provider arg kept for compat)."""
        try:
            self._get_client().list()
            return True
        except Exception:
            return False

    def _model(self, tier: str) -> str:
        return self._models.get(tier, self._models[TIER_BALANCED])

    def chat(
        self,
        provider: str,          # kept for API compatibility, value ignored
        messages: list[dict],
        tier: str = TIER_BALANCED,
        max_tokens: int = 1024,
        system: str = "",
    ) -> str:
        """Send a text chat to Ollama. Returns response text."""
        model = self._model(tier)
        all_messages = []
        if system:
            all_messages.append({"role": "system", "content": _disable_thinking(system, model)})
        all_messages.extend(messages)

        kwargs = dict(model=model, messages=all_messages,
                      options={"num_predict": max_tokens})
        if "qwen3" in model.lower():
            kwargs["think"] = False
        resp = self._get_client().chat(**kwargs)
        return _extract_content(resp)

    def chat_with_images(
        self,
        provider: str,          # kept for API compatibility, value ignored
        prompt: str,
        images_b64: list[str],  # raw base64 strings (no data-URL prefix)
        tier: str = TIER_BALANCED,
        max_tokens: int = 2048,
        system: str = "",
    ) -> str:
        """Send a vision request to Ollama. Always uses the vision model (qwen3-vl),
        not the text-tier models — text-only models silently ignore images."""
        import time
        model = self._vision_model
        log.info("Ollama vision call: model=%s images=%d max_tokens=%d", model, len(images_b64), max_tokens)
        messages = []
        if system:
            messages.append({"role": "system", "content": _disable_thinking(system, model)})
        messages.append({
            "role": "user",
            "content": prompt,
            "images": images_b64,
        })

        t0 = time.time()
        kwargs = dict(model=model, messages=messages,
                      options={"num_predict": max_tokens})
        if "qwen3" in model.lower():
            kwargs["think"] = False
        resp = self._get_client().chat(**kwargs)
        elapsed = time.time() - t0
        content = _extract_content(resp)
        log.info("Ollama vision call done in %.1fs (response len=%d)", elapsed, len(content))
        return content

    def list_models(self) -> list[str]:
        """Return names of all Ollama models installed locally."""
        try:
            return [m.model for m in self._get_client().list().models]
        except Exception:
            return []

    def is_available(self) -> bool:
        """Return True if Ollama daemon is reachable."""
        try:
            self._get_client().list()
            return True
        except Exception:
            return False


# ── Response parsing helpers ─────────────────────────────────────────────────

def parse_json_response(text: str) -> dict | list | None:
    """Extract JSON from an LLM response that may contain markdown fences.

    BUG-08: replaced fragile split-on-backtick with a regex that handles
    ```json\\n{...}\\n``` and nested code fences correctly.
    """
    # 1. Try a fenced code block first (handles ```json ... ``` and ``` ... ```)
    fence_match = re.search(r"```(?:json)?\s*([\s\S]+?)```", text)
    if fence_match:
        candidate = fence_match.group(1).strip()
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            pass

    # 2. Try the raw text (model may have answered without fences)
    cleaned = text.strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass

    # 3. Fallback: find the first {...} or [...] span in the text
    match = re.search(r"[\[{][\s\S]*[\]}]", text)
    if match:
        try:
            return json.loads(match.group())
        except json.JSONDecodeError:
            pass

    return None


def encode_image_b64(
    image_path: str | Path,
    max_dim: int = 1024,
    quality: int = 85,
) -> str | None:
    """Load an image, resize to max_dim, return base64-encoded JPEG string."""
    try:
        from PIL import Image
        img = Image.open(image_path)
        img.thumbnail((max_dim, max_dim))
        buf = io.BytesIO()
        img.convert("RGB").save(buf, format="JPEG", quality=quality)
        return base64.b64encode(buf.getvalue()).decode()
    except Exception as e:
        log.debug("encode_image_b64(%s) failed: %s", image_path, e)
        return None
