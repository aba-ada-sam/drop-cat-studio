"""Uncensored LLM client -- OpenAI-compatible backends (Featherless / KoboldCpp).

Replaces the former local-Ollama client (Ollama was uninstalled 2026-07-05).
The "uncensored" role -- a provider that analyses NSFW images and never refuses
content -- is now served by:

  * Featherless (cloud, default): https://api.featherless.ai/v1  -- OpenAI-compatible,
    model-per-request, Qwen3-VL for vision. No local GPU use (no contention with
    WanGP / ACE-Step / Forge).
  * KoboldCpp (local, optional):  http://localhost:5001/v1        -- OpenAI-compatible,
    one loaded model at a time. Runs on the local GPU.

Both speak the OpenAI Chat Completions API, so a single client serves them; the
`provider` argument ("featherless" | "kobold") selects base URL, key and models
from config at call time (so Settings changes apply without a restart).
"""
import base64
import io
import json
import logging
import re
from pathlib import Path

from core import config as cfg

log = logging.getLogger(__name__)

# AI model tiers -- tasks mapped to speed/quality
TIER_FAST     = "fast"      # quick responses, lighter model
TIER_BALANCED = "balanced"  # all-round quality
TIER_POWER    = "power"     # deep analysis, best model

# Phrases that indicate a LOCAL backend (KoboldCpp) cannot load a model due to
# RAM/VRAM shortage. These are permanent failures -- retrying won't help.
# (Cloud Featherless never OOMs on our side.)
_OOM_PHRASES = (
    "more system memory",
    "requires more system memory",
    "not enough memory",
    "out of memory",
    "insufficient memory",
    "cannot allocate",
    "out of system memory",
)


def _is_oom(exc: Exception) -> bool:
    s = str(exc).lower()
    return any(p in s for p in _OOM_PHRASES)


def _strip_think(text: str) -> str:
    """Remove <think>...</think> reasoning blocks some served models emit."""
    if not text:
        return text
    if "<think>" in text:
        text = re.sub(r"<think>[\s\S]*?</think>", "", text).strip()
        # Unclosed <think> (truncated by max_tokens): drop the dangling opener.
        if text.startswith("<think>"):
            text = text[len("<think>"):].strip()
    return text


def _read_key_file(path: str) -> str:
    try:
        p = Path(path)
        if p.exists():
            return p.read_text(encoding="utf-8").strip()
    except Exception as e:
        log.debug("could not read key file %s: %s", path, e)
    return ""


def _backend_config(provider: str) -> dict:
    """Resolve base_url / api_key / models for an uncensored provider from config.

    Read fresh on every call so Settings edits take effect without a restart.
    """
    if provider == "kobold":
        return {
            "base_url": (cfg.get("kobold_base") or "http://localhost:5001/v1").rstrip("/"),
            # KoboldCpp ignores the key but the OpenAI SDK requires a non-empty string.
            "api_key": "sk-no-key-required",
            "text_model":   cfg.get("kobold_model") or "koboldcpp",
            "vision_model": cfg.get("kobold_vision_model") or cfg.get("kobold_model") or "koboldcpp",
        }
    # default: featherless (cloud)
    key = cfg.get("featherless_key") or _read_key_file(
        cfg.get("featherless_key_file") or r"C:\JSON Credentials\featherless_api_key.txt"
    )
    return {
        "base_url": (cfg.get("featherless_base") or "https://api.featherless.ai/v1").rstrip("/"),
        "api_key": key,
        "text_model":   cfg.get("featherless_text_model")   or "Steelskull/L3.3-MS-Nevoria-70b",
        "vision_model": cfg.get("featherless_vision_model") or "Qwen/Qwen3-VL-32B-Instruct",
    }


