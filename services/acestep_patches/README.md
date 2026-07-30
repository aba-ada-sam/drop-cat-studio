# ACE-Step patches (no-silent-CPU-fallback)

`C:\DropCatGo-Music\ACE-Step-1.5` is a vendored ACE-Step checkout, gitignored by the
`DropCatGo-Music` repo (`.gitignore:9`) and not itself a git repo -- there is no version
control on it at all. A reinstall/update WIPES this edit silently. This file lives here
as the source of truth. To restore, copy it back to the path below.

| File | Restore to |
|------|-----------|
| `init_service_setup.py` | `C:\DropCatGo-Music\ACE-Step-1.5\acestep\core\generation\handler\init_service_setup.py` |

## What the patch does

Stock ACE-Step's `_resolve_initialize_device` silently resolves to `"cpu"` whenever no
GPU backend is found -- for a requested `"auto"` device with no CUDA/MPS/XPU, and for an
explicitly requested `"cuda"`/`"mps"`/`"xpu"` that turns out unavailable with no other
accelerator to fall back to. The DiT/VAE/text-encoder models then load onto CPU with only
a log line (`initialize_models_at_startup`: "No GPU detected, running on CPU") -- the
`/health` endpoint DCS polls doesn't check device, so the UI shows "ACE-Step ready"
whether it's actually on the 5080 or grinding away on CPU for hours.

The patch makes every one of those paths `raise RuntimeError` instead of returning
`"cpu"`. `initialize_service`'s own try/except turns that into `(error_msg, False)`,
and `startup_model_init.py` turns `not ok` into `raise RuntimeError(status_msg)` --
so a lost CUDA device now fails ACE-Step's startup loudly instead of quietly
succeeding on CPU.

## Companion piece

`services/manager.py`'s `start_acestep()` sets `env["ACESTEP_DEVICE"] = "cuda"`
explicitly (instead of leaving it on ACE-Step's own `"auto"` default) so this patch's
"cuda requested but unavailable" branch is the one that actually fires.

## Test coverage

The vendored install ships `acestep/core/generation/handler/init_service_test.py` with
unit tests for `_resolve_initialize_device`. One test
(`test_resolve_initialize_device_requested_cuda_falls_back_to_cpu`) pinned the OLD
silent-fallback behavior and was renamed/updated to
`test_resolve_initialize_device_requested_cuda_raises_without_accelerator`, asserting
`RuntimeError` instead. That test file is also gitignored/unversioned -- if ACE-Step
gets reinstalled, re-apply that same rename (search the file for `falls_back_to_cpu`)
after restoring this patch, or the suite will fail against the restored behavior.

Full suite verified green (59/59) after the patch, via:
`C:\DropCatGo-Music\ACE-Step-1.5\.venv\Scripts\python.exe -m unittest acestep.core.generation.handler.init_service_test`
