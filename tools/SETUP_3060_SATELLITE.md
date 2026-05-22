# Task: Configure this machine as a satellite service node

You are Claude Code running on the 3060 machine. The 5080 machine runs the main
Drop Cat Go Studio app and will connect to services on THIS machine over the LAN.
Your job is to configure three services to accept network connections, then report
the machine's IP address back to the user.

---

## Step 1 -- Find this machine's local IP

Run this and show the user the output:

```powershell
Get-NetIPAddress -AddressFamily IPv4 | Where-Object { $_.IPAddress -notlike '127.*' -and $_.PrefixOrigin -ne 'WellKnown' } | Select-Object IPAddress, InterfaceAlias | Format-Table
```

The user will need to enter this IP into DCS Settings on the 5080 when you are done.
Recommend they assign this machine a static IP in their router.

---

## Step 2 -- Open firewall ports

Run this in PowerShell as Administrator. If any rule already exists, that is fine.

```powershell
New-NetFirewallRule -DisplayName "DCS ACE-Step" -Direction Inbound -Protocol TCP -LocalPort 8019 -Action Allow -ErrorAction SilentlyContinue
New-NetFirewallRule -DisplayName "DCS Ollama"   -Direction Inbound -Protocol TCP -LocalPort 11434 -Action Allow -ErrorAction SilentlyContinue
New-NetFirewallRule -DisplayName "DCS Forge"    -Direction Inbound -Protocol TCP -LocalPort 7861 -Action Allow -ErrorAction SilentlyContinue
```

---

## Step 3 -- Configure ACE-Step to accept network connections (port 8019)

First, find the ACE-Step installation:

```powershell
$paths = @("C:\DropCatGo-Music\ACE-Step-1.5", "C:\pinokio\api\ace-step.git\app", "C:\ACE-Step")
$found = $paths | Where-Object { Test-Path $_ } | Select-Object -First 1
Write-Host "Found ACE-Step at: $found"
```

If none of those paths exist, search for it:

```powershell
Get-ChildItem C:\ -Recurse -Depth 4 -Filter "api_server.py" -ErrorAction SilentlyContinue | Select-Object FullName
```

Once you have the path, create a network startup script. Replace `ACESTEP_ROOT` with
the actual path you found:

```powershell
$aceRoot = "ACESTEP_ROOT"   # <-- replace with actual path
$script = @"
@echo off
cd /d "$aceRoot"
call .venv\Scripts\activate.bat
python acestep\api_server.py --host 0.0.0.0 --port 8019
"@
$script | Out-File -FilePath "C:\DCS-satellite\start_acestep.bat" -Encoding utf8
```

Create the folder first:
```powershell
New-Item -ItemType Directory -Force -Path "C:\DCS-satellite"
```

Verify ACE-Step is running (or start it):
```powershell
Start-Process "C:\DCS-satellite\start_acestep.bat" -WindowStyle Minimized
Start-Sleep 10
Invoke-WebRequest -Uri "http://localhost:8019/health" -UseBasicParsing | Select-Object StatusCode, Content
```

Should return `StatusCode: 200`.

---

## Step 4 -- Configure Ollama to accept network connections (port 11434)

Check if Ollama is installed:
```powershell
Get-Command ollama -ErrorAction SilentlyContinue | Select-Object Source
```

If not installed, tell the user to download it from https://ollama.com and install it,
then continue.

Set Ollama to listen on all interfaces (persists across reboots):
```powershell
[System.Environment]::SetEnvironmentVariable("OLLAMA_HOST", "0.0.0.0:11434", "Machine")
```

Stop and restart Ollama so the new environment variable takes effect:
```powershell
Stop-Process -Name "ollama" -ErrorAction SilentlyContinue
Start-Sleep 3
Start-Process "ollama" -ArgumentList "serve" -WindowStyle Hidden
Start-Sleep 5
```

Pull the vision model the 5080 expects (if not already present):
```powershell
ollama pull qwen3-vl:8b
```

Verify Ollama is reachable:
```powershell
Invoke-WebRequest -Uri "http://localhost:11434/api/tags" -UseBasicParsing | Select-Object StatusCode
```

Should return `StatusCode: 200`.

---

## Step 5 -- Configure Forge to accept network connections (port 7861)

Find the Forge installation:
```powershell
$forgePaths = @("C:\forge", "C:\pinokio\api\forge.git\app", "C:\stable-diffusion-webui-forge")
$forgeRoot = $forgePaths | Where-Object { Test-Path "$_\webui-user.bat" } | Select-Object -First 1
Write-Host "Found Forge at: $forgeRoot"
```

If not found, search:
```powershell
Get-ChildItem C:\ -Recurse -Depth 4 -Filter "webui-user.bat" -ErrorAction SilentlyContinue | Select-Object FullName
```

Read the current webui-user.bat to see what COMMANDLINE_ARGS already says:
```powershell
Get-Content "$forgeRoot\webui-user.bat"
```

Edit it to add `--listen --api` to the COMMANDLINE_ARGS line. If the line already has
`--api`, just add `--listen`. If COMMANDLINE_ARGS is blank, set it. Example result:

```
set COMMANDLINE_ARGS=--listen --api --port 7861
```

Make the edit using Read and Edit tools on the file `$forgeRoot\webui-user.bat`.

Then launch Forge:
```powershell
Start-Process "$forgeRoot\webui-user.bat" -WindowStyle Minimized
```

Wait up to 3 minutes for it to load, then verify:
```powershell
Start-Sleep 60
Invoke-WebRequest -Uri "http://localhost:7861/sdapi/v1/sd-models" -UseBasicParsing | Select-Object StatusCode
```

---

## Step 6 -- Create a single startup script for all three services

```powershell
$startup = @'
@echo off
echo Starting DCS satellite services...
start "ACE-Step"  /min "C:\DCS-satellite\start_acestep.bat"
start "Forge"     /min "FORGE_ROOT\webui-user.bat"
echo Services started. Ollama runs automatically via system service.
'@
$startup | Out-File -FilePath "C:\DCS-satellite\start_all.bat" -Encoding utf8
```

Replace `FORGE_ROOT` with the actual Forge path you found in Step 5.

Add a shortcut to this script in the Windows Startup folder so it runs on login:
```powershell
$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut("$env:APPDATA\Microsoft\Windows\Start Menu\Programs\Startup\DCS-satellite.lnk")
$shortcut.TargetPath = "C:\DCS-satellite\start_all.bat"
$shortcut.Save()
```

---

## Step 7 -- Report back to the user

Tell the user:
1. The IP address of this machine (from Step 1)
2. Which services are running and on which ports
3. Any services that could not be found or started, and why

Then tell the user to:
- Open DCS Settings on the 5080 machine
- Set **ACE-Step Host** to this machine's IP (e.g. `192.168.1.50`)
- Set **Ollama URL** to `http://[this-IP]:11434`
- Set **Forge URL** to `http://[this-IP]:7861`
- Click **Save Settings**
