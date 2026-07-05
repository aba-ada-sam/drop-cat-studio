# Drop Cat Go Studio - Setup Guide

## Prerequisites

### 1. Ollama (Required)

Drop Cat Go Studio runs **100% locally** using Ollama for all AI tasks. No internet connection needed after setup.

**Install Ollama:**
- Download from https://ollama.ai
- Run the installer
- Restart your computer to ensure Ollama is added to PATH

**Verify installation:**
```
ollama --version
```

### 2. Python 3.11+

Required for the FastAPI backend.

**Verify installation:**
```
python --version
```

Should return 3.11 or higher.

### 3. FFmpeg

Required for all video operations.

**Verify installation:**
```
ffmpeg -version
```

If missing, install from https://ffmpeg.org or via package manager.

---

## AI Provider Setup

No local model download is required. Drop Cat Go Studio's AI runs on one of:

- **Anthropic / OpenAI** (cloud) — add a key in Settings.
- **Featherless** (uncensored cloud, default for NSFW analysis) — put your key in
  `C:\JSON Credentials\featherless_api_key.txt` (or paste it in Settings). Vision uses
  `Qwen/Qwen3-VL-32B-Instruct`; text uses a large uncensored model.
- **KoboldCpp** (local, optional) — run a local KoboldCpp OpenAI server on
  `http://localhost:5001/v1`, then choose "KoboldCpp (local)" in Settings.

Anthropic/OpenAI images classified as NSFW are transparently retried on the uncensored
provider, so explicit/artistic photos still get analysed. (Ollama was removed 2026-07-05.)

---

## Running Drop Cat Go Studio

Once models are installed:

```
cd C:\DropCat-Studio
launch.bat
```

This will:
1. Check if the server is already running
2. Start the server if needed
3. Open Chrome to http://127.0.0.1:7860

The first time you use a feature, it may take extra time as Ollama loads the model into memory.

---

## Offline Operation

After the initial model download, **Drop Cat Go Studio runs completely offline**. No internet connection needed. All LLM calls use your local Ollama instance.

---

## Troubleshooting

### "Ollama not found on PATH"
- Restart your computer after installing Ollama
- Or manually add Ollama to PATH in Windows Environment Variables

### Models fail to download
- Check your internet connection
- Try running the installer again (it will resume)
- Check available disk space (models need ~30 GB)

### Server won't start
- Check that port 7860 is not already in use
- Run `launch.bat` again (it will kill zombie processes)
- Check `C:\DropCat-Studio\app.log` for errors

### AI Director returns errors
- Ensure all three models completed installation
- Run `ollama list` to verify models are present
- Restart the server: `launch.bat`

---

## Feature Activation

Once running, features light up automatically:
- **AI Director** — Talk to Ollama about creative ideas
- **SD Prompts** — Generate Stable Diffusion prompts with regional prompting (Forge Couple)
- **Video Tools** — Transform video with local processing
- **And more** — Each feature auto-detects its dependencies

Enjoy! 🎬
