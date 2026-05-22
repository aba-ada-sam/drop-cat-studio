# Task: Configure this machine as a satellite service node

You are Claude Code running on the 3060 machine. The 5080 machine runs the main
Drop Cat Go Studio app and will connect to services on THIS machine over the LAN.
Your job is to configure three services to accept network connections, then report
the machine's IP address back to the user.

Pinokio is already installed on this machine. WanGP is also installed but not
used by this task -- leave it alone.

---

## Step 1 -- Find this machine's local IP

Run this and show the user the output:

```powershell
Get-NetIPAddress -AddressFamily IPv4 | Where-Object { $_.IPAddress -notlike '127.*' -and $_.PrefixOrigin -ne 'WellKnown' } | Select-Object IPAddress, InterfaceAlias | Format-Table
```

Note the IP for use in Step 7. Tell the user to assign a static IP in their router.

---

## Step 2 -- Open firewall ports

Run as Administrator:

```powershell
New-NetFirewallRule -DisplayName "DCS ACE-Step" -Direction Inbound -Protocol TCP -LocalPort 8019 -Action Allow -ErrorAction SilentlyContinue
New-NetFirewallRule -DisplayName "DCS Ollama"   -Direction Inbound -Protocol TCP -LocalPort 11434 -Action Allow -ErrorAction SilentlyContinue
New-NetFirewallRule -DisplayName "DCS Forge"    -Direction Inbound -Protocol TCP -LocalPort 7861 -Action Allow -ErrorAction SilentlyContinue
```

---

## Step 3 -- Find ACE-Step and configure it for network access (port 8019)

### 3a. Find the installation

Pinokio installs apps under `C:\pinokio\api\`. Search for ACE-Step:

```powershell
# Check known Pinokio paths first
$acePaths = @(
    "C:\pinokio\api\ace-step.git\app",
    "C:\pinokio\api\ace_step.git\app",
    "C:\pinokio\api\ACE-Step.git\app",
    "C:\DropCatGo-Music\ACE-Step-1.5"
)
$aceRoot = $acePaths | Where-Object { Test-Path "$_\acestep\api_server.py" } | Select-Object -First 1

if (-not $aceRoot) {
    # Broader search in Pinokio api folder
    $aceRoot = Get-ChildItem "C:\pinokio\api" -Directory | ForEach-Object {
        $candidate = "$($_.FullName)\app"
        if (Test-Path "$candidate\acestep\api_server.py") { $candidate }
    } | Select-Object -First 1
}

if (-not $aceRoot) {
    # Full disk search fallback
    $aceRoot = (Get-ChildItem C:\ -Recurse -Depth 6 -Filter "api_server.py" -ErrorAction SilentlyContinue | Where-Object { $_.DirectoryName -like "*acestep*" } | Select-Object -First 1).DirectoryName
    if ($aceRoot) { $aceRoot = Split-Path $aceRoot -Parent }
}

Write-Host "ACE-Step root: $aceRoot"
```

### 3b. Find the Python interpreter

Pinokio may use a venv, conda env, or uv. Check in order:

```powershell
$python = $null
$candidates = @(
    "$aceRoot\.venv\Scripts\python.exe",
    "$aceRoot\env\python.exe",
    "$aceRoot\venv\Scripts\python.exe",
    (Get-ChildItem "$aceRoot" -Recurse -Depth 3 -Filter "python.exe" -ErrorAction SilentlyContinue | Select-Object -First 1).FullName
)
$python = $candidates | Where-Object { $_ -and (Test-Path $_) } | Select-Object -First 1

if (-not $python) {
    # Try uv
    $uv = (Get-Command uv -ErrorAction SilentlyContinue).Source
    Write-Host "No venv Python found. uv available: $uv"
} else {
    Write-Host "Python: $python"
}
```

### 3c. Create startup script

```powershell
New-Item -ItemType Directory -Force -Path "C:\DCS-satellite" | Out-Null

