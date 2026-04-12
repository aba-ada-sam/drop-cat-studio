$ws = New-Object -ComObject WScript.Shell
$desktop = [System.Environment]::GetFolderPath('Desktop')
$lnk = $ws.CreateShortcut($desktop + '\Drop Cat Go Studio.lnk')
$lnk.TargetPath = 'C:\Users\andre\Desktop\AI Editors\DropCat-Studio\launch.bat'
$lnk.WorkingDirectory = 'C:\Users\andre\Desktop\AI Editors\DropCat-Studio'
$lnk.IconLocation = 'C:\Users\andre\Desktop\AI Editors\DropCat-Studio\dropcat.ico'
$lnk.Description = 'Drop Cat Go Studio - AI Video Production'
$lnk.WindowStyle = 1
$lnk.Save()
Write-Host "Done: $desktop\Drop Cat Go Studio.lnk"
