<#
  门店运营系统 - 每日统一运维入口

  锁、重试、ops_runs 落库、失败告警都在 scripts/daily_sync.py 的 Python
  调度器中实现；本脚本只负责找到项目根目录并调用它。

  手动执行：
    powershell -NoProfile -ExecutionPolicy Bypass -File scripts\daily_sync.ps1

  定时注册（管理员 PowerShell）：
    $scriptPath = 'E:\项目\aiyuangong\门店运营系统\scripts\daily_sync.ps1'
    $action  = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument ('-NoProfile -ExecutionPolicy Bypass -File "{0}"' -f $scriptPath)
    $trigger = New-ScheduledTaskTrigger -Daily -At 08:30
    Register-ScheduledTask -TaskName '门店运营系统-每日同步' -Action $action -Trigger $trigger
#>
[CmdletBinding()]
param(
    [string]$StoreId = $(if ($env:STORE_AGENT_STORE_ID) { $env:STORE_AGENT_STORE_ID } else { 'WH-LION-001' }),
    [string]$Python = $(if ($env:PYTHON_BIN) { $env:PYTHON_BIN } else { 'python' }),
    [string]$Config = $env:STORE_AGENT_CONFIG,
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$ExtraArgs = @()
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$entrypoint = Join-Path $root 'scripts\daily_sync.py'
if (-not (Test-Path -LiteralPath $entrypoint -PathType Leaf)) {
    throw "找不到调度入口：$entrypoint"
}

$arguments = @($entrypoint, '--store-id', $StoreId)
if (-not [string]::IsNullOrWhiteSpace($Config)) {
    $arguments += @('--config', $Config)
}
if ($ExtraArgs.Count -gt 0) {
    $arguments += $ExtraArgs
}

$env:PYTHONIOENCODING = 'utf-8'
$env:PYTHONUTF8 = '1'
Push-Location -LiteralPath $root
try {
    & $Python @arguments
    exit $LASTEXITCODE
}
finally {
    Pop-Location
}
