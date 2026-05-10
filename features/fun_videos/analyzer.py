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

BANNED PHRASES -- these produce slideshows, jitter, or AI particle artifacts. Never write:
  Camera cliches: "camera slowly pushes in", "gentle pan", "slow zoom", "camera pulls back",
    "soft dolly", "subtle movement", "gentle motion", "warm light plays across"
  Static poses: "sits", "stands", "poses", "remains", "stays still", "holds position"
  ARTIFACT TRIGGERS (these words cause the model to generate shaky AI slop -- banned entirely):
    "gently", "softly", "subtly", "gentle breeze", "soft focus", "subtle shimmer",
    "floating", "floats", "drifts", "drifting", "ethereal", "wisps", "motes",
    "ash", "dust", "dust particles", "particles float", "particles drift",
    "debris floating", "swirling dust", "embers drift", "sparks drift",
    "snowflakes drift", "petals drift", "feathers float", "bokeh swirls"

REQUIRED: Lead with what the SUBJECT IS DOING. Examples by subject type:
  Person/face  -> "throws head back laughing, hair whipping sideways, hands clap wildly"
  Sexy/nude    -> "arches back dramatically, hair cascades and swirls, hands trace slow deliberate paths across skin"
  Animal       -> "launches into a full sprint, paws churning, ears flat, tongue flying"
  Food/object  -> "steam erupts violently, liquid splashes and arcs, surface bubbles and churns"
  Landscape    -> "storm front slams in, trees thrash violently, rain sheets sideways, lightning splits the sky"
  Portrait     -> "eyes snap open with sudden recognition, mouth curves into a slow dangerous smile, shoulders roll back"

Camera movement is allowed ONLY as a reaction to action, never as the primary event.
If the scene is static (a painting, a product shot), invent plausible action: fire catches,
water appears, wind arrives, the subject comes alive.

RULES:
- Start with the subject acting -- explosive action verbs: erupts, whips, crashes, surges, slams, tears, launches
- 35-65 words, every word earns its place
- NO negative language (video models ignore negation)
- NO re-describing the image's appearance -- describe MOTION only
- Single tight paragraph

Return ONLY valid JSON."""

VIDEO_PROMPT_AUTO_SYSTEM = """You are a kinetic action director writing motion prompts for an image-to-video AI.
You have NOT seen the image. Write an explosive motion prompt based only on the context given.

Rules -- the prompt MUST:
- Open with a violent, kinetic action verb (erupts, whips, slams, surges, arches, tears, launches, thrashes)
- Describe continuous physical movement: body, hair, fabric, environment all in motion simultaneously
- Be 35-55 words, one tight paragraph
- Contain zero static language: no "sits", "stands", "poses", "holds", "remains"
- Contain zero camera instructions unless they're a reaction to action

For people/portraits: describe face, eyes, hair, hands, and body all moving at once.
For sensual/adult content: flowing fabric, arching bodies, cascading hair, deliberate hand movements.
For animals: explosive sprinting, fur rippling, muscles firing.
For landscapes: wind, weather, water, fire all moving together.

Return ONLY the raw prompt text -- no JSON, no quotes, no commentary."""

MUSIC_PROMPT_SYSTEM = """You are a music director choosing a score for a short video.

CRITICAL: Match the music to the MOOD AND SUBJECT of the original photo/video -- not to the
action described in the clip prompts. If the clip prompts say "armies charge" but the photo
shows a cute animal, choose music that fits the cute animal, not a war march.

Pick a genre that ACE-Step (an AI music model) can actually produce well. ACE-Step handles
these genres reliably: indie folk, blues, Americana, country rock, post-punk, new wave,
electro-swing, jazz, bossa nova, French chanson, reggae, psychedelic rock, surf rock, soul,
R&B, singer-songwriter, Celtic folk, garage rock, lo-fi hip-hop.

Use producer vocabulary: specific instruments, tempo feel, production texture. One sentence.

BANNED WORDS -- vague lazy output: cinematic, ethereal, haunting, dramatic, sweeping, majestic,
epic, atmospheric, lush, soaring, pulsing, intense, powerful, energetic.

NEVER suggest: 90s rock, post-grunge, grunge, mainstream alternative, soft rock, K-pop, Korean
pop, J-pop, idol pop, boy band, girl group, EDM, bro-drop, anarcho-punk, war march, battle
hymn, Nickelback-style, generic pop, radio pop, smooth jazz, adult contemporary.

