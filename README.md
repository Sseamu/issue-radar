# Issue Radar — Slack 뉴스 에이전트

**실행 방식을 하나 고르세요**

| | **C. PC 아침 배치 (현재 선택)** | A. 클라우드 서버 + Claude API | B. PC 상시 가동 |
|---|---|---|---|
| PC 전원 | 매일 05:55 자동 부팅 → 07:00 브리핑 후 자동 종료 | 꺼도 됨 | 24시간 켜 둠 |
| 응답 시점 | **다음 날 아침** (밤사이 요청을 몰아서 처리) | 즉시 | 즉시 |
| LLM | Ollama qwen3:14b (로컬) | Claude Sonnet 5 + Haiku 4.5 | Ollama qwen3:14b |
| 쟁점 군집 | bge-m3 임베딩 (GPU) | TF-IDF | bge-m3 임베딩 (GPU) |
| 예측 채점 | 이모지 반응 ✅/❌ | 버튼 또는 이모지 | 버튼 또는 이모지 |
| 월 비용(추정) | 전기료 **약 2천 원** | 4~5천 원 | 전기료 약 1만 원 |
| 설정 | 3단계-B 설치 → **3단계-C** | 3단계-A | 3단계-B |

기능은 세 방식 모두 같습니다: 아침 브리핑(지난 24시간 Top 10), `@radar 키워드` 분석(최근 이슈 → 1년 쟁점 → 1·3·6·12개월 예측), 스레드 후속 질문, 예측 채점, `@radar 비용`(API 사용액). 차이는 응답 시점과 실행 위치뿐입니다.

**GitHub 업로드는 C 방식에 필요 없습니다.** 백업용으로 올린다면 Private 저장소로 올리고, `.github/workflows`가 있으면 Actions 탭에서 daily-brief를 Disable 하세요(브리핑 중복 방지).

```
bot.py                  Slack 봇 (Socket Mode: 공개 URL/포트 개방 불필요)
radar/morning.py        아침 배치 (밀린 요청 처리 + 채점 수집 + 자동 종료)
radar/handlers.py       봇·배치 공통 처리기
deploy/windows/         작업 스케줄러 설치 스크립트
radar/daily.py          아침 브리핑: RSS 수집 → 같은 사건 묶기 → 보도 매체 수로 순위 → LLM 요약
radar/agent.py          의도 파악 · 분석 단계 · 후속 질문(RAG) · 성적
radar/collect.py        키워드 뉴스 수집 (Google News RSS 1년치, 네이버 API, 빅카인즈 엑셀)
radar/topics.py         중복 제거 → 임베딩 → 쟁점 군집 → 월별 추이
radar/analyze.py        쟁점 요약 · 예측 · Brier 채점
feeds.json              브리핑용 섹션별 RSS 목록 (직접 수정 가능)
slack-manifest.yml      Slack 앱 설정
.github/workflows/      07:00 브리핑 예약 실행
```

---

## 1단계. Slack 준비 (약 10분)

1. **워크스페이스 만들기**: slack.com/get-started → 이메일로 가입 → 워크스페이스 이름 입력(예: `som-lab`). 무료 요금제로 충분합니다.
2. **채널 만들기**: `#daily-brief`(브리핑용), `#radar`(분석·채점용)
3. **앱 만들기**: api.slack.com/apps → *Create New App* → *From a manifest* → 워크스페이스 선택 → `slack-manifest.yml` 내용을 붙여넣기 → Create
4. **토큰 2개 발급**
   - *Basic Information* → *App-Level Tokens* → Generate (scope: `connections:write`) → **`xapp-…`** = `SLACK_APP_TOKEN`
   - *Install App* → Install to Workspace → **`xoxb-…`** = `SLACK_BOT_TOKEN`
5. **채널에 봇 초대**: 두 채널에서 각각 `/invite @radar` 입력

## 2단계. 아침 브리핑 — GitHub Actions (약 10분)

1. github.com에서 **Private** 저장소를 만들고 이 폴더를 올립니다(`.env`는 `.gitignore`에 들어 있어 업로드되지 않습니다).
2. 저장소 → *Settings → Secrets and variables → Actions* 에서:
   - **Secrets**: `SLACK_BOT_TOKEN`, 그리고 LLM 키 하나
     - Claude 사용: `ANTHROPIC_API_KEY` (console.anthropic.com)
     - Gemini 사용: `OPENAI_API_KEY`에 Gemini 키를 넣고, Variables에 `LLM_PROVIDER=openai`, `OPENAI_BASE_URL=https://generativelanguage.googleapis.com/v1beta/openai`, `OPENAI_MODEL=gemini-2.5-flash`
   - **Variables**(선택): `SLACK_BRIEF_CHANNEL`, `BRIEF_SECTIONS`
