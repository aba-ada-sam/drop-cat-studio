"""SD Prompt generation engine — generates Stable Diffusion prompts from images.

Ported from DropCatGo-SD-Prompts/wildcard_studio.py (Gradio -> REST).
Uses Ollama (via llm_router) to analyze images and generate regional prompts.
"""
import logging
import re

from core.llm_client import encode_image_b64, TIER_POWER

log = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are an expert Stable Diffusion prompt engineer. You write prompts using
comma-separated tags — NO natural language narration. Every element is a tag.

FORMAT:
1. First output a section headed ## PROMPT with a clean, general SD prompt
   describing the whole image (quality tags, subject, scene, mood, lighting).
2. Then output ## COLUMNS with 3 regional columns separated by the line
   ---COL_SEP---
   Each column is a focused SD-tag description for LEFT, CENTER, RIGHT regions.
   Columns use the BREAK keyword syntax for regional prompt systems.

WILDCARDS: When the user provides wildcard names, weave them naturally into
the prompt using __name__ syntax. Make them enhance variety, not force-fit.

Be creative, vivid, and precise. Use SD vocabulary: quality tags, artist
references, lighting terms, camera angles, material descriptions."""

FORMAT_REMINDER = """Please keep the same format:

## PROMPT
(base SD prompt with quality tags)

## COLUMNS
(left region tags)
---COL_SEP---
(center region tags)
---COL_SEP---
(right region tags)"""

# Populated dynamically at runtime via /api/prompts/models
AVAILABLE_MODELS = ["ollama"]


def _parse_sections(raw: str) -> dict:
    """Parse ## PROMPT and ## COLUMNS sections from the response."""
    result = {"raw": raw, "base_prompt": "", "columns": ["", "", ""]}

    prompt_match = re.search(r"##\s*PROMPT\s*\n(.*?)(?=##\s*COLUMNS|\Z)", raw, re.DOTALL | re.IGNORECASE)
    if prompt_match:
        result["base_prompt"] = prompt_match.group(1).strip()

    columns_match = re.search(r"##\s*COLUMNS\s*\n(.*)", raw, re.DOTALL | re.IGNORECASE)
    if columns_match:
        cols_text = columns_match.group(1).strip()
        parts = re.split(r"---COL_SEP---", cols_text)
        for i, part in enumerate(parts[:3]):
            result["columns"][i] = part.strip()

    return result


def generate_prompts(
    llm_router,
    image_path: str | None = None,
    concept: str = "",
    wildcard_labels: list[str] | None = None,
    wildcard_samples: dict | None = None,
    extra_instructions: str = "",
    model: str = "ollama",  # kept for API compat, tier used instead
) -> tuple[dict, dict]:
    """Generate SD prompts from an image and/or concept.

    Returns: (parsed_sections, conv_state)
    conv_state stores plain-text message history for multi-turn refine calls.
    """
    text_parts = []

    if concept:
        text_parts.append(f"CONCEPT: {concept}")
    if image_path:
        text_parts.append("I've attached a reference image. Analyze it and create prompts based on it.")

    if wildcard_labels:
        wc_block = "AVAILABLE WILDCARDS (use __name__ syntax where appropriate):\n"
        for label in wildcard_labels[:30]:
            samples = (wildcard_samples or {}).get(label, [])
            sample_str = ", ".join(samples[:5]) if samples else ""
            wc_block += f"  __{label}__ -- {sample_str}\n"
        text_parts.append(wc_block)

    if extra_instructions:
        text_parts.append(f"EXTRA INSTRUCTIONS: {extra_instructions}")

    text_parts.append(FORMAT_REMINDER)
    prompt_text = "\n\n".join(text_parts)

    # Vision call if image provided, text-only otherwise
    if image_path:
        b64 = encode_image_b64(image_path)
        if b64:
            raw = llm_router.route_vision(
                prompt_text, [b64],
                tier=TIER_POWER, max_tokens=4096, system=SYSTEM_PROMPT,
            )
        else:
            raw = llm_router.route(
                [{"role": "user", "content": prompt_text}],
                tier=TIER_POWER, max_tokens=4096, system=SYSTEM_PROMPT,
            )
    else:
        raw = llm_router.route(
            [{"role": "user", "content": prompt_text}],
            tier=TIER_POWER, max_tokens=4096, system=SYSTEM_PROMPT,
        )

    parsed = _parse_sections(raw)

    # Store plain-text conversation for refine (no image needed in follow-ups)
    conv_state = {
        "messages": [{"role": "user", "content": prompt_text}],
        "last_response": raw,
    }

    return parsed, conv_state


def refine_prompts(
    llm_router,
    conv_state: dict,
    feedback: str,
    model: str = "ollama",  # kept for API compat
) -> tuple[dict, dict]:
    """Refine prompts via multi-turn conversation.

    Returns: (parsed_sections, new_conv_state)
    """
    messages = list(conv_state.get("messages", []))
    messages.append({"role": "assistant", "content": conv_state.get("last_response", "")})
    messages.append({
        "role": "user",
        "content": f"{feedback}\n\nPlease revise the prompts accordingly.\n\n{FORMAT_REMINDER}",
    })

    raw = llm_router.route(
        messages,
        tier=TIER_POWER, max_tokens=4096, system=SYSTEM_PROMPT,
    )

    parsed = _parse_sections(raw)
    new_state = {"messages": messages, "last_response": raw}

    return parsed, new_state
