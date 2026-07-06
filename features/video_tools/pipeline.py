"""Chained video pipeline -- apply an ordered list of edits in a single run.

The user stacks steps (Upscale, Sharpen, Crop, Transform, Smooth) and this runs
them in order, feeding each step's output into the next, producing ONE final file.

Intermediate steps encode near-lossless (so quality doesn't erode across the chain);
only the final step uses the user's chosen quality. Every encode is GPU (NVENC)
when available -- see core.ffmpeg_utils.video_encode_args.
"""
import datetime
import logging
import os
import subprocess
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from core.ffmpeg_utils import probe_file, round_even, video_encode_args

log = logging.getLogger(__name__)

OUTPUT_DIR = Path(__file__).resolve().parent.parent.parent / "output"

# Near-lossless quality for files handed from one step to the next.
_INTERMEDIATE_CRF = 16

VALID_OPS = ("upscale", "sharpen", "crop", "transform", "smooth")

_OP_LABELS = {
    "upscale": "Upscaling",
    "sharpen": "Sharpening",
    "crop": "Cropping",
    "transform": "Transforming",
    "smooth": "Smoothing",
}


def _unique_path(path: Path) -> Path:
    """Return path, or path with a numeric suffix if it already exists."""
    if not path.exists():
        return path
    stem, suffix, parent = path.stem, path.suffix, path.parent
    n = 2
    while (parent / f"{stem}_{n}{suffix}").exists():
        n += 1
    return parent / f"{stem}_{n}{suffix}"


def _dest_dir(out_dir: str) -> Path:
    date_str = datetime.date.today().strftime("%Y-%m-%d")
    d = Path(out_dir) if out_dir else OUTPUT_DIR / date_str
    d.mkdir(parents=True, exist_ok=True)
    return d


def _run_chain(job, src: str, steps: list[dict], final: Path, progress=None) -> str:
    """Apply steps to src, writing the final file to `final`. Returns its path.

    Pure worker: does NOT touch job status/output or register the result -- the
    single/batch wrappers do that. `progress(frac_0_to_1, msg)` reports fine-grained
    progress when given; otherwise updates job.update directly.
    """
    total = len(steps)
    current = src
    with tempfile.TemporaryDirectory(prefix="dcs-pipeline-") as tmp:
        for i, step in enumerate(steps):
            if job.stop_event.is_set():
                raise RuntimeError("Cancelled")

            is_last = (i == total - 1)
            dst = str(final) if is_last else os.path.join(tmp, f"step{i}.mp4")
            crf = int(step.get("crf", 18)) if is_last else _INTERMEDIATE_CRF
            label = _OP_LABELS.get(step.get("op"), step.get("op", "Processing"))

            def _cb(frac, msg="", _i=i, _label=label):
                frac = max(0.0, min(1.0, frac))
                if progress:
                    progress((_i + frac) / total, msg or _label)
                else:
                    job.update(progress=int((_i + frac) / total * 100),
                               message=f"Step {_i + 1}/{total}: {msg or _label}")

            _cb(0.0)
            _apply_step(job, step, current, dst, crf, _cb)
            if not os.path.isfile(dst) or os.path.getsize(dst) == 0:
                raise RuntimeError(f"Step {i + 1} ({step.get('op')}) produced no output.")
            current = dst
    return str(final)


def _register(job, path: str) -> None:
    try:
        from core.inbox import copy_to_inbox
        copy_to_inbox(path)
    except Exception:
        pass
    try:
        from core.session import get_current as get_session
        get_session().add_file(Path(path).name, "video", "video_tools", path=path)
    except Exception:
        pass


def run_pipeline(job, src: str, steps: list[dict], out_dir: str = "") -> str:
    """Run the edit steps on a single video, returning the final output path."""
    if not steps:
        raise RuntimeError("No steps to apply.")
    final = _unique_path(_dest_dir(out_dir) / f"{Path(src).stem}_edited.mp4")
    _run_chain(job, src, steps, final)
    job.output = str(final)
    _register(job, str(final))
    job.update(status="done", progress=100, message=f"Saved: {final.name}")
    return str(final)


# One stream saturates one NVENC engine; the RTX-class cards have two, so two
# concurrent ffmpeg encodes ~double batch throughput. AI upscale (Real-ESRGAN)
# and RIFE smoothing lean on shared CUDA/VRAM instead, so those stay serial.
def _pick_concurrency(steps: list[dict]) -> int:
    for s in steps:
        if s.get("op") == "upscale" and s.get("engine") == "ai":
            return 1
        # "auto" may resolve to rife per-file, so treat it as heavy too -- running
        # two RIFE frame-explosions at once would thrash disk and VRAM.
        if s.get("op") == "smooth" and s.get("mode") in ("rife", "auto"):
            return 1
    return 2


class _SilentJob:
    """Per-file job stand-in for batch mode: shares cancellation, drops progress
    (the batch aggregates progress at the file level)."""
    def __init__(self, real_job):
        self.stop_event = real_job.stop_event
        self.meta = {}
        self.output = None
        self.message = ""

    def update(self, **_kwargs):
        pass


def _reserve(dest_dir: Path, src: str, used: set) -> Path:
    """A unique output path, avoiding both existing files and names already
    claimed by other files in this same batch."""
    base = f"{Path(src).stem}_edited"
    cand = dest_dir / f"{base}.mp4"
    n = 2
    while cand.exists() or str(cand) in used:
        cand = dest_dir / f"{base}_{n}.mp4"
        n += 1
    used.add(str(cand))
    return cand


