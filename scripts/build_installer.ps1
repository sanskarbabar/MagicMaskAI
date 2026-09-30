# Builds everything and produces dist\AICutout-Setup-<version>.exe
#   powershell -ExecutionPolicy Bypass -File scripts\build_installer.ps1 [-DevUser] [-SkipTests]
# Requirements: Python venv (.venv), llvm-mingw or MSVC for the native parts, Inno Setup 6 (winget install JRSoftware.InnoSetup)
param([switch]$DevUser, [switch]$SkipTests)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

function Step($m) { Write-Host "`n=== $m" -ForegroundColor Cyan }
$bash = "C:\Program Files\Git\bin\bash.exe"
$py = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) { throw "Create the venv first (see BUILD.md)" }

Step "Native core + OFX plugin"
& $bash scripts/build_native.sh
& $bash scripts/build_plugin.sh
if ($LASTEXITCODE -ne 0) { throw "plugin build failed" }

if (-not $SkipTests) {
    Step "Tests"
    & $bash scripts/build_host.sh
    & $py -m pytest tests -q -p no:cacheprovider
    if ($LASTEXITCODE -ne 0) { throw "tests failed - not building an installer from a failing tree" }
}

Step "Freezing the AI service and Companion (PyInstaller)"
& $py -m PyInstaller installer\aicutout.spec --noconfirm --distpath dist --workpath build\pyi
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed" }

Step "Compiling the installer (Inno Setup)"
$iscc = @("$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe", "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe", "$env:ProgramFiles\Inno Setup 6\ISCC.exe") | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $iscc) { throw "Inno Setup 6 not found" }
$defs = @("/DSourceRoot=$root")
if ($DevUser) { $defs += "/DDEV_USER_INSTALL" }
& $iscc @defs installer\AICutout.iss
if ($LASTEXITCODE -ne 0) { throw "Inno Setup failed" }
Get-ChildItem dist\*.exe | Select-Object Name, @{n = "MB"; e = { [math]::Round($_.Length / 1MB, 1) } }
