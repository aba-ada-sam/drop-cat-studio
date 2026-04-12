#!/usr/bin/env python3
"""Unified WanGP subprocess client — generates a single I2V video clip.

Merged from the three copies in Fun-Videos, BRIDGES, and Github Video Editor.
Called as a subprocess with WanGP's own Python environment.
"""

import argparse
import glob
import importlib.util
import os
import shutil
import sys
import time

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(SCRIPT_DIR)
if PROJECT_DIR not in sys.path:
    sys.path.insert(0, PROJECT_DIR)
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

# BUG-02/FLW-04: import from single source of truth in core/wangp_models.py
try:
    from core.wangp_models import MODEL_MAP, resolve_model_name, build_state, SAFE_DEFAULTS
except ImportError:
    from wangp_models import MODEL_MAP, resolve_model_name, build_state, SAFE_DEFAULTS


def find_generated_output(wgp, state, output_dir, before_files, started_at, desired_stem):
    candidates = []
    gen = wgp.get_gen_info(state)
    for path in gen.get("file_list", []):
        if isinstance(path, str) and path.lower().endswith(".mp4") and os.path.isfile(path):
            candidates.append(path)
    after_files = glob.glob(os.path.join(output_dir, "*.mp4"))
    for path in after_files:
        if path in before_files:
            continue
        try:
            if os.path.getmtime(path) + 1 >= started_at:
                candidates.append(path)
        except OSError:
            continue
    deduped = list(dict.fromkeys(candidates))
    if not deduped:
        return None
    exact = [p for p in deduped if os.path.splitext(os.path.basename(p))[0] == desired_stem]
    ranked = exact or deduped
    return max(ranked, key=lambda p: os.path.getmtime(p))


def _load_wangp(app_path):
    """Load wangp_runtime and then the wgp module."""
    runtime_path = os.path.join(PROJECT_DIR, "core", "wangp_runtime.py")
    if os.path.isfile(runtime_path):
        spec = importlib.util.spec_from_file_location("wangp_runtime", runtime_path)
        rt = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(rt)
        return rt.load_wangp_module(app_path)
    else:
        # Fallback: inline loading
        abs_path = os.path.abspath(app_path)
        if abs_path not in sys.path:
            sys.path.insert(0, abs_path)
        site_pkg = os.path.join(abs_path, "env", "Lib", "site-packages")
        if os.path.isdir(site_pkg) and site_pkg not in sys.path:
            sys.path.insert(0, site_pkg)
        import importlib as imp
        original_argv = sys.argv[:]
        original_cwd = os.getcwd()
        os.chdir(abs_path)
        sys.argv = [os.path.join(abs_path, "wgp.py")]
        try:
            return imp.import_module("wgp")
        finally:
            sys.argv = original_argv
            os.chdir(original_cwd)


def main():
    parser = argparse.ArgumentParser(description="Generate one WanGP I2V clip")
    parser.add_argument("--wangp-app", required=True)
    parser.add_argument("--prompt", required=True)
    parser.add_argument("--output_path", required=True)
    parser.add_argument("--num_frames", required=True, type=int)
    parser.add_argument("--width", required=True, type=int)
    parser.add_argument("--height", required=True, type=int)
    parser.add_argument("--steps", required=True, type=int)
    parser.add_argument("--guidance_scale", type=float, default=7.5)
    parser.add_argument("--model_name", required=True)
    parser.add_argument("--seed", type=int, default=-1)
    parser.add_argument("--start_image")
    parser.add_argument("--end_image")
    parser.add_argument("--image", action="append", default=[])
    args = parser.parse_args()

    output_path = os.path.abspath(args.output_path)
    output_dir = os.path.dirname(output_path) or os.getcwd()
    output_stem = os.path.splitext(os.path.basename(output_path))[0]
    os.makedirs(output_dir, exist_ok=True)

    os.chdir(os.path.abspath(args.wangp_app))
    wgp = _load_wangp(args.wangp_app)

    model_type = resolve_model_name(args.model_name)
    state = build_state(model_type)

    # Resolve images
    start_images = []
    end_images = []
    for path in [args.start_image, args.end_image] + list(args.image or []):
        if not path:
            continue
        full = os.path.abspath(path)
        if os.path.isfile(full):
            if not start_images:
                start_images = [full]
            elif not end_images:
                end_images = [full]

    defaults = wgp.get_default_settings(model_type).copy()
    defaults.update({
        "prompt": args.prompt,
        "resolution": f"{args.width}x{args.height}",
        "video_length": int(args.num_frames),
        "num_inference_steps": int(args.steps),
        "guidance_scale": float(args.guidance_scale),
        "repeat_generation": 1,
        "output_filename": output_stem,
        "mode": "",
        "seed": args.seed,
        "state": state,
        "model_type": model_type,
        "video_prompt_type": "",
        "multi_prompts_gen_type": "",
    })

    # BUG-02/FLW-04: use SAFE_DEFAULTS from core/wangp_models (shared single source)
    _safe = dict(SAFE_DEFAULTS)
    _safe["image_start"] = start_images
    _safe["image_end"] = end_images
    _safe["image_prompt_type"] = (
        "S"  if start_images and not end_images else
        "SE" if start_images and end_images else ""
    )
    defaults.update(_safe)

    wgp.server_config["save_path"] = output_dir
    wgp.server_config["image_save_path"] = output_dir
    wgp.server_config["audio_save_path"] = output_dir

    before_files = set(glob.glob(os.path.join(output_dir, "*.mp4")))
    started_at = time.time()

    defaults.setdefault("image_refs", [])
    wgp.set_model_settings(state, model_type, defaults)
    state["validate_success"] = 1
    wgp.process_prompt_and_add_tasks(state, 0, model_type)
    queue = wgp.get_gen_info(state).get("queue", [])
    if not queue:
        raise RuntimeError("WanGP did not create any tasks")

    success = wgp.process_tasks_cli(queue, state)
    if not success:
        raise RuntimeError("WanGP generation failed")

    generated = find_generated_output(wgp, state, output_dir, before_files, started_at, output_stem)
    if not generated:
        raise RuntimeError("WanGP completed without producing an MP4 output")

    if os.path.abspath(generated) != output_path:
        shutil.copy2(generated, output_path)

    print(output_path)


if __name__ == "__main__":
    main()
