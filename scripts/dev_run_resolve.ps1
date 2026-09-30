# Launch DaVinci Resolve so that it loads the *freshly built* plugin from build\plugin (no admin, nothing is installed),
# and make the plugin start the service from this repo's Python. Close Resolve normally when done.
$root = Split-Path -Parent $PSScriptRoot
$resolve = "C:\Program Files\Blackmagic Design\DaVinci Resolve\Resolve.exe"
if (-not (Test-Path $resolve)) { throw "Resolve.exe not found at $resolve" }
$plugin = Join-Path $root "build\plugin\AICutout.ofx.bundle\Contents\Win64\AICutout.ofx"
if (-not (Test-Path $plugin)) { throw "Build the plugin first: bash scripts/build_plugin.sh" }
$env:OFX_PLUGIN_PATH = Join-Path $root "build\plugin"
$py = Join-Path $root ".venv\Scripts\python.exe"
$env:AICUTOUT_SERVICE_CMD = "`"$py`" -m inference.server"
$env:PYTHONPATH = $root
Set-Location $root                      # the service is started with this working directory (inherited)
Write-Host "OFX_PLUGIN_PATH = $env:OFX_PLUGIN_PATH"
Write-Host "AICUTOUT_SERVICE_CMD = $env:AICUTOUT_SERVICE_CMD"
Start-Process $resolve
Write-Host "Resolve started. Look for 'OFX: loading com.aicutout.AICutout' in:"
Write-Host "  $env:APPDATA\Blackmagic Design\DaVinci Resolve\Support\logs\davinci_resolve.log"
