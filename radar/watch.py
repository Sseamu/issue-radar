"""관심 키워드: 요청하지 않아도 매일 아침 자동으로 챙겨 보는 키워드.

매일   : 최근 2일(WATCH_DAYS) 기사 수 + 평소 대비 배율 + 핵심 2~3줄 → 아침 브리핑 하단에 표시
정기   : 처음 등록한 다음 날 아침, 매주 월요일, 보도량 급증일에는 전체 분석(쟁점·예측)을 #radar 에 게시

목록은 DB에 저장되며 Slack에서 관리한다:
  @radar 관심                 목록 보기
  @radar 관심 추가 반도체/semiconductor   ('/' 뒤는 해외 언론 검색용 영문, 생략 가능)
  @radar 관심 삭제 반도체
"""
import json
import time
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta
from urllib.parse import quote_plus

import requests

from . import config, db
from .collect import UA, _row, _to_date, clean
from .llm import chat_json

SPIKE_RATIO = float(config.env("WATCH_SPIKE_RATIO", "2.0"))     # 평소 대비 이 배율 이상이면 급증
FULL_WEEKDAY = config.env("WATCH_FULL_WEEKDAY", "0")            # 정기 전체 분석 요일 (0=월 … 6=일, 비우면 안 함)
WATCH_MAX = int(config.env("WATCH_MAX", "10"))                  # 최대 등록 개수
WATCH_DAYS = int(config.env("WATCH_DAYS", "2"))                 # 동향·최근 이슈를 볼 기간(일)
MIN_BASE_DAYS = 3                                               # 평소 수준 계산에 필요한 최소 일수

# 키워드별 세부 설정. 이름이 같으면 자동 적용 (Slack 에서 '관심 추가 바이오' 해도 적용됨)
#   key      : DB 저장 이름 (검색 방향이 달라지면 기존 데이터와 섞이지 않게 새 이름 사용)
#   label    : Slack 표시 이름
#   ko_query : 한국어 검색식 (없으면 키워드 그대로)
#   en       : 해외 검색식 · sites: 해외 검색 대상 매체 · focus: 요약 지시
PRESETS = {
    "바이오": {
        "key": "해외바이오",
        "label": "바이오 (해외 중심)",
        "ko_query": "(FDA OR 빅파마 OR 글로벌 제약 OR 해외 바이오텍 OR 임상 3상) -특징주 -상한가 -코스닥",
        "en": "biotech OR biopharma OR \"FDA approval\" OR \"clinical trial\"",
        "sites": ["reuters.com", "bloomberg.com", "ft.com", "wsj.com", "cnbc.com", "statnews.com",
                  "fiercebiotech.com", "endpts.com", "biopharmadive.com", "nytimes.com"],
        "focus": "해외(미국·유럽·중국) 바이오·제약 동향 위주로 쓴다: FDA 승인·임상 결과·빅파마 M&A·약가 정책. "
                 "국내 상장 바이오 종목의 주가·특징주 기사는 제외한다.",
    },
}


def resolve(kw: dict) -> dict:
    """목록 항목에 프리셋을 입혀 실제 처리용 설정을 만든다."""
    p = PRESETS.get(kw["q"], {})
    return {"q": kw["q"], "key": p.get("key", kw["q"]), "label": p.get("label", kw["q"]),
            "ko_query": p.get("ko_query"), "en": p.get("en", kw.get("en", "")),
            "sites": p.get("sites"), "focus": p.get("focus", "")}
WEEKDAY = "월화수목금토일"


# ---------------------------------------------------------------- 목록 관리
def _parse(s: str) -> dict:
    ko, _, en = s.partition("/")
    return {"q": ko.strip(), "en": en.strip()}


def get_list() -> list[dict]:
    raw = db.kv_get("watchlist")
    if raw is None:  # 처음에는 .env 의 WATCHLIST 로 초기화
        items = [_parse(x) for x in config.env("WATCHLIST").split(",") if x.strip()]
        db.kv_set("watchlist", json.dumps(items, ensure_ascii=False))
        return items
    return json.loads(raw)


def _save(items: list[dict]):
    db.kv_set("watchlist", json.dumps(items, ensure_ascii=False))


