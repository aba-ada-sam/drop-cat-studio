# Drop Cat Go Studio -- Automated Laptop Installer
# Tested: Windows 11, RTX 4070 12GB
# Run via install.bat (handles admin elevation)

$ErrorActionPreference = "Stop"
$PSDefaultParameterValues['*:Encoding'] = 'utf8'

$LOG = "$env:USERPROFILE\Desktop\dcs-install-log.txt"
$HERE = Split-Path -Parent $MyInvocation.MyCommand.Path

function Log($msg) {
    $ts = Get-Date -Format "HH:mm:ss"
    $line = "[$ts] $msg"
    Write-Host $line
    Add-Content $LOG $line
}

function Step($n, $total, $label) {
    Write-Host ""
    Write-Host "  ============================================================" -ForegroundColor Cyan
    Write-Host "   STEP $n of $total -- $label" -ForegroundColor Cyan
    Write-Host "  ============================================================" -ForegroundColor Cyan
    Log "=== STEP $n/$total: $label ==="
}

function Done($label) {
    Write-Host "  [OK] $label" -ForegroundColor Green
    Log "[OK] $label"
}

function Fail($label) {
    Write-Host "  [FAIL] $label" -ForegroundColor Red
    Log "[FAIL] $label"
    Write-Host ""
    Write-Host "  Something went wrong. Check the log at:" -ForegroundColor Yellow
    Write-Host "  $LOG" -ForegroundColor Yellow
    Read-Host "Press Enter to exit"
    exit 1
}

function WingetInstall($id, $label) {
    Log "Installing $label via winget..."
    $result = winget install --id $id -e --source winget --accept-package-agreements --accept-source-agreements --silent 2>&1
    if ($LASTEXITCODE -eq 0 -or $LASTEXITCODE -eq -1978335189) {
        # -1978335189 = already installed, that is fine
        Done "$label installed"
    } else {
        Log "winget output: $result"
        Fail "$label install failed (exit $LASTEXITCODE)"
    }
}

function RefreshPath {
    $env:Path = [System.Environment]::GetEnvironmentVariable("Path","Machine") + ";" + [System.Environment]::GetEnvironmentVariable("Path","User")
}

# ---- paths ---------------------------------------------------------------
$DCS_DIR      = "C:\DropCat-Studio"
$WANGP_DIR    = "C:\WanGP"
$ACESTEP_DIR  = "C:\ACE-Step"
$CREDS_DIR    = "C:\JSON Credentials"
$REPO_URL     = Get-Content "$HERE\REPO_URL.txt" -ErrorAction SilentlyContinue
if (-not $REPO_URL) { $REPO_URL = "https://github.com/aba-ada-sam/drop-cat-studio.git" }

Log "=== Drop Cat Go Studio Installer started ==="
Log "Script dir : $HERE"
Log "DCS dir    : $DCS_DIR"
Log "WanGP dir  : $WANGP_DIR"

# ==========================================================================
Step 1 10 "Install prerequisites (git, python, ffmpeg, node)"
# ==========================================================================

WingetInstall "Git.Git"              "Git"
WingetInstall "Python.Python.3.11"   "Python 3.11"
WingetInstall "Gyan.FFmpeg"          "ffmpeg"
WingetInstall "OpenJS.NodeJS.LTS"    "Node.js"
WingetInstall "Ollama.Ollama"        "Ollama"

RefreshPath
Done "All prerequisites installed"

# ==========================================================================
Step 2 10 "Clone Drop Cat Go Studio"
# ==========================================================================

if (Test-Path "$DCS_DIR\app.py") {
    Log "DCS already cloned -- pulling latest"
    Push-Location $DCS_DIR
    git pull 2>&1 | ForEach-Object { Log $_ }
    Pop-Location
} else {
    git clone $REPO_URL $DCS_DIR 2>&1 | ForEach-Object { Log $_ }
    if (-not (Test-Path "$DCS_DIR\app.py")) { Fail "Clone failed -- app.py not found" }
}
Done "Drop Cat Go Studio repo ready at $DCS_DIR"

