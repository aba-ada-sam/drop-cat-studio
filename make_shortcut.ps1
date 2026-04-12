$ws = New-Object -ComObject WScript.Shell
$desktop = [System.Environment]::GetFolderPath('Desktop')
$lnk = $ws.CreateShortcut($desktop + '\Drop Cat Go Studio.lnk')
$lnk.TargetPath = 'C:\DropCat-Studio\launch.bat'
$lnk.WorkingDirectory = 'C:\DropCat-Studio'
$lnk.IconLocation = 'C:\DropCat-Studio\dropcat.ico'
$lnk.Description = 'Drop Cat Go Studio - AI Video Production'
$lnk.WindowStyle = 1
$lnk.Save()
Write-Host "Done: $desktop\Drop Cat Go Studio.lnk"
