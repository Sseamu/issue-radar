"""아침 배치: PC 자동 부팅 → 브리핑 → 밀린 요청 처리 → 채점 수집 → 자동 종료.

PC가 꺼져 있는 동안 Slack에 남긴 `@radar ...` 요청을 다음 날 아침에 몰아서 처리한다.
Windows 작업 스케줄러가 '부팅 시' + '매일 06:00'에 실행한다 (deploy/windows/install_morning.ps1).

  python -m radar.morning             # 실제 실행 (조건이 맞으면 끝나고 PC 종료)
  python -m radar.morning --no-end    # 종료 없이 실행 (테스트용)
  python -m radar.morning --dry-run   # Slack에서 밀린 요청만 조회해서 출력
  python -m radar.morning --no-end --retry-errors   # 실패했던 요청 재시도
"""
import argparse
import logging
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import requests

from . import agent, config, daily, db, handlers, watch
from .slack_out import client as slack_client

LOG_PATH = config.DB_PATH.parent / "morning.log"
LOCK_PATH = config.DB_PATH.parent / "morning.lock"
NO_END_FLAG = config.ROOT / "NO_SHUTDOWN"          # 이 파일이 있으면 절대 끄지 않음

MAX_MINUTES = float(config.env("MORNING_MAX_MINUTES", "65"))   # 전체 작업 시간 예산 (06:00~07:05)
EST_ANALYZE_MIN = float(config.env("EST_ANALYZE_MIN", "3"))    # 키워드 분석 1건 예상 소요 (실측 약 2분)
BRIEF_AT = config.env("BRIEF_AT", "")                          # 비우면: 준비되는 즉시 전송 (기본) / "07:00" 처럼 적으면 그 시각에 전송
BRIEF_LEAD_MIN = float(config.env("BRIEF_LEAD_MIN", "5"))      # 브리핑 생성에 걸리는 시간 (실측 약 2분)
# 1: 할 일이 끝나는 대로 브리핑을 만들어 Slack '예약 전송'(BRIEF_AT)으로 걸어 두고 PC는 바로 종료
# 0: 전송 직전(BRIEF_AT - BRIEF_LEAD_MIN)까지 기다렸다가 최신 뉴스로 만들어 정각에 직접 전송
BRIEF_SCHEDULE = config.env("BRIEF_SCHEDULE", "1") == "1"
# 이 시각 이전에 켜진 실행(예: 자정에 PC를 켠 경우)은 브리핑·관심 키워드를 건너뛰고 아침 실행에 맡긴다
BRIEF_EARLIEST = config.env("BRIEF_EARLIEST", "05:00")
END_ACTION = config.env("END_ACTION", "shutdown")              # shutdown | sleep | none
WAKE_WINDOW = config.env("WAKE_WINDOW", "05:30-06:40")           # 이 시간대에 시작했을 때만 자동 종료
SCAN_DAYS = int(config.env("SCAN_DAYS", "7"))                   # 밀린 요청을 찾아볼 기간
NOT_BEFORE = config.env("MORNING_NOT_BEFORE")                   # 테스트용: 이 시각 전에는 실행하지 않음 (HH:MM)

log = logging.getLogger("morning")


