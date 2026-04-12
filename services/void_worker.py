#!/usr/bin/env python3
"""Netflix VOID worker — loads the model once and serves inpainting requests.

VOID (Video Object Inpainting & Deletion) — Netflix Research, April 2026.
Built on CogVideoX-Fun-V1.5-5b-InP fine-tuned for physics-aware object removal.
Model: https://huggingface.co/netflix/void-model

Exposes:
  GET  /health   -- {"ok": true, "model_loaded": bool, "busy": bool}
  POST /inpaint  -- Submit inpainting job
  GET  /status   -- Current job progress
  POST /shutdown -- Graceful exit
"""

import argparse
import base64
import http.server
import io
import json
import os
import sys
import threading
import time
import traceback
from pathlib import Path

# ── Job state ────────────────────────────────────────────────────────────────

_lock = threading.Lock()
_job_status = {
    "busy": False,
    "progress": "Idle",
    "step": 0,
    "total_steps": 0,
    "result": None,
    "error": None,
}
_model_loaded = False
_pipeline = None


def _update_status(**kwargs):
    with _lock:
        _job_status.update(kwargs)


def _get_status() -> dict:
    with _lock:
        return dict(_job_status)


# ── Model loading ─────────────────────────────────────────────────────────────

def _load_model(model_dir: str):
    """Load the VOID pipeline (CogVideoX-Fun + netflix/void-model weights).

    On first run this downloads the model weights from HuggingFace (~10 GB).
    Subsequent runs load from the local cache.
    """
    global _pipeline, _model_loaded
    _update_status(progress="Loading VOID model (first run downloads ~10 GB)...")
    print("[void-worker] Loading VOID pipeline...", flush=True)

    try:
        import torch
        from diffusers import CogVideoXFunInpaintPipeline

        device = "cuda" if torch.cuda.is_available() else "cpu"
        dtype  = torch.bfloat16

        # Use local path if provided, otherwise HuggingFace auto-download
        model_id = model_dir.strip() if model_dir and model_dir.strip() else "netflix/void-model"

        pipe = CogVideoXFunInpaintPipeline.from_pretrained(
            model_id,
            torch_dtype=dtype,
        )
        pipe = pipe.to(device)

        # Enable memory optimizations for RTX-class GPUs
        try:
            pipe.enable_model_cpu_offload()
        except Exception:
            pass
        try:
            pipe.vae.enable_tiling()
        except Exception:
            pass

        _pipeline = pipe
        _model_loaded = True
        _update_status(progress="VOID model ready")
        print("[void-worker] VOID model loaded successfully", flush=True)
    except Exception as e:
        _update_status(progress=f"Model load failed: {e}", error=str(e))
        print(f"[void-worker] Model load failed: {e}", flush=True)
        traceback.print_exc()


# ── Inpainting logic ──────────────────────────────────────────────────────────

