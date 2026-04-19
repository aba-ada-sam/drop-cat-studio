$ws = New-Object -ComObject WScript.Shell
$desktop = [System.Environment]::GetFolderPath('Desktop')
$lnk = $ws.CreateShortcut($desktop + '\Drop Cat Go Studio.lnk')
# Use wscript.exe to run the VBS silently (no console window)
$lnk.TargetPath = 'wscript.exe'
$lnk.Arguments = '"C:\DropCat-Studio\launch-silent.vbs"'
$lnk.WorkingDirectory = 'C:\DropCat-Studio'
$lnk.IconLocation = 'C:\DropCat-Studio\dropcat.ico'
$lnk.Description = 'Drop Cat Go Studio - AI Video Production'
$lnk.WindowStyle = 7  # Minimized (wscript exits fast anyway)
$lnk.Save()

# Also create a debug shortcut that shows the console
$lnk2 = $ws.CreateShortcut($desktop + '\Drop Cat Go Studio (debug).lnk')
$lnk2.TargetPath = 'C:\DropCat-Studio\launch.bat'
$lnk2.WorkingDirectory = 'C:\DropCat-Studio'
$lnk2.IconLocation = 'C:\DropCat-Studio\dropcat.ico'
$lnk2.Description = 'Drop Cat Go Studio - Debug (shows console)'
$lnk2.WindowStyle = 1
$lnk2.Save()

Write-Host "Done: $desktop\Drop Cat Go Studio.lnk (silent)"
Write-Host "Done: $desktop\Drop Cat Go Studio (debug).lnk (shows console)"
