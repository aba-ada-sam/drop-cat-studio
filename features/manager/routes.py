"""AI Manager routes -- /api/manager/*

A single-step "brain" endpoint. The browser owns the agent loop: it sends the
goal + the current on-screen state + what it has done so far, and we return the
ONE next action to take. The browser executes that action against the live UI
(navigate / read / set a field / click / narrate / ask / done), snapshots the
new screen, and calls back for the next step. This keeps all DOM control on the
client (real clicks the user can watch) and keeps the server a thin LLM proxy.
"""
import asyncio
import json
import logging

from fastapi import APIRouter, HTTPException, Request

from core.llm_router import TIER_BALANCED
from core.llm_client import parse_json_response

log = logging.getLogger(__name__)
router = APIRouter()


# A concise map of the app so the agent knows where things live and which tab
# to navigate to for a given goal. Kept short on purpose -- the live screen read
# supplies the actual controls.
APP_MAP = """\
Drop Cat Go Studio turns photos into AI videos with music. Tabs (navigate by id):
- pipeline      "Studio Home"    -- launcher; pick what to create. Start here when unsure.
- express       "Quick Video"    -- one photo + an idea -> a short AI video clip. Fastest path.
- create-videos "Create Videos"  -- animate images into video + add AI music.
- music-video   "Music Video"    -- a song + a folder of photos -> full-length music video.
- bridges       "Video Bridges"  -- 2+ clips -> AI transition clips between them.
- video-tools   "Video Tools"    -- add music, trim, reverse, speed, upscale, smooth.
- queue         "Queue"          -- running/pending generations. Go here to watch progress.
- gallery       "Gallery"        -- browse everything generated so far.

GPU video/music jobs take 5-20 minutes. After you start one, navigate to 'queue'
and use read_screen to watch progress; narrate updates with 'say'."""


SYSTEM = f"""You are the Manager: an autonomous assistant embedded INSIDE the Drop Cat \
Go Studio desktop app. You operate the app FOR the user by clicking and filling \
its real controls, exactly as a human would. The user has given you full \
permission to act on their behalf -- do NOT ask for confirmation before doing \
something or starting a generation. Just do it, and narrate as you go.

{APP_MAP}

HOW YOU WORK
You act ONE step at a time. Each step you receive: the user's GOAL, the current \
SCREEN (the controls and buttons visible on the active tab, each with a stable \
"ref" id), and the HISTORY of what you've already done this task. You reply with \
your reasoning plus exactly ONE next action.

ACTIONS (reply with a single JSON object, nothing else):
{{"thought": "<one short sentence of reasoning>", "action": {{ ... }}}}

action shapes:
- {{"type":"navigate","tab":"<tab-id>"}}            -- switch to a tab (ids above)
- {{"type":"read_screen"}}                          -- re-read controls after a change/navigation
- {{"type":"set_field","ref":"<ref>","value":"<v>"}} -- type into / set a control by its ref
- {{"type":"click","ref":"<ref>"}}                   -- click a button or chip by its ref
- {{"type":"say","message":"<text>"}}               -- tell the user what you're doing (loop continues)
- {{"type":"ask","question":"<text>"}}              -- you NEED info you cannot see; pause for the user
- {{"type":"done","summary":"<text>"}}              -- the goal is complete; stop

RULES
1. Refs are only valid from the most recent SCREEN. After you navigate or click \
something that changes the page, do read_screen before set_field/click.
2. To set a chips/segmented control, click the chip whose label matches the value, \
or use set_field with the option text -- prefer click on the matching button ref.
3. Use 'say' liberally so the user can follow along, but don't narrate every trivial \
step -- group your narration.
4. Only use 'ask' when you are blocked on information that is genuinely not on screen \
and not inferable (e.g. which file to use, an ambiguous choice). Never use 'ask' just \
to confirm permission -- you already have it.
5. After starting a long generation, navigate to 'queue', read_screen to confirm it's \
running, say so, then 'done'. Do not poll forever.
6. If the same action keeps producing no change, stop looping: explain the obstacle \
with 'say' and then 'ask' or 'done'.
7. Keep moving toward the GOAL. When it is achieved (or queued and running), use 'done'.
Output ONLY the JSON object."""


def _fmt_screen(screen: dict) -> str:
    if not screen:
        return "(no screen captured yet -- start with read_screen or navigate)"
    try:
        return json.dumps(screen, indent=2, default=str)[:6000]
    except Exception:
        return str(screen)[:6000]


def _fmt_history(history: list) -> str:
    if not history:
        return "(nothing done yet)"
    lines = []
    for i, h in enumerate(history[-24:], 1):
        act = h.get("action") or {}
        res = h.get("result")
        atype = act.get("type", "?")
        detail = {k: v for k, v in act.items() if k != "type"}
        line = f"{i}. {atype} {json.dumps(detail, default=str)}"
        if res:
            line += f"  -> {str(res)[:300]}"
        lines.append(line)
    return "\n".join(lines)


@router.post("/think")
async def manager_think(request: Request):
    """Return the single next action for the in-browser agent loop."""
    body = await request.json()
    goal = (body.get("goal") or "").strip()
    history = body.get("history") or []
    screen = body.get("screen") or {}
    chat = body.get("chat") or []
    if not goal:
        raise HTTPException(400, "goal required")

    convo = ""
    if chat:
        convo = "RECENT CONVERSATION:\n" + "\n".join(
            f"{(m.get('role') or '?')}: {m.get('text') or ''}" for m in chat[-8:]
        ) + "\n\n"

    user_msg = (
        f"{convo}"
        f"GOAL: {goal}\n\n"
        f"CURRENT SCREEN:\n{_fmt_screen(screen)}\n\n"
        f"WHAT YOU'VE DONE THIS TASK:\n{_fmt_history(history)}\n\n"
        f"Reply with your single next action as JSON."
    )

    from app import get_llm_router
    llm = get_llm_router()

    def _call(force):
        return llm.route(
            [{"role": "user", "content": user_msg}],
            tier=TIER_BALANCED,
            max_tokens=700,
            system=SYSTEM,
            force_provider=force,
        )

    # The user picked Claude to power the Manager. Prefer Anthropic; fall back to
    # the configured provider if the key is missing or the call fails.
    raw = None
    try:
        raw = await asyncio.to_thread(_call, "anthropic")
    except Exception as e:
        log.warning("[manager] anthropic call failed (%s); falling back to configured provider", e)
        try:
            raw = await asyncio.to_thread(_call, None)
        except Exception as e2:
            log.exception("[manager] think failed")
            raise HTTPException(500, f"manager think failed: {e2}")

    parsed = parse_json_response(raw or "")
    if not isinstance(parsed, dict):
        # Model didn't return clean JSON -- surface its text to the user as a 'say'
        # so the loop can decide what to do rather than hard-failing.
        text = (raw or "").strip()[:500] or "I couldn't form a next step."
        return {"thought": "", "action": {"type": "say", "message": text}}

    thought = (parsed.get("thought") or "").strip()
    action = parsed.get("action")
    if not isinstance(action, dict) or not action.get("type"):
        # Some models emit the action fields flat. Salvage what we can.
        flat = {k: v for k, v in parsed.items() if k != "thought"}
        if flat.get("type"):
            action = flat
        else:
            action = {"type": "say", "message": thought or "Thinking..."}

    try:
        provider_used = llm._provider("anthropic")  # noqa: SLF001
    except Exception:
        provider_used = "auto"

    return {"thought": thought, "action": action, "provider_used": provider_used}
