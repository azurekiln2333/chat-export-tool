param([string]$OutputDirectory = (Join-Path $PSScriptRoot '..\..\.work\legacy-qq'))
$ErrorActionPreference = 'Stop'
$targetDirectory = [IO.Path]::GetFullPath($OutputDirectory)
[IO.Directory]::CreateDirectory($targetDirectory) | Out-Null
$target = Join-Path $targetDirectory 'legacy-qq-helper.exe'
if (Test-Path -LiteralPath $target) {
    throw 'Output already exists. Choose a new build directory to retain the previous helper.'
}
$compiler = Join-Path $env:WINDIR 'Microsoft.NET\Framework\v4.0.30319\csc.exe'
if (-not (Test-Path -LiteralPath $compiler)) { throw '.NET Framework 4 C# compiler is required.' }
$oldTemp = $env:TEMP
$oldTmp = $env:TMP
try {
    $env:TEMP = $targetDirectory
    $env:TMP = $targetDirectory
    & $compiler /nologo /codepage:65001 /platform:x86 /optimize+ /target:exe /reference:System.Web.Extensions.dll "/out:$target" (Join-Path $PSScriptRoot 'LegacyQq.cs')
    if ($LASTEXITCODE -ne 0) { throw 'Legacy QQ helper build failed.' }
    Write-Output $target
} finally {
    $env:TEMP = $oldTemp
    $env:TMP = $oldTmp
}
