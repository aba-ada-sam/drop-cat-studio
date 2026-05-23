# Task: Verify and fix DCS satellite setup on this machine

You are Claude Code on the 3060 machine. The automated setup script
`3060_FULL_SETUP.bat` should have already run. Your job is to verify
everything worked, fix anything that failed, and confirm the services
are reachable from the network.

---

## Step 1 -- Confirm what the batch script did

Check for the expected outputs:

```powershell
# Firewall rules
Get-NetFirewallRule -DisplayName "DCS*" | Select-Object DisplayName, Enabled

# Startup script
Test-Path "C:\DCS-satellite\start_all.bat"
Test-Path "C:\DCS-satellite\start_acestep.bat"

# Startup folder shortcut
Test-Path "$env:APPDATA\Microsoft\Windows\Start Menu\Programs\Startup\DCS-satellite.lnk"

# Ollama
(Get-Command ollama -ErrorAction SilentlyContinue).Source
[System.Environment]::GetEnvironmentVariable("OLLAMA_HOST", "Machine")
```

---

## Step 2 -- Verify services are responding

```powershell
# ACE-Step
try { (Invoke-WebRequest -Uri "http://localhost:8020/health" -UseBasicParsing -TimeoutSec 5).StatusCode } catch { "NOT RESPONDING" }

# Ollama
try { (Invoke-WebRequest -Uri "http://localhost:11434/api/tags" -UseBasicParsing -TimeoutSec 5).StatusCode } catch { "NOT RESPONDING" }

# Forge
try { (Invoke-WebRequest -Uri "http://localhost:7861/sdapi/v1/sd-models" -UseBasicParsing -TimeoutSec 10).StatusCode } catch { "NOT RESPONDING" }
```

---

## Step 3 -- Fix any failures

### ACE-Step not responding

Find ACE-Step and check the startup script is correct:

```powershell
# Find api_server.py
Get-ChildItem C:\pinokio\api -Recurse -Depth 5 -Filter "api_server.py" -ErrorAction SilentlyContinue | Select-Object FullName

# Show current startup script
Get-Content "C:\DCS-satellite\start_acestep.bat"
```

If the path is wrong, update the startup script:

```powershell
$aceRoot = "ACTUAL_PATH_HERE"  # replace with path containing acestep\api_server.py
$python = (Get-ChildItem $aceRoot -Recurse -Depth 3 -Filter "python.exe" -ErrorAction SilentlyContinue | Select-Object -First 1).FullName
"@echo off`r`ncd /d `"$aceRoot`"`r`n`"$python`" acestep\api_server.py --host 0.0.0.0 --port 8020" | Out-File "C:\DCS-satellite\start_acestep.bat" -Encoding utf8
Start-Process "C:\DCS-satellite\start_acestep.bat" -WindowStyle Minimized
```

### Forge not responding

Check if it has the --listen flag:
```powershell
$forgeRoot = (Get-ChildItem C:\pinokio\api -Recurse -Depth 3 -Filter "webui-user.bat" -ErrorAction SilentlyContinue | Select-Object -First 1).DirectoryName
Get-Content "$forgeRoot\webui-user.bat"
```

Edit the file to ensure this line exists:
```
set COMMANDLINE_ARGS=--listen --api --port 7861
```

Then start Forge:
```powershell
Start-Process "$forgeRoot\webui-user.bat" -WindowStyle Minimized
```

### Ollama not responding

```powershell
# Check the env var was set
[System.Environment]::GetEnvironmentVariable("OLLAMA_HOST", "Machine")

# Restart with the right binding
Stop-Process -Name "ollama" -Force -ErrorAction SilentlyContinue
Start-Sleep 3
Start-Process "ollama" -ArgumentList "serve" -WindowStyle Hidden
Start-Sleep 8
Invoke-WebRequest -Uri "http://localhost:11434/api/tags" -UseBasicParsing -TimeoutSec 5
```

---

## Step 4 -- Get the IP and report to user

```powershell
Get-NetIPAddress -AddressFamily IPv4 | Where-Object { $_.IPAddress -notlike '127.*' -and $_.PrefixOrigin -ne 'WellKnown' } | Select-Object IPAddress, InterfaceAlias
```

Tell the user:
1. This machine's IP address
2. Which services are confirmed working (200 status) and which are not
3. To open DCS Settings on the 5080 and enter:
   - **ACE-Step Host**: this IP (just the IP, no http://)
   - **Ollama URL**: `http://[this IP]:11434`
   - **Forge URL**: `http://[this IP]:7861`
   - Click **Save Settings**
4. Services start automatically at login. Manual start: double-click `C:\DCS-satellite\start_all.bat`
