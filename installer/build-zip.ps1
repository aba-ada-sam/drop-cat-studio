# Run from anywhere -- builds a timestamped installer zip on the Desktop
$HERE = Split-Path -Parent $MyInvocation.MyCommand.Path
$ROOT = Split-Path -Parent $HERE
$TS   = Get-Date -Format "yyyyMMdd-HHmm"
$ZIP  = "Z:\My Drive\1 Apache Directions\dcs-laptop-setup-$TS.zip"

$stage = "$env:TEMP\dcs-installer-staging"
Remove-Item $stage -Recurse -Force -ErrorAction SilentlyContinue
New-Item -ItemType Directory $stage | Out-Null

Copy-Item "$HERE\install.bat"               "$stage\install.bat"
Copy-Item "$HERE\install.ps1"               "$stage\install.ps1"
Copy-Item "$ROOT\config-laptop-template.json" "$stage\config-laptop-template.json"
Copy-Item "C:\JSON Credentials\QB_WC_credentials.json" "$stage\QB_WC_credentials.json"
"https://github.com/aba-ada-sam/drop-cat-studio.git" | Out-File "$stage\REPO_URL.txt" -Encoding utf8

@"
Drop Cat Go Studio -- Installer Package ($TS)
=============================================

NORMAL USE:
  Double-click install.bat and leave it running.
  It takes 30-60 minutes. One step asks you to download a model in a browser.

IF SOMETHING FAILS:
  Check dcs-install-log.txt on your Desktop.
  You can re-run install.bat safely -- it skips steps already done.
"@ | Out-File "$stage\README.txt" -Encoding utf8

Compress-Archive -Path "$stage\*" -DestinationPath $ZIP -CompressionLevel Optimal
Remove-Item $stage -Recurse -Force

Write-Host ""
Write-Host "  Built: $ZIP" -ForegroundColor Green
Write-Host "  Size : $([math]::Round((Get-Item $ZIP).Length/1KB,1)) KB" -ForegroundColor Green
Write-Host ""
