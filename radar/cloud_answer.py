"""PC가 꺼져 있을 때의 대리 답변 (GitHub Actions에서 30분마다 실행).

Slack에 남은 '@radar 키워드' 요청 중 아직 아무도 손대지 않은 것을 찾아
Claude(또는 OpenAI)의 웹 검색으로 '간이 분석'을 바로 답한다.
1년치 수집·쟁점 군집·예측 저장이 들어간 '전체 분석'은 다음 날 아침 PC 배치가 같은 스레드에 이어서 올린다.

중복 방지는 DB 없이 Slack 반응으로 한다:
  ☁️(cloud) = 클라우드가 답함 · 👀/✅ = PC가 처리 중/완료 → 이런 반응이 있으면 건드리지 않는다.

  python -m radar.cloud_answer            # 실제 실행
  python -m radar.cloud_answer --dry-run  # 답할 대상만 출력
"""
import argparse
import os
import re
import time
from datetime import datetime, timedelta

import requests

from .slack_out import client as slack_client


def env(k, d=""):
    return os.getenv(k, d).strip()


PROVIDER = env("CLOUD_PROVIDER", "anthropic")                  # anthropic | openai
ANTHROPIC_MODEL = env("CLOUD_ANTHROPIC_MODEL", "claude-sonnet-5")
OPENAI_MODEL = env("CLOUD_OPENAI_MODEL", env("OPENAI_MODEL", ""))
LOOKBACK_H = float(env("CLOUD_LOOKBACK_HOURS", "12"))          # 이 시간 안의 요청만 대상
MIN_AGE_MIN = float(env("CLOUD_MIN_AGE_MIN", "3"))             # 방금 올린 요청은 PC 봇에 양보
MAX_PER_RUN = int(env("CLOUD_MAX_PER_RUN", "3"))               # 1회 실행당 최대 답변 수 (비용 상한)
MAX_SEARCHES = int(env("CLOUD_MAX_SEARCHES", "4"))              # 답변 1건당 웹 검색 횟수 상한
HANDLED = {"cloud", "eyes", "white_check_mark", "x", "hourglass"}

# PC의 DB가 필요한 명령은 클라우드가 건드리지 않고 PC 배치에 맡긴다
PC_ONLY = re.compile(r"^(도움말|help|\?|(오늘\s*)?(아침\s*)?(브리핑|뉴스)(\s*보여줘)?|(예측\s*)?(성적|점수|brier)"
                     r"|(api\s*)?(비용|사용량|요금)|관심.*)$", re.I)

SYSTEM = """너는 한국어 뉴스·산업 애널리스트다. 웹 검색으로 최신 기사를 찾아 근거로 삼고, 근거 없는 사실은 쓰지 마라.
아래 Slack 형식(mrkdwn)으로 2,500자 이내로 답하라. 굵게는 *별표 하나*, 목록은 • 로 쓴다.

*지금 핵심* 한 문장
*최근 1주 동향*
• 3~5개, 각 1문장, 날짜 포함
*지난 1년 주요 쟁점*
• 3~5개, 쟁점명 — 언제 무슨 일이 있었고 지금 상태(부상/지속/소멸)
*향후 전망* (확률은 추정치)
• 1개월: 판정 가능한 구체적 사건 한 줄 `확률%` — 근거
• 3개월 / 6개월 / 1년: 같은 형식
확률은 5~95% 사이로, 기간이 길수록 불확실성을 반영하고 과신하지 마라."""


# ---------------------------------------------------------------- 대상 찾기
def _bot_reacted(m: dict, bot_uid: str) -> bool:
    return any(r["name"] in HANDLED and bot_uid in r.get("users", []) for r in m.get("reactions", []))


def find_pending(client, bot_uid: str) -> list[dict]:
    now = time.time()
    oldest = f"{now - LOOKBACK_H * 3600:.6f}"
    tag = f"<@{bot_uid}>"
    out = []
    chans = client.users_conversations(types="public_channel,private_channel", exclude_archived=True,
                                       limit=200)["channels"]
    for ch in chans:
        hist = client.conversations_history(channel=ch["id"], oldest=oldest, limit=200)["messages"]
        for m in hist:
            cands = [(m, None)]
            if m.get("reply_count"):
                rep = client.conversations_replies(channel=ch["id"], ts=m["ts"], oldest=oldest, limit=200)["messages"]
                cands += [(x, m) for x in rep if x["ts"] != m["ts"]]
            for x, parent in cands:
                if x.get("bot_id") or x.get("user") == bot_uid or tag not in x.get("text", ""):
                    continue
                if now - float(x["ts"]) < MIN_AGE_MIN * 60 or _bot_reacted(x, bot_uid):
                    continue
                text = re.sub(r"<@[A-Z0-9]+>", "", x["text"]).strip()
                if not text or PC_ONLY.match(text):
                    continue
                out.append(dict(channel=ch["id"], ts=x["ts"], thread_ts=parent["ts"] if parent else None,
                                text=text, parent=re.sub(r"<@[A-Z0-9]+>", "", parent["text"]).strip() if parent else ""))
    return sorted(out, key=lambda x: float(x["ts"]))[:MAX_PER_RUN]


