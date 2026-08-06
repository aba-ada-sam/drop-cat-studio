# v16 overnight render -- detached from any Claude session (2026-08-05 close).
# Safe to re-run: skips if the output already exists, and refuses to start a
# second render while one is already in flight.
#
# 2026-08-05 23:10 REPAIR (gui-v2 session): the original version assumed the
# WanGP worker on :7899 was up. It was not -- the night-close stopped DCS,
# which owns the worker, so the 22:01 render sat against a dead worker, made
# no progress after audio prep at 22:02, and died. chain.py does NOT start a
# worker of its own (engine/chain.py:23-24). This version starts the worker
# itself when it is missing, and guards against double-starting.

$rev = "C:\DropCat-Studio\review\assets"
$out = "$rev\chain60_v16.mp4"
$log = "C:\DropCat-Studio\review\v16_render_log.txt"
$wlog = "C:\DropCat-Studio\review\v16_worker_log.txt"

if (Test-Path $out) { exit 0 }

# -- Guard: is a v16 chain.py already running? (the 00:20 failsafe must not
# start a second render on top of a live one -- two renders would fight for
# the same 16GB of VRAM and the same worker.)
$running = Get-CimInstance Win32_Process -Filter "Name like 'python%'" |
           Where-Object { $_.CommandLine -like "*chain.py*" -and $_.CommandLine -like "*chain60_v16*" }
if ($running) { "v16 render already running (PID $($running.ProcessId)) -- exiting" | Out-File $wlog -Append; exit 0 }

# -- Ensure the WanGP worker is alive on :7899.
function Test-Worker {
  try {
    $r = Invoke-WebRequest -Uri "http://127.0.0.1:7899/health" -TimeoutSec 5 -UseBasicParsing
    return ($r.StatusCode -eq 200)
  } catch { return $false }
}

if (-not (Test-Worker)) {
  "$(Get-Date -f 'HH:mm:ss') worker down -- starting it" | Out-File $wlog -Append
  $wanRoot = "C:\pinokio\api\wan.git\app"
  $wanPy   = "$wanRoot\env\Scripts\python.exe"
  $wscript = "C:\DropCat-Studio\services\wangp_worker.py"
  if (-not (Test-Path $wanPy))   { "MISSING wan python $wanPy" | Out-File $wlog -Append; exit 1 }
  if (-not (Test-Path $wscript)) { "MISSING worker script $wscript" | Out-File $wlog -Append; exit 1 }
  $env:PYTHONUNBUFFERED = "1"
  Start-Process -FilePath $wanPy `
                -ArgumentList @($wscript, "--wangp-app", $wanRoot, "--port", "7899") `
                -WorkingDirectory $wanRoot -WindowStyle Hidden `
                -RedirectStandardOutput "C:\DropCat-Studio\review\v16_worker_stdout.txt" `
                -RedirectStandardError  "C:\DropCat-Studio\review\v16_worker_stderr.txt"
  # Model load into VRAM is slow: poll (localhost only) up to 10 minutes.
  $ok = $false
  foreach ($i in 1..120) {
    Start-Sleep -Seconds 5
    if (Test-Worker) { $ok = $true; break }
  }
  if (-not $ok) { "$(Get-Date -f 'HH:mm:ss') worker never became healthy -- aborting" | Out-File $wlog -Append; exit 1 }
  "$(Get-Date -f 'HH:mm:ss') worker healthy" | Out-File $wlog -Append
}

Set-Location C:\DropCat-Studio\engine
$pA = "A single subject, centered, facing the camera, mouth opening and closing in precise sync with the singing vocals. Behind him, a man in denim overalls stands calmly at the crates, still and relaxed, slowly nodding along with the music. Stay in the original scene and setting, soft cinematic lighting, shallow depth of field, steady framing."
$pB = "A single subject, centered, facing the camera, mouth opening and closing in precise sync with the singing vocals, standing in a golden wheat field, wheat stalks in the foreground, soft afternoon light, steady framing."
$prompts = "$pA||$pB"
"$(Get-Date -f 'HH:mm:ss') starting chain.py" | Out-File $wlog -Append
python chain.py --image "C:\DropCat-Studio\uploads\awm_alien_source.png" --song "$rev\adam60_dense.wav" --output $out --worker http://127.0.0.1:7899 --seeds-per-clip 4 --frames-per-clip 241 --crossfade 0.15 --min-clip-frames 169 --smart-seams --judge-select --images "C:\DropCat-Studio\uploads\awm_alien_source.png,$rev\sceneB_field.png" --scene-prompts $prompts *> $log
"$(Get-Date -f 'HH:mm:ss') chain.py exited $LASTEXITCODE" | Out-File $wlog -Append