# ==========================================================================
Step 3 10 "Install Drop Cat Go Studio Python dependencies"
# ==========================================================================

Push-Location $DCS_DIR
python -m pip install --upgrade pip --quiet
pip install -r requirements.txt --quiet 2>&1 | ForEach-Object { Log $_ }
Pop-Location
$check = python -c "import fastapi, PIL; print('ok')" 2>&1
if ($check -ne "ok") { Fail "DCS Python deps check failed: $check" }
Done "DCS Python dependencies installed"

# ==========================================================================
Step 4 10 "Restore credentials and configure"
# ==========================================================================

New-Item -ItemType Directory -Force $CREDS_DIR | Out-Null
$credSrc = "$HERE\QB_WC_credentials.json"
if (Test-Path $credSrc) {
    Copy-Item $credSrc "$CREDS_DIR\QB_WC_credentials.json" -Force
    Done "Credentials restored to $CREDS_DIR"
} else {
    Log "WARNING: QB_WC_credentials.json not found in installer folder -- skipping"
}

# Write laptop config (paths filled in after WanGP/ACE-Step installs below)
$configSrc = "$HERE\config-laptop-template.json"
if (-not (Test-Path "$DCS_DIR\config.json") -and (Test-Path $configSrc)) {
    Copy-Item $configSrc "$DCS_DIR\config.json" -Force
    Done "Config template placed (paths will be updated in step 8)"
} else {
    Done "Config already exists -- skipping template copy"
}

# ==========================================================================
Step 5 10 "Install Ollama models (offline AI -- runs without internet)"
# ==========================================================================

# Start Ollama service if not running
$ollamaRunning = Get-Process ollama -ErrorAction SilentlyContinue
if (-not $ollamaRunning) {
    Start-Process ollama -ArgumentList "serve" -WindowStyle Hidden
    Start-Sleep -Seconds 8
}

Log "Pulling ollama models (this takes a few minutes)..."
ollama pull dolphin3:8b 2>&1 | ForEach-Object { Log $_ }
ollama pull qwen2.5vl:7b 2>&1 | ForEach-Object { Log $_ }
Done "Ollama models ready"

# ==========================================================================
Step 6 10 "Clone and set up WanGP (AI video engine)"
# ==========================================================================

if (-not (Test-Path "$WANGP_DIR\wgp.py")) {
    git clone https://github.com/deepbeepmeep/Wan2GP.git $WANGP_DIR 2>&1 | ForEach-Object { Log $_ }
    if (-not (Test-Path "$WANGP_DIR\wgp.py")) { Fail "WanGP clone failed" }
} else {
    Log "WanGP already cloned -- skipping"
}

# Create WanGP Python virtual environment
if (-not (Test-Path "$WANGP_DIR\venv\Scripts\python.exe")) {
    Log "Creating WanGP virtual environment..."
    python -m venv "$WANGP_DIR\venv" 2>&1 | ForEach-Object { Log $_ }
}

$WANGP_PY = "$WANGP_DIR\venv\Scripts\python.exe"
$WANGP_PIP = "$WANGP_DIR\venv\Scripts\pip.exe"

Log "Installing PyTorch with CUDA 12.4..."
& $WANGP_PIP install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cu124 --quiet 2>&1 | ForEach-Object { Log $_ }

Log "Installing WanGP requirements..."
Push-Location $WANGP_DIR
& $WANGP_PIP install -r requirements.txt --quiet 2>&1 | ForEach-Object { Log $_ }
Pop-Location

$gpuCheck = & $WANGP_PY -c "import torch; print(torch.cuda.is_available())" 2>&1
Log "CUDA available: $gpuCheck"
if ($gpuCheck -ne "True") {
    Log "WARNING: CUDA not detected in WanGP venv. Video generation may be CPU-only (very slow)."
    Log "Check that NVIDIA drivers are up to date."
}

