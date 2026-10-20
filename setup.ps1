[CmdletBinding()]
param(
    [string]$Python,
    [switch]$Locked
)

$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$taskRuntime = $Python
if (-not $taskRuntime) {
    $taskLauncher = Get-Command py.exe -ErrorAction SilentlyContinue
    if ($taskLauncher) {
        $taskRuntime = & $taskLauncher.Source -3 -c 'import sys; print(sys.executable)'
        if ($LASTEXITCODE -ne 0) { throw 'Python launcher failed. Pass -Python with a Python 3.11+ executable.' }
    } else {
        $taskCommand = Get-Command python.exe -ErrorAction SilentlyContinue
        if ($taskCommand -and $taskCommand.Source -notlike '*\WindowsApps\*') {
            $taskRuntime = $taskCommand.Source
        }
    }
}
if (-not $taskRuntime) { throw 'Python 3.11+ was not found. Install Python or pass -Python C:\path\python.exe.' }
& $taskRuntime -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)'
if ($LASTEXITCODE -ne 0) { throw 'Python 3.11 or newer is required.' }
$taskEnvironment = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $taskEnvironment)) {
    & $taskRuntime -m venv --without-pip .venv
    if ($LASTEXITCODE -ne 0) { throw 'Virtual environment creation failed.' }
}
& $taskEnvironment -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)'
if ($LASTEXITCODE -ne 0) { throw 'The existing .venv needs Python 3.11+. Preserve it and create a new project checkout with a supported Python.' }
$taskRequirements = if ($Locked) { 'requirements-lock.txt' } else { 'requirements.txt' }
& $taskRuntime -m pip --python $taskEnvironment install --index-url https://pypi.org/simple -r $taskRequirements
if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed.' }
& '.\.venv\Scripts\python.exe' -m unittest discover -s tests -q
if ($LASTEXITCODE -ne 0) { throw 'Validation failed.' }
Write-Output 'Ready. Start the visible GUI with start_veusz.cmd.'
