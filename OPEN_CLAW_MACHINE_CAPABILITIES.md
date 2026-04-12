# Machine Capabilities Available to Open Claw
*Prepared for the Open Claw project manager — this documents what's already installed and working on Andrew's machine as a result of the Drop Cat Go Studio project. You don't need to set any of this up from scratch.*

---

## The Short Version

This machine has a full AI video + image production stack already running. That includes:
- Local LLMs (Ollama, no API key needed)
- Anthropic Claude API (key already configured)
- AI video generation via WanGP (GPU, local, no per-call cost)
- AI music generation via ACE-Step (GPU, local, no per-call cost)
- Stable Diffusion image generation via Forge
- Python 3.10.11 with a FastAPI web-app pattern already proven and working

---

## AI / LLM

### Anthropic Claude (Cloud API)
- **Key**: Already configured in `C:/JSON Credentials/` and the DropCat config
- **Models available**:
  - `claude-haiku-4-5-20251001` — fast, cheap, good for structured tasks
  - `claude-sonnet-4-6` — balanced quality/cost
- **Best for**: Vision tasks (analyzing images), structured JSON output, creative writing, prompt generation

### Ollama (Local — no API cost)
- **Endpoint**: `http://localhost:11434`
- **Models already pulled**:
  - `qwen3-vl:8b` — 8B vision-language model, fast, runs locally
  - `qwen3-vl:30b` — 30B vision-language model, slower but higher quality
- **Best for**: Vision analysis, prompt generation, tasks where you want zero API cost or offline capability

### OpenAI (Cloud API)
- Key slot is configured in the app but was empty as of project handoff — check with Andrew if he's added one since. Models would be `gpt-4o-mini` and `gpt-4o`.

---

## AI Video Generation — WanGP

- **Location**: `C:\pinokio\api\wan.git\app`
- **Port**: 7899 (worker runs as a persistent subprocess)
- **What it does**: Takes an image + prompt → generates a short video clip (image-to-video). Also supports text-to-video.
- **Models available** (already downloaded):
  - `LTX-2 Dev19B Distilled` — currently active default
  - `Wan2.1-T2V-1.3B` — text-to-video, lightweight
  - `Wan2.1-T2V-14B` — text-to-video, high quality
  - `Wan2.1-I2V-14B-480P` — image-to-video, 480p
  - `Wan2.1-I2V-14B-720P` — image-to-video, 720p
  - `Wan2.1-VACE-1.3B` — video-conditioned generation
- **Configurable**: Steps, guidance scale, seed, resolution, frame count, start/end image conditioning
- **GPU**: Runs on the local GPU — no per-generation cloud cost
- **Important**: Only one GPU job runs at a time (sequential queue). If DropCat Studio is also running a job, Open Claw will need to wait, or you'll need to coordinate.

---

## AI Music Generation — ACE-Step

- **Location**: `C:\DropCatGo-Music\ACE-Step-1.5`
- **Port**: 8019
- **What it does**: Generates music from text prompts. Outputs MP3 or WAV.
- **Configurable**: Steps, guidance scale, instrumental vs. vocal, output format
- **GPU**: Local, no per-generation cost

---

## Stable Diffusion Image Generation — Forge

- **Location**: `C:\forge`
- **Port**: 7861
- **What it does**: Full Stable Diffusion image generation pipeline
- **Features available**:
  - Text-to-image
  - Regional conditioning (Forge Couple — 3 spatial regions)
  - HiRes Fix (upscaling pass in-generation)
  - ADetailer (automatic face/hand refinement)
  - LoRA support
  - Dynamic wildcards (`__wildcard__` syntax, auto-expanded)
  - Live sampler/scheduler selection via API
- **Default settings**: DPM++ 2M SDE sampler, 25 steps, CFG 7.0, 1024×1024
- **Wildcards directory**: `Z:\Python310\stable-diffusion-webui\extensions\sd-dynamic-prompts\wildcards`
- **Heads up**: Forge must be started manually with `--api` flag before it's accessible. The machine's existing SD wildcard library at the path above is available to any app that calls the Forge API.
- **No cloud cost** — fully local

