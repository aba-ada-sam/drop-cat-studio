"""Photo analysis and prompt generation for Fun Videos.

Uses Claude/GPT to analyze photos and generate creative I2V video prompts.
Ported from DropCatGo-Fun-Videos_w_Audio/main.py analyzer functions.
"""
import logging

from core.llm_client import TIER_FAST, TIER_BALANCED, TIER_POWER, parse_json_response

log = logging.getLogger(__name__)

ANALYSIS_SYSTEM = """You are a creative director analyzing a photograph for a fun short video project.
Given the image, return ONLY valid JSON with this structure:
{
  "title": "short descriptive title (max 6 words)",
  "scene_description": "1-2 sentences describing the image content, subjects, setting",
  "mood": "dominant mood/atmosphere",
  "setting": "location/environment type",
  "subjects": ["list", "of", "main", "subjects"],
  "dominant_colors": ["color1", "color2", "color3"],
  "visual_style": "photographic style notes",
  "energy": "calm | gentle | moderate | energetic | intense"
}"""

VIDEO_PROMPT_SYSTEM = """You are a kinetic action director writing prompts for an image-to-video AI (Wan2GP / LTX-2).
The model already sees the image. Your job: describe explosive PHYSICAL ACTION, not camera moves.

BANNED — these produce boring pan/zoom slideshows. Never write:
  "camera slowly pushes in", "gentle pan", "slow zoom", "camera pulls back",
  "soft dolly", "subtle movement", "gentle motion", "warm light plays across"

REQUIRED: Lead with what the SUBJECT IS DOING. Examples by subject type:
  Person/face  → "throws head back laughing, hair whipping sideways, hands clap wildly"
  Animal       → "launches into a full sprint, paws churning, ears flat, tongue flying"
  Food/object  → "steam erupts violently, liquid splashes and arcs, surface bubbles and churns"
  Landscape    → "storm front slams in, trees thrash violently, rain sheets sideways, lightning splits the sky"
  Portrait     → "eyes snap open with sudden recognition, mouth curves into a slow dangerous smile, shoulders roll back"

Camera movement is allowed ONLY as a reaction to action, never as the primary event.
If the scene is static (a painting, a product shot), invent plausible action: fire catches,
water appears, wind arrives, the subject comes alive.

RULES:
- Start with the subject acting — explosive action verbs: erupts, whips, crashes, surges, slams, tears, launches
- 35-65 words, every word earns its place
- NO negative language (video models ignore negation)
- NO re-describing the image's appearance
- Single tight paragraph

Return ONLY valid JSON."""

MUSIC_PROMPT_SYSTEM = """You are a music director analyzing a generated video to suggest matching audio.
Given frames from the video and context, return ONLY valid JSON:
{
  "music_prompt": "genre, mood, instrumentation description for AI music generation",
  "bpm": 80,
  "key_suggestion": "optional key/scale",
  "reasoning": "brief explanation of why this music fits"
}"""

LYRICS_SYSTEM = """You are a sardonic, witty songwriter with a gift for irony and gentle mockery.
You write short song lyrics for AI-generated videos — fun, clever, slightly absurdist.
Your style: think Randy Newman meets Flight of the Conchords. Dry humor. Observational irony.
Poke fun at the subject without being mean. Celebrate the mundane as if it were epic.

Format your lyrics in ACE-Step style with section tags:
[verse]
...
[chorus]
...
[verse]
...
[outro]
...

Rules:
- 3-4 short lines per section (8-12 syllables per line)
- Rhyme scheme: AABB or ABAB, keep it loose
- Match the energy/mood of the music prompt
- Return ONLY the raw lyrics text — no JSON, no commentary, no quotes"""


def analyze_photo(router, image_b64: str) -> dict:
    """Analyze a photo and return scene understanding. Raises on failure."""
    text = router.route_vision(
        "Analyze this photograph for a creative video project.",
        [image_b64],
        tier=TIER_BALANCED,
        system=ANALYSIS_SYSTEM,
    )
    result = parse_json_response(text)
    if not result:
        raise RuntimeError("AI returned unparseable response for photo analysis")
    return result


