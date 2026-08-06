# Self-locating: uses $PSScriptRoot (this script's own folder) instead of a
# hardcoded path, so the SAME script works unmodified in DropCat-Studio-V2
# without accidentally pointing shortcuts at v1's C:\DropCat-Studio.
$root = $PSScriptRoot
$ws = New-Object -ComObject WScript.Shell
$desktop = [System.Environment]::GetFolderPath('Desktop')
$lnk = $ws.CreateShortcut($desktop + '\DropCat Studio V2.lnk')
# Use wscript.exe to run the VBS silently (no console window)
$lnk.TargetPath = 'wscript.exe'
$lnk.Arguments = "`"$root\launch-silent.vbs`""
$lnk.WorkingDirectory = $root
$lnk.IconLocation = "$root\dropcat_output.ico"
$lnk.Description = 'DropCat Studio V2 - AI Video Production'
$lnk.WindowStyle = 7  # Minimized (wscript exits fast anyway)
$lnk.Save()

# Also create a debug shortcut that shows the console
$lnk2 = $ws.CreateShortcut($desktop + '\DropCat Studio V2 (debug).lnk')
$lnk2.TargetPath = "$root\launch.bat"
$lnk2.WorkingDirectory = $root
$lnk2.IconLocation = "$root\dropcat_output.ico"
$lnk2.Description = 'DropCat Studio V2 - Debug (shows console)'
$lnk2.WindowStyle = 1
$lnk2.Save()

Write-Host "Done: $desktop\DropCat Studio V2.lnk (silent)"
Write-Host "Done: $desktop\DropCat Studio V2 (debug).lnk (shows console)"
