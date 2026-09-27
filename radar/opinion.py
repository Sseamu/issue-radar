"""오늘의 논점: 사설·칼럼에서 외교·국제 쟁점 하나를 골라 여러 매체의 논지를 나란히 비교한다 (논술·면접 대비).

- 피드: feeds_<profile>.json 의 _opinion.feeds (국내 사설 + 해외 오피니언)
- 선정: 외교 핵심 단어가 들어간 사설만 → LLM이 여러 매체가 함께 다룬 쟁점 하나와 서로 다른 시각의 글 3~4편을 고른다
- 한계: RSS에는 제목과 도입부만 있어 논지 요약은 도입부 기준이다. 전문은 링크로 읽는다.

실행: python -m radar.opinion --profile diplomacy [--post THREAD_TS]
"""
import argparse
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

from . import config, daily

_PREFIX = re.compile(r"^\s*[\[【(<]\s*(사설|칼럼|시론|기고|오피니언|논단|포럼)[^\]】)>]*[\]】)>]\s*")


def settings(profile: str) -> dict | None:
    raw = daily._raw_feeds(profile)
    return raw.get("_opinion")


def collect(profile: str, now: datetime | None = None) -> tuple[list[dict], dict]:
    op = settings(profile) or {}
    cfg = daily.load_brief_config(profile)
    now = now or datetime.now(daily.KST)
    since = now - timedelta(hours=int(op.get("hours", 36)))
    feeds = op.get("feeds", [])
    items, status = [], {}
    with ThreadPoolExecutor(6) as ex:
        futs = [ex.submit(daily.fetch_feed, f, "사설·칼럼") for f in feeds]
        for f, fut in zip(feeds, futs):
            try:
                got = fut.result()
                if f.get("type") == "google" and cfg["trusted"]:
                    got = [i for i in got if daily._trusted(i["outlet"], cfg["trusted"])]
                got = [i for i in got if i["published"] is None or i["published"] >= since]
                status[f["name"]] = len(got)
                items += got
            except Exception as e:
                status[f["name"]] = f"실패: {type(e).__name__}"
    words = [w.lower() for w in op.get("require_words") or cfg["require_words"]]
    cap = int(op.get("max_per_outlet", 6))
    out, seen, per = [], set(), {}
    items.sort(key=lambda i: i["published"] or now, reverse=True)
    for i in items:
        i["title"] = _PREFIX.sub("", i["title"]).strip()
        key = re.sub(r"\W", "", i["title"].lower())[:40]
        if not i["title"] or key in seen:
            continue
        # 제목 기준으로만 판단: 도입부의 '대통령'·'정부' 같은 말 때문에 국내 사설이 섞이는 것을 막는다
        if words and not daily._has(i["title"].lower(), words):
            continue
        if per.get(i["outlet"], 0) >= cap:  # 한 매체가 목록을 독차지하지 않게
            continue
        per[i["outlet"]] = per.get(i["outlet"], 0) + 1
        seen.add(key)
        out.append(i)
    return out, status


SYS = ("너는 외교관 후보자의 논술·면접을 돕는 논설 분석가다. 주어진 사설·칼럼의 제목과 도입부만 근거로 쓴다. "
       "도입부에 없는 주장을 지어내지 마라. 언론사를 진보·보수로 딱지 붙이지 말고, 각 글의 주장과 근거만 정리하라. "
       "영어 글도 한국어로 정리하라.")


