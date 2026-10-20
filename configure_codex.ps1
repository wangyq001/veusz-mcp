$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$taskPython = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
$taskServer = Join-Path $PSScriptRoot 'veusz_mcp.py'
if (-not (Test-Path -LiteralPath $taskPython)) { throw 'Run setup.ps1 first.' }
$taskOldPreference = $ErrorActionPreference
$ErrorActionPreference = 'Continue'
$taskExisting = & codex mcp get veusz --json 2>$null
$taskGetExit = $LASTEXITCODE
$ErrorActionPreference = $taskOldPreference
if ($taskGetExit -eq 0) {
    $taskDefinition = $taskExisting | ConvertFrom-Json
    if ($taskDefinition.transport.command -ne $taskPython -or $taskDefinition.transport.args[0] -ne $taskServer) {
        throw 'An unrelated MCP server named veusz already exists. Resolve this name conflict first.'
    }
    Write-Output 'The Veusz MCP is already configured for this project.'
} else {
    & codex mcp add veusz -- $taskPython $taskServer
    if ($LASTEXITCODE -ne 0) { throw 'MCP registration failed.' }
}
Write-Output 'Reload MCP servers in Codex, or restart Codex without closing Veusz.'
