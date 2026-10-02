# Launch DaVinci Resolve so that it loads the freshly built effect from build\plugin (no admin, nothing is installed).
# The effect's "Open AI Cutout" button then starts the app from this repo's Python.
$root = Split-Path -Parent $PSScriptRoot
$resolve = "C:\Program Files\Blackmagic Design\DaVinci Resolve\Resolve.exe"
if (-not (Test-Path $resolve)) { throw "Resolve.exe not found at $resolve" }
$plugin = Join-Path $root "build\plugin\AICutout.ofx.bundle\Contents\Win64\AICutout.ofx"
if (-not (Test-Path $plugin)) { throw "Build the plugin first: bash scripts/build_plugin.sh" }
$py = Join-Path $root ".venv\Scripts\python.exe"
$env:OFX_PLUGIN_PATH = Join-Path $root "build\plugin"
$env:AICUTOUT_APP_CMD = "`"$py`" -m plugin.UI.companion_main"
$env:PYTHONPATH = $root
Set-Location $root
Write-Host "OFX_PLUGIN_PATH   = $env:OFX_PLUGIN_PATH"
Write-Host "AICUTOUT_APP_CMD  = $env:AICUTOUT_APP_CMD"
Start-Process $resolve
Write-Host "Resolve started. Look for 'OFX: loading com.aicutout.AICutout' in:"
Write-Host "  $env:APPDATA\Blackmagic Design\DaVinci Resolve\Support\Logs\davinci_resolve.log"
