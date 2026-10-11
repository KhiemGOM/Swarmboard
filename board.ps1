param(
    [ValidateSet('start','status','mutex','jobs','retire','wake','configure','cleanup')]
    [string]$Command = 'start',
    [string]$AgentId,
    [string]$Reason,
    [string]$Workspace,
    [string]$Database,
    [int]$Port,
    [string]$ThreadId,
    [string]$Socket,
    [string]$Codex,
    [string]$McpConfig,
    [switch]$Once,
    [switch]$IncludeExisting
)
$ErrorActionPreference = 'Stop'
$boardPython = (Get-Command python -ErrorAction Stop).Source
$boardArguments = @((Join-Path $PSScriptRoot 'boardctl.py'), $Command)
if ($AgentId) { $boardArguments += $AgentId }
if ($Reason) { $boardArguments += @('--reason', $Reason) }
if ($Workspace) { $boardArguments += @('--workspace', $Workspace) }
if ($PSBoundParameters.ContainsKey('Port')) { $boardArguments += @('--port', "$Port") }
if ($Database) { $boardArguments += @('--database', $Database) }
if ($ThreadId) { $boardArguments += @('--thread-id', $ThreadId) }
if ($Socket) { $boardArguments += @('--socket', $Socket) }
if ($McpConfig) { $boardArguments += @('--mcp-config', $McpConfig) }
if ($Codex) { $boardArguments += @('--codex', $Codex) }
if ($Once) { $boardArguments += '--once' }
if ($IncludeExisting) { $boardArguments += '--include-existing' }
& $boardPython @boardArguments
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
