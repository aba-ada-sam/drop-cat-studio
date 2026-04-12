"""AI-powered wildcard file curation operations.

Ported from DropCatGo-SD-Prompts/wildcard_manager.py (Gradio -> REST).
Provides prune, expand, merge, audit, and auto-curator workflows via Ollama.
"""
import logging
import os
import re
from pathlib import Path

from core.llm_client import TIER_BALANCED, TIER_POWER

log = logging.getLogger(__name__)

PRUNE_SYSTEM = """You are curating Stable Diffusion wildcard files. Remove entries that
weaken the file's quality. Return 3 sections: ## REMOVED, ## KEPT, ## NOTES.
Each section lists entries one per line. If nothing to remove, say "none" under REMOVED."""

EXPAND_SYSTEM = """You are expanding a Stable Diffusion wildcard file with new creative entries.
Output ONLY new entries, one per line. No numbering, no bullets, no explanation.
Make them varied, vivid, and consistent with the existing file's style and purpose."""

AUDIT_SYSTEM = """You are auditing a Stable Diffusion wildcard library.
Give specific, actionable recommendations: what to merge, delete, split, or expand.
Name exact files. Be concise and direct."""

MERGE_SYSTEM = """You are merging Stable Diffusion wildcard files.
Output ONLY the merged entries, one per line. No numbering, no bullets.
Remove exact duplicates and near-duplicates; keep the most vivid and specific version."""

CURATOR_ANALYZE_SYSTEM = """You are an expert curator of Stable Diffusion wildcard libraries.
Analyze the library structure and content. Return markdown sections:
## ANALYSIS, ## QUESTIONS (for the user), ## PREVIEW (summary of planned changes)."""

CURATOR_PLAN_SYSTEM = """You are planning curation actions for a Stable Diffusion wildcard library.
Output ONLY pipe-delimited action lines, one per line. No other text.
Format: ACTION | target | parameters | reason
Actions: PRUNE, MERGE, EXPAND, RENAME, DELETE"""

AGGRESSIVENESS_MAP = {
    1: "only remove exact or near-exact duplicates",
    2: "remove duplicates and clearly redundant entries",
    3: "remove duplicates, redundant entries, and weak/generic entries",
    4: "aggressively remove anything not adding real variety or SD value",
    5: "ruthlessly prune -- keep only the strongest, most varied entries",
}


def _strip_numbering(line: str) -> str:
    """Remove leading numbers, bullets, etc."""
    line = re.sub(r"^\d+[\.\)]\s+", "", line)
    line = re.sub(r"^[-*]\s+", "", line)
    return line.strip()


def _read_file_lines(path: str) -> list[str]:
    """Read wildcard file, strip empties and comments."""
    try:
        return [
            line.strip()
            for line in Path(path).read_text(encoding="utf-8", errors="replace").splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
    except Exception:
        return []


def _write_entries(path: str, entries: list[str]):
    """Write entries to wildcard file."""
    Path(path).write_text("\n".join(entries) + "\n", encoding="utf-8")


def ai_prune(llm_router, label: str, entries: list[str], level: int = 3, model: str = "ollama") -> dict:
    """AI-powered deduplication and pruning.

    Returns: {"kept": [...], "removed": [...], "notes": str}
    """
    aggressiveness = AGGRESSIVENESS_MAP.get(level, AGGRESSIVENESS_MAP[3])
    content = f"""Wildcard file: {label}
Aggressiveness: {aggressiveness}

Entries ({len(entries)} total):
{chr(10).join(entries)}"""

    raw = llm_router.route(
        [{"role": "user", "content": content}],
        tier=TIER_BALANCED, max_tokens=4096, system=PRUNE_SYSTEM,
    )

    kept, removed, notes = [], [], ""
    current_section = None
    for line in raw.splitlines():
        lower = line.strip().lower()
        if "## removed" in lower:
            current_section = "removed"
        elif "## kept" in lower:
            current_section = "kept"
        elif "## notes" in lower:
            current_section = "notes"
        elif current_section == "removed" and line.strip() and line.strip().lower() != "none":
            removed.append(_strip_numbering(line))
        elif current_section == "kept" and line.strip():
            kept.append(_strip_numbering(line))
        elif current_section == "notes":
            notes += line + "\n"

    return {"kept": kept, "removed": removed, "notes": notes.strip()}


def ai_expand(llm_router, label: str, existing: list[str], count: int = 20, model: str = "ollama") -> list[str]:
    """Generate new entries matching the file's style."""
    import random
    sample = existing[:40] if len(existing) <= 40 else random.sample(existing, 40)

    content = f"""Wildcard file: {label}
Generate {count} new entries matching this file's style.

Existing entries (sample of {len(sample)}):
{chr(10).join(sample)}"""

    raw = llm_router.route(
        [{"role": "user", "content": content}],
        tier=TIER_BALANCED, max_tokens=4096, system=EXPAND_SYSTEM,
    )
    return [_strip_numbering(line) for line in raw.splitlines() if line.strip()]


def ai_merge(llm_router, files_data: list[tuple[str, list[str]]], model: str = "ollama") -> list[str]:
    """Merge multiple wildcard files with AI deduplication."""
    blocks = []
    for label, entries in files_data:
        blocks.append(f"=== {label} ===\n" + "\n".join(entries))

    content = "Merge these wildcard files, removing duplicates:\n\n" + "\n\n".join(blocks)

    raw = llm_router.route(
        [{"role": "user", "content": content}],
        tier=TIER_BALANCED, max_tokens=4096, system=MERGE_SYSTEM,
    )
    return [_strip_numbering(line) for line in raw.splitlines() if line.strip()]


def ai_audit(llm_router, entries_summary: str, model: str = "ollama") -> str:
    """Audit entire wildcard library."""
    return llm_router.route(
        [{"role": "user", "content": f"Audit this wildcard library:\n\n{entries_summary}"}],
        tier=TIER_POWER, max_tokens=4096, system=AUDIT_SYSTEM,
    )


def curator_analyze(llm_router, entries_summary: str, instructions: str = "", model: str = "ollama") -> str:
    """Phase 1: Analyze library and ask questions."""
    content = f"Analyze this wildcard library:\n\n{entries_summary}"
    if instructions:
        content += f"\n\nUser instructions: {instructions}"

    return llm_router.route(
        [{"role": "user", "content": content}],
        tier=TIER_POWER, max_tokens=4096, system=CURATOR_ANALYZE_SYSTEM,
    )


def curator_plan(llm_router, entries_summary: str, analysis: str, answers: str = "", instructions: str = "", model: str = "ollama") -> str:
    """Phase 2: Generate pipe-delimited action plan."""
    content = f"""Previous analysis:
{analysis}

User answers: {answers or '(none)'}
Instructions: {instructions or '(none)'}

Library:
{entries_summary}"""

    return llm_router.route(
        [{"role": "user", "content": content}],
        tier=TIER_POWER, max_tokens=4096, system=CURATOR_PLAN_SYSTEM,
    )


def parse_plan_actions(plan_text: str) -> list[dict]:
    """Parse pipe-delimited action plan into structured actions."""
    actions = []
    for line in plan_text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = [p.strip() for p in line.split("|")]
        if len(parts) < 2:
            continue
        action_type = parts[0].upper()
        if action_type in ("PRUNE", "MERGE", "EXPAND", "RENAME", "DELETE"):
            actions.append({
                "type": action_type,
                "target": parts[1] if len(parts) > 1 else "",
                "params": parts[2] if len(parts) > 2 else "",
                "reason": parts[3] if len(parts) > 3 else "",
            })
    return actions