def run_pipeline_batch(job, files: list[str], steps: list[dict], out_dir: str = "") -> list[str]:
    """Run the same edit steps over many videos, up to 2 encodes at once."""
    if not steps:
        raise RuntimeError("No steps to apply.")
    files = list(files)
    total = len(files)
    if total == 1:
        return [run_pipeline(job, files[0], steps, out_dir)]

    dest_dir = _dest_dir(out_dir)
    concurrency = _pick_concurrency(steps)
    used: set = set()
    targets = [(f, _reserve(dest_dir, f, used)) for f in files]

    outputs: list[str] = []
    errors: list[str] = []
    lock = threading.Lock()
    completed = [0]
    inflight = [0]

    def _tick():
        job.update(progress=int(completed[0] / total * 100),
                   message=f"{completed[0]}/{total} done"
                           + (f" - {inflight[0]} encoding" if inflight[0] else ""))

    def _one(pair):
        src, final = pair
        if job.stop_event.is_set():
            return
        with lock:
            inflight[0] += 1
        _tick()
        try:
            out = _run_chain(_SilentJob(job), src, steps, final)
            with lock:
                outputs.append(out)
        except Exception as e:  # noqa: BLE001 -- one bad file shouldn't sink the batch
            with lock:
                errors.append(f"{os.path.basename(src)}: {e}")
            log.error("[pipeline] %s failed: %s", os.path.basename(src), e)
        finally:
            with lock:
                inflight[0] -= 1
                completed[0] += 1
            _tick()

    _tick()
    with ThreadPoolExecutor(max_workers=concurrency) as ex:
        list(ex.map(_one, targets))

    # Register results on the main thread (session state isn't thread-safe).
    for out in outputs:
        _register(job, out)
    job.output = outputs[0] if outputs else None
    job.meta["outputs"] = outputs
    job.meta["errors"] = errors
    job.meta["total"] = total
    job.meta["processed"] = len(outputs)
    if not outputs and errors:
        raise RuntimeError("; ".join(errors[:3]))
    note = f" ({len(errors)} failed)" if errors else ""
    job.update(status="done", progress=100, message=f"Edited {len(outputs)}/{total} videos{note}")
    return outputs


def _apply_step(job, step: dict, src: str, dst: str, crf: int, cb) -> None:
    op = step.get("op")
    if op == "upscale":
        from core.upscaler import upscale_video
        scale = float(step.get("scale", 2.0))
        method = "ai" if step.get("engine") == "ai" else "ffmpeg"
        out, err = upscale_video(src, dst, scale=scale, method=method, crf=crf,
                                 progress_cb=lambda f, m="": cb(f, m))
        if not out:
            raise RuntimeError(err or "Upscale failed")

    elif op == "sharpen":
        strength = float(step.get("strength", 1.0))
        info = probe_file(src)
        keep_audio = info.get("has_audio", False)
        cmd = ["ffmpeg", "-y", "-i", src,
               "-vf", f"unsharp=5:5:{strength:.2f}:5:5:0.0",
               *video_encode_args(crf=crf)]
        cmd += ["-c:a", "copy"] if keep_audio else ["-an"]
        cmd += [dst]
        _run(cmd, cb)

    elif op == "crop":
        _apply_crop(src, dst, step, crf, cb)

    elif op == "transform":
        from features.video_tools.reverser import build_ffmpeg_cmd
        settings = {
            "reverse_vid": bool(step.get("reverse_vid", False)),
            "reverse_aud": bool(step.get("reverse_aud", step.get("reverse_vid", False))),
            "mirror": bool(step.get("mirror", False)),
            "vflip": bool(step.get("vflip", False)),
            "speed": float(step.get("speed", 1.0)),
            "volume": int(step.get("volume", 100)),
            "mute_audio": bool(step.get("mute_audio", False)),
            "keep_audio": bool(step.get("keep_audio", True)),
            "out_format": "mp4",
            "crf": crf,
        }
        _run(build_ffmpeg_cmd(src, dst, settings), cb)

    elif op == "smooth":
        from features.video_tools.interpolator import interpolate_video
        interpolate_video(job, src, dst, float(step.get("target_fps", 0)),
                          step.get("mode", "blend"))

    else:
        raise RuntimeError(f"Unknown step: {op}")


def _apply_crop(src: str, dst: str, step: dict, crf: int, cb) -> None:
    rect = step.get("rect") or {}
    try:
        rx, ry = float(rect["x"]), float(rect["y"])
        rw, rh = float(rect["w"]), float(rect["h"])
    except (KeyError, ValueError, TypeError):
        raise RuntimeError("Crop step needs a rect with numeric x, y, w, h.")
    info = probe_file(src)
    W, H = info.get("width"), info.get("height")
    if not W or not H:
        raise RuntimeError("Could not read video dimensions for crop.")
    cw = max(2, min(round_even(rw * W), W - (W % 2)))
    ch = max(2, min(round_even(rh * H), H - (H % 2)))
    cx = max(0, min(int(round(rx * W)), W - cw))
    cy = max(0, min(int(round(ry * H)), H - ch))
    keep_audio = step.get("keep_audio", True) and info.get("has_audio", False)
    cmd = ["ffmpeg", "-y", "-i", src, "-vf", f"crop={cw}:{ch}:{cx}:{cy}",
           *video_encode_args(crf=crf)]
    cmd += ["-c:a", "copy"] if keep_audio else ["-an"]
    cmd += [dst]
    _run(cmd, cb)


def _run(cmd: list[str], cb, timeout: int = 12 * 3600) -> None:
    cb(0.1, "encoding...")
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        detail = (r.stderr or "").strip().splitlines()
        raise RuntimeError("ffmpeg failed: " + (detail[-1] if detail else "unknown error"))
    cb(1.0, "done")