3. *Actions* 탭 → `daily-brief` → **Run workflow** 버튼으로 바로 테스트해 봅니다.

비용: 1회당 LLM 입력 약 5천~1만 토큰이라 Claude Haiku 기준 **월 1천 원 미만**입니다. GitHub Actions는 비공개 저장소도 월 2,000분까지 무료이고, 이 작업은 1회 2~3분이면 끝납니다.
주의: GitHub 예약 실행은 **5~20분 늦게 도착할 수 있습니다.** 정각에 받아야 한다면 PC의 `BRIEF_LOCAL=1` 방식을 쓰세요.

## 3단계-A. 클라우드 서버 배포 (약 20분)

1. console.cloud.google.com → Compute Engine → **VM 만들기**
   - 리전 **us-central1 / us-west1 / us-east1 중 하나** (무료는 이 3곳만 해당), 머신 **e2-micro**, 부팅 디스크 Ubuntu 24.04 / **표준 영구 디스크** 30GB
   - 방화벽 인바운드 포트는 열 필요가 없습니다 (Socket Mode는 서버에서 Slack으로 나가는 연결만 씀)
2. VM의 SSH 버튼으로 접속한 뒤:
```bash
git clone <내 private 저장소 주소> issue-radar && cd issue-radar   # 또는 zip을 업로드해 풀기
bash deploy/setup.sh          # 첫 실행은 .env만 만들고 멈춤
nano .env                     # ANTHROPIC_API_KEY, SLACK_BOT_TOKEN, SLACK_APP_TOKEN 입력
bash deploy/setup.sh          # 다시 실행 → 서비스 등록 (재부팅·오류 시 자동 재시작)
.venv/bin/python -m radar.daily --check-feeds   # RSS 점검
journalctl -u radar-bot -f    # 로그 보기
```
3. 서버가 `BRIEF_LOCAL=1`로 07:00 정각에 브리핑을 보냅니다. 이때 **GitHub Actions 예약은 꺼 두세요**(중복 방지): 저장소 → Actions → daily-brief → Disable workflow. 2단계는 건너뛰어도 됩니다.
4. 코드 업데이트: `git pull && sudo systemctl restart radar-bot`
5. 백업: DB는 `data/radar.db` 파일 하나입니다. 가끔 `scp`로 내려받아 두세요.

## 3단계-B. 분석 봇 — 내 PC (Windows 11, RTX 5070 Ti)

```powershell
cd Documents\issue-radar
python -m venv .venv
.venv\Scripts\activate
pip install torch --index-url https://download.pytorch.org/whl/cu128   # 5070 Ti(Blackwell)는 CUDA 12.8 빌드 필요
pip install -r requirements.txt
ollama pull qwen3:14b                    # ollama.com에서 Ollama 설치 후 실행
copy .env.example .env                   # SLACK_BOT_TOKEN, SLACK_APP_TOKEN 입력
python -m radar.daily --check-feeds      # RSS 주소 점검 (ERR 항목은 feeds.json에서 수정·삭제)
python -m radar.daily                    # 브리핑을 콘솔에서 먼저 확인
python bot.py                            # 봇 실행 → Slack에서 @radar 도움말
```

PC가 켜질 때 봇이 자동으로 실행되게 하려면: 작업 스케줄러 → 기본 작업 만들기 → 트리거 "로그온할 때" → 프로그램 `.venv\Scripts\pythonw.exe`, 인수 `bot.py`, 시작 위치 `Documents\issue-radar`.

## 3단계-C. 아침 배치 (PC 자동 부팅 → 처리 → 자동 종료)

3단계-B의 설치(가상환경, torch, Ollama, `.env`)까지 마친 뒤 진행합니다. `bot.py`는 실행하지 않아도 됩니다.

**① 먼저 수동으로 테스트**
```powershell
.venv\Scripts\activate
python -m radar.morning --dry-run    # Slack에 쌓인 @radar 요청 목록만 확인
python -m radar.morning --no-end     # 전체 실행 (종료 없이). 걸린 시간은 data\morning.log 에 기록
```

**② 작업 스케줄러 등록** (관리자 PowerShell)
```powershell
cd $HOME\Documents\issue-radar
powershell -ExecutionPolicy Bypass -File deploy\windows\install_morning.ps1
Start-ScheduledTask -TaskName IssueRadarMorning   # 로그인한 상태라 PC는 꺼지지 않음 → 로그만 확인
```
스크립트가 하는 일: 빠른 시작 끄기, 절전 해제 타이머 허용, 업데이트 사용 시간 05~23시, 작업 등록(부팅 1분 후 + 매일 06:00, 로그인 없이 실행).

