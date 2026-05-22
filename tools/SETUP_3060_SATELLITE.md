# 3060 Satellite Node Setup

This machine will run three services that offload non-video work from the 5080
machine: ACE-Step (music generation), Ollama (LLM/vision), and Forge SD (images).
The 5080 dedicates all 15.9 GB to WanGP video generation with no eviction pauses.

Services stay running permanently. The 5080 connects to them over the LAN.

---

## 1. Find This Machine's IP Address

Run in PowerShell:

```powershell
(Get-NetIPAddress -AddressFamily IPv4 | Where-Object { $_.IPAddress -notlike '127.*' -and $_.PrefixOrigin -eq 'Dhcp' })[0].IPAddress
```

Note that IP. You will enter it in DCS Settings on the 5080 at the end.
Give this machine a static local IP in your router to prevent it changing.

---

## 2. ACE-Step (music generation -- port 8019)

### Install

ACE-Step should already be installed on this machine if it was previously used.
Check: `Test-Path "C:\DropCatGo-Music\ACE-Step-1.5"` or wherever it lives.

If not installed, use Pinokio to install "ACE-Step" -- or clone the repo and
run `pip install -e .` inside a `.venv`.

### Configure to accept network connections

ACE-Step must bind to `0.0.0.0` (all interfaces), not `127.0.0.1`.

Create a startup script `C:\DropCatGo-Music\start_acestep_network.bat`:

```bat
@echo off
cd /d "C:\DropCatGo-Music\ACE-Step-1.5"
call .venv\Scripts\activate.bat
python acestep\api_server.py --host 0.0.0.0 --port 8019
```

Replace the path if ACE-Step is installed elsewhere. The key flag is `--host 0.0.0.0`.

Test it works: run the script, then from the 5080 machine run:
`curl http://[3060-IP]:8019/health` -- should return `{"status":"ok"}`.

---

## 3. Ollama (LLM + vision -- port 11434)

### Install

Download from https://ollama.com and install. Pull the model used on the 5080:

```powershell
ollama pull qwen3-vl:8b
```

### Configure to accept network connections

Ollama binds to localhost by default. Add a system environment variable:

```powershell
[System.Environment]::SetEnvironmentVariable("OLLAMA_HOST", "0.0.0.0:11434", "Machine")
```

Then restart Ollama (stop the system tray icon and relaunch, or reboot).

Verify: `curl http://[3060-IP]:11434/api/tags` from the 5080 should list models.

---

## 4. Forge SD (Stable Diffusion images -- port 7861)

### Install

If not installed: clone `https://github.com/lllyasviel/stable-diffusion-webui-forge`
or use Pinokio to install "Forge".

Typical location: `C:\forge` or `C:\pinokio\api\forge.git\app`.

### Configure to accept network connections and expose API

Edit `webui-user.bat` (in the Forge root) and add `--listen --api` to the
`COMMANDLINE_ARGS` line:

```bat
set COMMANDLINE_ARGS=--listen --api --port 7861
```

`--listen` makes it bind to `0.0.0.0` so the 5080 can reach it.
`--api` enables the REST API that DCS uses.

Verify: `curl http://[3060-IP]:7861/sdapi/v1/sd-models` from the 5080 should
return a JSON list of models.

---

## 5. Windows Firewall -- open inbound ports

Run in PowerShell as Administrator:

```powershell
New-NetFirewallRule -DisplayName "DCS ACE-Step" -Direction Inbound -Protocol TCP -LocalPort 8019 -Action Allow
New-NetFirewallRule -DisplayName "DCS Ollama"   -Direction Inbound -Protocol TCP -LocalPort 11434 -Action Allow
New-NetFirewallRule -DisplayName "DCS Forge"    -Direction Inbound -Protocol TCP -LocalPort 7861 -Action Allow
```

---

## 6. Auto-start on login (optional but recommended)

Create `C:\DropCatGo-Music\start_satellite.bat`:

```bat
@echo off
REM -- ACE-Step (network mode)
start "ACE-Step" /min cmd /c "C:\DropCatGo-Music\start_acestep_network.bat"

REM -- Forge (already handles its own startup via webui-user.bat)
start "Forge" /min "C:\forge\webui-user.bat"

REM -- Ollama auto-starts as a system service after setting OLLAMA_HOST above
```

Add a shortcut to this batch file in your Startup folder:
`shell:startup` in Run dialog -> paste shortcut there.

---

## 7. Configure DCS on the 5080 machine

Once all three services are running and reachable, open DCS Settings on the 5080:

| Setting | Value |
|---------|-------|
| ACE-Step Host | `[3060-IP]` (e.g. `192.168.1.50`) |
| Ollama URL | `http://[3060-IP]:11434` |
| Forge URL | `http://[3060-IP]:7861` |

Save Settings. DCS will:
- Stop trying to start ACE-Step/Ollama locally
- No longer evict WanGP when generating music (they're on separate hardware)
- WanGP stays warm between all clip jobs -- no 30-60s reload pauses

---

## 8. Verify the split is working

On the 5080, open DCS and check the service status pills:
- WanGP: green (local, owns full 15.9 GB)
- ACE-Step: green with "(remote)" in the tooltip
- Ollama: green with "(remote)" in the tooltip
- Forge: green (remote)

Generate a multi-clip video with music. The log should show:
```
[gpu] acquire acestep -- remote, skipping local eviction
```
instead of the old eviction sequence that killed and reloaded WanGP.

---

## Hardware notes

RTX 3060 (12 GB) service capacity:
- ACE-Step: ~7 GB loaded -- fits cleanly
- Ollama qwen3-vl:8b: ~6 GB -- fits, though not simultaneously with ACE-Step
- Forge SD: ~4-6 GB -- fits

Ollama and ACE-Step won't run simultaneously on 12 GB. That is fine -- DCS
generates music and LLM prompts at different pipeline phases. If both are
needed at the same time (rare), Ollama will briefly offload to RAM; this is
slower but not catastrophic since LLM calls are not on the critical path for
video generation.

For LTX-2 Dev13B video quality: the 3060 cannot help here (needs 20+ GB).
That model requires a 3090/4090 when you upgrade.
