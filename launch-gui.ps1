$ErrorActionPreference = 'Stop'

function Test-GuiPython {
    param([string]$Executable)

    # Windows PowerShell 5.1 treats native stderr as an error even with 2>$null.
    # Read both pipes asynchronously so a failed import cannot stop fallback or
    # block a probe when either pipe fills.
    $probe = New-Object System.Diagnostics.Process
    try {
        $probe.StartInfo = New-Object System.Diagnostics.ProcessStartInfo
        $probe.StartInfo.FileName = $Executable
        $probe.StartInfo.Arguments = '-c "import PyQt6.QtWidgets; import qfluentwidgets"'
        $probe.StartInfo.UseShellExecute = $false
        $probe.StartInfo.CreateNoWindow = $true
        $probe.StartInfo.RedirectStandardOutput = $true
        $probe.StartInfo.RedirectStandardError = $true
        if (-not $probe.Start()) { return $false }
        $stdout = $probe.StandardOutput.ReadToEndAsync()
        $stderr = $probe.StandardError.ReadToEndAsync()
        if (-not $probe.WaitForExit(10000)) {
            $probe.Kill()
            [void]$probe.WaitForExit(1000)
            return $false
        }
        $drained = [System.Threading.Tasks.Task]::WaitAll(
            [System.Threading.Tasks.Task[]]@($stdout, $stderr), 1000)
        return $drained -and $probe.ExitCode -eq 0
    }
    catch {
        return $false
    }
    finally {
        $probe.Dispose()
    }
}

$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$launcher = Join-Path $projectRoot 'start-gui.pyw'
$candidates = @((Join-Path $projectRoot '.venv\Scripts\python.exe'))
$globalPython = Get-Command python.exe -ErrorAction SilentlyContinue
if ($globalPython) { $candidates += $globalPython.Source }
$selectedPython = $null
foreach ($candidate in $candidates) {
    if (-not (Test-Path -LiteralPath $candidate)) { continue }
    if (Test-GuiPython $candidate) { $selectedPython = $candidate; break }
}
if (-not $selectedPython) {
    throw "GUI dependencies are missing or Python probes failed. From `"$projectRoot`", run: .\.venv\Scripts\python.exe -m pip install `".[gui]`""
}
$pythonw = Join-Path (Split-Path -Parent $selectedPython) 'pythonw.exe'
if (-not (Test-Path -LiteralPath $pythonw)) { $pythonw = $selectedPython }
$processArguments = '"' + $launcher + '"'
Start-Process -FilePath $pythonw -ArgumentList $processArguments -WorkingDirectory $projectRoot -WindowStyle Normal
