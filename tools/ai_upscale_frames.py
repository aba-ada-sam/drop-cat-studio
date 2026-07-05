"""Real-ESRGAN frame batch worker -- runs inside venv-upscale.

Called by core/upscaler.py as a subprocess so the CUDA torch stack lives in
its own venv (mirroring Forge's proven torch 2.11.0+cu128) instead of the
app's system Python.

Usage: python ai_upscale_frames.py <frames_dir> <out_dir> <scale>
Prints "PROGRESS <done> <total>" lines to stdout as frames complete.
Exit 0 on success, non-zero on failure.
"""
import sys
from pathlib import Path

MODELS_DIR = Path(__file__).resolve().parent.parent / "models"

# Official release weights -- auto-downloaded by RealESRGANer on first use
# when no local copy exists in MODELS_DIR.
_ESRGAN_URLS = {
    2: "https://github.com/xinntao/Real-ESRGAN/releases/download/v0.2.1/RealESRGAN_x2plus.pth",
    4: "https://github.com/xinntao/Real-ESRGAN/releases/download/v0.1.0/RealESRGAN_x4plus.pth",
}


def main() -> int:
    if len(sys.argv) < 4:
        print("ERROR usage: ai_upscale_frames.py <frames_dir> <out_dir> <scale>", flush=True)
        return 2
    frames_dir, out_dir, scale = Path(sys.argv[1]), Path(sys.argv[2]), float(sys.argv[3])

    import cv2
    import torch
    from basicsr.archs.rrdbnet_arch import RRDBNet
    from realesrgan import RealESRGANer

    frames = sorted(frames_dir.glob("*.png"))
    if not frames:
        print("ERROR no frames found", flush=True)
        return 2

    model_scale = 4 if scale > 2.5 else 2
    local = MODELS_DIR / f"RealESRGAN_x{model_scale}plus.pth"
    model_path = str(local) if local.exists() else _ESRGAN_URLS[model_scale]
    net = RRDBNet(num_in_ch=3, num_out_ch=3, num_feat=64,
                  num_block=23, num_grow_ch=32, scale=model_scale)
    upsampler = RealESRGANer(scale=model_scale, model_path=model_path, model=net,
                             tile=512, tile_pad=10, pre_pad=0,
                             half=torch.cuda.is_available())

    # cv2 BGR in/out is Real-ESRGAN's native path; compression level 1 keeps
    # the CPU out of the GPU's way (default PNG level 6 dominates frame time
    # on 4K outputs).
    total = len(frames)
    for i, frame in enumerate(frames):
        img = cv2.imread(str(frame), cv2.IMREAD_COLOR)
        out, _ = upsampler.enhance(img, outscale=scale)
        cv2.imwrite(str(out_dir / frame.name), out,
                    [cv2.IMWRITE_PNG_COMPRESSION, 1])
        if i % 5 == 0 or i == total - 1:
            print(f"PROGRESS {i + 1} {total}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
