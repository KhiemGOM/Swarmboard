param(
    [string]$Workspace = (Get-Location).Path,
    [string]$Database,
    [int]$Port = 4317
)
$ErrorActionPreference = 'Stop'
$boardPython = (Get-Command python -ErrorAction Stop).Source
$boardArguments = @((Join-Path $PSScriptRoot 'start_board.py'), '--workspace', $Workspace, '--port', "$Port")
if ($Database) { $boardArguments += @('--database', $Database) }
& $boardPython @boardArguments
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
