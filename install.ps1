param([switch]$CoreOnly)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$pythonPath = Join-Path $projectRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $pythonPath)) {
    & python -m venv (Join-Path $projectRoot '.venv')
    if ($LASTEXITCODE -ne 0) { throw 'Could not create a Python environment. Install Python 3.10 or newer.' }
}
$installTarget = if ($CoreOnly) { $projectRoot } else { "$projectRoot[full]" }
& $pythonPath -m pip install $installTarget
if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed. See the pip output above.' }
& $pythonPath -m duoshan_export doctor
if ($LASTEXITCODE -ne 0) { throw 'The installation check failed. See doctor output above.' }
Write-Host "Run: & '$pythonPath' -m duoshan_export --help"
