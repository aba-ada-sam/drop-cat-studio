# WIP autosave -- commits dirty work in the V2 checkout and every agent worktree.
#
# WHY THIS EXISTS (2026-08-06): the Claude Code session died twice overnight
# while build agents were mid-task. Merged work was safe because it was
# committed; three agents lost everything they had not committed yet. Long
# jobs already survive session death by running detached -- source code did
# not. This closes that gap: a commit every few minutes means the worst case
# is losing minutes, not hours.
#
# Deliberately conservative:
#   - commits ONLY, never pushes, never merges, never checks out
#   - never touches C:\DropCat-Studio (v1, Andrew's working app)
#   - skips a repo mid-merge/rebase (a WIP commit there would corrupt the state)
#   - a WIP commit is a safety net, not a real commit: agents still make their
#     own logical commits, and these can be squashed away later.
#
# Run: powershell -NoProfile -ExecutionPolicy Bypass -File tools\wip_autosave.ps1

$ErrorActionPreference = 'SilentlyContinue'

$roots = @('C:\DropCat-Studio-V2')
$scratch = 'C:\Users\andre\AppData\Local\Temp\claude\C--Users-andre\d5b811c8-03d6-466b-9803-921f87a4c938\scratchpad'
if (Test-Path $scratch) {
  Get-ChildItem $scratch -Directory -Filter 'wt-*' | ForEach-Object { $roots += $_.FullName }
}

$stamp = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'
$log   = 'C:\DropCat-Studio-V2\tools\wip_autosave.log'

foreach ($r in $roots) {
  if (-not (Test-Path (Join-Path $r '.git'))) { continue }

  # Never autosave on top of an in-progress merge/rebase -- committing then
  # would bake conflict markers or a half-resolved index into history.
  $gitdir = (git -C $r rev-parse --git-dir 2>$null)
  if ($gitdir) {
    $gd = if ([System.IO.Path]::IsPathRooted($gitdir)) { $gitdir } else { Join-Path $r $gitdir }
    if ((Test-Path (Join-Path $gd 'MERGE_HEAD')) -or
        (Test-Path (Join-Path $gd 'rebase-merge')) -or
        (Test-Path (Join-Path $gd 'rebase-apply'))) {
      "$stamp  SKIP (merge/rebase in progress): $r" | Out-File $log -Append
      continue
    }
  }

  $dirty = git -C $r status --porcelain 2>$null
  if (-not $dirty) { continue }

  $n = ($dirty | Measure-Object).Count
  $branch = (git -C $r rev-parse --abbrev-ref HEAD 2>$null)
  git -C $r add -A 2>$null
  git -C $r commit -q -m "WIP autosave $stamp ($n files) -- safety net, squash freely" 2>$null
  if ($LASTEXITCODE -eq 0) {
    "$stamp  SAVED $n files on $branch in $r" | Out-File $log -Append
  } else {
    "$stamp  commit failed in $r" | Out-File $log -Append
  }
}
