"""Standalone Demucs vocal-isolation helper.

Run with the MuseTalk venv's Python (which has demucs + torch cu128), NOT the
DCS Python. Uses Demucs as a LIBRARY and writes the vocal stem with soundfile,
sidestepping Demucs 4.x's torchcodec save dependency (unavailable on Windows).

  <musetalk_python> _demucs_separate.py <in_audio> <out_vocals_wav>
"""
import sys

import numpy as np
import soundfile as sf
import torch
from demucs.apply import apply_model
from demucs.pretrained import get_model


def main(in_path: str, out_path: str) -> int:
    dev = "cuda" if torch.cuda.is_available() else "cpu"
    model = get_model("htdemucs")
    model.to(dev).eval()

    data, sr = sf.read(in_path, dtype="float32")          # (samples,) or (samples, ch)
    if data.ndim == 1:
        data = np.stack([data, data], axis=1)
    wav = torch.tensor(data.T)                            # (ch, samples)
    if sr != model.samplerate:
        import torchaudio
        wav = torchaudio.functional.resample(wav, sr, model.samplerate)

    ref = wav.mean(0)
    wav = (wav - ref.mean()) / (ref.std() + 1e-8)
    with torch.no_grad():
        stems = apply_model(model, wav[None].to(dev), device=dev, split=True, overlap=0.25)[0].cpu()
    stems = stems * (ref.std() + 1e-8) + ref.mean()

    vocals = stems[model.sources.index("vocals")].numpy().T   # (samples, ch)
    sf.write(out_path, vocals, model.samplerate)
    print(f"[demucs] wrote vocals: {out_path} ({vocals.shape[0] / model.samplerate:.2f}s)")
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("usage: _demucs_separate.py <in_audio> <out_vocals_wav>")
        sys.exit(2)
    sys.exit(main(sys.argv[1], sys.argv[2]))
