"""DropCat-Studio venv shim for MuseTalk.

PyTorch >= 2.6 flipped torch.load's default to weights_only=True, which cannot
read MuseTalk's legacy .tar-format checkpoints (face-parse resnet/bisenet, the
vendored SFD face detector, whisper, cached latents). Every weight in this
dedicated, isolated venv is a trusted MuseTalk file, so default weights_only
back to False here. Scoped to this venv only -- never affects Forge/DCS.
"""
try:
    import torch as _torch

    _orig_load = _torch.load

    def _load_compat(*args, **kwargs):
        kwargs.setdefault("weights_only", False)
        return _orig_load(*args, **kwargs)

    _torch.load = _load_compat
except Exception:
    pass