**③ BIOS 자동 전원 켜기 (매일 05:55)**: PC를 켤 때 `Del` 키로 BIOS에 들어가서 설정합니다.

| 메인보드 | 메뉴 위치 (버전마다 조금 다름) |
|---|---|
| ASUS | Advanced → APM Configuration → **Power On By RTC** → Enabled, 날짜 0(매일), 5:55:00 |
| MSI | Settings → Advanced → Wake Up Event Setup → **Resume By RTC Alarm** |
| GIGABYTE | Settings → Platform Power → **Resume by Alarm** |
| ASRock | Advanced → ACPI Configuration → **RTC Alarm Power On** |

- 같은 메뉴의 **ErP**(또는 EuP / Deep Sleep)는 반드시 **Disabled**로 두세요. 켜져 있으면 대기 전력이 끊겨 예약 부팅이 되지 않습니다.
- 멀티탭 스위치를 끄면 동작하지 않습니다.

**자동 종료 안전장치**: 아래 조건을 **모두** 만족할 때만 2분 예고 후 종료합니다.
- 시작 시각이 `WAKE_WINDOW`(기본 05:30~06:40) 안에 있음
- 부팅한 지 약 85분 이내
- 로그인한 사용자가 없음
- 프로젝트 폴더에 `NO_SHUTDOWN` 파일이 없음

아침에 PC를 쓸 날은 로그인만 하면 꺼지지 않습니다. 종료 예고가 떴을 때는 `shutdown /a`로 취소할 수 있습니다.

**BIOS 예약 부팅이 안 되는 보드라면**: `.env`에서 `END_ACTION=sleep`으로 바꾸세요. PC를 끄지 않고 절전 상태로 두면 작업 스케줄러가 06:00에 깨웁니다. 절전 전력은 약 2~5W, 월 1~3kWh 수준입니다.

**하루 일정 (기본값)**
```
05:55  BIOS 자동 부팅 → 05:57 작업 시작 (Ollama·모델 로딩 약 1~2분)
06:00  예측 채점 반영 → 밤사이 쌓인 @radar 요청 처리 (1건당 약 2분)
06:55  브리핑 생성 시작 (최신 뉴스 기준)
07:00  #daily-brief 로 브리핑 전송 → 2분 예고 후 종료
```
- **처리 용량**: 06:00~06:55 사이 약 **20건** (1건당 실측 약 2분, 예산 계산은 3분으로 여유 있게). 넘치면 스레드에 "내일 처리" 안내를 남깁니다.
- 요청이 적은 날은 처리를 끝낸 뒤 06:55까지 대기합니다. 대기 전력(약 60~80W)까지 포함해 월 전기료는 약 2천 원으로 추정합니다.
- 관련 `.env` 설정: `BRIEF_AT=07:00`(비우면 부팅 직후 전송), `MORNING_MAX_MINUTES=65`, `EST_ANALYZE_MIN=3`, `BRIEF_LEAD_MIN=5`.
- 설치 스크립트를 이전 버전으로 이미 실행했다면 **한 번 더 실행**하세요. 작업 제한 시간이 45분에서 90분으로 바뀌었습니다.

## 관심 키워드 (요청 없이 매일 자동)
- 등록된 키워드는 매일 아침 **지난 24시간 기사 수, 평소 대비 배율, 핵심 2~3줄**을 브리핑 맨 아래 "관심 키워드" 칸에 보여 줍니다.
- 다음 경우에는 **전체 분석**(1년 쟁점 + 예측)을 `#radar`에 따로 올립니다.
  - 처음 등록한 다음 날 아침
  - 매주 월요일 (`WATCH_FULL_WEEKDAY`)
  - 보도량이 평소의 2배 이상일 때 (`WATCH_SPIKE_RATIO`)
- "평소 수준"은 최근 14일 평균입니다. 등록 후 3일이 지나야 배율이 표시됩니다.
- Slack에서 관리합니다: `@radar 관심` (목록) · `@radar 관심 추가 반도체/semiconductor` · `@radar 관심 삭제 반도체`
- 최대 10개입니다. 1개당 매일 약 30초, 전체 분석이 도는 날은 2~4분이 걸립니다.

## PC가 꺼져 있을 때: 클라우드 대리 답변 (선택)
PC가 꺼져 있는 동안 남긴 `@radar 키워드` 요청에 **GitHub Actions가 30분마다 Claude(또는 OpenAI) 웹 검색으로 간이 분석**을 답합니다. 다음 날 아침에는 PC가 같은 스레드에 1년치 데이터 기반 전체 분석을 이어서 올립니다.