---

## Video Processing — ffmpeg

- **Status**: Should be on PATH — confirm with Andrew. If not, it needs to be installed and the `bin` folder added to system PATH.
- **What's available once installed**:
  - H.264/H.265 encoding (CPU via libx264/libx265)
  - Hardware encoding if GPU supports it:
    - NVIDIA NVENC (`h264_nvenc`, `hevc_nvenc`)
    - AMD AMF (`h264_amf`, `hevc_amf`)
    - Intel QuickSync (`h264_qsv`, `hevc_qsv`)
  - Video probing (resolution, FPS, duration, audio stream detection)
  - Frame extraction from video
  - Resolution presets: 480p, 580p, 720p, 1080p

---

## Python Environment

- **Version**: Python 3.10.11
- **Key packages already installed**:
  - `fastapi` + `uvicorn` — web API server (proven pattern in DropCat)
  - `Pillow` — image processing
  - `anthropic` — Anthropic SDK
  - `openai` — OpenAI SDK
  - `ollama` — Ollama client

The DropCat project's `requirements.txt` covers all of the above. If Open Claw uses the same Python env, these are already present.

---

## Infrastructure Patterns Already Built (Reusable)

DropCat Studio contains several modules that Open Claw could potentially reuse or reference rather than rebuilding:

| Module | Location | What it does |
|--------|----------|--------------|
| LLM Router | `core/llm_router.py` | Auto-selects Ollama / Anthropic / OpenAI, rate limiting, hot-switchable |
| API Key Manager | `core/keys.py` | Loads keys from config or `C:/JSON Credentials/` |
| Job Queue | `core/job_manager.py` | Sequential GPU job queue, prevents GPU contention |
| Session Tracker | `core/session.py` | Registers generated files so they're discoverable cross-feature |
| ffmpeg Utils | `core/ffmpeg_utils.py` | Video probing, frame extraction, hardware encoder detection |
| Hardware Encoders | `core/hw_encoders.py` | Detects NVENC/AMF/QuickSync at startup |
| WanGP Worker | `services/wangp_worker.py` | Persistent WanGP HTTP server (loads model once, serves requests) |
| Forge Client | `services/forge_client.py` | Stable Diffusion Forge API client |

These are all in `C:\Users\andre\Desktop\AI Editors\DropCat-Studio\`.

---

## Port Map — What's Running on This Machine

| Port | Service | Status |
|------|---------|--------|
| 7860 | Drop Cat Go Studio (FastAPI) | Runs when launched via `launch.bat` |
| 7861 | Forge Stable Diffusion | Runs when Andrew starts Forge manually |
| 7899 | WanGP video worker | Starts automatically when DropCat launches |
| 8019 | ACE-Step music worker | Starts automatically when DropCat launches |
| 11434 | Ollama | Always running (system service) |

**Open Claw should avoid port 7860** and pick something else (e.g., 7870, 8080, 8888).

If Open Claw also needs WanGP or ACE-Step, coordinate with Andrew — both projects starting the same worker subprocess will conflict.

---

## Credentials & Config Files

| File | Contents |
|------|----------|
| `C:/JSON Credentials/chatbot/config.json` | Anthropic key, possibly others |
| `C:\Users\andre\Desktop\AI Editors\DropCat-Studio\config.json` | Full 53-key app config (paths, model settings, all feature params) |
| `C:\Users\andre\Desktop\AI Editors\DropCat-Studio\keys.json` | Secondary key storage (app-specific) |

Ask Andrew before reading or modifying any of these. The credential file at `C:/JSON Credentials/` is shared across multiple projects.

---

## What This Machine Does NOT Have (as of project handoff)

- No ElevenLabs / speech synthesis
- No Whisper / transcription (not installed)
- No ComfyUI (Forge is the SD frontend, not ComfyUI)
- No Docker
- OpenAI key may be empty — check with Andrew

---

*Last updated: 2026-04-12. Reflects state at end of Drop Cat Go Studio Phases 1–3.*