if ($python) {
    $startScript = "@echo off`r`ncd /d `"$aceRoot`"`r`n`"$python`" acestep\api_server.py --host 0.0.0.0 --port 8019`r`n"
} else {
    $startScript = "@echo off`r`ncd /d `"$aceRoot`"`r`nuv run --no-sync acestep-api --host 0.0.0.0 --port 8019`r`n"
}
$startScript | Out-File -FilePath "C:\DCS-satellite\start_acestep.bat" -Encoding utf8
Write-Host "Created C:\DCS-satellite\start_acestep.bat"
```

### 3d. Test it

```powershell
Start-Process "C:\DCS-satellite\start_acestep.bat" -WindowStyle Minimized
Write-Host "Waiting 20 seconds for ACE-Step to load..."
Start-Sleep 20
try {
    $r = Invoke-WebRequest -Uri "http://localhost:8019/health" -UseBasicParsing -TimeoutSec 5
    Write-Host "ACE-Step OK: $($r.StatusCode)"
} catch {
    Write-Host "ACE-Step not responding yet -- it may still be loading models. Check again in 60s."
}
```

---

## Step 4 -- Configure Ollama (port 11434)

### 4a. Check if installed

```powershell
$ollamaPath = (Get-Command ollama -ErrorAction SilentlyContinue).Source
if ($ollamaPath) { Write-Host "Ollama found: $ollamaPath" }
else { Write-Host "Ollama NOT installed. User must download from https://ollama.com and install, then re-run from Step 4." }
```

If not installed: stop here, tell the user to install Ollama, then come back to Step 4.

### 4b. Set to listen on all interfaces

```powershell
[System.Environment]::SetEnvironmentVariable("OLLAMA_HOST", "0.0.0.0:11434", "Machine")
Write-Host "OLLAMA_HOST set to 0.0.0.0:11434 (persists across reboots)"
```

### 4c. Restart Ollama with new setting

```powershell
Stop-Process -Name "ollama" -Force -ErrorAction SilentlyContinue
Start-Sleep 3
Start-Process "ollama" -ArgumentList "serve" -WindowStyle Hidden
Start-Sleep 8
```

### 4d. Pull the model the 5080 expects

```powershell
ollama pull qwen3-vl:8b
```

This downloads ~5GB on first run. Tell the user it may take a few minutes.

### 4e. Verify

```powershell
try {
    $r = Invoke-WebRequest -Uri "http://localhost:11434/api/tags" -UseBasicParsing -TimeoutSec 5
    Write-Host "Ollama OK: $($r.StatusCode)"
} catch { Write-Host "Ollama not responding: $_" }
```

---

## Step 5 -- Configure Forge (port 7861)

### 5a. Find the installation

```powershell
$forgePaths = @(
    "C:\pinokio\api\forge.git\app",
    "C:\pinokio\api\stable-diffusion-webui-forge.git\app",
    "C:\forge",
    "C:\stable-diffusion-webui-forge"
)
$forgeRoot = $forgePaths | Where-Object { Test-Path "$_\webui-user.bat" } | Select-Object -First 1

if (-not $forgeRoot) {
    $forgeRoot = (Get-ChildItem "C:\pinokio\api" -Directory | ForEach-Object {
        $candidate = "$($_.FullName)\app"
        if (Test-Path "$candidate\webui-user.bat") { $candidate }
    } | Select-Object -First 1)
}

if (-not $forgeRoot) {
    $forgeRoot = (Get-ChildItem C:\ -Recurse -Depth 4 -Filter "webui-user.bat" -ErrorAction SilentlyContinue | Select-Object -First 1).DirectoryName
}

Write-Host "Forge root: $forgeRoot"
```

### 5b. Read and edit webui-user.bat

Read the file first:
```powershell
Get-Content "$forgeRoot\webui-user.bat"
```

The file has a line like `set COMMANDLINE_ARGS=...`. Edit that file using your Edit tool
to ensure the COMMANDLINE_ARGS line reads:

```
set COMMANDLINE_ARGS=--listen --api --port 7861
```

`--listen` makes it accept connections from other machines.
`--api` enables the REST API that DCS uses.

If COMMANDLINE_ARGS is missing entirely, add the line after the `@echo off` line.

### 5c. Launch and verify

```powershell
Start-Process "$forgeRoot\webui-user.bat" -WindowStyle Minimized
Write-Host "Forge loading -- this takes 2-3 minutes on first run. Waiting 90 seconds..."
Start-Sleep 90
try {
    $r = Invoke-WebRequest -Uri "http://localhost:7861/sdapi/v1/sd-models" -UseBasicParsing -TimeoutSec 10
    Write-Host "Forge OK: $($r.StatusCode)"
} catch { Write-Host "Forge still loading or not responding: $_. Check again in 60s." }
```

---

## Step 6 -- Create startup script and add to Windows startup

```powershell
# Build the startup script using the actual paths found above
$startAll = "@echo off`r`necho Starting DCS satellite services...`r`n"
$startAll += "start `"ACE-Step`" /min `"C:\DCS-satellite\start_acestep.bat`"`r`n"
$startAll += "start `"Forge`" /min `"$forgeRoot\webui-user.bat`"`r`n"
$startAll += "echo Done. Ollama starts automatically as a system service.`r`n"
$startAll | Out-File -FilePath "C:\DCS-satellite\start_all.bat" -Encoding utf8

# Add to Windows startup folder
$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut("$env:APPDATA\Microsoft\Windows\Start Menu\Programs\Startup\DCS-satellite.lnk")
$shortcut.TargetPath = "C:\DCS-satellite\start_all.bat"
$shortcut.Save()

Write-Host "Startup script created at C:\DCS-satellite\start_all.bat"
Write-Host "Shortcut added to Windows startup folder -- services will start on login"
```

---

## Step 7 -- Report to user

Tell the user exactly:

1. **This machine's IP address** (from Step 1)
2. **Which services are running** (ACE-Step on 8019, Ollama on 11434, Forge on 7861)
3. **Any failures** -- which service could not start and the error message
4. **What to do on the 5080** -- open DCS, go to Settings, enter:
   - ACE-Step Host: `[this IP]`
   - Ollama URL: `http://[this IP]:11434`
   - Forge URL: `http://[this IP]:7861`
   - Click Save Settings
5. **To start services after reboot**: double-click `C:\DCS-satellite\start_all.bat`
   (also runs automatically at login via the startup shortcut)