def _do_inpaint(params: dict) -> dict:
    """Run VOID inpainting on a video with a painted mask.

    The mask encodes:
      0   = keep (black in simple binary mask)
      255 = remove (white in simple binary mask)
    VOID's quadmask system (0/63/127/255) is used internally; a binary mask is
    automatically up-converted: 0 stays 0, non-zero becomes 255.
    """
    import torch
    import numpy as np
    from PIL import Image

    video_path  = params.get("video_path", "")
    mask_b64    = params.get("mask_b64", "")
    output_path = params.get("output_path", "")
    prompt      = params.get("prompt", "remove the object and fill naturally")
    steps       = int(params.get("steps", 30))
    seed        = int(params.get("seed", -1))
    num_frames  = int(params.get("num_frames", 49))

    if not _pipeline:
        return {"ok": False, "error": "Model not loaded"}

    _update_status(progress="Preparing video...", step=0, total_steps=steps)

    # ── Load video frames ─────────────────────────────────────────────────────
    try:
        import subprocess, shutil, tempfile

        if not shutil.which("ffmpeg"):
            return {"ok": False, "error": "ffmpeg not found — required for video processing"}

        # Extract frames from input video
        frames_dir = Path(output_path).parent / "frames_in"
        frames_dir.mkdir(parents=True, exist_ok=True)

        subprocess.run([
            "ffmpeg", "-y", "-i", video_path,
            "-vframes", str(num_frames),
            "-q:v", "2",
            str(frames_dir / "frame_%04d.jpg"),
        ], capture_output=True, check=True)

        frame_files = sorted(frames_dir.glob("frame_*.jpg"))
        if not frame_files:
            return {"ok": False, "error": "Could not extract frames from video"}

        frames = [Image.open(f).convert("RGB") for f in frame_files]
        w, h = frames[0].size
        actual_frames = len(frames)
        _update_status(progress=f"Loaded {actual_frames} frames ({w}x{h})")

    except Exception as e:
        return {"ok": False, "error": f"Video loading failed: {e}"}

    # ── Decode and prepare mask ───────────────────────────────────────────────
    try:
        mask_bytes = base64.b64decode(mask_b64)
        mask_img = Image.open(io.BytesIO(mask_bytes)).convert("L")
        mask_img = mask_img.resize((w, h), Image.NEAREST)
        mask_arr = np.array(mask_img)
        # Binary to quadmask: 0=keep, 255=remove
        mask_arr = np.where(mask_arr > 127, 255, 0).astype(np.uint8)
        # Replicate single frame mask across all video frames
        masks = [Image.fromarray(mask_arr)] * actual_frames
    except Exception as e:
        return {"ok": False, "error": f"Mask processing failed: {e}"}

    # ── Run VOID inference ────────────────────────────────────────────────────
    try:
        _update_status(progress="Running VOID inference...", step=0, total_steps=steps)

        generator = None
        if seed >= 0:
            generator = torch.Generator(device=_pipeline.device).manual_seed(seed)

        def _step_callback(pipe, i, t, kwargs):
            _update_status(step=i, total_steps=steps, progress=f"Step {i}/{steps}")
            return kwargs

        result = _pipeline(
            prompt=prompt,
            video=frames,
            mask_video=masks,
            num_inference_steps=steps,
            generator=generator,
            callback_on_step_end=_step_callback,
            callback_on_step_end_tensor_inputs=["latents"],
        )

        output_frames = result.frames[0]  # list of PIL images

    except Exception as e:
        return {"ok": False, "error": f"VOID inference failed: {e}"}

    # ── Reassemble video ──────────────────────────────────────────────────────
    try:
        _update_status(progress="Reassembling output video...")
        frames_out_dir = Path(output_path).parent / "frames_out"
        frames_out_dir.mkdir(exist_ok=True)

        for i, frame in enumerate(output_frames):
            frame.save(frames_out_dir / f"frame_{i:04d}.jpg", quality=95)

        subprocess.run([
            "ffmpeg", "-y",
            "-framerate", "24",
            "-i", str(frames_out_dir / "frame_%04d.jpg"),
            "-c:v", "libx264", "-crf", "18", "-pix_fmt", "yuv420p",
            output_path,
        ], capture_output=True, check=True)

        import shutil as _shutil
        _shutil.rmtree(str(frames_dir), ignore_errors=True)
        _shutil.rmtree(str(frames_out_dir), ignore_errors=True)

        return {"ok": True, "output": output_path}
    except Exception as e:
        return {"ok": False, "error": f"Video reassembly failed: {e}"}


# ── HTTP server ───────────────────────────────────────────────────────────────

class VoidHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass  # suppress default access log

    def _json(self, data: dict, status: int = 200):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/health":
            snap = _get_status()
            self._json({"ok": True, "model_loaded": _model_loaded, "busy": snap["busy"]})
        elif self.path == "/status":
            self._json(_get_status())
        else:
            self._json({"error": "Not found"}, 404)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length)

        if self.path == "/inpaint":
            if _get_status()["busy"]:
                self._json({"error": "Worker is busy"}, 409)
                return
            if not _model_loaded:
                self._json({"error": "Model not loaded yet — wait for startup to complete"}, 503)
                return
            try:
                params = json.loads(raw)
            except json.JSONDecodeError:
                self._json({"error": "Invalid JSON"}, 400)
                return

            def _run():
                _update_status(busy=True, progress="Starting...", result=None, error=None, step=0)
                try:
                    res = _do_inpaint(params)
                    _update_status(
                        busy=False,
                        result=res.get("output"),
                        error=res.get("error"),
                        progress="Done" if res["ok"] else "Failed",
                    )
                except Exception as e:
                    _update_status(busy=False, error=str(e), progress="Error")
                    traceback.print_exc()

            threading.Thread(target=_run, daemon=True).start()
            self._json({"ok": True, "message": "Inpainting started"})

        elif self.path == "/shutdown":
            self._json({"ok": True})
            threading.Thread(target=lambda: (time.sleep(0.5), os._exit(0)), daemon=True).start()
        else:
            self._json({"error": "Not found"}, 404)


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Netflix VOID persistent worker")
    parser.add_argument("--port", type=int, default=7901)
    parser.add_argument("--model-dir", default="", help="Path to VOID model dir (blank = HF auto-download)")
    args = parser.parse_args()

    # Load model in background so the HTTP server starts immediately
    threading.Thread(target=_load_model, args=(args.model_dir,), daemon=True).start()

    server = http.server.ThreadingHTTPServer(("127.0.0.1", args.port), VoidHandler)
    print(f"[void-worker] Listening on port {args.port} (model loading in background)", flush=True)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("[void-worker] Shutting down", flush=True)
        server.shutdown()


if __name__ == "__main__":
    main()