# ---------------------------------------------------------------- LLM + 웹 검색
def _question(it: dict) -> str:
    today = datetime.now().strftime("%Y-%m-%d")
    if it["parent"]:
        return f"오늘은 {today}. 주제 '{it['parent']}' 에 대한 후속 질문: {it['text']}\n웹 검색으로 확인해 5문장 이내로 답하라."
    return f"오늘은 {today}. 분석 대상: {it['text']}\n위 형식대로 분석하라. 문장형 질문이면 핵심 주제를 먼저 파악하라."


def ask_anthropic(q: str) -> tuple[str, list[tuple[str, str]]]:
    import anthropic
    c = anthropic.Anthropic(api_key=env("ANTHROPIC_API_KEY"), max_retries=3)
    msgs = [{"role": "user", "content": q}]
    tools = [{"type": "web_search_20250305", "name": "web_search", "max_uses": MAX_SEARCHES}]
    text, cites = "", {}
    for _ in range(4):  # 서버 도구가 긴 작업을 나눠 돌려주는 경우(pause_turn) 이어서 호출
        r = c.messages.create(model=ANTHROPIC_MODEL, max_tokens=2500, system=SYSTEM, messages=msgs, tools=tools)
        for b in r.content:
            if b.type == "text":
                text += b.text
                for ct in getattr(b, "citations", None) or []:
                    url = getattr(ct, "url", None)
                    if url:
                        cites[url] = getattr(ct, "title", "") or url
        if r.stop_reason != "pause_turn":
            break
        msgs.append({"role": "assistant", "content": r.content})
    return text.strip(), list(cites.items())


def ask_openai(q: str) -> tuple[str, list[tuple[str, str]]]:
    if not OPENAI_MODEL:
        raise RuntimeError("CLOUD_OPENAI_MODEL(또는 OPENAI_MODEL)을 설정하세요")
    r = requests.post("https://api.openai.com/v1/responses", timeout=180,
                      headers={"Authorization": f"Bearer {env('OPENAI_API_KEY')}"},
                      json={"model": OPENAI_MODEL, "instructions": SYSTEM, "input": q,
                            "tools": [{"type": "web_search"}]})
    r.raise_for_status()
    text, cites = "", {}
    for item in r.json().get("output", []):
        if item.get("type") != "message":
            continue
        for part in item.get("content", []):
            if part.get("type") == "output_text":
                text += part.get("text", "")
                for a in part.get("annotations", []):
                    if a.get("type") == "url_citation" and a.get("url"):
                        cites[a["url"]] = a.get("title") or a["url"]
    return text.strip(), list(cites.items())


def answer(it: dict) -> str:
    q = _question(it)
    text, cites = ask_openai(q) if PROVIDER == "openai" else ask_anthropic(q)
    name = "OpenAI" if PROVIDER == "openai" else "Claude"
    head = (f"_:cloud: PC가 꺼져 있어 {name} 웹 검색으로 간이 분석했습니다."
            + ("" if it["parent"] else " 1년치 데이터 기반 전체 분석은 다음 날 아침 PC 배치가 이 스레드에 이어서 올립니다.")
            + "_\n\n")
    src = "\n".join(f"[{i}] <{u}|{t[:50]}>" for i, (u, t) in enumerate(cites[:5], 1))
    return head + text + (f"\n\n*출처*\n{src}" if src else "")


# ---------------------------------------------------------------- 메인
def _post(client, channel, thread_ts, text):
    chunks, cur = [], ""
    for line in text.split("\n"):
        if len(cur) + len(line) > 2900:
            chunks.append(cur)
            cur = ""
        cur += line + "\n"
    chunks.append(cur)
    client.chat_postMessage(channel=channel, thread_ts=thread_ts, text=text[:3000], unfurl_links=False,
                            blocks=[{"type": "section", "text": {"type": "mrkdwn", "text": c}} for c in chunks if c.strip()])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    client = slack_client()
    bot_uid = client.auth_test()["user_id"]
    items = find_pending(client, bot_uid)
    print(f"{datetime.now():%H:%M} 미처리 요청 {len(items)}건")
    for it in items:
        print(" -", it["text"][:60], "(스레드)" if it["thread_ts"] else "")
        if a.dry_run:
            continue
        root = it["thread_ts"] or it["ts"]
        try:
            client.reactions_add(channel=it["channel"], timestamp=it["ts"], name="cloud")  # 먼저 선점
        except Exception:
            pass
        try:
            _post(client, it["channel"], root, answer(it))
        except Exception as e:
            print("   실패:", type(e).__name__, e)
            try:  # 반응을 되돌려 다음 실행에서 다시 시도
                client.reactions_remove(channel=it["channel"], timestamp=it["ts"], name="cloud")
            except Exception:
                pass


if __name__ == "__main__":
    main()
