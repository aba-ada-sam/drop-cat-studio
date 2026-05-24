# Drop Cat Go Studio — Claude Handoff

**Date:** 2026-05-24  
**Repo:** C:\DropCat-Studio  
**App URL:** http://127.0.0.1:7860  
**User:** Andrew (andrew@lynncove.com)

---

## What This Is

FastAPI + vanilla JS AI video production app. Generates music videos from a photo + song using WanGP (LTX-2 19B Distilled) on an RTX 5080 (15.9GB VRAM). There is also a 3060 satellite at 192.168.86.49 but it is **currently disabled** — do not touch it.

---

## Current State (what's running right now)

A music video batch is running:
- **38 images** from `C:\Users\andre\Desktop\possible`
- **Song:** `uploads\b7e9d2c4_Adam_Friends.mpeg`
- **Settings:** 8 steps, LTX-2 Distilled, 50% coverage (~14 clips/video), repeat=true
- **Status:** index=2/38, both 5080 and 3060 generating (3060 is named in current_image but satellite is disabled in UI — the job may still be satellite type from before the disable)

Check status: `GET http://127.0.0.1:7860/api/song-video/batch/status`

---

## Architecture

```
manager.pyw          — watchdog, spawns app.py, handles Chrome window lifecycle
app.py               — FastAPI server, port 7860
services/manager.py  — starts WanGP worker subprocess on port 7899
features/song_video/ — Music Video feature (song + folder batch pipeline)
features/fun_videos/ — Create Videos feature (single video)
static/js/           — vanilla JS frontend, ES modules, no build step
```

**Key files:**
- `features/song_video/pipeline.py` — the actual video generation pipeline
- `features/song_video/batch_runner.py` — server-side batch loop (38 images)
- `features/song_video/routes.py` — API endpoints including /batch/start
- `features/fun_videos/video_generator.py` — WanGP HTTP client
- `static/js/tab-music-video.js` — Music Video tab UI
- `core/job_manager.py` — job queue, GPU serialization

**Cache busting:** Every JS import has `?v=YYYYMMDD[letter]`. When you change any JS file, bump the letter in both the file's import in app.js AND in index.html's `<script>` tag. All stamps must match.

---

## What Works Well

- **Music Video batch** — upload song, choose folder, generates one full music video per image, loops continuously. State saved to `output/batch_state.json` between restarts.
- **Single image generation** — Music Video tab, "Generate One Video" button
- **Gallery, Queue tab, session tracking** — solid
- **WanGP restart on clip failure** — pipeline detects failure, restarts WanGP, retries clip automatically. Now shows status in UI instead of freezing.
- **LTX-2 Distilled step cap** — song pipeline caps at 8 steps regardless of config (20-30 step default was wasting 2x time with no quality gain)

---

## Known Issues / Do Not Touch

### Satellite (3060 at 192.168.86.49) — DISABLED
The satellite was attempted this session and caused repeated crashes. The architecture works in theory but has not been validated end-to-end. Specifically:
- Output paths sent to 3060 WanGP don't exist on that machine
- File download via relay works but hasn't been tested successfully
- Satellite toggle is hidden in the UI
- Do NOT re-enable until you can test it in complete isolation from the main pipeline
- The relay code at `tools/dcs_relay.py` has `/wangp/*` proxy and `/download?path=` endpoints

### App crashes kill the batch
The batch runner is in-memory. When app.py crashes, the batch dies. `output/batch_state.json` saves progress but does NOT auto-resume on startup (user explicitly does not want auto-resume). After a crash, the batch must be manually restarted.

### WanGP instability
LTX-2 19B Distilled is 20GB — larger than the 5080's 16GB VRAM. It uses RAM offloading. After ~20 clips it sometimes deadlocks. The pipeline detects this and restarts WanGP. This is normal and handled.

---

## What Andrew Wants

1. **Reliable overnight batch** — folder of 38 images, one music video per image, repeat continuously, doesn't require babysitting
2. **Good quality outputs** — consistent character, no Ken Burns zooms, no pox/drift
3. **Simple UI** — Music Video tab is the primary interface
4. **Not to babysit background processes** — when he closes the app window, everything dies cleanly

## What Pissed Him Off This Session (don't repeat)

1. **Opening extra browser windows** — never use `Start-Process "http://..."`. Use the desktop shortcut or nothing.
2. **Auto-resuming batches without asking** — removed, do not add back
3. **Describing a "false reality"** — always check actual API state before saying something is working
4. **Asking him to do things** — if you need a refresh/restart, do it via PowerShell/tools, don't ask him
5. **Satellite destabilizing the main pipeline** — the 3060 integration caused multiple crashes and lost batch runs
6. **Restarting app while batch was running** — always check if a batch is running before touching anything

---

## How to Restart the Batch

```powershell
$images = (Invoke-RestMethod "http://127.0.0.1:7860/api/zoom/scan-folder" -Method POST -Body '{"folder":"C:/Users/andre/Desktop/possible"}' -ContentType "application/json").files | Where-Object { -not $_.is_video } | ForEach-Object { @{path=$_.path; name=$_.name} }
$body = @{
    audio_path     = "C:\DropCat-Studio\uploads\b7e9d2c4_Adam_Friends.mpeg"
    folder         = "C:/Users/andre/Desktop/possible"
    images         = $images
    repeat         = $true
    use_satellite  = $false
    model          = "LTX-2 Dev19B Distilled"
    clip_duration  = 8
    steps          = 8
    guidance       = 3.5
    coverage_ratio = 0.5
} | ConvertTo-Json -Depth 3
Invoke-RestMethod "http://127.0.0.1:7860/api/song-video/batch/start" -Method POST -Body $body -ContentType "application/json"
```

## How to Check Batch Status

```powershell
Invoke-RestMethod "http://127.0.0.1:7860/api/song-video/batch/status" | ConvertTo-Json
Invoke-RestMethod "http://127.0.0.1:7860/api/jobs" | ConvertTo-Json -Depth 2
```

## How to Start the App (if down)

```powershell
Start-Process "python" -ArgumentList "C:\DropCat-Studio\app.py" -WorkingDirectory "C:\DropCat-Studio" -WindowStyle Hidden
Start-Sleep 14
(Get-Content "C:\DropCat-Studio\.dcs-port" | ConvertFrom-Json).port
```

Or use the desktop shortcut: `C:\Users\andre\Desktop\Drop Cat Go Studio.lnk`

---

## Recent Significant Changes (this session)

- `core/job_manager.py` — satellite jobs (JOB_FUN_MULTI_VIDEO_SAT) bypass GPU queue, run in prep thread
- `features/song_video/batch_runner.py` — two-slot parallel runner (disabled via use_satellite=false)
- `features/fun_videos/video_generator.py` — satellite file download via relay, double timeout for remote workers
- `features/song_video/pipeline.py` — 8-step cap for LTX Distilled, WanGP restart shows in UI
- `services/manager.py` — atexit handler closes Job Object handle so WanGP dies when app exits
- `manager.pyw` — closing the window always shuts everything down, no dialog
- `static/js/tab-music-video.js` — satellite toggle hidden
- `tools/dcs_relay.py` — WanGP watchdog + /download endpoint (for future satellite use)