class LLMClient:
    """OpenAI-compatible client for the uncensored backends (Featherless / KoboldCpp).

    Usage:
        client = LLMClient()
        text = client.chat("featherless", messages, tier=TIER_BALANCED)
        text = client.chat_with_images("featherless", prompt, images_b64, tier=TIER_POWER)
    """

    def __init__(self, *args, **kwargs):
        # Positional/keyword args are accepted and ignored for backwards
        # compatibility with the old Ollama constructor signature
        # (host=, fast_model=, ...). All configuration now comes from config.py.
        self._timeout = 180.0

    def _client(self, provider: str):
        from openai import OpenAI
        conf = _backend_config(provider)
        if not conf["api_key"]:
            # Path kept out of the f-string expression: Python 3.10 forbids a
            # backslash inside f-string {..} (only allowed from 3.12+).
            key_file = cfg.get("featherless_key_file") or r"C:\JSON Credentials\featherless_api_key.txt"
            raise RuntimeError(
                "Featherless API key not found -- add it to "
                f"{key_file} "
                "or set featherless_key in Settings (or switch the LLM provider to a local KoboldCpp)."
            )
        return OpenAI(base_url=conf["base_url"], api_key=conf["api_key"], timeout=self._timeout), conf

    # -- old Ollama shims kept so callers/tests don't break -------------------

    def update_config(self, *args, **kwargs):
        """No-op: configuration is read from config.py on every call now."""
        return

    def has_provider(self, provider: str) -> bool:
        return self.is_available(provider)

    def is_available(self, provider: str = "featherless") -> bool:
        """True if the given uncensored backend is usable."""
        try:
            conf = _backend_config(provider)
            if provider == "kobold":
                # local: is the server up?
                import urllib.request
                base = conf["base_url"].rsplit("/v1", 1)[0]
                urllib.request.urlopen(base + "/v1/models", timeout=3).read()
                return True
            return bool(conf["api_key"])  # featherless: key present
        except Exception:
            return False

    def list_models(self, provider: str = "featherless") -> list[str]:
        try:
            client, _ = self._client(provider)
            return [m.id for m in client.models.list().data]
        except Exception:
            return []

    # -- inference ------------------------------------------------------------

    def chat(
        self,
        provider: str,
        messages: list[dict],
        tier: str = TIER_BALANCED,
        max_tokens: int = 1024,
        system: str = "",
    ) -> str:
        """Send a text chat to the uncensored backend. Returns response text."""
        client, conf = self._client(provider)
        model = conf["text_model"]
        all_messages = []
        if system:
            all_messages.append({"role": "system", "content": system})
        all_messages.extend(messages)
        resp = client.chat.completions.create(
            model=model, messages=all_messages, max_tokens=max_tokens,
        )
        if not resp.choices or resp.choices[0].message.content is None:
            reason = getattr(resp.choices[0], "finish_reason", "unknown") if resp.choices else "no choices"
            raise ValueError(f"{provider} returned empty response (finish_reason={reason!r})")
        return _strip_think(resp.choices[0].message.content)

    def chat_with_images(
        self,
        provider: str,
        prompt: str,
        images_b64: list[str],  # raw base64 strings (no data-URL prefix)
        tier: str = TIER_BALANCED,
        max_tokens: int = 2048,
        system: str = "",
        format_json: bool = False,
    ) -> str:
        """Send a vision request. Always uses the backend's vision model."""
        import time
        client, conf = self._client(provider)
        model = conf["vision_model"]
        log.info("%s vision call: model=%s images=%d max_tokens=%d",
                 provider, model, len(images_b64), max_tokens)
        content = [{"type": "text", "text": prompt}]
        for img in images_b64[:5]:
            content.append({
                "type": "image_url",
                "image_url": {"url": f"data:image/jpeg;base64,{img}"},
            })
        all_messages = []
        if system:
            all_messages.append({"role": "system", "content": system})
        all_messages.append({"role": "user", "content": content})

        kwargs = dict(model=model, messages=all_messages, max_tokens=max_tokens)
        if format_json:
            kwargs["response_format"] = {"type": "json_object"}

        t0 = time.time()
        try:
            resp = client.chat.completions.create(**kwargs)
        except Exception as exc:
            if format_json and "response_format" in str(exc).lower():
                # Backend doesn't support JSON mode -- retry without it.
                kwargs.pop("response_format", None)
                resp = client.chat.completions.create(**kwargs)
            else:
                raise
        if not resp.choices or resp.choices[0].message.content is None:
            reason = getattr(resp.choices[0], "finish_reason", "unknown") if resp.choices else "no choices"
            raise ValueError(f"{provider} vision returned empty response (finish_reason={reason!r})")
        content_txt = _strip_think(resp.choices[0].message.content)
        log.info("%s vision call done in %.1fs (response len=%d)",
                 provider, time.time() - t0, len(content_txt))
        return content_txt


# -- Response parsing helpers -------------------------------------------------

def parse_json_response(text: str) -> dict | list | None:
    """Extract JSON from an LLM response that may contain markdown fences."""
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

    # 3. Scan every { and [ position; use raw_decode so trailing text is ignored.
    _decoder = json.JSONDecoder()
    for i, ch in enumerate(text):
        if ch in '{[':
            try:
                val, _ = _decoder.raw_decode(text, i)
                return val
            except json.JSONDecodeError:
                continue

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
