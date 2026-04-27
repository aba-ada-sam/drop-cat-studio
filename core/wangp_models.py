"""Shared WanGP model definitions — single source of truth.

Previously duplicated (and diverging) between services/wan_bridge_client.py and
services/wangp_worker.py. Both files now import from here. (BUG-02 / FLW-04)
"""

MODEL_MAP: dict[str, str] = {
    "Wan2.1-T2V-1.3B":          "t2v_1.3B",
    "Wan2.1-T2V-14B":           "t2v",
    "Wan2.1-I2V-14B-480P":      "i2v",
    "Wan2.1-I2V-14B-720P":      "i2v_720p",
    "Wan2.1-VACE-1.3B":         "vace_1.3B",
    "LTX-2 Dev19B Distilled":   "ltx2_distilled",
    "LTX-2 Dev13B":             "ltxv_13B",
}


def resolve_model_name(model_name: str) -> str:
    """Map a user-facing model name to the internal WanGP model type string."""
    if model_name in MODEL_MAP:
        return MODEL_MAP[model_name]
    lowered = (model_name or "").lower()
    if "ltx" in lowered:
        if "distilled" in lowered:
            return "ltx2_distilled"
        if "13" in lowered:
            return "ltxv_13B"
        return "ltx2_19B"
    if "vace" in lowered and "1.3" in lowered:
        return "vace_1.3B"
    if "i2v" in lowered and "720" in lowered:
        return "i2v_720p"
    if "i2v" in lowered:
        return "i2v"
    if "1.3" in lowered:
        return "t2v_1.3B"
    return "t2v"


def build_state(model_type: str) -> dict:
    """Build a fresh WanGP state dict for the given model type."""
    return {
        "active_form": "add",
        "model_type": model_type,
        "gen": {
            "queue": [], "in_progress": False,
            "file_list": [], "file_settings_list": [],
            "audio_file_list": [], "audio_file_settings_list": [],
            "selected": 0, "audio_selected": 0,
            "prompt_no": 0, "prompts_max": 0,
            "repeat_no": 0, "total_generation": 1,
            "window_no": 0, "total_windows": 0,
            "progress_status": "", "process_status": "process:main",
        },
        "loras": [],
        "last_model_per_family": {},
        "last_model_per_type": {},
    }


# Default values for all optional generation parameters.
# Keeps both wan_bridge_client.py and wangp_worker.py in sync.
SAFE_DEFAULTS: dict = {
    "image_refs": [],
    "image_guide": [],
    "image_mask": [],
    "video_guide": [],
    "video_source": [],
    "video_mask": [],
    "video_guide_outpainting": [],
    "audio_source": None,
    "audio_guide": None,
    "audio_guide2": None,
    "custom_guide": None,
    "audio_prompt_type": "",
    "MMAudio_setting": 0,
    "image_mode": 0,        # 0=video output, >0=image output -- always want video
    "model_mode": "",
    "activated_loras": [],
    "loras_multipliers": [],
    "custom_settings": {},
    "self_refiner_plan": "",
    "self_refiner_setting": "",
    "spatial_upsampling": "",
    "skip_steps_cache_type": "",
    "speakers_locations": "",
    "frames_positions": "",
    "guidance_phases": 0,
    "motion_amplitude": 1,    # WanGP rejects values < 1
    "denoising_strength": 1.0,
    "masking_strength": 1.0,
    "model_switch_phase": 0,
    "switch_threshold": 0,
    "switch_threshold2": 0,
    "keep_frames_video_guide": "",
    "keep_frames_video_source": "",
    "force_fps": "",
    # WanGP validates these even when sliding window isn't actually needed.
    "sliding_window_size": 129,   # WanGP UI default; only activates if video > this frame count
    "sliding_window_overlap": 17,
    "sliding_window_discard_last_frames": 0,
    # Keys accessed directly via inputs["key"] in wgp.py — must be present or KeyError
    "multi_images_gen_type": 0,
    "image_quality": "",
    "video_quality": "",
    "base_model_type": "",
    "lset_name": "",
    "model_filename": "",
    "modules": [],
    "settings_version": 0,
    "type": "",
}
