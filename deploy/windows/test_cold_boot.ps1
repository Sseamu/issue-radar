# 콜드 부팅 테스트: "PC 꺼짐 → BIOS 자동 켜짐 → 로그인 없이 작업 실행 → 브리핑 → 자동 종료"를 지금 바로 확인
# 사용법 (일반 PowerShell):
#   cd $HOME\Documents\issue-radar
#   powershell -ExecutionPolicy Bypass -File deploy\windows\test_cold_boot.ps1            # 테스트 준비
#   powershell -ExecutionPolicy Bypass -File deploy\windows\test_cold_boot.ps1 -Restore   # 테스트 후 원래대로
param(
    [int]$BootInMinutes = 12,   # 지금부터 몇 분 뒤에 켜지게 할지 (BIOS 설정 + 종료할 시간 포함)
    [switch]$Restore
)
$ErrorActionPreference = "Stop"
$Root = (Resolve-Path "$PSScriptRoot\..\..").Path
$Env = Join-Path $Root ".env"
$Bak = Join-Path $Root ".env.before-test"
$Py = Join-Path $Root ".venv\Scripts\python.exe"
$utf8 = New-Object System.Text.UTF8Encoding $false

if ($Restore) {
    if (Test-Path $Bak) { Copy-Item $Bak $Env -Force; Remove-Item $Bak; Write-Host ".env 원래대로 복구 완료" -ForegroundColor Green }
    else { Write-Host "백업 파일이 없습니다 (이미 복구됨)" }
    Write-Host "BIOS의 Resume by Alarm 시각도 5:55:00 으로 되돌리세요." -ForegroundColor Yellow
    exit 0
}

if (-not (Get-ScheduledTask -TaskName IssueRadarMorning -ErrorAction SilentlyContinue)) {
    throw "작업이 등록되어 있지 않습니다. 관리자 PowerShell에서 deploy\windows\install_morning.ps1 을 먼저 실행하세요."
}

$boot  = (Get-Date).AddMinutes($BootInMinutes)
$boot  = $boot.AddSeconds(-$boot.Second)
$brief = $boot.AddMinutes(10)                         # 부팅 10분 뒤 브리핑 전송
$win   = "{0:HH:mm}-{1:HH:mm}" -f $boot.AddMinutes(-5), $boot.AddMinutes(8)

if (-not (Test-Path $Bak)) { Copy-Item $Env $Bak }   # 원본 백업 (한 번만)
$lines = [IO.File]::ReadAllLines($Env, $utf8) | Where-Object { $_ -notmatch '^(WAKE_WINDOW|BRIEF_AT|BRIEF_LEAD_MIN|MORNING_NOT_BEFORE)=' }
$lines += "WAKE_WINDOW=$win"
$lines += ("BRIEF_AT={0:HH:mm}" -f $brief)
$lines += "BRIEF_LEAD_MIN=4"
# BIOS 설정 후 윈도우가 잠깐 켜질 때 작업이 미리 돌아 버리지 않도록, 예정 부팅 3분 전까지는 실행 안 함
$lines += ("MORNING_NOT_BEFORE={0:HH:mm}" -f $boot.AddMinutes(-3))
[IO.File]::WriteAllLines($Env, $lines, $utf8)

# 오늘 브리핑을 이미 보냈다는 기록을 지워 테스트에서 다시 보내게 함
& $Py -c "from radar import db; db.kv_set('brief_date', '')"

Write-Host ""
Write-Host "테스트 준비 완료" -ForegroundColor Green
Write-Host ("  자동 부팅 예정 : {0:HH:mm}  (BIOS Resume by Alarm 을 {0:H} / {0:mm} / 0 으로 설정)" -f $boot)
Write-Host ("  브리핑 전송    : {0:HH:mm}" -f $brief)
Write-Host ("  자동 종료      : 브리핑 후 약 2분  (자동 종료 허용 시간대 {0})" -f $win)
Write-Host ""
Write-Host "다음 순서로 진행하세요:" -ForegroundColor Cyan
Write-Host "  1) (선택) Slack 에 '@radar 엔비디아' 처럼 요청 하나 남기기"
Write-Host "  2) 재부팅 → Del → F2 → Settings → Platform Power → Resume by Alarm 시각을 위 시각으로 → F10 저장"
Write-Host "  3) 윈도우가 켜지면 로그인하지 말고 바로 '시스템 종료'  (로그인하면 안전장치 때문에 안 꺼집니다)"
Write-Host "  4) 켜지는지 보고, 결과는 휴대폰 Slack 으로 확인. 로그인·마우스 조작 금지"
Write-Host "  5) 끝나면 PC를 켜서 이 스크립트를 -Restore 로 실행 + BIOS 시각을 5:55 로 복구"