def analyze(items: list[dict], picks: int = 4, llm_provider: str | None = None) -> dict:
    from .llm import chat_json
    lines = [f"[id={k}] ({i['outlet']}{', 해외' if i.get('lang') == 'en' else ''}) {i['title']}"
             + (f" | 도입부: {i['snippet'][:180]}" if i.get("snippet") else "") for k, i in enumerate(items)]
    user = f"""외교·국제 관련 사설·칼럼 {len(items)}편:
{chr(10).join(lines)}

1) 여러 매체가 공통으로 다룬 외교·국제 쟁점 하나를 고른다 (없으면 가장 중요한 쟁점).
2) 반드시 그 쟁점을 직접 다룬 글만 2~{picks}편 고른다. 다른 쟁점의 글, 국내 정치·사회 글은 절대 넣지 마라.
   같은 매체는 1편만. 가능하면 매체가 서로 다른(성향이 다른) 글을 고른다. 해외 글은 같은 쟁점일 때만 넣는다 (억지로 넣지 마라).
3) 주장·근거는 제목과 도입부에서 확인되는 것만 쓴다. 도입부만으로 논지가 불분명하면 claim 앞에 "(도입부 기준)"을 붙인다.
4) 논술 뼈대는 이 쟁점으로 실제 답안을 쓸 때의 논지로, 일반론('균형·협력이 중요') 대신 구체적인 주장과 논거를 쓴다.
JSON: {{"issue": "쟁점 한 줄 (예: 한미 관세 협상, 양보냐 버티기냐)",
 "background": "왜 지금 쟁점인가 1문장",
 "picks": [{{"id": 숫자, "claim": "이 글의 주장 1문장", "basis": "근거 1문장"}}],
 "split": "입장이 갈리는 축 1~2문장 (예: 동맹 신뢰 vs 경제 실리)",
 "outline": {{"intro": "서론: 문제 제기 1문장", "issue": "쟁점: 대립하는 두 논리 1~2문장",
             "alternative": "대안: 한국이 취할 구체적 정책 1~2문장", "conclusion": "결론: 원칙 1문장"}},
 "evidence": ["답안에 쓸 수 있는 사실·개념·선례 2~3개 (도입부나 널리 알려진 사실만)"],
 "question": "이 쟁점으로 나올 만한 면접 질문 1개와 → 답변 방향"}}"""
    from .llm import chat_json_prefer
    return chat_json_prefer(llm_provider, SYS, user, max_tokens=3000, fast=True)


def _as_int(x):
    try:
        return int(x)
    except (TypeError, ValueError):
        return None


def build(profile: str, now: datetime | None = None, use_llm: bool = True) -> dict:
    op = settings(profile) or {}
    items, status = collect(profile, now)
    items = items[:30]
    res, err = {}, ""
    if use_llm and len(items) >= 2:
        try:
            res = analyze(items, int(op.get("picks", 4)), daily._raw_feeds(profile).get("_llm"))
        except Exception as e:
            err = str(e)[:120] or type(e).__name__
    picked = []
    for p in res.get("picks") or []:
        k = _as_int(p.get("id"))
        if (k is not None and 0 <= k < len(items) and k not in [x[0] for x in picked]
                and items[k]["outlet"] not in [items[x[0]]["outlet"] for x in picked]):   # 매체당 1편
            picked.append((k, p))
    others = [i for k, i in enumerate(items) if k not in {x[0] for x in picked}][:int(op.get("others", 6))]
    return dict(res=res, err=err, items=items, status=status,
                picked=[(items[k], p) for k, p in picked], others=others)


def _s(x) -> str:
    return daily._s(x)


