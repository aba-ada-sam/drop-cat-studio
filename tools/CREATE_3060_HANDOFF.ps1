# Run this on the 5080 machine.
# Creates a ready-to-use folder on Google Drive that lets you continue
# this conversation from the 3060 by double-clicking one file.

$dest = "Z:\My Drive\DCS-3060-Handoff"
New-Item -ItemType Directory -Force -Path $dest | Out-Null

# -- Copy all memory files --
$memSrc = "$env:USERPROFILE\.claude\projects\c--DropCat-Studio\memory"
Copy-Item "$memSrc\*" $dest -Recurse -Force

# -- Write the CLAUDE.md that Claude Code auto-reads on startup --
@'
# Drop Cat Go Studio -- 3060 Handoff

You are Claude Code on the 3060 satellite machine. Andrew is continuing a
conversation that started on the 5080 machine. Read this file fully before
responding to anything.

## What this project is

Drop Cat Go Studio (DCS) -- Andrew's unified AI video production app.
FastAPI + vanilla JS. Repo at C:\DropCat-Studio on the 5080 machine,
also on GitHub at https://github.com/aba-ada-sam/drop-cat-studio.git

## What we were doing in this session (2026-05-22)

We were setting up THIS machine (the 3060) as a satellite service node
so the 5080 can offload GPU services here and dedicate its full 15.9GB
VRAM to WanGP video generation.

The 3060 should run:
- ACE-Step (AI music generation) on port 8019
- Ollama (LLM inference) on port 11434
- Forge SD (image generation) on port 7861

The 5080 will call these services over the LAN. DCS has already been
updated to route to a remote host when configured in Settings.

## What was happening when we switched machines

Andrew ran tools/3060_FULL_SETUP.bat on this machine (3060). It was
having trouble with something. Your first job is to find out what went
wrong and fix it.

## How to help

1. Ask Andrew what error or problem the bat file hit
2. Diagnose and fix it
3. Verify all three services are running and accepting network connections
4. Get the machine IP and tell Andrew what to enter in DCS Settings

## Key things to know about Andrew

- Never assign tasks to Andrew -- you do the work, he watches
- Never paste URLs for him to open -- use Start-Process instead
- Always commit code changes immediately (but you are on the 3060, not
  the main repo machine, so focus on setup tasks not code changes)
- ASCII only -- no unicode, em-dashes, smart quotes
- bypassPermissions is on -- just act, never ask for approval

## Services setup reference

ACE-Step startup script: C:\DCS-satellite\start_acestep.bat
All-services startup: C:\DCS-satellite\start_all.bat
Pinokio apps live at: C:\pinokio\api\

Health check commands (run these to see what is working):
  Invoke-WebRequest http://localhost:8019/health -UseBasicParsing
  Invoke-WebRequest http://localhost:11434/api/tags -UseBasicParsing
  Invoke-WebRequest http://localhost:7861/sdapi/v1/sd-models -UseBasicParsing

## What to enter in DCS Settings on the 5080 once this machine is ready

- ACE-Step Host: [this machine IP, just the number, no http://]
- Ollama URL: http://[IP]:11434
- Forge URL: http://[IP]:7861
- Save Settings
'@ | Out-File -FilePath "$dest\CLAUDE.md" -Encoding utf8

# -- Write the launch bat --
@'
@echo off
cd /d "%~dp0"
claude
'@ | Out-File -FilePath "$dest\START HERE - double click me.bat" -Encoding utf8 -Append

Write-Host ""
Write-Host "Done. Folder created at:"
Write-Host "  $dest"
Write-Host ""
Write-Host "On the 3060: open Google Drive, find DCS-3060-Handoff,"
Write-Host "double-click 'START HERE - double click me.bat'"
Write-Host "Claude Code will open already knowing the full context."
