# v16 render supervisor -- detects a FROZEN render and restarts it.
#
# WHY (2026-08-05/06): the failure mode here is not a crash, it is a freeze.
# A GPU driver reset (nvlddmkm event 153) invalidates the CUDA context; WanGP
# does not notice and hangs forever waiting on it. chain.py then sits in its
# clip-poll loop for ~29 minutes before giving up, and everything rendered so
# far is thrown away. That happened at 23:58 with 6 of 7 clips finished.
#
# Silence is the only symptom, so silence is what this watches: if the render
# log stops advancing while chain.py is still "running", the job is dead and
# only a restart will help.
#
# Local-only checks. No remote hosts, no network. Commits nothing, kills only
# the v16 render's own processes.

$rev      = "C:\DropCat-Studio\review"
$out      = "$rev\assets\chain60_v16.mp4"
$log      = "$rev\v16_render_log.txt"
$suplog   = "$rev\v16_supervisor.log"
$countF   = "$rev\.v16_restart_count"
$script   = "$rev\render_v16_detached.ps1"
$STALE_MIN   = 10    # no log progress for this long => frozen
$MAX_RESTART = 5     # give up rather than loop forever

function Say($m) { "$(Get-Date -f 'yyyy-MM-dd HH:mm:ss')  $m" | Out-File $suplog -Append }

if (Test-Path $out) { Say "output exists -- supervisor idle"; exit 0 }

$chain = Get-CimInstance Win32_Process -Filter "Name like 'python%'" |
         Where-Object { $_.CommandLine -like "*chain.py*" -and $_.CommandLine -like "*chain60_v16*" }

$stale = $false
if (Test-Path $log) {
  $ageMin = ((Get-Date) - (Get-Item $log).LastWriteTime).TotalMinutes
  if ($ageMin -gt $STALE_MIN) { $stale = $true }
} else {
  # no log at all and no chain.py means nothing is running
  if (-not $chain) { $stale = $true }
}

if ($chain -and -not $stale) { exit 0 }          # healthy, advancing
if (-not $chain -and -not $stale) { exit 0 }     # log fresh, process just exiting -- let it be

$count = 0
if (Test-Path $countF) { $count = [int](Get-Content $countF -ErrorAction SilentlyContinue) }
if ($count -ge $MAX_RESTART) {
  Say "FROZEN again but restart cap ($MAX_RESTART) reached -- NOT restarting. Needs a human."
  exit 1
}

if ($chain) {
  Say "FROZEN: log stale, killing chain.py $($chain.ProcessId) and the worker"
  Stop-Process -Id $chain.ProcessId -Force -ErrorAction SilentlyContinue
} else {
  Say "render process gone with no output -- restarting"
}

# The worker is the thing that actually wedged; it must go too or the restart
# reconnects to the same dead context.
Get-CimInstance Win32_Process -Filter "Name like 'python%'" |
  Where-Object { $_.CommandLine -like "*wangp_worker*" } |
  ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }

Start-Sleep -Seconds 5
$count++
$count | Out-File $countF -Encoding ascii
Say "restart #$count launching render_v16_detached.ps1"
Start-Process powershell.exe -ArgumentList '-NoProfile','-ExecutionPolicy','Bypass',
  '-WindowStyle','Hidden','-File',$script -WindowStyle Hidden
