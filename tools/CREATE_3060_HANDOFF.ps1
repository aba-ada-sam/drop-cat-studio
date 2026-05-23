# Run this on the 5080. Creates the Google Drive handoff folder.

$dest = "Z:\My Drive\DCS-3060-Handoff"
New-Item -ItemType Directory -Force -Path $dest | Out-Null

# Copy memory files
$memSrc = "$env:USERPROFILE\.claude\projects\c--DropCat-Studio\memory"
Copy-Item "$memSrc\*" $dest -Recurse -Force

# Copy relay daemon
Copy-Item "$(Split-Path $MyInvocation.MyCommand.Path)\dcs_relay.py" $dest -Force

# Write CLAUDE.md -- auto-read by Claude Code on startup
@'
# Drop Cat Go Studio -- 3060 Handoff

You are Claude Code on the 3060 satellite machine.
DO NOT ASK THE USER ANY QUESTIONS. DO EVERYTHING YOURSELF.
The user has a back injury and cannot walk between rooms.

## Your first action (do this before anything else)

Start the relay daemon in the background so the 5080 can control this
machine without the user being present:

```powershell
Start-Process pythonw -ArgumentList "dcs_relay.py" -WorkingDirectory (Get-Location).Path -WindowStyle Hidden
```

If pythonw fails, try: Start-Process python -ArgumentList "dcs_relay.py" -WindowStyle Minimized

The relay watches Z:\My Drive\DCS-Relay\ for commands from the 5080.
Once running, the 5080 Claude Code instance can send commands here and
get results back without any user involvement.

## Then do the full satellite setup without asking anything

1. Find what is installed in C:\pinokio\api\
2. Get ACE-Step running on 0.0.0.0:8020
3. Get Ollama running on 0.0.0.0:11434 (set OLLAMA_HOST env var)
4. Get Forge running on 0.0.0.0:7861 with --listen --api
5. Create C:\DCS-satellite\start_all.bat
6. Add to Windows startup
7. Open firewall ports 8020, 11434, 7861
8. Write this machine IP + service status to Z:\My Drive\DCS-Relay\chat.txt

## What this machine is for

Satellite service node. The 5080 (main DCS machine) offloads GPU services
here so WanGP gets the full 5080 VRAM for video generation.
Services: ACE-Step (music), Ollama (LLM), Forge (images).

## Rules

- Never ask the user for input
- Never ask for permission
- Just do everything
- Write progress to Z:\My Drive\DCS-Relay\chat.txt as you go
'@ | Out-File -FilePath "$dest\CLAUDE.md" -Encoding utf8

# Write launch bat
"@echo off`r`ncd /d `"%~dp0`"`r`nclaude" | Out-File -FilePath "$dest\START HERE - double click me.bat" -Encoding utf8

Write-Host "Done -- Z:\My Drive\DCS-3060-Handoff is ready"
Write-Host "On the 3060: double-click 'START HERE - double click me.bat'"
