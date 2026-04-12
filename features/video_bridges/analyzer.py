"""Video analysis and bridge prompt generation for Video Bridges.

Analyzes clips and generates creative transition prompts between them.
Ported from DropCatGo-Video-BRIDGES/analyzer.py.
"""
import logging

from core.llm_client import TIER_BALANCED, TIER_POWER, parse_json_response
from core.ffmpeg_utils import extract_frame_b64

log = logging.getLogger(__name__)

ANALYSIS_SYSTEM = """You are a professional video editor and cinematographer analyzing footage.
Given a sequence of frames from a video clip or a still image, provide a concise JSON analysis. Return ONLY valid JSON with this exact structure:
{
  "title": "short descriptive title (max 6 words)",
  "scene_description": "1-2 sentence description of what's in the video",
  "mood": "dominant mood/atmosphere (e.g. serene, intense, playful, dramatic)",
  "setting": "location/environment type (e.g. outdoor-nature, indoor-office, urban)",
  "visual_style": "cinematographic style notes",
  "dominant_colors": ["color1", "color2", "color3"],
  "motion_level": "still | slow | moderate | fast | chaotic",
  "bridge_notes": "brief advice for what kind of transition INTO or OUT OF this clip works best",
  "timeline": [
    {"time": 0.0, "description": "brief note about what happens at this moment"}
  ]
}
Include 3-5 timeline entries at key moments."""

BRIDGE_PROMPT_SYSTEM = """You are a visionary motion designer who writes prompts for AI video generation.
Your specialty is finding unexpected visual connections between two scenes and describing creative
transformations — shapes morphing into other shapes, colors bleeding between worlds, objects
metamorphosing into something new. You never describe static scenes. You only describe motion,
change, and transformation. Output ONLY the prompt text — no JSON, no markdown, no explanation."""

TRANSITION_STYLES = {
    "continuity": "smooth motion continuity — match speed, direction, and energy so the cut is invisible",
    "cinematic": "cinematic camera movement with atmospheric transformation — push, pull, or arc through the scene change",
    "kinetic": "high-energy directional motion — velocity carries visual elements from one scene into the next",
    "surreal": "dreamlike morph — impossible physics, organic shape-shifting, reality bending between scenes",
    "meld": "full melt morph — textures liquefy, surfaces warp and flow, one material becomes the other",
    "morph": "full melt morph — textures liquefy, surfaces warp and flow, one material becomes the other",
    "shape_match": "shape-and-color matching — contours align first, then texture and color fields transfer",
    "fade": "minimal linear dissolve — preserve scene geometry, avoid extra stylistic effects",
}


def analyze_media(router, media_path: str, frames_b64: list[str] | None = None) -> dict:
    """Analyze a video or image for scene understanding."""
    if not frames_b64:
        # Extract frames at various positions
        frames_b64 = []
        for pos in [0.1, 0.25, 0.5, 0.75, 0.9]:
            b64 = extract_frame_b64(media_path, position=pos, max_dim=320)
            if b64:
                frames_b64.append(b64)
        if not frames_b64:
            # Try as image
            b64 = extract_frame_b64(media_path, position=0.5, max_dim=320)
            if b64:
                frames_b64 = [b64]

    if not frames_b64:
        return {"error": "Could not extract frames", "title": "Unknown",
                "scene_description": "Unknown content", "mood": "neutral"}

    text = router.route_vision(
        "Analyze this video/image clip for a transition project.",
        frames_b64,
        tier=TIER_BALANCED,
        system=ANALYSIS_SYSTEM,
    )
    result = parse_json_response(text)
    if not result:
        raise RuntimeError("AI returned unparseable response for clip analysis")
    return result


def generate_bridge_prompt(
    router,
    analysis_a: dict,
    analysis_b: dict,
    frame_b64_a: str | None,
    frame_b64_b: str | None,
    transition_mode: str = "cinematic",
    prompt_mode: str = "ai_informed",
    creativity: float = 7.0,
    user_guidance: str = "",
) -> str:
    """Generate a creative bridge prompt connecting two scenes."""
    style_desc = TRANSITION_STYLES.get(transition_mode, TRANSITION_STYLES["cinematic"])

    if prompt_mode == "direct":
        # Simple concatenation, no AI call
        title_a = analysis_a.get("title", "Scene A")
        title_b = analysis_b.get("title", "Scene B")
        colors_a = ", ".join(analysis_a.get("dominant_colors", [])[:2])
        colors_b = ", ".join(analysis_b.get("dominant_colors", [])[:2])
        return (
            f"Smooth {transition_mode} transition from {title_a} "
            f"with {colors_a} tones morphing into {title_b} "
            f"with {colors_b} palette, continuous fluid motion"
        )

    # Build context for AI
    context = f"""CLIP A: {analysis_a.get('scene_description', 'Unknown')}
Mood: {analysis_a.get('mood', 'neutral')} | Motion: {analysis_a.get('motion_level', 'moderate')}
Bridge notes: {analysis_a.get('bridge_notes', 'none')}

CLIP B: {analysis_b.get('scene_description', 'Unknown')}
Mood: {analysis_b.get('mood', 'neutral')} | Motion: {analysis_b.get('motion_level', 'moderate')}
Bridge notes: {analysis_b.get('bridge_notes', 'none')}

TRANSITION STYLE: {style_desc}
CREATIVITY LEVEL: {creativity}/10"""

    if user_guidance:
        context += f'\nDIRECTOR NOTES: "{user_guidance}"'

    context += """

Write a single 20-35 word prompt describing the visual transformation
from Clip A to Clip B. Focus on MOTION and CHANGE only. No static descriptions."""

    images = []
    if frame_b64_a:
        images.append(frame_b64_a)
    if frame_b64_b:
        images.append(frame_b64_b)

    try:
        if images:
            text = router.route_vision(
                context, images,
                tier=TIER_POWER,
                system=BRIDGE_PROMPT_SYSTEM,
            )
        else:
            text = router.route(
                [{"role": "user", "content": context}],
                tier=TIER_POWER,
                system=BRIDGE_PROMPT_SYSTEM,
            )
        return text.strip()
    except Exception as e:
        log.warning("Bridge prompt generation failed: %s", e)
        return (
            f"Smooth fluid transformation with continuous motion, "
            f"colors shifting gradually, {transition_mode} style transition"
        )
