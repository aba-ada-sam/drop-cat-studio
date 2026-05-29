# MuseTalk patches (Blackwell / Windows / creature-face lip sync)

These are copies of files we patched inside the external MuseTalk install
(`C:\MuseTalk`, NOT a git submodule). A Pinokio pull or MuseTalk reinstall
WIPES these edits and lip sync silently regresses, so they live here as the
source of truth. To restore, copy each file back to the path below.

| File | Restore to |
|------|-----------|
| `inference.py` | `C:\MuseTalk\scripts\inference.py` |
| `preprocessing.py` | `C:\MuseTalk\musetalk\utils\preprocessing.py` |
| `sitecustomize.py` | `C:\MuseTalk\venv\Lib\site-packages\sitecustomize.py` |

## What each patch does

### preprocessing.py
- Swaps mmpose (mmcv won't build on this stack) for the `face-alignment`
  package: `FaceAlignment(LandmarksType.TWO_D)` -> 68 dlib landmarks.
- DERIVES the face box from those landmarks instead of gating on the SFD
  human-face detector. SFD missed the creature faces (~13/776 frames passed);
  landmark-derived boxes pass ~505/776. `coord_placeholder` is appended only
  for degenerate boxes.

### inference.py
- Write loop writes the ORIGINAL frame for any skipped/placeholder index, so
  the `%08d` PNG sequence stays CONTIGUOUS. Gaps made ffmpeg's `image2` reader
  stop at the first missing index -> 27/776 frames = near-frozen "no lip sync".

### sitecustomize.py
- Forces `torch.load(weights_only=False)` venv-wide. PyTorch 2.6 flipped the
  default to `True`, which breaks MuseTalk's legacy `.tar` checkpoint loads.

## Runtime env (set by features/lipsync/runner.py, not baked into files)
- `TORCHDYNAMO_DISABLE=1` -- no Triton on Windows (face-alignment torch.compile).
- `PYTHONUTF8=1` / `PYTHONIOENCODING=utf-8` -- MuseTalk prints non-ASCII; cp1252 crashes.
