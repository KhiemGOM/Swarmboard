param(
    [ValidateSet('start','status','mutex','jobs','retire')]
    [string]$Command = 'start',
    [string]$AgentId,
    [string]$Reason,
    [string]$Workspace,
    [string]$Database,
    [int]$Port
)
$ErrorActionPreference = 'Stop'
$boardPython = (Get-Command python -ErrorAction Stop).Source
$boardArguments = @((Join-Path $PSScriptRoot 'boardctl.py'), $Command)
if ($AgentId) { $boardArguments += $AgentId }
if ($Reason) { $boardArguments += @('--reason', $Reason) }
if ($Workspace) { $boardArguments += @('--workspace', $Workspace) }
if ($Port) { $boardArguments += @('--port', "$Port") }
if ($Database) { $boardArguments += @('--database', $Database) }
& $boardPython @boardArguments
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