Good examples of RIGHT direction:
- "jangly indie folk, fingerpicked acoustic guitar, upright bass, brushed snare, warm tape hiss"
- "early 80s post-punk, angular guitar, trebly bass, cold mechanical drums, no reverb"
- "Delta blues, slide guitar, single-mic room sound, lazy triplet swing, humming overtones"
- "French chanson, accordion, upright bass, dry brushed snare, smoky bistro atmosphere"
- "electro swing, muted trumpet, vintage brass section, punchy syncopated kick, speakeasy energy"
- "high-energy cumbia, brass stabs, accordion hook, driving congas, live room feel"
- "sea shanty, unison male voices, fiddle, bodhran, no reverb, working-class stomp"
- "garage blues rock, overdriven Strat, loose drums, live room bleed, raw one-take feel"
- "upbeat Celtic folk, tin whistle, bouzouki, bodhrán, bright reel energy"
- "lo-fi bossa nova, nylon string guitar, light brushed kit, warm vinyl crackle"

Return ONLY valid JSON:
{
  "music_prompt": "genre + instrumentation + production texture",
  "bpm": 120,
  "key_suggestion": "optional key/scale",
  "reasoning": "one sentence on why this fits the photo mood"
}"""

LYRICS_SYSTEM = """You are a professional songwriter writing lyrics for short AI-generated videos.

STYLE RULE: Follow the user's stated music style exactly. If they say "gypsy punk" write gypsy punk.
If no style is given, use dry wit and light irony (Randy Newman / Flight of the Conchords flavor).

CRITICAL TIMING RULE: ACE-Step begins your lyrics on beat 1, bar 1.
The FIRST word must be the FIRST thing sung -- no empty sections, no instrumental intros.

Format -- exactly THREE sections, EXACTLY TWO LINES EACH (no more, no less):
[verse]
Line 1 (7-9 syllables)
Line 2 (7-9 syllables)
[chorus]
Line 1 (6-9 syllables)
Line 2 (6-9 syllables)
[verse]
Line 1 (7-9 syllables)
Line 2 (7-9 syllables)

