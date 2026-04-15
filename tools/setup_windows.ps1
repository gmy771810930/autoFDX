$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

# Script is in tools folder; project root is parent.
Set-Location -Path (Join-Path $PSScriptRoot "..")

$pyVersion = "3.12"
$pyFull = "3.12.10"
$installer = "python-$pyFull-amd64.exe"
$installerUrl = "https://www.python.org/ftp/python/$pyFull/$installer"

function Write-Step($msg) {
    Write-Host $msg -ForegroundColor Cyan
}

function Write-Info($msg) {
    Write-Host $msg -ForegroundColor Gray
}

function Write-WarnMsg($msg) {
    Write-Host $msg -ForegroundColor Yellow
}

function Write-Ok($msg) {
    Write-Host $msg -ForegroundColor Green
}

function Write-ErrMsg($msg) {
    Write-Host $msg -ForegroundColor Red
}

function Get-PythonCommand {
    if (Get-Command python -ErrorAction SilentlyContinue) {
        return "python"
    }
    if (Get-Command py -ErrorAction SilentlyContinue) {
        return "py -$pyVersion"
    }
    return $null
}

try {
    Write-Host ""
    Write-Step "[1/5] Checking Python..."
    $pyCmd = Get-PythonCommand

    if (-not $pyCmd) {
        Write-WarnMsg "Python not found. Installing Python $pyVersion..."

        if (Get-Command winget -ErrorAction SilentlyContinue) {
            Write-Info "Using winget to install Python..."
            winget install --id Python.Python.3.12 -e --silent --accept-package-agreements --accept-source-agreements | Out-Null
        }
        else {
            Write-Info "winget not found. Downloading official Python installer..."
            Invoke-WebRequest -Uri $installerUrl -OutFile $installer

            if (-not (Test-Path $installer)) {
                throw "Failed to download Python installer."
            }

            Write-Info "Running silent installer..."
            Start-Process -FilePath $installer -ArgumentList "/quiet InstallAllUsers=0 PrependPath=1 Include_test=0" -Wait
        }

        $pyCmd = Get-PythonCommand
    }

    if (-not $pyCmd) {
        throw "Python install failed or PATH not refreshed. Restart terminal/PC and try again."
    }

    Write-Host ""
    Write-Step "[2/5] Python detected:"
    Invoke-Expression "$pyCmd --version"

    if (-not (Test-Path "tools/requirements.txt")) {
        throw "tools/requirements.txt not found at: $(Get-Location)"
    }

    Write-Host ""
    Write-Step "[3/5] Upgrading pip..."
    Invoke-Expression "$pyCmd -m pip install --upgrade pip"

    Write-Host ""
    Write-Step "[4/5] Installing dependencies from tools/requirements.txt..."
    Invoke-Expression "$pyCmd -m pip install -r tools/requirements.txt"

    Write-Host ""
    Write-Step "[5/5] Verifying PyAutoGUI screenshot stack (pyscreeze / Pillow)..."
    $verifyPy = "$pyCmd -c `"import pyscreeze; import PIL; import pyautogui; print('pyscreeze+Pillow+pyautogui OK')`""
    Invoke-Expression $verifyPy
    if ($LASTEXITCODE -ne 0) {
        Write-WarnMsg "Import verification failed. Force-reinstalling screenshot-related packages..."
        Invoke-Expression "$pyCmd -m pip install --upgrade --force-reinstall pyscreeze Pillow pyautogui"
        Invoke-Expression $verifyPy
        if ($LASTEXITCODE -ne 0) {
            throw "Still cannot import pyscreeze/PIL/pyautogui. Try: python -m pip install -r tools/requirements.txt"
        }
    }
    Write-Ok "Import verification passed."

    Write-Host ""
    Write-Ok "[DONE] Setup completed."
    Write-Info "You can now run:"
    Write-Host "  python fallen_doll.py" -ForegroundColor Green
    Write-Info "Note: If pip printed 'Scripts is not on PATH', it is usually harmless when you run programs via 'python -m ...'."
    exit 0
}
catch {
    Write-Host ""
    Write-ErrMsg "[FAILED] Setup failed."
    Write-ErrMsg $_.Exception.Message
    exit 1
}
