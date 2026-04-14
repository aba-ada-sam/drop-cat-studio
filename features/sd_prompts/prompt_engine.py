"""SD Prompt generation engine — generates Stable Diffusion prompts from images.

Ported from DropCatGo-SD-Prompts/wildcard_studio.py (Gradio -> REST).
Uses Ollama (via llm_router) to analyze images and generate regional prompts.
"""
import logging
import re

from core.llm_client import encode_image_b64, TIER_POWER

log = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are a creative Stable Diffusion prompt engineer embedded in a chat interface. The user talks to you naturally and you improve their prompts.

RESPONSE FORMAT — always output in this exact order:

REPLY: [1-2 sentences conversationally explaining what you changed and why. Be specific. E.g. "I've made the left column more gothic by adding stone archways and candlelight. The center now features a more dramatic figure pose."]

## PROMPT
[10-18 comma-separated SD tags — quality, subject, scene, mood, lighting. NO sentences.]

## COLUMNS
[4-8 tags for left/first region]
---COL_SEP---
[4-8 tags for center/second region]
---COL_SEP---
[4-8 tags for right/third region]

TAG RULES:
- Tags only: "woman, standing" not "a woman standing in..."
- No repeated tags between base prompt and columns
- No filler or padding
- If the user's grid has more/fewer columns, add/remove ---COL_SEP--- sections accordingly

WILDCARDS: use __name__ syntax when wildcard names are provided.

WILDCARD CREATION: If the user asks you to create a wildcard or add entries to one, append this section AFTER COLUMNS:

## CREATE_WILDCARD
name: snake_case_name
entries:
entry one
entry two
entry three

Rules: one entry per line, no commas within entries, 10-30 entries, descriptive and varied. Only include this section when explicitly asked."""

FORMAT_REMINDER = """Respond in this format:

REPLY: [short conversational explanation of what you changed]

## PROMPT
tag1, tag2, tag3, ...

## COLUMNS
region 1 tags
---COL_SEP---
region 2 tags
---COL_SEP---
region 3 tags
(add or remove ---COL_SEP--- sections to match the requested number of regions)"""

# Populated dynamically at runtime via /api/prompts/models
AVAILABLE_MODELS = ["ollama"]


def _parse_sections(raw: str) -> dict:
    """Parse ## PROMPT and ## COLUMNS sections from the response.

    Robust: tries multiple header styles and separators. If structure is
    not found at all, the entire raw output becomes the base_prompt so the
    user can at least see and edit it.
    """
    result = {"raw": raw, "base_prompt": "", "columns": ["", "", ""], "chat_reply": "", "create_wildcard": None}

    # Extract REPLY: line (conversational response)
    reply_match = re.search(r"REPLY\s*:\s*(.+?)(?=\n|$)", raw, re.IGNORECASE)
    if reply_match:
        result["chat_reply"] = reply_match.group(1).strip()

    # Try to find ## PROMPT (or **PROMPT** or PROMPT:)
    prompt_match = re.search(
        r"(?:##\s*PROMPT|(?:\*\*)?PROMPT(?:\*\*)?)\s*:?\s*\n(.*?)(?=(?:##\s*COLUMNS|(?:\*\*)?COLUMNS(?:\*\*)?)\s*:?|\Z)",
        raw, re.DOTALL | re.IGNORECASE,
    )
    if prompt_match:
        result["base_prompt"] = prompt_match.group(1).strip()

    # Try to find ## COLUMNS (or **COLUMNS** or COLUMNS:)
    columns_match = re.search(
        r"(?:##\s*COLUMNS|(?:\*\*)?COLUMNS(?:\*\*)?)\s*:?\s*\n(.*)",
        raw, re.DOTALL | re.IGNORECASE,
    )
    if columns_match:
        cols_text = columns_match.group(1).strip()
        # Try ---COL_SEP---, then ### LEFT/CENTER/RIGHT, then numbered headers
        parts = re.split(r"---COL_SEP---", cols_text)
        if len(parts) < 3:
            parts = re.split(r"(?:###?\s*(?:LEFT|CENTER|RIGHT|Column\s*\d)[^\n]*\n)", cols_text, flags=re.IGNORECASE)
            parts = [p.strip() for p in parts if p.strip()]
        for i, part in enumerate(parts[:3]):
            result["columns"][i] = part.strip()

    # Parse CREATE_WILDCARD intent (guarded — most responses never contain this)
    wc_match = "CREATE_WILDCARD" in raw and re.search(
        r"##\s*CREATE_WILDCARD\s*\n(.*?)(?=##\s*|\Z)", raw, re.DOTALL | re.IGNORECASE
    )
    if wc_match:
        wc_text = wc_match.group(1).strip()
        name_m = re.search(r"name\s*:\s*(\S+)", wc_text, re.IGNORECASE)
        entries_m = re.search(r"entries\s*:\s*\n(.*)", wc_text, re.DOTALL | re.IGNORECASE)
        if name_m:
            entries = []
            if entries_m:
                entries = [ln.strip() for ln in entries_m.group(1).splitlines() if ln.strip()]
            result["create_wildcard"] = {"name": name_m.group(1).strip().lower(), "entries": entries}

    # Fallback: if parser found nothing, put raw text in base_prompt
    if not result["base_prompt"] and not any(result["columns"]):
        log.warning("LLM output did not match expected format — using raw output as base prompt")
        result["base_prompt"] = raw.strip()

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
                tier=TIER_POWER, max_tokens=1024, system=SYSTEM_PROMPT,
            )
        else:
            raw = llm_router.route(
                [{"role": "user", "content": prompt_text}],
                tier=TIER_POWER, max_tokens=1024, system=SYSTEM_PROMPT,
            )
    else:
        raw = llm_router.route(
            [{"role": "user", "content": prompt_text}],
            tier=TIER_POWER, max_tokens=1024, system=SYSTEM_PROMPT,
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
        tier=TIER_POWER, max_tokens=1024, system=SYSTEM_PROMPT,
    )

    parsed = _parse_sections(raw)
    new_state = {"messages": messages, "last_response": raw}

    return parsed, new_state