STRICT RULES:
- Exactly 3 sections, exactly 2 lines per section = 6 lines total
- NEVER write 3 or 4 lines in a section -- 2 lines only
- Loose rhyme: AABB or ABAB
- Under 50 words total -- ACE-Step needs room to breathe between sections
- Return ONLY the raw lyrics text -- no JSON, no quotes, no explanation, no section numbering"""


def generate_video_prompt_auto(router, user_direction: str = "", subject_hint: str = "") -> str:
    """Generate a kinetic motion prompt with no image -- fast cloud text call.

    Used when the user hasn't written a video prompt and a cloud API is active
    (we can't send potentially NSFW images to cloud vision models).
    Returns a raw prompt string, empty on failure.
    """
    parts = ["Write an explosive motion prompt for a short AI-generated video clip."]
    if subject_hint:
        parts.append(f"Subject/scene: {subject_hint}")
    if user_direction:
        parts.append(f"Creative direction: {user_direction}")
    if not subject_hint and not user_direction:
        parts.append("Subject: a person in a dramatic, dynamic scene.")
    parts.append("Make it kinetic -- every element should be in violent, expressive motion.")
    try:
        text = router.route(
            [{"role": "user", "content": "\n".join(parts)}],
            tier=TIER_FAST,
            system=VIDEO_PROMPT_AUTO_SYSTEM,
            max_tokens=120,
        )
        return (text or "").strip()
    except Exception as e:
        log.warning("Auto video prompt generation failed: %s", e)
        return ""


_ANALYSIS_FALLBACK = {
    "title": "Unknown scene",
    "scene_description": "A photograph submitted for video generation.",
    "mood": "neutral",
    "setting": "unknown",
    "subjects": ["subject"],
    "dominant_colors": ["unknown"],
    "visual_style": "photographic",
    "energy": "moderate",
}

def analyze_photo(router, image_b64: str) -> dict:
    """Analyze a photo and return scene understanding. Returns fallback on failure."""
    try:
        text = router.route_vision(
            "Analyze this photograph for a creative video project.",
            [image_b64],
            tier=TIER_BALANCED,
            system=ANALYSIS_SYSTEM,
            force_provider="ollama",  # user images may be NSFW; cloud APIs refuse them
            format_json=True,
        )
        result = parse_json_response(text)
        if result and isinstance(result, dict):
            return result
    except Exception as e:
        log.warning("analyze_photo failed: %s", e)
    log.warning("analyze_photo: returning fallback scene description")
    return dict(_ANALYSIS_FALLBACK)


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
        # format_json=True forces JSON-only token sampling from qwen3-vl,
        # so responses are compact; 512 tokens is ample for 4 structured prompts.
        actual_max_tokens = min(max_tokens, 512)
        last_text = router.route_vision(
            "\n\n".join(prompt_parts),
            [image_b64],
            tier=tier,
            max_tokens=actual_max_tokens,
            system=VIDEO_PROMPT_SYSTEM,
            force_provider=force_provider or "ollama",  # user images may be NSFW
            format_json=True,
        )
        result = parse_json_response(last_text)
        if result and isinstance(result, dict) and result.get("prompts"):
            return result
        # Model returned a top-level array instead of {"prompts": [...]} -- common
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


def generate_lyrics(router, video_frames_b64: list[str], music_prompt: str = "", user_direction: str = "",
                    scene_description: str = "") -> str:
    """Auto-generate ironic/satirical lyrics for a video.

    Uses text-only routing (fast, no vision model) -- the music_prompt and
    scene_description provide enough context. Passing frames triggers qwen3-vl
    which is 4-5x slower due to thinking-mode overhead.
    """
    parts = ["Write sardonic song lyrics matching this music and scene."]
    if music_prompt:
        parts.append(f'Music style: "{music_prompt}"')
    if scene_description:
        parts.append(f'Scene: "{scene_description}"')
    elif user_direction:
        parts.append(f'Creative direction: "{user_direction}"')
    if user_direction and scene_description:
        parts.append(f'Creative direction: "{user_direction}"')

    prompt = "\n\n".join(parts)
    try:
        text = router.route(
            [{"role": "user", "content": prompt}],
            tier=TIER_FAST,
            system=LYRICS_SYSTEM,
            max_tokens=200,  # ~150 tokens output keeps ACE-Step under its KV block limit
        )
        return text.strip() if text else ""
    except Exception as e:
        log.warning("Lyrics generation failed: %s", e)
        return ""


def generate_music_prompt(router, video_frames_b64: list[str], user_direction: str = "",
                          video_prompt: str = "") -> dict:
    """Suggest music direction for a video.

    When frames are provided and Ollama is the active provider, sends them for
    visual analysis.  When no frames are given (or a cloud provider is active),
    falls back to text-only generation from user_direction + video_prompt -- cloud
    APIs refuse NSFW images so we never send frames to them.
    """
    use_vision = bool(video_frames_b64) and router._provider() == "ollama"

    if use_vision:
        prompt = "Analyze these frames from a generated video and suggest matching background music."
        if user_direction:
            prompt += f'\nCreative direction: "{user_direction}"'
        try:
            text = router.route_vision(
                prompt, video_frames_b64,
                tier=TIER_BALANCED, system=MUSIC_PROMPT_SYSTEM,
                force_provider="ollama",
                format_json=True,
            )
            result = parse_json_response(text)
            return result or {"music_prompt": "indie folk, fingerpicked acoustic guitar, upright bass, brushed drums", "bpm": 80}
        except Exception as e:
            log.warning("Music prompt vision call failed, falling back to text: %s", e)

    # Text-only path -- fast on cloud APIs, also used as fallback when vision fails
    parts = ["Suggest background music for a short AI-generated video."]
    if video_prompt:
        parts.append(f'Video description: "{video_prompt}"')
    if user_direction:
        parts.append(f'Creative direction: "{user_direction}"')
    if not video_prompt and not user_direction:
        parts.append("The video is a creative short clip.")
    parts.append("Return JSON with music_prompt, bpm, key_suggestion, and reasoning fields.")
    try:
        text = router.route(
            [{"role": "user", "content": "\n".join(parts)}],
            tier=TIER_FAST, system=MUSIC_PROMPT_SYSTEM, max_tokens=300,
        )
        result = parse_json_response(text)
        return result or {"music_prompt": "indie folk, fingerpicked acoustic guitar, upright bass, brushed drums", "bpm": 80}
    except Exception as e:
        log.warning("Music prompt text generation failed: %s", e)
        return {"music_prompt": "indie folk, fingerpicked acoustic guitar, upright bass, brushed drums", "bpm": 80, "error": str(e)}
