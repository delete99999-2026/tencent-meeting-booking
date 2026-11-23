#Requires -RunAsAdministrator
<#
.SYNOPSIS
  无显示器虚拟机一键配置脚本（腾讯会议预订平台）。

.DESCRIPTION
  把 Windows 11 虚拟机配置为「自动登录到交互桌面 + 不锁屏 + 不休眠 + 登录时自动启动预订服务」，
  使依赖 GUI 的 pywinauto 驱动能在没有物理显示器的虚拟机里运行。

  本脚本做四件事（均可逆，用 -Undo 撤销）：
    1. 启用自动登录（写 Winlogon 注册表：AutoAdminLogon / DefaultUserName / DefaultPassword / DefaultDomainName）
    2. 关闭屏保密码锁定、关闭锁屏
    3. 关闭睡眠/休眠（交流供电）
    4. 创建「登录时」运行本项目的任务计划（交互桌面、最高权限）

  安全警告：
    - 自动登录会把账户明文密码写入注册表 DefaultPassword，这是微软自动登录的标准机制，但有安全风险。
    - 请务必为这台虚拟机使用【专用受限账户】，不要用你的主力账户或个人微软账户。
    - 配置完成后该机开机即自动进入桌面，任何能物理/远程接触控制台的人都可访问。
    - 生产环境请放在隔离网络，仅开放必要端口。

.PARAMETER Username
  自动登录使用的 Windows 本地账户名。

.PARAMETER Password
  该账户的明文密码（用于自动登录 + 任务计划）。

.PARAMETER ProjectDir
  项目目录，默认取脚本所在目录。

.PARAMETER TaskName
  计划任务名称，默认 TencentMeetingBooking。

.PARAMETER Undo
  撤销全部配置（关闭自动登录、恢复屏保锁、删除计划任务）。不会删除密码文件以外的东西。

.EXAMPLE
  .\setup_headless.ps1 -Username "meet" -Password "YourStrongPass!123"

.EXAMPLE
  .\setup_headless.ps1 -Undo
#>
param(
    [string]$Username = "",
    [string]$Password = "",
    [string]$ProjectDir = $PSScriptRoot,
    [string]$TaskName = "TencentMeetingBooking",
    [switch]$Undo
)

$ErrorActionPreference = "Stop"
function Info($m) { Write-Host "[INFO]  $m" -ForegroundColor Cyan }
function Ok($m)   { Write-Host "[ OK ]  $m" -ForegroundColor Green }
function Warn($m) { Write-Host "[WARN]  $m" -ForegroundColor Yellow }
function Err($m)  { Write-Host "[FAIL]  $m" -ForegroundColor Red }

$winlogon = "HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon"
$deskKey  = "HKCU:\Control Panel\Desktop"
$policySys = "HKLM:\SOFTWARE\Policies\Microsoft\Windows\System"

# ---------------------------------------------------------------- Undo
if ($Undo) {
    Info "Undoing headless configuration ..."
    try {
        Set-ItemProperty -Path $winlogon -Name AutoAdminLogon -Value "0" -Force
        Remove-ItemProperty -Path $winlogon -Name DefaultPassword -ErrorAction SilentlyContinue
        Ok "Auto logon disabled (password value removed)."
    } catch { Warn "Could not fully reset Winlogon: $_" }
    try {
        Set-ItemProperty -Path $deskKey -Name ScreenSaverIsSecure -Value "1" -Force
        Ok "Screen saver lock restored (default)."
    } catch { Warn "Could not restore screen saver lock: $_" }
    try {
        if (Test-Path $policySys) { Remove-ItemProperty -Path $policySys -Name DisableLockScreen -ErrorAction SilentlyContinue }
        Ok "Lock screen policy restored."
    } catch { Warn "Could not restore lock screen policy: $_" }
    try {
        $t = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
        if ($t) { Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false; Ok "Scheduled task removed." }
        else { Info "Scheduled task not present." }
    } catch { Warn "Could not remove scheduled task: $_" }
    Warn "Undo does not restore power plan timeouts; run: powercfg /change standby-timeout-ac 10"
    Info "Done."
    exit 0
}

