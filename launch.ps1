$ErrorActionPreference = "Stop"
$toolScript = Join-Path $PSScriptRoot "media_asset_collector.py"
$bundledPython = Join-Path $env:USERPROFILE ".cache\codex-runtimes\codex-primary-runtime\dependencies\python\pythonw.exe"

try {
    $launcher = Get-Command "pyw.exe" -ErrorAction SilentlyContinue
    if ($null -ne $launcher) {
        Start-Process -FilePath $launcher.Source -ArgumentList @("-3", $toolScript)
        exit 0
    }

    if (Test-Path -LiteralPath $bundledPython -PathType Leaf) {
        Start-Process -FilePath $bundledPython -ArgumentList @($toolScript)
        exit 0
    }

    $pythonw = Get-Command "pythonw.exe" -ErrorAction SilentlyContinue
    if ($null -ne $pythonw) {
        Start-Process -FilePath $pythonw.Source -ArgumentList @($toolScript)
        exit 0
    }

    Add-Type -AssemblyName PresentationFramework
    [System.Windows.MessageBox]::Show(
        "Python 3 was not found. Please install Python 3 and select Add Python to PATH.",
        "Unable to start",
        "OK",
        "Error"
    ) | Out-Null
    exit 1
}
catch {
    Add-Type -AssemblyName PresentationFramework
    [System.Windows.MessageBox]::Show(
        "Unable to start: $($_.Exception.Message)",
        "Unable to start",
        "OK",
        "Error"
    ) | Out-Null
    exit 1
}
