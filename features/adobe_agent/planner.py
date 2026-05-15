"""AI planner: natural-language goal -> structured task list via Claude."""
import json
import logging

log = logging.getLogger(__name__)

_SYSTEM = """You are an expert Adobe Premiere Pro and After Effects operator.
The user will describe what they want to accomplish in their Adobe project.
Output a JSON array of task steps. No markdown, no explanation -- only valid JSON.

Each step shape:
  { "app": "premiere"|"aftereffects", "op": "<name>", "args": {...},
    "label": "plain-English description", "abort_on_error": true }

PREMIERE operations (op: args):
  project_info: {}
  sequence_info: {}
  set_active_sequence: {name}
  import_file: {path}
  add_clip: {name, track, time_secs}
  trim_clip: {name, start_secs, end_secs}
  move_clip: {name, time_secs}
  delete_clip: {name, ripple}
  set_clip_speed: {name, speed}  -- 0.5=half, 2.0=double
  add_transition: {transition, track_index, cut_time_secs, duration_secs}
    transitions: cross_dissolve | dip_to_black | film_dissolve | additive_dissolve
  set_opacity: {name, opacity}  -- 0-100
  set_audio_level: {name, db}
  add_marker: {name, time_secs, comment}
  apply_effect: {clip_name, effect_name}
  export_sequence: {output_path, preset_path}

AFTER EFFECTS operations (op: args):
  project_info: {}
  composition_info: {comp_name?}
  new_comp: {name, width, height, duration, framerate}
  import_file: {path, sequence?}
  add_layer: {source_name, start_time?, duration?, layer_name?}
  add_solid: {name, color:[r,g,b 0-1], duration?}
  add_text: {text, layer_name?, font_size?, color:[r,g,b]?, start_time?, duration?}
  add_null: {name, duration?}
  set_position: {layer_name, x, y}
  set_scale: {layer_name, scale}  -- percent
  set_opacity: {layer_name, opacity}  -- 0-100
  set_rotation: {layer_name, rotation}  -- degrees
  add_keyframe: {layer_name, property, time, value}
  set_expression: {layer_name, property, expression}
  apply_effect: {layer_name, effect_name, params?}
  set_effect_param: {layer_name, effect_name, param, value}
  set_parent: {layer_name, parent_name}
  trim_layer: {layer_name, in_point?, out_point?}
  render_comp: {comp_name?, output_path?, template?}
  save_project: {}

Rules:
- Output ONLY the JSON array. No other text.
- Times are always seconds (float). Colors are [r,g,b] with 0.0-1.0 channels.
- Start with info-gathering (project_info, sequence_info) before making edits.
- If goal is ambiguous, add markers to flag decision points rather than guessing.
- Paths use forward slashes.
"""


def plan_goal(goal: str, llm_router) -> list[dict]:
    """Call Claude via the DCS LLM router to produce a task list."""
    log.info("[adobe-planner] Planning: %s", goal[:80])
    raw = llm_router.route(
        user_msg=goal,
        system_msg=_SYSTEM,
        tier="power",
    )
    # Strip markdown fences if model wrapped output
    text = raw.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1].rsplit("```", 1)[0].strip()
    tasks = json.loads(text)
    log.info("[adobe-planner] Got %d tasks", len(tasks))
    return tasks
