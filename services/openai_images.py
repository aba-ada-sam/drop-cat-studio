"""OpenAI DALL-E image generation — SFW only (enforced by OpenAI policy)."""
import base64
import logging
import time
from pathlib import Path

log = logging.getLogger(__name__)

SIZES = {
    "1:1":   "1024x1024",
    "16:9":  "1792x1024",
    "9:16":  "1024x1792",
}
OUTPUT_DIR = Path(__file__).resolve().parent.parent / "output"


def generate(prompt: str, aspect: str = "1:1", quality: str = "standard") -> tuple[Path | None, str | None]:
    from core.keys import get_key
    api_key = get_key("openai")
    if not api_key:
        return None, "No OpenAI API key — add it in Settings"

    size = SIZES.get(aspect, "1024x1024")

    try:
        import openai
        client = openai.OpenAI(api_key=api_key)
        resp = client.images.generate(
            model="dall-e-3",
            prompt=prompt,
            n=1,
            size=size,
            quality=quality,
            response_format="b64_json",
        )
        b64 = resp.data[0].b64_json
        if not b64:
            return None, "No image data returned"

        out_dir = OUTPUT_DIR / time.strftime("%Y-%m-%d")
        out_dir.mkdir(parents=True, exist_ok=True)
        dest = out_dir / f"openai_{int(time.time() * 1000)}.png"
        dest.write_bytes(base64.b64decode(b64))
        log.info("OpenAI image saved: %s", dest)
        return dest, None
    except Exception as e:
        log.error("OpenAI image generation failed: %s", e)
        return None, str(e)
