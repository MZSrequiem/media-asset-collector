param(
    [string]$Python = ""
)

$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot

if ([string]::IsNullOrWhiteSpace($Python)) {
    $systemPython = Get-Command "python.exe" -ErrorAction SilentlyContinue
    if ($null -ne $systemPython) {
        $Python = $systemPython.Source
    }
    else {
        $bundledPython = Join-Path $env:USERPROFILE ".cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"
        if (Test-Path -LiteralPath $bundledPython -PathType Leaf) {
            $Python = $bundledPython
        }
    }
}

if ([string]::IsNullOrWhiteSpace($Python) -or -not (Test-Path -LiteralPath $Python -PathType Leaf)) {
    throw "Python 3 was not found. Pass its full path with -Python."
}

$venvPath = Join-Path $PSScriptRoot ".venv-build"
$buildPython = Join-Path $venvPath "Scripts\python.exe"
if (-not (Test-Path -LiteralPath $buildPython -PathType Leaf)) {
    & $Python -m venv $venvPath
    if ($LASTEXITCODE -ne 0) { throw "Unable to create the build environment." }
}

& $buildPython -m pip install --disable-pip-version-check -r requirements-build.txt
if ($LASTEXITCODE -ne 0) { throw "Unable to install build dependencies." }

& $buildPython -m PyInstaller `
    --noconfirm `
    --clean `
    --onefile `
    --windowed `
    --name "media-asset-collector-windows-x64" `
    media_asset_collector.py
if ($LASTEXITCODE -ne 0) { throw "PyInstaller build failed." }

Write-Output "Built: $PSScriptRoot\dist\media-asset-collector-windows-x64.exe"