Done "WanGP environment ready at $WANGP_DIR"

# Write WanGP memory profile config for RTX 4070 (12GB)
$wgpConfig = "$WANGP_DIR\wgp_config.json"
if (Test-Path $wgpConfig) {
    $cfg = Get-Content $wgpConfig | ConvertFrom-Json
} else {
    $cfg = [PSCustomObject]@{}
}
# Set performance profile for 12GB VRAM
Add-Member -InputObject $cfg -MemberType NoteProperty -Name "profile"        -Value 3 -Force
Add-Member -InputObject $cfg -MemberType NoteProperty -Name "video_profile"  -Value 3 -Force
Add-Member -InputObject $cfg -MemberType NoteProperty -Name "image_profile"  -Value 3 -Force
Add-Member -InputObject $cfg -MemberType NoteProperty -Name "vae_config"     -Value 1 -Force
Add-Member -InputObject $cfg -MemberType NoteProperty -Name "compile"        -Value "" -Force
Add-Member -InputObject $cfg -MemberType NoteProperty -Name "attention_mode" -Value "auto" -Force
$cfg | ConvertTo-Json -Depth 10 | Set-Content $wgpConfig -Encoding utf8
Done "WanGP profile set for RTX 4070 (profile 3, vae_config 1)"

# ==========================================================================
Step 7 10 "Clone and set up ACE-Step (AI music engine)"
# ==========================================================================

if (-not (Test-Path "$ACESTEP_DIR\run_inference.py")) {
    git clone https://github.com/ace-step/ACE-Step.git $ACESTEP_DIR 2>&1 | ForEach-Object { Log $_ }
    if (-not (Test-Path "$ACESTEP_DIR\run_inference.py")) { Fail "ACE-Step clone failed" }
} else {
    Log "ACE-Step already cloned -- skipping"
}

if (-not (Test-Path "$ACESTEP_DIR\venv\Scripts\python.exe")) {
    Log "Creating ACE-Step virtual environment..."
    python -m venv "$ACESTEP_DIR\venv" 2>&1 | ForEach-Object { Log $_ }
}

$ACESTEP_PIP = "$ACESTEP_DIR\venv\Scripts\pip.exe"
Log "Installing ACE-Step requirements..."
Push-Location $ACESTEP_DIR
& $ACESTEP_PIP install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cu124 --quiet 2>&1 | ForEach-Object { Log $_ }
& $ACESTEP_PIP install -r requirements.txt --quiet 2>&1 | ForEach-Object { Log $_ }
Pop-Location

Done "ACE-Step environment ready at $ACESTEP_DIR"

# ==========================================================================
Step 8 10 "Finalize DCS configuration"
# ==========================================================================

$config = Get-Content "$DCS_DIR\config.json" | ConvertFrom-Json

# Point DCS at the local WanGP and ACE-Step installs
$config.wan2gp_root  = $WANGP_DIR
$config.wan2gp_python = "$WANGP_DIR\venv\Scripts\python.exe"
$config.acestep_root = $ACESTEP_DIR

# Laptop-appropriate defaults
$config.wan_model    = "Wan2.1-I2V-14B-480P"  # user picks the downloaded model in UI
$config.fun_model    = "Wan2.1-I2V-14B-480P"
$config.resolution   = "480p"

$config | ConvertTo-Json -Depth 10 | Set-Content "$DCS_DIR\config.json" -Encoding utf8
Done "DCS config updated with local paths"

# ==========================================================================
Step 9 10 "Download AI video model (WanGP)"
# ==========================================================================

