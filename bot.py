"""Issue Radar Slack 봇 (Socket Mode: 공개 URL/서버 불필요, 집 PC에서 실행)

실행: python bot.py
"""
import logging
import queue
import threading
import traceback

from apscheduler.schedulers.background import BackgroundScheduler
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

from radar import agent, config, daily, db
from radar.handlers import (collect_reaction_scores, mark_resolved_msg, post_due_predictions,
                            run_analyze, run_brief, say_thread)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("radar")

app = App(token=config.SLACK_BOT_TOKEN or "xoxb-missing", token_verification_enabled=bool(config.SLACK_BOT_TOKEN))
jobs: "queue.Queue[tuple]" = queue.Queue()  # GPU 한 장이므로 무거운 작업은 1개씩 순서대로


def worker():
    while True:
        fn, args, (client, channel, ts) = jobs.get()
        try:
            fn(client, channel, ts, *args)
        except Exception as e:
            log.error(traceback.format_exc())
            say_thread(client, channel, ts, f":x: 오류: `{type(e).__name__}: {e}`")
        finally:
            jobs.task_done()


# ---------------------------------------------------------------- 이벤트
@app.event("app_mention")
def on_mention(event, client):
    channel, text = event["channel"], event.get("text", "")
    thread_ts = event.get("thread_ts")
    ts = thread_ts or event["ts"]
    intent = agent.parse_intent(text, db.thread_query(thread_ts) if thread_ts else None)
    db.mark_processed(event["ts"], channel, "live")  # 아침 배치가 중복 처리하지 않도록
    log.info("mention: %s -> %s", text, intent)
    ctx = (client, channel, ts)

    if intent["intent"] == "help":
        say_thread(client, channel, ts, agent.HELP)
    elif intent["intent"] == "score":
        say_thread(client, channel, ts, agent.score_text())
    elif intent["intent"].startswith("watch_"):
        from radar.handlers import watch_reply
        say_thread(client, channel, ts, watch_reply(intent))
    elif intent["intent"] == "cost":
        say_thread(client, channel, ts, agent.cost_text())
    elif intent["intent"] == "brief":
        say_thread(client, channel, ts, f"브리핑 생성 중… (대기 작업 {jobs.qsize()}개)")
        jobs.put((lambda c, ch, t: run_brief(c, ch, t), (), ctx))
    elif intent["intent"] == "followup":
        jobs.put((lambda c, ch, t, q, qs: say_thread(c, ch, t, agent.followup(q, qs)),
                  (intent["query"], intent["question"]), ctx))
    else:
        q, en = intent["query"], intent.get("en", "")
        extra = f" + 해외(`{en}`)" if en else ""
        say_thread(client, channel, ts, f"*{q}*{extra} 분석을 시작합니다. 대기 작업 {jobs.qsize()}개 · "
                                        f"첫 수집은 2~4분 걸립니다.")
        jobs.put((run_analyze, (q, en), ctx))


@app.event("message")
def ignore_messages():  # 멘션이 아닌 일반 메시지는 무시 (Bolt 경고 방지)
    pass


# ---------------------------------------------------------------- 예측 채점 (버튼 또는 이모지 반응)
def _resolve(ack, body, client, outcome):
    ack()
    pid = int(body["actions"][0]["value"])
    db.resolve_prediction(pid, outcome, note="button")
    user = body["user"].get("username") or body["user"]["id"]
    mark_resolved_msg(client, body["channel"]["id"], body["message"]["ts"], outcome, user)


def daily_scoring():
    post_due_predictions(app.client, with_buttons=True)
    collect_reaction_scores(app.client, app.client.auth_test()["user_id"])


@app.action("resolve_yes")
def on_yes(ack, body, client):
    _resolve(ack, body, client, 1)


@app.action("resolve_no")
def on_no(ack, body, client):
    _resolve(ack, body, client, 0)


# ---------------------------------------------------------------- 시작
if __name__ == "__main__":
    if not (config.SLACK_BOT_TOKEN and config.SLACK_APP_TOKEN):
        raise SystemExit("SLACK_BOT_TOKEN / SLACK_APP_TOKEN을 .env에 설정하세요 (README 참고)")
    threading.Thread(target=worker, daemon=True).start()
    sched = BackgroundScheduler(timezone="Asia/Seoul")
    sched.add_job(daily_scoring, "cron", hour=9, minute=0)  # 매일 09:00 채점 요청 게시 + 반응 채점
    if config.env("BRIEF_LOCAL") == "1":  # GitHub Actions 대신 PC에서 아침 브리핑을 보낼 때
        from radar.slack_out import post
        sched.add_job(lambda: post(*daily.to_blocks(daily.build_brief()), channel=config.SLACK_BRIEF_CHANNEL),
                      "cron", hour=7, minute=0)
    sched.start()
    log.info("Issue Radar 봇 시작 (Socket Mode)")
    SocketModeHandler(app, config.SLACK_APP_TOKEN).start()
