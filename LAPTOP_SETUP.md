# Drop Cat Go Studio -- Laptop Setup Guide
# (Instructions for Claude Code)

This document is written for a Claude Code instance running on a fresh Windows gaming
laptop. Follow every step in order. Do not skip the verification steps.

Target machine: RTX 4070 (12 GB VRAM), Windows 11.
Goal: full DCS stack running offline (no internet needed at the campsite).

---

## 0. Read Before Starting

- All credential files should already be in a zip on this machine or a USB drive.
  Look for `dcs-laptop-setup.zip` on the Desktop.
- The app repo is at https://github.com/ -- ask Andrew for the exact repo URL or
  check the zip for a `REPO_URL.txt` file.
- Never pause to ask Andrew to run something -- do it yourself via Bash/PowerShell.
- After every major step, run a verification command and confirm it passed before
  continuing.

---

## 1. Check and Install Prerequisites

### 1a. Git
```powershell
git --version
```
If missing: `winget install --id Git.Git -e --source winget`

### 1b. Python 3.11
```powershell
python --version
```
Need 3.11.x. If missing or wrong version:
`winget install --id Python.Python.3.11 -e --source winget`
Then restart the shell so the new PATH takes effect.

### 1c. ffmpeg
```powershell
ffmpeg -version
```
If missing: `winget install --id Gyan.FFmpeg -e --source winget`
Verify it is on PATH after install.

### 1d. Node.js (for JS syntax checks only)
```powershell
node --version
```
If missing: `winget install --id OpenJS.NodeJS.LTS -e --source winget`

---

## 2. Clone the Repository

```powershell
cd C:\
git clone <REPO_URL> DropCat-Studio
cd C:\DropCat-Studio
```

Verify:
```powershell
ls C:\DropCat-Studio\app.py
```

---

## 3. Install Python Dependencies

```powershell
cd C:\DropCat-Studio
python -m pip install --upgrade pip
pip install -r requirements.txt
```

Verify (should print no errors):
```powershell
python -c "import fastapi, PIL, anthropic; print('deps OK')"
```

---

## 4. Restore Credentials

Unzip `dcs-laptop-setup.zip` (ask Andrew where it is -- Desktop or USB drive).

```powershell
# Create the credentials folder if needed
New-Item -ItemType Directory -Force "C:\JSON Credentials"

# Copy the credentials file from the zip extract location
# (adjust source path to wherever you unzipped it)
Copy-Item ".\QB_WC_credentials.json" "C:\JSON Credentials\QB_WC_credentials.json"
```

Verify:
```powershell
Get-Content "C:\JSON Credentials\QB_WC_credentials.json"
```
Should show JSON with `anthropic_key` present and non-empty.

---

## 5. Install Pinokio

Pinokio is the one-click launcher for WanGP and ACE-Step.