def setup_logging():
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        handlers=[logging.FileHandler(LOG_PATH, encoding="utf-8"), logging.StreamHandler()])
    for noisy in ("httpx", "httpcore", "urllib3", "huggingface_hub", "sentence_transformers"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


# ---------------------------------------------------------------- 준비
def wait_network(timeout: int = 180) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        try:
            requests.get("https://slack.com/api/api.test", timeout=5)
            return True
        except Exception:
            time.sleep(5)
    return False


def ensure_ollama(timeout: int = 180) -> bool:
    """로그인 없이 부팅된 상태라 Ollama 앱이 안 떠 있을 수 있다 → 직접 serve 실행 후 모델 예열."""
    if config.LLM_PROVIDER != "ollama":
        return True
    url = config.OLLAMA_URL

    def alive():
        try:
            return requests.get(f"{url}/api/tags", timeout=3).ok
        except Exception:
            return False

    if not alive():
        exe = os.path.expandvars(config.env("OLLAMA_EXE", r"%LOCALAPPDATA%\Programs\Ollama\ollama.exe"))
        exe = exe if Path(exe).exists() else "ollama"
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        log.info("Ollama 시작: %s serve", exe)
        subprocess.Popen([exe, "serve"], creationflags=flags, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        end = time.time() + timeout
        while not alive():
            if time.time() > end:
                return False
            time.sleep(3)
    try:  # 모델을 GPU에 미리 올려 둠 (첫 호출 지연 제거)
        requests.post(f"{url}/api/generate", json={"model": config.OLLAMA_MODEL, "prompt": "", "keep_alive": "40m"},
                      timeout=180)
    except Exception as e:
        log.warning("모델 예열 실패: %s", e)
    return True


# ---------------------------------------------------------------- 밀린 요청 찾기
def scan_mentions(client, bot_uid: str, days: int = SCAN_DAYS) -> list[dict]:
    """봇이 들어가 있는 채널에서, 아직 처리하지 않은 @radar 멘션(스레드 답글 포함)을 오래된 순으로 반환."""
    oldest = f"{(datetime.now() - timedelta(days=days)).timestamp():.6f}"
    tag = f"<@{bot_uid}>"
    out = []
    chans = client.users_conversations(types="public_channel,private_channel", exclude_archived=True,
                                       limit=200)["channels"]
    for ch in chans:
        cid, cursor = ch["id"], None
        while True:
            r = client.conversations_history(channel=cid, oldest=oldest, limit=200, cursor=cursor)
            for m in r["messages"]:
                if m.get("user") == bot_uid or m.get("bot_id"):
                    continue
                if tag in m.get("text", "") and not db.is_processed(m["ts"]):
                    out.append(dict(channel=cid, ts=m["ts"], thread_ts=None, text=m["text"], user=m.get("user")))
                if m.get("reply_count") and float(m.get("latest_reply", 0)) > float(oldest):
                    rr = client.conversations_replies(channel=cid, ts=m["ts"], oldest=oldest, limit=200)
                    for x in rr["messages"]:
                        if x["ts"] == m["ts"] or x.get("user") == bot_uid or x.get("bot_id"):
                            continue
                        cloud_done = any(r["name"] == "cloud" and bot_uid in r.get("users", [])
                                         for r in x.get("reactions", []))   # 후속 질문은 클라우드가 이미 답했으면 생략
                        if tag in x.get("text", "") and not db.is_processed(x["ts"]) and not cloud_done:
                            out.append(dict(channel=cid, ts=x["ts"], thread_ts=m["ts"], text=x["text"],
                                            user=x.get("user")))
            cursor = (r.get("response_metadata") or {}).get("next_cursor")
            if not cursor:
                break
    return sorted(out, key=lambda x: float(x["ts"]))


def _react(client, channel, ts, name):
    try:
        client.reactions_add(channel=channel, timestamp=ts, name=name)
    except Exception:
        pass  # 이미 달려 있음 등


def process_queue(client, bot_uid: str, deadline: float) -> tuple[int, int]:
    items = scan_mentions(client, bot_uid)
    log.info("밀린 요청 %d건", len(items))
    done = 0
    for i, it in enumerate(items):
        root = it["thread_ts"] or it["ts"]
        intent = agent.parse_intent(it["text"], db.thread_query(it["thread_ts"]) if it["thread_ts"] else None)
        need = EST_ANALYZE_MIN if intent["intent"] in ("analyze", "brief") else 1
        if time.time() + need * 60 > deadline:
            left = len(items) - i
            log.info("시간 예산 소진 → %d건 내일로 이월", left)
            handlers.say_thread(client, it["channel"], root,
                                f"_:hourglass: 오늘 아침 처리 시간이 부족해 내일 아침에 처리합니다 (남은 요청 {left}건)._")
            return done, left
        _react(client, it["channel"], it["ts"], "eyes")
        asked = datetime.fromtimestamp(float(it["ts"])).strftime("%m/%d %H:%M")
        if intent["intent"] == "analyze":
            handlers.say_thread(client, it["channel"], root,
                                f"_{asked} 요청 → 아침 배치에서 처리합니다: *{intent['query']}*_")
        try:
            handlers.handle(intent, client, it["channel"], root)
            db.mark_processed(it["ts"], it["channel"], "done")
            _react(client, it["channel"], it["ts"], "white_check_mark")
            done += 1
        except Exception as e:
            log.exception("처리 실패: %s", it["text"])
            handlers.say_thread(client, it["channel"], root, f":x: 오류: `{type(e).__name__}: {e}`")
            db.mark_processed(it["ts"], it["channel"], f"error: {e}"[:200])
    return done, 0


# ---------------------------------------------------------------- 종료
def _uptime_min() -> float | None:
    try:
        import ctypes
        return ctypes.windll.kernel32.GetTickCount64() / 60000
    except Exception:
        return None


def _someone_logged_in() -> bool:
    """explorer.exe가 떠 있으면 누군가 로그인해 PC를 쓰는 중으로 본다."""
    try:
        out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq explorer.exe", "/NH"], capture_output=True,
                             text=True, timeout=10).stdout
        return "explorer.exe" in out.lower()
    except Exception:
        return True  # 판단 불가 → 안전하게 '사용 중'으로


def _in_window(t: datetime) -> bool:
    a, b = WAKE_WINDOW.split("-")
    return a <= t.strftime("%H:%M") <= b


def end_decision(started: datetime) -> tuple[str, str]:
    if END_ACTION == "none":
        return "none", "END_ACTION=none"
    if sys.platform != "win32":
        return "none", "Windows가 아님"
    if NO_END_FLAG.exists():
        return "none", "NO_SHUTDOWN 파일 존재"
    if not _in_window(started):
        return "none", f"시작 시각 {started:%H:%M}이 자동 종료 시간대({WAKE_WINDOW}) 밖 → 사용자가 켠 것으로 판단"
    if END_ACTION == "shutdown":
        up = _uptime_min()
        if up is not None and up > MAX_MINUTES + 20:
            return "none", f"부팅 후 {up:.0f}분 경과 → 자동 부팅이 아님"
    if _someone_logged_in():
        return "none", "로그인한 사용자가 있음"
    return END_ACTION, "조건 충족"


def do_end(action: str):
    if action == "shutdown":
        subprocess.run(["shutdown", "/s", "/t", "120", "/c",
                        "Issue Radar 아침 작업 완료. 2분 뒤 종료합니다. 취소: 명령 프롬프트에서 shutdown /a"])
    elif action == "sleep":
        subprocess.run(["powershell", "-NoProfile", "-Command",
                        "Add-Type -Assembly System.Windows.Forms;"
                        "[System.Windows.Forms.Application]::SetSuspendState('Suspend', $false, $false)"])


# ---------------------------------------------------------------- 관심 키워드
def run_watch(client, deadline: float, now: datetime) -> list[dict]:
    results = []
    for kw in watch.get_list():
        if time.time() + 60 > deadline:
            log.info("관심 키워드: 시간 부족으로 %s 이후 생략", kw["q"])
            break
        try:
            results.append(watch.daily_update(kw))
        except Exception as e:
            log.exception("관심 키워드 동향 실패: %s", kw["q"])
    for r in results:  # 전체 분석은 필요한 키워드만, 남은 시간 안에서
        reason = watch.full_reason(r, now)
        if not reason:
            continue
        if time.time() + EST_ANALYZE_MIN * 60 > deadline:
            log.info("관심 키워드 전체 분석 생략(시간 부족): %s", r["q"])
            continue
        try:
            head = client.chat_postMessage(channel=config.SLACK_ALERT_CHANNEL,
                                           text=f"*[관심 키워드 분석] {r['label']}* · {reason} · 최근 이슈는 {watch.WATCH_DAYS}일 기준")
            handlers.run_analyze(client, head["channel"], head["ts"], r["key"], r.get("en", ""),
                                 ko_query=r.get("ko_query"), foreign_sites=r.get("sites"),
                                 recent_days=watch.WATCH_DAYS)
            r["full"] = reason
        except Exception as e:
            log.exception("관심 키워드 전체 분석 실패: %s", r["q"])
            try:
                handlers.say_thread(client, head["channel"], head["ts"], f":x: 분석 실패: `{type(e).__name__}: {e}`")
            except Exception:
                pass
    return results


# ---------------------------------------------------------------- 브리핑 시각
def _brief_time(started: datetime) -> datetime | None:
    """오늘 BRIEF_AT 시각. 이미 지났거나 설정이 비어 있으면 None(= 바로 전송)."""
    if not BRIEF_AT:
        return None
    h, m = map(int, BRIEF_AT.split(":"))
    t = started.replace(hour=h, minute=m, second=0, microsecond=0)
    return t if t > started else None


def _sleep_until(ts: float | None, what: str):
    if ts is None:
        return
    wait = ts - time.time()
    if wait > 0:
        log.info("%s까지 %.0f분 대기", what, wait / 60)
        time.sleep(wait)


def _sections():
    return [s.strip() for s in config.env("BRIEF_SECTIONS").split(",") if s.strip()] or None


# ---------------------------------------------------------------- 메인
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-end", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--retry-errors", action="store_true", help="실패했던 요청을 다시 처리 대상으로")
    a = ap.parse_args()
    setup_logging()
    if a.retry_errors:
        log.info("실패 요청 %d건을 다시 처리 대상으로 되돌림", db.clear_failed())
    started = datetime.now()
    if NOT_BEFORE and started.strftime("%H:%M") < NOT_BEFORE and not (a.no_end or a.dry_run):
        log.info("MORNING_NOT_BEFORE=%s 이전이라 실행하지 않음 (테스트 대기 중)", NOT_BEFORE)
        return
    deadline = time.time() + MAX_MINUTES * 60

    # 중복 실행 방지 (부팅 트리거 + 06:00 트리거가 겹칠 때)
    if LOCK_PATH.exists() and time.time() - LOCK_PATH.stat().st_mtime < MAX_MINUTES * 60 + 600:
        log.info("이미 실행 중 → 종료")
        return
    LOCK_PATH.write_text(str(os.getpid()))
    summary = []
    try:
        if not wait_network():
            log.error("네트워크 연결 실패")
            summary.append("네트워크 실패")
            return
        client = slack_client()
        bot_uid = client.auth_test()["user_id"]
        if a.dry_run:
            for it in scan_mentions(client, bot_uid):
                print(datetime.fromtimestamp(float(it["ts"])), it["channel"], it["text"][:80])
            return
        if not ensure_ollama():
            log.error("Ollama를 시작하지 못했습니다")
            client.chat_postMessage(channel=config.SLACK_ALERT_CHANNEL,
                                    text=":x: 아침 배치: 로컬 LLM(Ollama)을 시작하지 못했습니다. morning.log 확인 필요")
            return

        today = started.date().isoformat()
        want_brief = (config.env("BRIEF_LOCAL", "1") == "1" and db.kv_get("brief_date") != today
                      and started.strftime("%H:%M") >= BRIEF_EARLIEST)
        brief_at = _brief_time(started) if want_brief else None
        # 브리핑 생성 시작 시각 = 전송 시각 - 생성 소요 시간. 그 전까지 밀린 요청을 처리한다
        queue_deadline = deadline
        if brief_at:
            queue_deadline = min(deadline, brief_at.timestamp() - BRIEF_LEAD_MIN * 60)

        # 1) 어제 달린 채점 반응 반영 + 새 채점 요청 게시
        scored = handlers.collect_reaction_scores(client, bot_uid)
        posted = handlers.post_due_predictions(client, with_buttons=False)
        summary.append(f"채점 반영 {scored}건 · 채점 요청 {posted}건")

        # 2) 관심 키워드: 24시간 동향 + (첫 분석·정기·급증 시) 전체 분석
        watch_results = run_watch(client, queue_deadline, started) if want_brief else []   # 하루 한 번
        if watch_results:
            summary.append(f"관심 키워드 {len(watch_results)}개")

        def send_brief():
            """브리핑 생성·전송. BRIEF_AT 이 없으면 즉시, 있으면 예약 전송(또는 대기 후 전송)."""
            try:
                if brief_at and not BRIEF_SCHEDULE:
                    _sleep_until(brief_at.timestamp() - BRIEF_LEAD_MIN * 60, "브리핑 생성")
                blocks, title = daily.to_blocks(daily.build_brief(_sections()))
                blocks = blocks[:-1] + watch.blocks(watch_results) + blocks[-1:]   # 하단 요약줄 앞에 삽입
                if brief_at and BRIEF_SCHEDULE and brief_at.timestamp() - time.time() > 90:
                    # Slack 예약 전송: PC가 꺼져도 Slack 서버가 BRIEF_AT 에 보낸다
                    client.chat_scheduleMessage(channel=config.SLACK_BRIEF_CHANNEL, blocks=blocks[:50], text=title,
                                                post_at=int(brief_at.timestamp()), unfurl_links=False)
                    summary.append(f"브리핑 {datetime.now():%H:%M} 생성 → {brief_at:%H:%M} 예약 전송")
                else:
                    if brief_at:
                        _sleep_until(brief_at.timestamp(), "브리핑 전송")
                    client.chat_postMessage(channel=config.SLACK_BRIEF_CHANNEL, blocks=blocks[:50], text=title,
                                            unfurl_links=False)
                    summary.append(f"브리핑 전송 {datetime.now():%H:%M}")
                db.kv_set("brief_date", today)
            except Exception as e:
                log.exception("브리핑 실패")
                summary.append(f"브리핑 실패({type(e).__name__})")

        # 3) 즉시 전송 모드(BRIEF_AT 비움): 브리핑을 먼저 보내고 나서 밀린 요청 처리
        if want_brief and not brief_at:
            send_brief()

        # 4) 밀린 요청 처리
        done, left = process_queue(client, bot_uid, queue_deadline)
        summary.append(f"요청 처리 {done}건" + (f" · 이월 {left}건" if left else ""))

        # 5) 시각 지정 모드: 요청 처리 후 브리핑 (예약 전송 또는 대기 후 전송)
        if want_brief and brief_at:
            send_brief()
    except Exception:
        log.exception("아침 배치 오류")
        summary.append("오류 발생(morning.log 확인)")
    finally:
        LOCK_PATH.unlink(missing_ok=True)
        elapsed = (datetime.now() - started).total_seconds() / 60
        action, why = ("none", "--no-end") if (a.no_end or a.dry_run) else end_decision(started)
        log.info("완료 %.1f분 | %s | 종료: %s (%s)", elapsed, " / ".join(summary), action, why)
        if action != "none":
            do_end(action)


if __name__ == "__main__":
    main()
