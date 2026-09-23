# Issue Radar 아침 배치 설치 스크립트
# 사용법: 시작 메뉴에서 "PowerShell"을 우클릭 → [관리자 권한으로 실행] 후
#   cd $HOME\Documents\issue-radar
#   powershell -ExecutionPolicy Bypass -File deploy\windows\install_morning.ps1
# 제거:  powershell -ExecutionPolicy Bypass -File deploy\windows\install_morning.ps1 -Uninstall
param(
    [string]$Time = "06:00",       # 매일 실행 시각 (BIOS 자동 부팅은 이보다 5분 앞으로 설정)
    [switch]$Uninstall
)
$ErrorActionPreference = "Stop"
$TaskName = "IssueRadarMorning"

if (-not ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
        [Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw "관리자 권한 PowerShell에서 실행하세요."
}

if ($Uninstall) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
    Write-Host "작업 '$TaskName' 제거 완료 (전원 설정은 그대로 둡니다)"
    exit 0
}

$Root = (Resolve-Path "$PSScriptRoot\..\..").Path
$Py = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path $Py)) { throw "가상환경이 없습니다: $Py  (README의 설치 단계를 먼저 진행하세요)" }
if (-not (Test-Path (Join-Path $Root ".env"))) { throw ".env 파일이 없습니다. .env.example을 복사해 토큰을 입력하세요." }

Write-Host "[1/4] 빠른 시작 끄기 (켜져 있으면 BIOS 예약 부팅이 실패할 수 있음)"
reg add "HKLM\SYSTEM\CurrentControlSet\Control\Session Manager\Power" /v HiberbootEnabled /t REG_DWORD /d 0 /f | Out-Null

Write-Host "[2/4] 절전 해제 타이머 허용 (절전 상태에서 06:00에 깨우기용)"
powercfg /SETACVALUEINDEX SCHEME_CURRENT SUB_SLEEP RTCWAKE 1
powercfg /SETACTIVE SCHEME_CURRENT

Write-Host "[3/4] Windows 업데이트 '사용 시간' 05:00~23:00 (아침 작업 중 자동 재시작 방지)"
$wu = "HKLM\SOFTWARE\Microsoft\WindowsUpdate\UX\Settings"
reg add $wu /v SmartActiveHoursState /t REG_DWORD /d 0 /f | Out-Null
reg add $wu /v ActiveHoursStart /t REG_DWORD /d 5 /f | Out-Null
reg add $wu /v ActiveHoursEnd /t REG_DWORD /d 23 /f | Out-Null

Write-Host "[4/4] 작업 스케줄러 등록: 부팅 1분 후 + 매일 $Time (로그인 없이 실행)"
$action = New-ScheduledTaskAction -Execute $Py -Argument "-m radar.morning" -WorkingDirectory $Root
$tBoot = New-ScheduledTaskTrigger -AtStartup
$tBoot.Delay = "PT1M"
$tDaily = New-ScheduledTaskTrigger -Daily -At $Time
$settings = New-ScheduledTaskSettingsSet -WakeToRun -StartWhenAvailable -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 90) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
# S4U: 비밀번호를 저장하지 않고 '로그온 여부와 관계없이' 실행 (인터넷 사용 가능)
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType S4U -RunLevel Highest
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger @($tBoot, $tDaily) -Settings $settings `
    -Principal $principal -Force | Out-Null

Write-Host ""
Write-Host "설치 완료. 다음을 확인하세요:" -ForegroundColor Green
Write-Host " 1) 지금 한 번 실행 테스트:  Start-ScheduledTask -TaskName $TaskName"
Write-Host "    → 로그: $Root\data\morning.log  (로그인 상태이므로 PC는 꺼지지 않습니다)"
Write-Host " 2) BIOS에서 매일 05:55 자동 전원 켜기 설정 (README 'BIOS 설정' 참고)"
Write-Host " 3) 아침에 PC를 쓰고 싶은 날은 $Root\NO_SHUTDOWN 파일을 만들어 두면 자동 종료하지 않습니다"