Download the latest Windows installer from https://pinokio.computer and run it.
Default install location: `C:\pinokio\`

Verify after install:
```powershell
ls C:\pinokio\api
```

---

## 6. Install WanGP via Pinokio

Open Pinokio (it is a desktop app), search for "WanGP" or "Wan2GP" and install it.
Let it finish completely -- it downloads several GB of base files.

Default install path: `C:\pinokio\api\wan.git\app`

Verify:
```powershell
ls C:\pinokio\api\wan.git\app\wgp.py
```

### 6a. Download a model suited for 12 GB VRAM

The 14B model used on the desktop is too large for 12 GB. Inside WanGP's UI, download
one of these (whichever is available; pick the first one that appears):

Priority order:
1. `Wan2.1-I2V-480p-14B` with int8 quantization -- will RAM-stream but still works
2. `LTX-Video 2.0` or `LTX-Video 2.1` -- fast, fits cleanly, good quality
3. `Wan2.1-I2V-480p-1.3B` -- smallest, fastest, lower quality but fine for friends

After downloading, note the exact model name shown in the WanGP dropdown. You will
need it in Step 9.

### 6b. Configure WanGP memory profile

Edit `C:\pinokio\api\wan.git\app\wgp_config.json`.
Find and set these keys (add them if missing):

```json
"profile": 3,
"video_profile": 3,
"image_profile": 3,
"vae_config": 1,
"compile": "",
"attention_mode": "auto"
```

Profile 3 = LowRAM_HighVRAM_Medium (best performance for >= 12 GB cards).
`compile: ""` means off -- do not enable it, the warmup cost is not worth it.

---

## 7. Install ACE-Step via Pinokio

In Pinokio, search for "ACE-Step" and install it.

After install, find the actual app root path:
```powershell
ls C:\pinokio\api\*ace* -ErrorAction SilentlyContinue
ls C:\pinokio\api\*ACE* -ErrorAction SilentlyContinue
```

Note the full path -- you will need it in Step 9. It will look something like:
`C:\pinokio\api\ace-step.git\app`

---

## 8. Install Ollama (for offline AI features)

Ollama provides the local LLM when there is no internet connection.

```powershell
winget install --id Ollama.Ollama -e --source winget
```

After install, start Ollama then pull a fast model:
```powershell
Start-Process ollama -ArgumentList "serve" -NoNewWindow
Start-Sleep -Seconds 5
ollama pull dolphin3:8b
```

Verify:
```powershell
ollama list
```
Should show `dolphin3:8b` in the list.

For vision (image analysis), also pull:
```powershell
ollama pull qwen2.5vl:7b
```

---

## 9. Configure Drop Cat Go Studio

Copy the template config from the zip:
```powershell
Copy-Item ".\config-laptop-template.json" "C:\DropCat-Studio\config.json"
```

Then open `C:\DropCat-Studio\config.json` and update these specific keys to match
what you found in steps 6 and 7:

```json
"wan2gp_root":  "<path to wan.git/app -- e.g. C:\\pinokio\\api\\wan.git\\app>",
"acestep_root": "<path to ace-step app folder found in step 7>",
"wan_model":    "<exact model name from WanGP dropdown>",
"fun_model":    "<same model name>"
```

Leave all other keys as they are in the template.

Verify the config loads:
```powershell
cd C:\DropCat-Studio
python -c "from core import config as c; print(c.get('wan2gp_root'))"
```
Should print the WanGP path, not an error.

---

## 10. Run Smoke Tests

```powershell
cd C:\DropCat-Studio
python tests/smoke.py
```

All tests should pass. If any fail, read the error carefully -- it will point at a
missing dependency or misconfigured path.

Also check JS for syntax errors:
```powershell
node --check static/js/app.js
node --check static/js/tab-fun-videos.js
```

---

## 11. First Launch

```powershell
cd C:\DropCat-Studio
python app.py
```

Watch the console output. After a few seconds you should see a port number (7860-7879).
Open that URL in the browser.

Check the service status pills in the header:
- WanGP: may show red until the first video job starts (lazy-loads the model)
- ACE-Step: same
- Ollama: should go green within 10 seconds

---

## 12. Test a Video Generation

1. Go to the "Create Videos" tab
2. Drop any image in as the start image
3. Type a short prompt ("a cat jumping in slow motion")
4. Set clips to 1, steps to 20 (faster for a test)
5. Click "Create Story"
6. Watch the log -- you should see WanGP load the model and begin denoising steps

If you see steps incrementing (Step 1/20, 2/20...) everything is working.

---

## 13. Verify Image Generation

1. Go to the "Generate Images" tab (SD Prompts)
2. Type a prompt, click Generate
3. If Forge SD is not installed (it probably is not on the laptop), image generation
   will use the OpenAI API (needs internet) or Ollama for prompts only
4. For offline image generation, you would need to install Forge SD separately --
   skip this if internet is not available at the campsite

---

## Known Limitations on RTX 4070 (12 GB)

- Wan 14B I2V model will RAM-stream (~10-15s per step). Use LTX or 1.3B for speed.
- Cannot run WanGP and ACE-Step simultaneously -- the app's GPU orchestrator handles
  this automatically (it evicts one before loading the other).
- Forge SD image generation not covered here -- install separately if needed.
- Without internet, LLM features fall back to Ollama (dolphin3:8b). Prompt quality
  will be lower than Anthropic Claude but perfectly usable.

---

## Troubleshooting

**Splash shows raw HTML text ("Connecting to server..." not styled):**
A JS syntax error killed all scripts. Run:
`node --check static/js/app.js`
and fix whatever it reports.

**WanGP never starts / stays red:**
Check `wan2gp_root` in config.json points to the right folder.
Try starting WanGP manually from Pinokio and see if it errors.

**"Step 0/N" forever:**
WanGP is loading the model (can take 2-3 minutes on first run). Wait it out.
If it never advances past step 0 after 5 minutes, restart the WanGP worker from
Settings > Services.

**ACE-Step not found:**
Double-check `acestep_root` in config.json. The path must point to the folder that
contains `run_inference.py`.

**Port already in use:**
Another process is on 7860-7879. Find and kill it:
`netstat -ano | findstr :786`
then `taskkill /PID <pid> /F`
