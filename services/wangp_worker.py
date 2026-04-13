#!/usr/bin/env python3
"""Persistent WanGP worker — loads the model ONCE and serves generation requests.

Runs as a long-lived subprocess with WanGP's Python environment.
Copied from DropCatGo-Fun-Videos_w_Audio/wangp_worker.py with import path
updated to use core.wangp_runtime.

Exposes a tiny HTTP server:
  GET  /health    → {"ok": true, "model": "..."}
  POST /generate  → submit a generation job (JSON body)
  GET  /status    → current generation status
"""

import argparse
import glob
import http.server
import importlib.util
import json
import os
import shutil
import sys
import threading
import time
import traceback

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(SCRIPT_DIR)
if PROJECT_DIR not in sys.path:
    sys.path.insert(0, PROJECT_DIR)
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

# ── Model mapping (BUG-02/FLW-04: import from single source of truth) ────────

try:
    from core.wangp_models import MODEL_MAP, resolve_model_name, build_state as _build_state, SAFE_DEFAULTS
except ImportError:
    # Fallback if run before sys.path is configured — shouldn't happen in practice
    from wangp_models import MODEL_MAP, resolve_model_name, build_state as _build_state, SAFE_DEFAULTS


# ── Global state ─────────────────────────────────────────────────────────────

wgp = None
app_path = None
current_model = None
_lock = threading.Lock()
_job_status = {"busy": False, "progress": "", "step": 0, "total_steps": 0, "result": None, "error": None}

# Hook called by the tqdm patch on each step update: (step: int, total: int) -> None
_step_hook = None


def _install_tqdm_hook():
    """Patch tqdm BEFORE importing WanGP so all sub-modules get the hooked class.

    WanGP's diffusion loop calls tqdm.update() once per inference step.
    We intercept those calls to update _job_status with real step progress.
    """
    try:
        import tqdm as _tqdm_mod
        _orig = _tqdm_mod.tqdm

        class _HookedTqdm(_orig):
            def update(self, n=1):
                super().update(n)
                # Only track bars that look like inference step bars (total >= 5)
                if _step_hook and self.total and self.total >= 5:
                    try:
                        _step_hook(int(self.n), int(self.total))
                    except Exception:
                        pass

        _tqdm_mod.tqdm = _HookedTqdm
        # Also patch tqdm.auto which some WanGP sub-modules may import
        try:
            import tqdm.auto as _auto
            _auto.tqdm = _HookedTqdm
        except Exception:
            pass
        print("[worker] tqdm progress hook installed", flush=True)
    except Exception as e:
        print(f"[worker] Could not install tqdm hook: {e}", flush=True)


def _update_status(**kwargs):
    """Update _job_status under the lock. BUG-04: always hold lock on writes."""
    with _lock:
        _job_status.update(kwargs)


def _get_status_snapshot() -> dict:
    """Return a copy of _job_status under the lock. BUG-04: lock on reads too."""
    with _lock:
        return dict(_job_status)


# ── Generation logic ─────────────────────────────────────────────────────────