def add(spec: str) -> str:
    items, new = get_list(), _parse(spec)
    if not new["q"]:
        return "추가할 키워드를 적어 주세요. 예: `@radar 관심 추가 반도체/semiconductor`"
    if any(i["q"] == new["q"] for i in items):
        return f"*{new['q']}* 은(는) 이미 관심 키워드에 있습니다."
    if len(items) >= WATCH_MAX:
        return f"관심 키워드는 최대 {WATCH_MAX}개입니다 (아침 처리 시간 때문). 하나를 삭제한 뒤 추가하세요."
    items.append(new)
    _save(items)
    en = f" (해외: {new['en']})" if new["en"] else ""
    return f"관심 키워드에 *{new['q']}*{en} 추가. 내일 아침 첫 전체 분석을 #radar 에 올립니다.\n" + list_text(items)


def remove(q: str) -> str:
    items = get_list()
    left = [i for i in items if i["q"] != q.strip()]
    if len(left) == len(items):
        return f"*{q}* 은(는) 관심 키워드에 없습니다.\n" + list_text(items)
    _save(left)
    return f"*{q}* 삭제 완료.\n" + list_text(left)


def list_text(items: list[dict] | None = None) -> str:
    items = get_list() if items is None else items
    if not items:
        return "관심 키워드가 없습니다. `@radar 관심 추가 키워드` 로 등록하세요."
    body = "\n".join(f"• *{resolve(i)['label']}*" + (f"  (해외: {resolve(i)['en']})" if resolve(i)["en"] else "")
                     for i in items)
    day = f"매주 {WEEKDAY[int(FULL_WEEKDAY)]}요일" if FULL_WEEKDAY != "" else "정기 분석 없음"
    return (f"*관심 키워드 {len(items)}/{WATCH_MAX}*\n{body}\n"
            f"_매일 아침 브리핑에 24시간 동향 표시 · 전체 분석: 첫 등록 다음 날, {day}, 보도량 {SPIKE_RATIO:g}배 급증 시_")


# ---------------------------------------------------------------- 매일 동향
def _search_1d(q: str, lang: str, sites: list[str] | None = None, days: int = 1) -> list[dict]:
    if lang == "en":
        sites_q = " OR ".join(f"site:{s}" for s in (sites or config.FOREIGN_SITES))
        full, region = f"({q}) ({sites_q}) when:{days}d", "hl=en-US&gl=US&ceid=US:en"
    else:
        full, region = f"{q} when:{days}d", "hl=ko&gl=KR&ceid=KR:ko"
    r = requests.get(f"https://news.google.com/rss/search?q={quote_plus(full)}&{region}", headers=UA, timeout=15)
    r.raise_for_status()
    out = []
    for it in ET.fromstring(r.content).iter("item"):
        title = clean(it.findtext("title"))
        src = (it.findtext("source") or "").strip()
        if src and title.endswith(" - " + src):
            title = title[: -len(" - " + src)]
        if title:
            out.append(dict(title=title, url=it.findtext("link"), source=src,
                            published=_to_date(it.findtext("pubDate") or "") or date.today().isoformat()))
    return out


def _baseline(q: str, today: str) -> tuple[float | None, int]:
    with db.conn() as c:
        rows = c.execute("SELECT n + n_en FROM watch_stats WHERE query=? AND day<? ORDER BY day DESC LIMIT 14",
                         (q, today)).fetchall()
    vals = [r[0] for r in rows]
    return (sum(vals) / len(vals) if len(vals) >= MIN_BASE_DAYS else None), len(vals)


