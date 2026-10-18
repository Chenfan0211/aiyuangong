<#
  门店运营系统 - 每日数据同步（批次 2）

  作用：扫描 imports/inbox/ 里的平台后台导出文件 → 批量导入 → 输出数据新鲜度与近 7 天摘要，
        全过程追加写入 data/logs/daily_sync_YYYYMMDD.log。
        只要有文件导入失败，脚本退出码为 1（便于计划任务/监控告警）。

  手动执行：
    powershell -NoProfile -ExecutionPolicy Bypass -File scripts\daily_sync.ps1

  定时注册（一次性，管理员 PowerShell；本脚本不会自动注册）：
    $scriptPath = 'E:\项目\aiyuangong\门店运营系统\scripts\daily_sync.ps1'
    $action  = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument ('-NoProfile -ExecutionPolicy Bypass -File "{0}"' -f $scriptPath)
    $trigger = New-ScheduledTaskTrigger -Daily -At 08:30
    Register-ScheduledTask -TaskName '门店运营系统-每日同步' -Action $action -Trigger $trigger -Description '每日导入平台报表并输出状态'

  取消注册：
    Unregister-ScheduledTask -TaskName '门店运营系统-每日同步' -Confirm:$false
#>
[CmdletBinding()]
param(
    [string]$StoreId = 'WH-LION-001',
    [string]$Python = 'python'
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$logDir = Join-Path $root 'data\logs'
if (-not (Test-Path -LiteralPath $logDir)) {
    New-Item -ItemType Directory -Path $logDir -Force | Out-Null
}
$logFile = Join-Path $logDir ('daily_sync_{0}.log' -f (Get-Date -Format 'yyyyMMdd'))

function Write-Log {
    param([string]$Text)
    $line = '[{0}] {1}' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $Text
    Add-Content -LiteralPath $logFile -Value $line -Encoding utf8
}

function Invoke-SyncStep {
    param([string]$Label, [string[]]$Arguments)
    Write-Log ('--- {0} ---' -f $Label)
    Write-Log ('执行: {0} {1}' -f $Python, ($Arguments -join ' '))
    Push-Location -LiteralPath $root
    try {
        $output = & $Python @Arguments 2>&1
        $code = $LASTEXITCODE
    }
    finally {
        Pop-Location
    }
    foreach ($line in @($output)) { Write-Log ('  ' + [string]$line) }
    Write-Log ('退出码: {0}' -f $code)
    return $code
}

Write-Log ('=== 每日同步开始（store_id={0}）===' -f $StoreId)

$importCode = Invoke-SyncStep -Label '批量导入 inbox' -Arguments @('platform_sync.py', '--store-id', $StoreId, 'import-dir')
$qualityCode = Invoke-SyncStep -Label '数据质量体检' -Arguments @('platform_sync.py', '--store-id', $StoreId, 'data-quality', '--days', '30')
$planCode = Invoke-SyncStep -Label '生成当日计划' -Arguments @('store_cli.py', 'plan', '--date', 'today')
$notifyCode = Invoke-SyncStep -Label '推送日报（默认草稿）' -Arguments @('store_cli.py', 'notify', '--kind', 'daily')
$statusCode = Invoke-SyncStep -Label '数据新鲜度' -Arguments @('platform_sync.py', '--store-id', $StoreId, 'status')
$summaryCode = Invoke-SyncStep -Label '近 7 天摘要' -Arguments @('platform_sync.py', '--store-id', $StoreId, 'summary', '--days', '7')

if ($importCode -ne 0 -or $qualityCode -ne 0 -or $planCode -ne 0 -or $notifyCode -ne 0) {
    Write-Log '=== 结束：存在导入失败的文件或体检命令异常，请查看上面的输出 ==='
    exit 1
}
Write-Log ('=== 结束：导入/体检/派单完成（quality={0} plan={1} status={2} summary={3}）===' -f $qualityCode, $planCode, $statusCode, $summaryCode)
exit 0