# ---------------------------------------------------------------- Validate
if ([string]::IsNullOrWhiteSpace($Username) -or [string]::IsNullOrWhiteSpace($Password)) {
    Err "Username and Password are required. Example:"
    Write-Host "  .\setup_headless.ps1 -Username 'meet' -Password 'YourStrongPass!123'"
    exit 1
}
if (-not (Test-Path (Join-Path $ProjectDir "server\app.py"))) {
    Err "ProjectDir does not look right (server\app.py not found): $ProjectDir"
    exit 1
}

$pythonExe = (Get-Command python -ErrorAction SilentlyContinue).Source
if (-not $pythonExe) { Err "python not found in PATH."; exit 1 }
Info "python = $pythonExe"
Info "project = $ProjectDir"

# ---------------------------------------------------------------- 1. Auto logon
Info "[1/4] Configure auto logon ..."
Set-ItemProperty -Path $winlogon -Name AutoAdminLogon    -Value "1"          -Force
Set-ItemProperty -Path $winlogon -Name DefaultUserName   -Value $Username    -Force
Set-ItemProperty -Path $winlogon -Name DefaultPassword   -Value $Password    -Force
# 本地账户：DefaultDomainName 填空或机器名均可；填机器名更稳
Set-ItemProperty -Path $winlogon -Name DefaultDomainName -Value $env:COMPUTERNAME -Force
Ok "Auto logon set for $env:COMPUTERNAME\$Username"

# ---------------------------------------------------------------- 2. No lock screen
Info "[2/4] Disable screen lock ..."
Set-ItemProperty -Path $deskKey -Name ScreenSaverIsSecure -Value "0" -Force
Set-ItemProperty -Path $deskKey -Name ScreenSaveActive    -Value "0" -Force
Set-ItemProperty -Path $deskKey -Name SCRNSAVE.EXE        -Value ""  -Force -ErrorAction SilentlyContinue
if (-not (Test-Path $policySys)) { New-Item -Path $policySys -Force | Out-Null }
New-ItemProperty -Path $policySys -Name DisableLockScreen -Value 1 -PropertyType DWord -Force | Out-Null
Ok "Screen lock disabled."

# ---------------------------------------------------------------- 3. No sleep
Info "[3/4] Disable sleep / hibernate (AC) ..."
powercfg /change standby-timeout-ac 0    | Out-Null
powercfg /change hibernate-timeout-ac 0  | Out-Null
powercfg /change monitor-timeout-ac 0    | Out-Null
Ok "Sleep/hibernate/monitor timeout disabled on AC power."

# ---------------------------------------------------------------- 4. Scheduled task at logon
Info "[4/4] Create logon startup task ..."
$startBat = Join-Path $ProjectDir "启动服务.bat"
if (-not (Test-Path $startBat)) { Warn "启动服务.bat not found; will start python -m server.app directly." }

$action = if (Test-Path $startBat) {
    New-ScheduledTaskAction -Execute "cmd.exe" -Argument "/c `"$startBat`"" -WorkingDirectory $ProjectDir
} else {
    New-ScheduledTaskAction -Execute $pythonExe -Argument "-m server.app" -WorkingDirectory $ProjectDir
}
# 关键：-LogonType Interactive + RunLevel Highest => 服务运行在交互桌面，pywinauto 能看到窗口
$principal = New-ScheduledTaskPrincipal -UserId "$env:COMPUTERNAME\$Username" -LogonType Interactive -RunLevel Highest
$trigger = New-ScheduledTaskTrigger -AtLogOn -User "$env:COMPUTERNAME\$Username"
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit (New-TimeSpan -Days 3650)

$t = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($t) { Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false }
Register-ScheduledTask -TaskName $TaskName -Action $action -Principal $principal -Trigger $trigger -Settings $settings -Description "Tencent Meeting Booking service at interactive logon" | Out-Null
Ok "Scheduled task '$TaskName' created (runs at logon, interactive desktop, highest privileges)."

Write-Host ""
Ok "Headless configuration complete."
Warn "Remember: virtual display driver (e.g. Parsec VDD) must keep one display ONLINE."
Warn "Remember: Tencent Meeting client must stay logged in on this account."
Info "Reboot to apply. After reboot run: python scripts\preflight_headless.py"