def daily_update(kw: dict) -> dict:
    """최근 WATCH_DAYS일 기사 수집 → DB 저장(전체 분석에도 재사용) → 평소 대비 배율 → LLM 2~3줄 요약."""
    c = resolve(kw)
    q, en, today = c["key"], c["en"], date.today().isoformat()
    ko = _search_1d(c["ko_query"] or c["q"], "ko", days=WATCH_DAYS)
    fo = _search_1d(en, "en", c["sites"], days=WATCH_DAYS) if en else []
    time.sleep(0.5)
    db.insert_articles([_row(q, a["title"], a["url"], a["source"], a["published"], "", "ko", "google_ko") for a in ko]
                       + [_row(q, a["title"], a["url"], a["source"], a["published"], "", "en", "google_foreign")
                          for a in fo])
    n, n_en = len({a["title"] for a in ko}), len({a["title"] for a in fo})
    base, days = _baseline(q, today)
    per_day = (n + n_en) / WATCH_DAYS                     # 하루 평균으로 환산해 저장·비교
    with db.conn() as cx:
        cx.execute("INSERT OR REPLACE INTO watch_stats(day,query,n,n_en) VALUES (?,?,?,?)",
                   (today, q, round(n / WATCH_DAYS), round(n_en / WATCH_DAYS)))
    ratio = per_day / base if base else None

    bullets, headline = [], ""
    ko_n, fo_n = (10, 35) if c["focus"] else (30, 15)     # 해외 중심 키워드는 해외 기사 비중을 높임
    titles = [f"- {a['published']} {a['title']} ({a['source']})" for a in ko[:ko_n]] + \
             [f"- {a['published']} [해외] {a['title']} ({a['source']})" for a in fo[:fo_n]]
    if titles:
        try:
            r = chat_json("너는 뉴스 모니터링 담당자다. 주어진 제목만 근거로 한국어로 쓴다. 사실을 지어내지 마라.",
                          f'관심 키워드 "{c["label"]}" 최근 {WATCH_DAYS}일 기사 제목 (날짜 포함):\n' + "\n".join(titles) +
                          (f"\n\n작성 지침: {c['focus']}" if c["focus"] else "") +
                          '\n\nJSON: {"headline": "가장 중요한 움직임 한 문장", "bullets": ["주요 동향 2~3개, 각 1문장"]}',
                          max_tokens=800, fast=True)
            headline = str(r.get("headline") or "")
            bullets = [str(b) for b in (r.get("bullets") or [])][:3]
        except Exception as e:
            headline = f"(요약 실패: {type(e).__name__})"
    top = (fo[:2] + ko[:1]) if c["focus"] else (fo[:1] + ko[:2])
    links = [(a["source"], a["url"]) for a in top if a.get("url")]
    return dict(q=c["q"], key=q, label=c["label"], en=en, ko_query=c["ko_query"], sites=c["sites"],
                n=n, n_en=n_en, capped=len(ko) >= 100, ratio=ratio, base_days=days,
                headline=headline, bullets=bullets, links=links, full=None)


def full_reason(r: dict, now: datetime) -> str | None:
    """전체 분석(쟁점·예측)을 돌릴 이유. 없으면 None."""
    if db.latest_report(r["key"], "brief") is None:
        return "첫 분석"
    if r["ratio"] and r["ratio"] >= SPIKE_RATIO:
        return f"보도량 {r['ratio']:.1f}배 급증"
    if FULL_WEEKDAY != "" and now.weekday() == int(FULL_WEEKDAY):
        return f"{WEEKDAY[now.weekday()]}요일 정기 분석"
    return None


# ---------------------------------------------------------------- 브리핑 블록
def blocks(results: list[dict]) -> list[dict]:
    if not results:
        return []
    out = [{"type": "divider"},
           {"type": "section", "text": {"type": "mrkdwn", "text": f"*관심 키워드*  ({len(results)}개)"}}]
    for r in results:
        n_txt = f"최근 {WATCH_DAYS}일 {r['n']}{'+' if r['capped'] else ''}건" + (f" · 해외 {r['n_en']}건" if r["en"] else "")
        if r["ratio"] is None:
            trend = f"평소 수준 계산 중 ({r['base_days']}/{MIN_BASE_DAYS}일)"
        else:
            trend = f"평소 대비 {r['ratio']:.1f}배" + (" :chart_with_upwards_trend: 급증" if r["ratio"] >= SPIKE_RATIO
                                                   else " ↓" if r["ratio"] < 0.5 else "")
        txt = f"*{r['label']}*   `{n_txt}` `{trend}`"
        if r["headline"]:
            txt += f"\n{r['headline']}"
        txt += "".join(f"\n• {b}" for b in r["bullets"])
        if r["links"]:
            txt += "\n" + " · ".join(f"<{u}|{o or '원문'}>" for o, u in r["links"])
        if r.get("full"):
            txt += f"\n_→ 전체 분석({r['full']})을 #radar 스레드에 올렸습니다_"
        out.append({"type": "section", "text": {"type": "mrkdwn", "text": txt[:2900]}})
    return out