def _do_generate(params: dict) -> dict:
    global current_model

    prompt = params.get("prompt", "")
    model_name = params.get("model_name", "LTX-2 Dev19B Distilled")
    output_path = params.get("output_path", "")
    num_frames = int(params.get("num_frames", 81))
    width = int(params.get("width", 768))
    height = int(params.get("height", 512))
    steps = int(params.get("steps", 30))
    guidance = float(params.get("guidance_scale", 7.5))
    seed = int(params.get("seed", -1))
    start_image = params.get("start_image")
    end_image = params.get("end_image")

    output_dir = os.path.dirname(output_path) or os.getcwd()
    output_stem = os.path.splitext(os.path.basename(output_path))[0]
    os.makedirs(output_dir, exist_ok=True)

    model_type = resolve_model_name(model_name)
    current_model = model_name

    state = _build_state(model_type)

    start_images = []
    end_images = []
    if start_image and os.path.isfile(start_image):
        start_images = [os.path.abspath(start_image)]
    if end_image and os.path.isfile(end_image):
        end_images = [os.path.abspath(end_image)]

    defaults = wgp.get_default_settings(model_type).copy()
    defaults.update({
        "prompt": prompt,
        "resolution": f"{width}x{height}",
        "video_length": num_frames,
        "num_inference_steps": steps,
        "guidance_scale": guidance,
        "repeat_generation": 1,
        "output_filename": output_stem,
        "mode": "",
        "seed": seed,
        "state": state,
        "model_type": model_type,
        "video_prompt_type": "",
        "multi_prompts_gen_type": "",
    })

    # BUG-02/FLW-04: use SAFE_DEFAULTS from core/wangp_models (shared single source)
    # Use setdefault so WanGP's own defaults (e.g. sliding_window_size) are preserved
    for k, v in SAFE_DEFAULTS.items():
        defaults.setdefault(k, v)
    # Image settings must always override
    defaults["image_start"] = start_images
    defaults["image_end"] = end_images
    defaults["image_prompt_type"] = (
        "S"  if start_images and not end_images else
        "SE" if start_images and end_images else ""
    )

    # server_config alone isn't enough — WanGP copies save_path into module-level
    # variables at import time. Override those directly so files land in output_dir.
    os.makedirs(output_dir, exist_ok=True)
    wgp.server_config["save_path"] = output_dir
    wgp.server_config["image_save_path"] = output_dir
    wgp.server_config["audio_save_path"] = output_dir
    wgp.save_path = output_dir
    wgp.image_save_path = output_dir
    wgp.audio_save_path = output_dir

    wangp_outputs = os.path.join(os.getcwd(), "outputs")
    before_files = (
        set(glob.glob(os.path.join(output_dir, "*.mp4")))
        | set(glob.glob(os.path.join(wangp_outputs, "*.mp4")))
        | set(glob.glob(os.path.join(wangp_outputs, "*", "*.mp4")))
    )
    started_at = time.time()

    defaults.setdefault("image_refs", [])
    wgp.set_model_settings(state, model_type, defaults)
    state["validate_success"] = 1

    _update_status(progress="Queuing generation task...")
    wgp.process_prompt_and_add_tasks(state, 0, model_type)
    queue = wgp.get_gen_info(state).get("queue", [])
    if not queue:
        return {"ok": False, "output": None, "error": "WanGP did not create any tasks"}

    # Arm the tqdm hook so inference steps update _job_status in real time
    global _step_hook
    _update_status(step=0, total_steps=steps, progress=f"Step 0/{steps}")

    def _on_step(n, total):
        _update_status(step=n, total_steps=total, progress=f"Step {n}/{total}")

    _step_hook = _on_step
    try:
        success = wgp.process_tasks_cli(queue, state)
    finally:
        _step_hook = None

    if not success:
        return {"ok": False, "output": None, "error": "WanGP generation failed"}

    # Find output
    candidates = []
    gen = wgp.get_gen_info(state)
    file_list = gen.get("file_list", [])
    print(f"[worker-debug] file_list from gen_info: {file_list}", flush=True)
    for path in file_list:
        if isinstance(path, str) and path.lower().endswith(".mp4") and os.path.isfile(path):
            candidates.append(path)

    wangp_cwd = os.getcwd()
    search_dirs = [output_dir]
    for extra in [os.path.join(wangp_cwd, "outputs"), wangp_cwd,
                  os.path.join(wangp_cwd, "output")]:
        if os.path.isdir(extra) and extra not in search_dirs:
            search_dirs.append(extra)

    print(f"[worker-debug] searching dirs: {search_dirs}", flush=True)

    # Search one level deep AND one level into subdirs (WanGP sometimes uses date folders)
    for search_dir in search_dirs:
        patterns = [
            os.path.join(search_dir, "*.mp4"),
            os.path.join(search_dir, "*", "*.mp4"),
        ]
        for pattern in patterns:
            for path in glob.glob(pattern):
                if path in before_files:
                    continue
                try:
                    if os.path.getmtime(path) + 1 >= started_at:
                        print(f"[worker-debug] found candidate: {path}", flush=True)
                        candidates.append(path)
                except OSError:
                    continue

    deduped = list(dict.fromkeys(candidates))
    print(f"[worker-debug] final candidates: {deduped}", flush=True)
    if not deduped:
        return {"ok": False, "output": None, "error": "No output MP4 produced"}

    exact = [p for p in deduped if os.path.splitext(os.path.basename(p))[0].startswith(output_stem)]
    best = exact[0] if exact else max(deduped, key=lambda p: os.path.getmtime(p))

    if os.path.abspath(best) != os.path.abspath(output_path):
        shutil.copy2(best, output_path)

    return {"ok": True, "output": output_path, "error": None}


# ── HTTP server ──────────────────────────────────────────────────────────────

class WorkerHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def _send_json(self, data: dict, status: int = 200):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        # BUG-04: read _job_status through the lock-protected snapshot helper
        if self.path == "/health":
            snap = _get_status_snapshot()
            self._send_json({"ok": True, "model": current_model, "busy": snap["busy"]})
        elif self.path == "/status":
            self._send_json(_get_status_snapshot())
        else:
            self._send_json({"error": "Not found"}, 404)

    def do_POST(self):
        if self.path == "/generate":
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length)
            try:
                params = json.loads(body)
            except json.JSONDecodeError:
                self._send_json({"error": "Invalid JSON"}, 400)
                return

            if _get_status_snapshot()["busy"]:
                self._send_json({"error": "Worker is busy"}, 409)
                return

            def _run():
                with _lock:
                    _update_status(busy=True, progress="Starting...", result=None, error=None)
                    try:
                        result = _do_generate(params)
                        _update_status(busy=False, result=result.get("output"),
                                       error=result.get("error"),
                                       progress="Done" if result["ok"] else "Failed")
                    except Exception as e:
                        _update_status(busy=False, error=str(e), progress="Error")
                        traceback.print_exc()

            threading.Thread(target=_run, daemon=True).start()
            self._send_json({"ok": True, "message": "Generation started"})

        elif self.path == "/shutdown":
            self._send_json({"ok": True, "message": "Shutting down"})
            threading.Thread(target=lambda: (time.sleep(0.5), os._exit(0)), daemon=True).start()
        else:
            self._send_json({"error": "Not found"}, 404)


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    global wgp, app_path

    parser = argparse.ArgumentParser(description="Persistent WanGP worker")
    parser.add_argument("--wangp-app", required=True, help="WanGP root directory")
    parser.add_argument("--port", type=int, default=7899, help="Worker HTTP port")
    parser.add_argument("--model", default="i2v", help="Initial model type to load")
    args = parser.parse_args()

    app_path = args.wangp_app
    print(f"[worker] Loading WanGP from {app_path}...", flush=True)

    os.chdir(os.path.abspath(app_path))

    # Load wangp_runtime from the core module
    runtime_path = os.path.join(PROJECT_DIR, "core", "wangp_runtime.py")
    if os.path.isfile(runtime_path):
        spec = importlib.util.spec_from_file_location("wangp_runtime", runtime_path)
        rt = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(rt)
        rt.prepare_wangp_runtime(app_path)
    else:
        # Fallback: prepare manually
        site_packages = os.path.join(app_path, "env", "Lib", "site-packages")
        scripts_dir = os.path.join(app_path, "env", "Scripts")
        if os.path.isdir(scripts_dir):
            os.environ["PATH"] = scripts_dir + os.pathsep + os.environ.get("PATH", "")
        if app_path not in sys.path:
            sys.path.insert(0, app_path)
        if os.path.isdir(site_packages):
            sys.path.insert(0, site_packages)

    # Install tqdm hook BEFORE importing WanGP so all sub-modules that do
    # `from tqdm import tqdm` at import time get the hooked class.
    _install_tqdm_hook()

    original_argv = sys.argv[:]
    wgp_path = os.path.join(app_path, "wgp.py")
    sys.argv = [wgp_path]
    try:
        wgp = importlib.import_module("wgp")
    finally:
        sys.argv = original_argv

    import types

    class _DummyPluginManager:
        def run_data_hooks(self, *args, **kwargs):
            if args and len(args) >= 2:
                return args[1]
            return kwargs.get("inputs", {})
        def __getattr__(self, name):
            return lambda *a, **k: None

    dummy_app = types.SimpleNamespace(plugin_manager=_DummyPluginManager())
    wgp.app = dummy_app
    wgp_mod = sys.modules.get("wgp")
    if wgp_mod:
        wgp_mod.app = dummy_app

    print("[worker] WanGP module loaded successfully", flush=True)

    try:
        model_type = resolve_model_name(args.model)
        defaults = wgp.get_default_settings(model_type)
        print(f"[worker] Model settings loaded for: {model_type} ({len(defaults)} params)", flush=True)
    except Exception as e:
        print(f"[worker] Warning: could not pre-load model settings: {e}", flush=True)

    # BUG-03: ThreadingHTTPServer lets /status polls and /generate run concurrently
    server = http.server.ThreadingHTTPServer(("127.0.0.1", args.port), WorkerHandler)
    print(f"[worker] Ready — listening on port {args.port}", flush=True)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("[worker] Shutting down", flush=True)
        server.shutdown()


if __name__ == "__main__":
    main()
