"""Slack 요청 처리기. 상시 봇(bot.py)과 아침 배치(morning.py)가 함께 쓴다."""
from datetime import date

from . import agent, config, daily, db, watch
from .slack_out import md_blocks

YES = {"white_check_mark", "heavy_check_mark", "o", "+1"}
NO = {"x", "heavy_multiplication_x", "negative_squared_cross_mark", "-1"}


def say_thread(client, channel, thread_ts, text):
    client.chat_postMessage(channel=channel, thread_ts=thread_ts, text=text[:3000],
                            blocks=md_blocks(text), unfurl_links=False)


def run_analyze(client, channel, ts, query, en):
    db.save_thread(ts, channel, query)
    say_thread(client, channel, ts, agent.step_collect(query, en))
    say_thread(client, channel, ts, agent.step_recent(query))
    text, res, brief = agent.step_topics(query)
    say_thread(client, channel, ts, text)
    say_thread(client, channel, ts, agent.step_forecast(query, res, brief))
    say_thread(client, channel, ts, "_이 스레드에서 `@radar 질문` 으로 후속 질문을 할 수 있습니다._")


def run_brief(client, channel, thread_ts=None):
    secs = [s for s in config.env("BRIEF_SECTIONS").split(",") if s.strip()] or None
    blocks, title = daily.to_blocks(daily.build_brief(secs))
    client.chat_postMessage(channel=channel, thread_ts=thread_ts, blocks=blocks[:50], text=title, unfurl_links=False)


def handle(intent: dict, client, channel: str, ts: str):
    """의도 하나를 동기적으로 처리 (배치용). 상시 봇은 무거운 작업을 큐에 넣는다."""
    kind = intent["intent"]
    if kind == "help":
        say_thread(client, channel, ts, agent.HELP)
    elif kind == "score":
        say_thread(client, channel, ts, agent.score_text())
    elif kind == "cost":
        say_thread(client, channel, ts, agent.cost_text())
    elif kind == "brief":
        run_brief(client, channel, ts)
    elif kind.startswith("watch_"):
        say_thread(client, channel, ts, watch_reply(intent))
    elif kind == "followup":
        say_thread(client, channel, ts, agent.followup(intent["query"], intent["question"]))
    else:
        run_analyze(client, channel, ts, intent["query"], intent.get("en", ""))


def watch_reply(intent: dict) -> str:
    if intent["intent"] == "watch_add":
        return watch.add(intent["spec"])
    if intent["intent"] == "watch_remove":
        return watch.remove(intent["q"])
    return watch.list_text()


def post_due_predictions(client, with_buttons: bool):
    """기한이 된 예측을 한 번만 게시. 배치 모드에서는 버튼 대신 이모지 반응으로 채점."""
    preds = db.load_predictions()
    if preds.empty:
        return 0
    due = preds[preds["outcome"].isna() & (preds["due_date"] <= date.today().isoformat()) & preds["slack_ts"].isna()]
    for p in due.to_dict("records"):
        how = "아래 버튼 또는 " if with_buttons else ""
        blocks = [{"type": "section", "text": {"type": "mrkdwn", "text":
                   f"*예측 채점 요청* · `{p['query']}` · {p['horizon']} · 작성 {p['created_at'][:10]}\n"
                   f">{p['statement']}\n예측 확률 *{p['probability']:.0%}* · 기한 {p['due_date']}\n"
                   f"_{how}이 메시지에 :white_check_mark: (발생) / :x: (미발생) 반응을 달아 주세요._"}}]
        if with_buttons:
            blocks.append({"type": "actions", "elements": [
                {"type": "button", "text": {"type": "plain_text", "text": "발생"}, "style": "primary",
                 "action_id": "resolve_yes", "value": str(p["id"])},
                {"type": "button", "text": {"type": "plain_text", "text": "미발생"}, "style": "danger",
                 "action_id": "resolve_no", "value": str(p["id"])}]})
        r = client.chat_postMessage(channel=config.SLACK_ALERT_CHANNEL, text=f"예측 채점: {p['statement']}",
                                    blocks=blocks)
        db.set_prediction_msg(int(p["id"]), r["channel"], r["ts"])
    return len(due)


def mark_resolved_msg(client, channel, ts, outcome: int, by: str):
    client.chat_update(channel=channel, ts=ts, text="채점 완료", blocks=[
        {"type": "section", "text": {"type": "mrkdwn", "text": f"~채점 요청~ → *{'발생' if outcome else '미발생'}*으로 채점됨 (by {by})"}},
        {"type": "context", "elements": [{"type": "mrkdwn", "text": agent.score_text().splitlines()[0]}]}])


def collect_reaction_scores(client, bot_uid: str) -> int:
    """게시된 채점 요청에 달린 ✅/❌ 반응을 읽어 채점한다."""
    preds = db.load_predictions()
    if preds.empty:
        return 0
    n = 0
    for p in preds[preds["outcome"].isna() & preds["slack_ts"].notna()].to_dict("records"):
        try:
            msg = client.reactions_get(channel=p["slack_channel"], timestamp=p["slack_ts"])["message"]
        except Exception:
            continue
        votes = {r["name"] for r in msg.get("reactions", []) if set(r.get("users", [])) - {bot_uid}}
        yes, no = bool(votes & YES), bool(votes & NO)
        if yes != no:  # 둘 다 있거나 없으면 보류
            outcome = 1 if yes else 0
            db.resolve_prediction(int(p["id"]), outcome, note="reaction")
            mark_resolved_msg(client, p["slack_channel"], p["slack_ts"], outcome, "반응")
            n += 1
    return n