| | 클라우드 간이 분석 | PC 전체 분석 |
|---|---|---|
| 응답 | 요청 후 약 5~45분 (한국 시간 08~24시) | 다음 날 06~07시 |
| 근거 | 웹 검색 몇 번 | 기사 1년치 수천 건 + 쟁점 군집 |
| 예측 저장·채점 | X | O |
| 비용 | 1건당 약 100~150원 (Sonnet 5 + 검색 약 4회) | 전기료 |

설정 방법:
1. github.com에서 **Private** 저장소를 만들고 이 폴더를 올립니다(`.env`와 `data/`는 자동으로 제외됩니다).
2. `deploy/github/cloud_answer.yml`을 저장소의 **`.github/workflows/cloud_answer.yml`**로 복사합니다.
3. 저장소 → Settings → Secrets and variables → Actions → **Secrets**에 `SLACK_BOT_TOKEN`, `ANTHROPIC_API_KEY`를 넣습니다.
   - OpenAI를 쓰려면 Secret `OPENAI_API_KEY`를 넣고, **Variables**에 `CLOUD_PROVIDER=openai`, `CLOUD_OPENAI_MODEL=<웹 검색을 지원하는 모델명>`을 넣습니다.
4. Actions 탭 → cloud-answer → **Run workflow**로 테스트합니다.
5. console.anthropic.com에서 **월 사용 한도**를 꼭 설정하세요. 1회 실행당 최대 3건(`CLOUD_MAX_PER_RUN`)이라, 하루에 몰아서 요청해도 상한이 있습니다.
- 중복 방지: 클라우드가 답한 요청에는 ☁️ 반응이 달립니다. PC가 처리한 것(👀/✅)은 클라우드가 건드리지 않습니다. 스레드 후속 질문은 클라우드가 이미 답했으면 PC가 다시 답하지 않습니다.
- `브리핑`·`성적`·`비용`·`관심` 명령은 PC의 DB가 필요해서 PC 배치만 처리합니다.

## Slack 명령

```
@radar 삼성전자                        최근 이슈 → 1년 쟁점 → 예측 (스레드에 3단계로 답변)
@radar 삼성전자 / Samsung Electronics   해외 메이저 언론(Reuters·Bloomberg·FT 등 10곳) 포함
@radar 요즘 전세사기 어떻게 돼가?        문장으로 물어도 키워드를 자동 추출
@radar 노조 이슈 더 자세히               (분석 스레드 안에서) 수집 기사 근거로 답변 + 출처
@radar 관심 / 관심 추가 X / 관심 삭제 X      관심 키워드 관리
@radar 브리핑 / @radar 성적 / @radar 비용 / @radar 도움말
```
아침 배치(C) 방식: 요청이 접수되면 :eyes:, 완료되면 :white_check_mark: 반응이 달립니다. 채점 요청 메시지에는 ✅(발생) 또는 ❌(미발생) 반응을 달면 다음 날 아침에 반영됩니다.

## 알려진 한계
- **순위 = 보도 매체 수이지 조회수가 아닙니다.** 조회수를 제공하는 공개 API가 없습니다. 후보 30개는 구글 뉴스 '지난 1일' 검색으로 실제 보도 매체 수를 다시 셉니다.
- 언론사 RSS 주소는 예고 없이 바뀝니다. 브리핑 하단에 `피드 N/M 정상`이 표시되니, 이 숫자가 떨어지면 `--check-feeds`로 점검하세요.
- 키워드 분석의 1년치 수집은 Google News에서 쿼리당 100건까지만 가져옵니다. 기사가 많은 키워드는 `.env`에서 `SLICE_DAYS=7`로 줄이거나 빅카인즈 엑셀을 대시보드에서 업로드하세요.
- 서버 방식(A)은 쟁점 군집에 TF-IDF를 씁니다. 표현이 다른 같은 사건("반도체 수출 규제" vs "칩 수출 통제")을 한 쟁점으로 묶는 능력이 임베딩 방식보다 떨어집니다. 이름 붙이기와 요약은 Claude가 하므로 결과 품질 차이는 대부분 이 단계에서 생깁니다.
- API 사용액은 `@radar 비용`으로 확인하고, console.anthropic.com에서 **월 사용 한도(spend limit)**를 설정해 두세요.
- 아침 배치(C)는 PC 자동 부팅에 의존합니다. 정전, BIOS 초기화(CMOS 배터리 교체 포함), 윈도우 대형 업데이트가 있으면 그날은 건너뛸 수 있습니다. 브리핑만은 매일 꼭 받아야 한다면 GitHub Actions(2단계)를 함께 켜고 `.env`에서 `BRIEF_LOCAL=0`으로 두세요.
- 예측은 보도량 추이를 근거로 LLM이 추론한 것입니다. **채점이 30건 이상 쌓이기 전에는 확률을 믿지 마세요.** Brier 0.25는 동전 던지기 수준입니다.