def to_blocks(o: dict) -> tuple[list[dict], str]:
    title = "오늘의 논점 · 사설·칼럼 비교"
    blocks = [{"type": "header", "text": {"type": "plain_text", "text": title}}]
    r = o["res"]
    if o["picked"] and r.get("issue"):
        head = f"*쟁점: {_s(r['issue'])}*"
        if r.get("background"):
            head += f"\n{_s(r['background'])}"
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": head}})
        for it, p in o["picked"]:
            tag = " `해외`" if it.get("lang") == "en" else ""
            txt = f"*{it['outlet']}*{tag}  <{it['url']}|{it['title']}>\n• 주장: {_s(p.get('claim'))}"
            if p.get("basis"):
                txt += f"\n• 근거: {_s(p.get('basis'))}"
            blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": txt[:2900]}})
        tail = ""
        if r.get("split"):
            tail += f"*입장이 갈리는 지점*\n{_s(r['split'])}\n\n"
        ol = r.get("outline") or {}
        if isinstance(ol, dict) and any(ol.values()):
            tail += ("*논술 뼈대*\n" + "\n".join(f"{n}. {lab}: {_s(ol.get(k))}" for n, (k, lab) in enumerate(
                [("intro", "서론"), ("issue", "쟁점"), ("alternative", "대안"), ("conclusion", "결론")], 1) if ol.get(k)))
        ev = [x for x in (r.get("evidence") or []) if x] if isinstance(r.get("evidence"), list) else []
        if ev:
            tail += "\n\n*쓸 만한 논거*\n" + "\n".join(f"• {_s(x)}" for x in ev[:3])
        if r.get("question"):
            tail += f"\n\n:speech_balloon: *면접 질문*: {_s(r['question'])}"
        if tail.strip():
            blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": tail.strip()[:2900]}})
    elif o["err"]:
        blocks.append({"type": "section", "text": {"type": "mrkdwn",
                       "text": f"_(논점 분석 실패: {o['err']} → 사설 목록만 표시)_"}})
    elif not o["items"]:
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": "_지난 36시간 외교·국제 관련 사설이 없습니다._"}})
    rest = o["others"] if o["picked"] else o["items"][:10]
    if rest:
        blocks.append({"type": "divider"})
        lines = [f"• {i['outlet']}{' (해외)' if i.get('lang') == 'en' else ''}: <{i['url']}|{i['title']}>" for i in rest]
        blocks.append({"type": "section", "text": {"type": "mrkdwn",
                       "text": ("*그 밖의 외교·국제 사설·칼럼*\n" + "\n".join(lines))[:2900]}})
    ok = sum(isinstance(v, int) for v in o["status"].values())
    blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text":
                   f"사설·칼럼 {len(o['items'])}편 (외교·국제 관련) · 피드 {ok}/{len(o['status'])} 정상 · "
                   "요약은 제목·도입부 기준이니 전문은 링크로 확인"}]})
    return blocks, title


def post(client, profile: str, channel: str, thread_ts: str | None = None, post_at: int | None = None):
    """논점 메시지 게시. thread_ts 가 있으면 브리핑의 스레드 답글로 단다."""
    if not settings(profile):
        return None
    blocks, title = to_blocks(build(profile))
    if post_at:
        return client.chat_scheduleMessage(channel=channel, blocks=blocks[:50], text=title, post_at=post_at,
                                           unfurl_links=False)
    return client.chat_postMessage(channel=channel, thread_ts=thread_ts, blocks=blocks[:50], text=title,
                                   unfurl_links=False, unfurl_media=False)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--profile", default="diplomacy")
    ap.add_argument("--post", nargs="?", const="", default=None, help="Slack 게시 (값을 주면 그 메시지의 스레드에)")
    ap.add_argument("--check-feeds", action="store_true")
    ap.add_argument("--no-llm", action="store_true")
    a = ap.parse_args()
    if a.check_feeds:
        items, status = collect(a.profile)
        for k, v in status.items():
            print(f"{'OK ' if isinstance(v, int) else 'ERR'} {v!s:>12}  {k}")
        print(f"→ 외교·국제 관련 {len(items)}편")
        return
    o = build(a.profile, use_llm=not a.no_llm)
    if a.post is not None:
        from .slack_out import client
        post(client(), a.profile, daily.profile_channel(a.profile), a.post or None)
        print("Slack 전송 완료")
        return
    r = o["res"]
    print("쟁점:", r.get("issue"), "|", o["err"])
    for it, p in o["picked"]:
        print(f"- {it['outlet']}: {it['title']}\n    주장: {p.get('claim')}\n    근거: {p.get('basis')}")
    print("분기:", r.get("split"), "\n뼈대:", r.get("outline"), "\n질문:", r.get("question"))
    print("그 밖:", [(i["outlet"], i["title"]) for i in o["others"]])


if __name__ == "__main__":
    main()