def generate_video_prompts(
    router,
    image_b64: str,
    user_direction: str = "",
    num_prompts: int = 4,
    creativity: float = 8.0,
    max_tokens: int = 2048,
    force_provider: str | None = None,
) -> dict:
    """Generate creative I2V video prompts from a photo."""
    # Build creativity instruction
    if creativity <= 4:
        style = "grounded, realistic motion"
    elif creativity <= 7:
        style = "balanced creativity with cinematic flair"
    else:
        style = "wildly imaginative, surreal, unexpected transformations"

    prompt_parts = [
        f"Generate {num_prompts} creative video prompts for this image.",
        f"Style: {style} (creativity level {creativity}/10).",
    ]
    if user_direction:
        prompt_parts.append(f'USER\'S CREATIVE DIRECTION (incorporate into ALL prompts):\n"{user_direction}"')

    prompt_parts.append(
        f"""Return JSON:
{{
  "prompts": [
    {{
      "label": "short 2-4 word label",
      "prompt": "the actual I2V generation prompt (30-60 words)",
      "mood": "one word mood",
      "style": "one word style"
    }}
  ]
}}
Generate exactly {num_prompts} prompts, each with different mood, camera work, and action."""
    )

    last_text = ""
    for attempt in range(2):
        tier = TIER_FAST if num_prompts == 1 else TIER_BALANCED
        last_text = router.route_vision(
            "\n\n".join(prompt_parts),
            [image_b64],
            tier=tier,
            max_tokens=max_tokens,
            system=VIDEO_PROMPT_SYSTEM,
            force_provider=force_provider,
        )
        result = parse_json_response(last_text)
        if result and isinstance(result, dict) and result.get("prompts"):
            return result
        # Model returned a top-level array instead of {"prompts": [...]} — common
        if isinstance(result, list) and result:
            prompts = []
            for i, item in enumerate(result[:num_prompts]):
                if isinstance(item, str):
                    prompts.append({"label": f"Take {i+1}", "prompt": item, "mood": "cinematic", "style": "dynamic"})
                elif isinstance(item, dict) and item.get("prompt"):
                    prompts.append({
                        "label": item.get("label") or f"Take {i+1}",
                        "prompt": item["prompt"],
                        "mood":   item.get("mood")  or "cinematic",
                        "style":  item.get("style") or "dynamic",
                    })
            if prompts:
                log.info("Wrapped top-level array into prompts structure (%d prompts)", len(prompts))
                return {"prompts": prompts}
        log.warning("Prompt generation attempt %d: unparseable response (len=%d): %.300s",
                    attempt + 1, len(last_text), last_text)

    # Salvage: pull any quoted or sentence-length strings from the raw response and
    # treat them as prompts. Better than a blank error screen.
    import re as _re
    salvaged = []
    # Look for "prompt": "..." patterns first
    for m in _re.finditer(r'"prompt"\s*:\s*"([^"]{20,})"', last_text):
        salvaged.append(m.group(1))
    # Fallback: grab long sentences / paragraphs
    if not salvaged:
        for chunk in _re.split(r'\n{2,}|(?<=[.!?])\s+(?=[A-Z])', last_text):
            chunk = chunk.strip().strip('"').strip("'")
            if 20 < len(chunk) < 400:
                salvaged.append(chunk)
    if not salvaged:
        salvaged = [last_text[:300].strip()]

    log.warning("Using salvaged prompts from raw response (%d found)", len(salvaged))
    return {
        "prompts": [
            {"label": f"Take {i+1}", "prompt": p, "mood": "cinematic", "style": "dynamic"}
            for i, p in enumerate(salvaged[:num_prompts])
        ]
    }


def generate_lyrics(router, video_frames_b64: list[str], music_prompt: str = "", user_direction: str = "") -> str:
    """Auto-generate ironic/satirical lyrics for a video using Claude."""
    parts = ["Write fun, sardonic song lyrics for this AI-generated video."]
    if music_prompt:
        parts.append(f'Music style: "{music_prompt}"')
    if user_direction:
        parts.append(f'Creative direction: "{user_direction}"')
    parts.append("Look at these frames from the video and write lyrics that are SPECIFIC to what you see — the subject, their expression, what they're doing, the setting. Make it feel written FOR this video, not generic.")

    prompt = "\n\n".join(parts)
    try:
        if video_frames_b64:
            text = router.route_vision(prompt, video_frames_b64, tier=TIER_FAST, system=LYRICS_SYSTEM)
        else:
            text = router.route([{"role": "user", "content": prompt}], tier=TIER_FAST, system=LYRICS_SYSTEM)
        return text.strip() if text else ""
    except Exception as e:
        log.warning("Lyrics generation failed: %s", e)
        return ""


def generate_music_prompt(router, video_frames_b64: list[str], user_direction: str = "") -> dict:
    """Analyze video frames and suggest matching music."""
    prompt = "Analyze these frames from a generated video and suggest matching background music."
    if user_direction:
        prompt += f'\nCreative direction: "{user_direction}"'

    try:
        text = router.route_vision(
            prompt,
            video_frames_b64,  # caller controls count via _sample_music_frames
            tier=TIER_BALANCED,
            system=MUSIC_PROMPT_SYSTEM,
        )
        result = parse_json_response(text)
        return result or {"music_prompt": "cinematic ambient, warm strings, gentle piano", "bpm": 80}
    except Exception as e:
        log.warning("Music prompt generation failed: %s", e)
        return {"music_prompt": "cinematic ambient, warm strings, gentle piano", "bpm": 80, "error": str(e)}