Write-Host ""
Write-Host "  ============================================================" -ForegroundColor Yellow
Write-Host "   ONE STEP REQUIRES YOUR ATTENTION" -ForegroundColor Yellow
Write-Host "  ============================================================" -ForegroundColor Yellow
Write-Host ""
Write-Host "   WanGP is about to open in your browser." -ForegroundColor White
Write-Host ""
Write-Host "   Do this ONE thing:" -ForegroundColor White
Write-Host "   1. Look for a 'Models' or 'Download' section in the UI" -ForegroundColor White
Write-Host "   2. Download one of these (whichever appears first):" -ForegroundColor White
Write-Host "      - LTX-Video 2.0 or 2.1   <-- BEST for your GPU, pick this" -ForegroundColor Green
Write-Host "      - Wan2.1 I2V 480P 1.3B   <-- second choice" -ForegroundColor Green
Write-Host "      - Wan2.1 I2V 480P 14B int8 <-- works but slower" -ForegroundColor Green
Write-Host "   3. Wait for the download to complete (10-30 min)" -ForegroundColor White
Write-Host "   4. Note the EXACT model name that appeared in the dropdown" -ForegroundColor White
Write-Host "   5. Come back here and press Enter" -ForegroundColor White
Write-Host ""
Log "Launching WanGP web UI for model download..."

# Start WanGP server in background
$wgpArgs = "-c `"cd '$WANGP_DIR'; & '$WANGP_DIR\venv\Scripts\python.exe' wgp.py --server_port 7899`""
Start-Process powershell -ArgumentList $wgpArgs -WindowStyle Normal
Start-Sleep -Seconds 8

# Open browser to WanGP
Start-Process "http://127.0.0.1:7899"

Read-Host "  Press Enter when the model download is complete"

# Ask which model was downloaded so we can update config
Write-Host ""
$modelName = Read-Host "  Paste the exact model name from the WanGP dropdown (or press Enter to skip)"
if ($modelName -and $modelName.Trim() -ne "") {
    $config = Get-Content "$DCS_DIR\config.json" | ConvertFrom-Json
    $config.wan_model = $modelName.Trim()
    $config.fun_model = $modelName.Trim()
    $config | ConvertTo-Json -Depth 10 | Set-Content "$DCS_DIR\config.json" -Encoding utf8
    Done "Model name saved to config: $($modelName.Trim())"
} else {
    Log "Model name skipped -- update wan_model in config.json manually if needed"
}

# Stop WanGP (DCS will manage it from here)
Stop-Process -Name "python" -ErrorAction SilentlyContinue

# ==========================================================================
Step 10 10 "Create desktop shortcut and run smoke tests"
# ==========================================================================

# Desktop shortcut
$shell    = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut("$env:USERPROFILE\Desktop\Drop Cat Go Studio.lnk")
$shortcut.TargetPath       = "$DCS_DIR\launch.bat"
$shortcut.WorkingDirectory = $DCS_DIR
$shortcut.Description      = "Drop Cat Go Studio"
$shortcut.Save()
Done "Desktop shortcut created"

# Smoke tests
Push-Location $DCS_DIR
$testOut = python tests/smoke.py 2>&1
Log "Smoke test output: $testOut"
if ($LASTEXITCODE -eq 0) {
    Done "Smoke tests passed"
} else {
    Log "WARNING: Smoke tests had failures -- see log. App may still work."
}
Pop-Location

# ==========================================================================
Write-Host ""
Write-Host "  ============================================================" -ForegroundColor Green
Write-Host "   ALL DONE!" -ForegroundColor Green
Write-Host "  ============================================================" -ForegroundColor Green
Write-Host ""
Write-Host "   Double-click 'Drop Cat Go Studio' on your Desktop to launch." -ForegroundColor White
Write-Host "   First video will take 2-3 min to load the AI model." -ForegroundColor White
Write-Host "   After that, each clip takes 3-10 minutes on RTX 4070." -ForegroundColor White
Write-Host ""
Write-Host "   Install log saved to: $LOG" -ForegroundColor Gray
Write-Host ""
Log "=== Installation complete ==="
