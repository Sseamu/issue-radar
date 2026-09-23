"""아침 브리핑: 지난 24시간 섹션별 Top 뉴스 10개.

순위 기준 = '몇 개 언론사가 같은 사건을 보도했나'(보도 매체 수).
조회수는 공개 API가 없고 연예·가십 쪽으로 치우치므로, 여러 매체가 동시에 다룬 사건을 중요 사건으로 본다.

실행:
  python -m radar.daily                 # 콘솔 출력
  python -m radar.daily --post          # Slack 채널로 전송
  python -m radar.daily --check-feeds   # RSS 주소 점검
"""
import argparse
import json
import re
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

import numpy as np
from sklearn.cluster import AgglomerativeClustering
from sklearn.feature_extraction.text import TfidfVectorizer

from . import config
from .collect import UA, clean

KST = timezone(timedelta(hours=9))
FEEDS_PATH = config.ROOT / "feeds.json"
WEEKDAY = "월화수목금토일"


# ---------------------------------------------------------------- 수집
def _raw_feeds() -> dict:
    return json.loads(FEEDS_PATH.read_text(encoding="utf-8"))


def load_feeds(sections: list[str] | None = None) -> dict:
    feeds = _raw_feeds()
    return {k: v for k, v in feeds.items() if not k.startswith("_") and (not sections or k in sections)}


DEFAULT_GROUPS = [{"name": "주요 뉴스", "sections": None, "n": 10, "min_foreign": 0, "boost": False}]


def load_brief_config() -> dict:
    """feeds.json의 _groups / _boost_words / _exclude_* 설정. 없으면 전체 섹션에서 10개."""
    raw = _raw_feeds()
    groups = raw.get("_groups") or DEFAULT_GROUPS
    all_secs = list(load_feeds().keys())
    for g in groups:
        g["sections"] = [x for x in (g.get("sections") or all_secs) if x in all_secs]
    return dict(groups=groups, boost_words=[w.lower() for w in raw.get("_boost_words", [])],
                boost_factor=float(raw.get("_boost_factor", 1.4)),
                exclude_sections=set(raw.get("_exclude_sections", [])),
                exclude_words=[w.lower() for w in raw.get("_exclude_words", [])])


def _google_cluster_size(desc_html: str) -> int:
    """구글 뉴스 섹션 피드의 description에는 같은 사건의 관련 기사 목록(<li>)이 들어 있다."""
    return max(1, len(re.findall(r"<li>", desc_html or "")))


ATOM = "{http://www.w3.org/2005/Atom}"


def _parse_date(txt: str | None):
    if not txt:
        return None
    try:
        return parsedate_to_datetime(txt).astimezone(KST)      # RSS: RFC 822
    except Exception:
        pass
    try:
        return datetime.fromisoformat(txt.replace("Z", "+00:00")).astimezone(KST)  # Atom: ISO 8601
    except Exception:
        return None


def fetch_feed(feed: dict, section: str) -> list[dict]:
    import requests
    r = requests.get(feed["url"], headers=UA, timeout=15)
    r.raise_for_status()
    root = ET.fromstring(r.content)
    out, is_google = [], feed.get("type") == "google"
    lang = feed.get("lang", "ko")
    outlet_default = feed.get("outlet") or feed["name"].split()[0]
    for it in root.iter("item"):                                 # RSS 2.0
        title = clean(it.findtext("title"))
        if not title:
            continue
        src_el = it.find("source")
        outlet = (src_el.text or "").strip() if (is_google and src_el is not None) else outlet_default
        if outlet and title.endswith(" - " + outlet):
            title = title[: -len(" - " + outlet)]
        desc = it.findtext("description") or ""
        out.append(dict(section=section, outlet=outlet, title=title, url=it.findtext("link"),
                        published=_parse_date(it.findtext("pubDate")), lang=lang,
                        snippet="" if is_google else clean(desc)[:200],
                        g_hint=_google_cluster_size(desc) if is_google else 0))
    for it in root.iter(ATOM + "entry"):                         # Atom (The Verge 등)
        title = clean(it.findtext(ATOM + "title"))
        if not title:
            continue
        link = it.find(ATOM + "link")
        summary = it.findtext(ATOM + "summary") or it.findtext(ATOM + "content") or ""
        out.append(dict(section=section, outlet=outlet_default, title=title,
                        url=link.get("href") if link is not None else "",
                        published=_parse_date(it.findtext(ATOM + "published") or it.findtext(ATOM + "updated")),
                        lang=lang, snippet=clean(summary)[:200], g_hint=0))
    return out


def collect_all(sections: list[str] | None = None, hours: int = 24, now: datetime | None = None):
    now = now or datetime.now(KST)
    since = now - timedelta(hours=hours)
    jobs = [(f, s) for s, fl in load_feeds(sections).items() for f in fl]
    items, status = [], {}
    with ThreadPoolExecutor(8) as ex:
        for (f, s), fut in zip(jobs, [ex.submit(fetch_feed, f, s) for f, s in jobs]):
            try:
                got = fut.result()
                status[f["name"]] = len(got)
                items += [i for i in got if i["published"] is None or i["published"] >= since]
            except Exception as e:
                status[f["name"]] = f"실패: {type(e).__name__}"
    return items, status


# ---------------------------------------------------------------- 사건 묶기 + 순위
def cluster_events(items: list[dict], distance: float = 0.72) -> list[dict]:
    """제목 문자 n-gram 유사도로 같은 사건을 묶는다. 최종 병합은 LLM 단계에서 한 번 더 한다."""
    if not items:
        return []
    # [포토], (종합) 같은 말머리를 지우되, 지우고 남는 게 없으면 원래 제목을 쓴다
    titles = []
    for i in items:
        t = re.sub(r"\[[^\]]*\]|\([^)]*\)", " ", i["title"]).strip()
        titles.append(t if len(t) >= 2 else i["title"])
    X = TfidfVectorizer(analyzer="char_wb", ngram_range=(2, 4), sublinear_tf=True).fit_transform(titles).toarray()
    labels = np.arange(len(items))                      # 기본값: 기사마다 따로 (벡터가 0인 제목 포함)
    ok = np.where(np.linalg.norm(X, axis=1) > 0)[0]     # 코사인 거리는 0 벡터를 허용하지 않음
    if len(ok) >= 2:
        sub = AgglomerativeClustering(n_clusters=None, metric="cosine", linkage="average",
                                      distance_threshold=distance).fit_predict(X[ok])
        labels[ok] = sub + len(items)                   # 기존 번호와 겹치지 않게
    events = []
    for lab in np.unique(labels):
        mem = [items[i] for i in np.where(labels == lab)[0]]
        outlets = {m["outlet"] for m in mem if m["outlet"]}
        g_extra = max((m["g_hint"] for m in mem), default=0)
        sec = max({m["section"] for m in mem}, key=lambda s: sum(m["section"] == s for m in mem))
        latest = max((m["published"] for m in mem if m["published"]), default=None)
        mem.sort(key=lambda m: (m["outlet"] != "연합뉴스", -len(m.get("snippet", ""))))  # 통신사·요약 있는 기사 우선
        lang = "en" if sum(m.get("lang") == "en" for m in mem) * 2 > len(mem) else "ko"
        events.append(dict(section=sec, lang=lang, coverage=max(len(outlets), g_extra), n_articles=len(mem),
                           outlets=sorted(outlets), latest=latest.isoformat() if latest else "",
                           titles=list(dict.fromkeys(m["title"] for m in mem))[:4],
                           snippet=next((m["snippet"] for m in mem if m.get("snippet")), ""),
                           links=list({m["url"]: (m["outlet"], m["url"]) for m in mem}.values())[:3]))
    events.sort(key=lambda e: (-e["coverage"], -e["n_articles"]))
    for i, e in enumerate(events):
        e["id"] = i
    return events


def pick_candidates(events: list[dict], sections: list[str], per_section: int = 8, total: int = 30):
    """LLM에 넘길 후보: 섹션별 상위 per_section + 전체 상위로 total까지 채움."""
    chosen = {}
    for s in sections:
        for e in [e for e in events if e["section"] == s][:per_section]:
            chosen[e["id"]] = e
    for e in events:
        if len(chosen) >= total:
            break
        chosen.setdefault(e["id"], e)
    return sorted(chosen.values(), key=lambda e: -e["coverage"])


def _event_query(e: dict, n: int = 3) -> str:
    from collections import Counter
    from .topics import tokenize
    c = Counter(w for t in e["titles"] for w in dict.fromkeys(tokenize(t)))
    first = tokenize(e["titles"][0])
    words = sorted(c, key=lambda w: (-c[w], first.index(w) if w in first else 99))[:n]
    return " ".join(words)


def enrich_coverage(cands: list[dict], sleep: float = 0.5) -> None:
    """후보 사건마다 구글 뉴스를 '지난 1일'로 검색해 실제 보도 매체 수를 센다.
    자체 피드(섹션당 언론사 6~8곳)만으로는 매체 수 상한이 낮기 때문. 후보 30개 기준 약 30초."""
    import time
    import requests
    from urllib.parse import quote_plus
    for e in cands:
        q = _event_query(e)
        if not q:
            continue
        region = "hl=en-US&gl=US&ceid=US:en" if e.get("lang") == "en" else "hl=ko&gl=KR&ceid=KR:ko"
        url = f"https://news.google.com/rss/search?q={quote_plus(q + ' when:1d')}&{region}"
        try:
            root = ET.fromstring(requests.get(url, headers=UA, timeout=10).content)
            srcs = {(it.findtext("source") or "").strip() for it in root.iter("item")} - {""}
            e["query"], e["web_coverage"] = q, len(srcs)
            e["coverage"] = max(e["coverage"], len(srcs))
        except Exception:
            pass
        time.sleep(sleep)
    cands.sort(key=lambda e: -e["coverage"])


# ---------------------------------------------------------------- 필터 · 점수 · 선정
def _text(e: dict) -> str:
    return (" ".join(e["titles"]) + " " + e.get("snippet", "")).lower()


def _has(text: str, words: list[str]) -> bool:
    """영문 단어는 단어 경계로 비교 (said 속의 ai 같은 오탐 방지), 한글은 부분 일치."""
    for w in words:
        if w.isascii():
            if re.search(r"(?<![a-z0-9])" + re.escape(w) + r"(?![a-z0-9])", text):
                return True
        elif w in text:
            return True
    return False


def is_incident(e: dict, cfg: dict) -> bool:
    """정치·사회/국제 섹션의 개인·지역 사건사고(화재, 교통사고, 범죄 피해 등)는 제외."""
    return e["section"] in cfg["exclude_sections"] and _has(_text(e), cfg["exclude_words"])


def score_events(cands: list[dict], cfg: dict, boost: bool):
    """국내·해외는 매체 수 규모가 달라 직접 비교할 수 없으므로 언어별 백분위 순위로 환산.
    경제·테크 그룹은 AI·반도체 등 관심 키워드가 들어간 사건에 가중치."""
    for lang in {e.get("lang", "ko") for e in cands}:
        grp = sorted([e for e in cands if e.get("lang", "ko") == lang], key=lambda e: e["coverage"])
        n = len(grp)
        for i, e in enumerate(grp):
            e["score"] = (i + 1) / n
    for e in cands:
        e["boosted"] = boost and _has(_text(e), cfg["boost_words"])
        if e["boosted"]:
            e["score"] *= cfg["boost_factor"]


def pick_group(cands: list[dict], n: int, min_foreign: int) -> list[dict]:
    ranked = sorted(cands, key=lambda e: (-e["score"], -e["coverage"]))
    chosen = [e for e in ranked if e.get("lang") == "en"][:min_foreign]
    for e in ranked:
        if len(chosen) >= n:
            break
        if e not in chosen:
            chosen.append(e)
    return sorted(chosen, key=lambda e: -e["score"])


SYS = ("너는 아침 뉴스 브리핑 에디터다. 주어진 기사 제목·요약문만 근거로 쓰고 사실을 지어내지 마라. "
       "항목 하나는 반드시 사건 하나만 다룬다. 서로 다른 사건을 한 항목에 섞지 마라. "
       "영어 기사도 모두 자연스러운 한국어로 작성하라.")


def summarize(events: list[dict]) -> dict:
    from .llm import chat_json
    lines = [f"[id={e['id']}] ({e['section']}{', 영문' if e.get('lang') == 'en' else ''}) "
             + " | ".join(e["titles"][:3]) + (f"\n    요약문: {e['snippet'][:150]}" if e["snippet"] else "")
             for e in events]
    user = f"""사건 {len(events)}개 (각 id는 서로 다른 사건 후보):
{chr(10).join(lines)}

각 id마다 항목을 하나씩, 입력 순서대로 작성하라. 다른 id의 내용을 섞지 마라.
- 두 id가 명백히 같은 사건이면 뒤의 id에만 "duplicate_of": 앞 id 를 넣어라.
- 개인·지역 단위 사건사고(화재, 교통사고, 추락, 범죄 피해, 실종 등)이면 "incident": true 로 표시하라.
JSON: {{"mood": "어제 국내외 뉴스 전체 분위기 한 문장",
 "items": [{{"id": 숫자, "headline": "이 사건만 다룬 30자 이내 한국어 제목", "summary": "무슨 일이 있었나 1~2문장",
            "why": "왜 중요한가 1문장", "duplicate_of": null, "incident": false}}]}}"""
    return chat_json(SYS, user, max_tokens=4000, fast=True)


def _as_int(x):
    try:
        return int(x) if x not in (None, "", "null") else None
    except (TypeError, ValueError):
        return None


def build_brief(sections: list[str] | None = None, hours: int = 24, top_n: int | None = None,
                use_llm: bool = True, enrich: bool = True, now: datetime | None = None) -> dict:
    """sections 인자는 하위 호환용. 그룹 구성은 feeds.json의 _groups가 결정한다."""
    cfg = load_brief_config()
    groups = cfg["groups"]
    all_secs = list(dict.fromkeys(s for g in groups for s in g["sections"]))
    items, status = collect_all(all_secs, hours, now)
    events = cluster_events(items)
    excluded = [e for e in events if is_incident(e, cfg)]
    events = [e for e in events if not is_incident(e, cfg)]

    pools, cands_all = [], []
    for g in groups:
        ev = [e for e in events if e["section"] in g["sections"]]
        cands = pick_candidates(ev, g["sections"], per_section=6, total=6 * len(g["sections"]))
        cands_all += [c for c in cands if c not in cands_all]
        pools.append(cands)
    if enrich:
        enrich_coverage(cands_all)
    for g, cands in zip(groups, pools):
        score_events(cands, cfg, bool(g.get("boost")))
    shortlist = [pick_group(c, g["n"] + 3, g.get("min_foreign", 0)) for g, c in zip(groups, pools)]

    out = dict(date=(now or datetime.now(KST)), n_items=len(items), n_events=len(events) + len(excluded),
               n_excluded=len(excluded), feeds=status, mood="", groups=[], items=[])
    written = {}
    todo = [e for sl in shortlist for e in sl]
    if use_llm and todo:
        try:
            res = summarize(todo)
            out["mood"] = _s(res.get("mood"))
            written = {_as_int(it.get("id")): it for it in res.get("items", []) if _as_int(it.get("id")) is not None}
        except Exception as e:
            out["mood"] = f"(LLM 요약 실패: {type(e).__name__}. 제목만 표시합니다)"

    for g, sl in zip(groups, shortlist):
        by_id = {e["id"]: e for e in sl}
        kept = []
        for e in sl:
            w = written.get(e["id"], {})
            if w.get("incident") is True and e["section"] in cfg["exclude_sections"]:
                out["n_excluded"] += 1
                continue
            dup = _as_int(w.get("duplicate_of"))
            if dup in by_id and dup != e["id"] and by_id[dup] in kept:   # 같은 사건 → 앞 항목에 합침
                tgt = by_id[dup]
                tgt["coverage"] = max(tgt["coverage"], e["coverage"])
                tgt["links"] = list({u: (o, u) for o, u in tgt["links"] + e["links"]}.values())[:3]
                continue
            e["_w"] = w
            kept.append(e)
        final = pick_group(kept, g["n"], g.get("min_foreign", 0))
        rows = [dict(section=e["section"], foreign=e.get("lang") == "en", boosted=e.get("boosted", False),
                     headline=_s(e["_w"].get("headline")) or e["titles"][0],
                     summary=_s(e["_w"].get("summary")) or e["snippet"], why=_s(e["_w"].get("why")),
                     coverage=e["coverage"], links=e["links"]) for e in final]
        out["groups"].append(dict(name=g["name"], items=rows))
        out["items"] += rows
    return out


def _s(x) -> str:
    if isinstance(x, (list, tuple)):
        return " ".join(str(i) for i in x)
    return "" if x is None else str(x).strip()


# ---------------------------------------------------------------- Slack 포맷
def to_blocks(b: dict) -> tuple[list[dict], str]:
    d = b["date"]
    title = f"{d.month}월 {d.day}일({WEEKDAY[d.weekday()]}) 아침 브리핑 · 지난 24시간"
    blocks = [{"type": "header", "text": {"type": "plain_text", "text": title}}]
    if b.get("mood"):
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": f"_{b['mood']}_"}})
    groups = b.get("groups") or [dict(name="주요 뉴스", items=b["items"])]
    no = 0
    for g in groups:
        blocks.append({"type": "divider"})
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": f"*{g['name']}*  ({len(g['items'])}건)"}})
        for it in g["items"]:
            no += 1
            links = " · ".join(f"<{u}|{o or '원문'}>" for o, u in it["links"] if u)
            foreign_tag = " `해외`" if it.get("foreign") and not it["section"].startswith("해외") else ""
            tags = f"`{it['section']}`{foreign_tag} `매체 {it['coverage']}곳`"
            txt = f"*{no}. {it['headline']}*   {tags}\n{it['summary']}"
            if it.get("why"):
                txt += f"\n> {it['why']}"
            if links:
                txt += f"\n{links}"
            blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": txt[:2900]}})
    ok = sum(isinstance(v, int) and v > 0 for v in b["feeds"].values())
    blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text":
                   f"기사 {b['n_items']}건 → 사건 {b['n_events']}개 (사건사고 {b.get('n_excluded', 0)}건 제외) · "
                   f"피드 {ok}/{len(b['feeds'])} 정상 · 순위 = 보도 매체 수(국내·해외 각각) + 관심 키워드 가중치"}]})
    return blocks, title


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--post", action="store_true")
    ap.add_argument("--check-feeds", action="store_true")
    ap.add_argument("--no-llm", action="store_true")
    ap.add_argument("--no-enrich", action="store_true", help="구글 검색으로 매체 수 보강 생략")
    ap.add_argument("--hours", type=int, default=24)
    ap.add_argument("--sections", default=config.env("BRIEF_SECTIONS"), help="쉼표 구분, 비우면 전체")
    a = ap.parse_args()
    sections = [s.strip() for s in a.sections.split(",") if s.strip()] or None

    if a.check_feeds:
        _, status = collect_all(None, hours=24 * 365)   # 모든 섹션 점검
        for k, v in status.items():
            print(f"{'OK ' if isinstance(v, int) and v else 'ERR'} {v!s:>12}  {k}")
        return
    b = build_brief(sections, a.hours, use_llm=not a.no_llm, enrich=not a.no_enrich)
    blocks, title = to_blocks(b)
    if a.post:
        from .slack_out import post
        post(blocks, title, channel=config.SLACK_BRIEF_CHANNEL)
        print("Slack 전송 완료:", title)
    else:
        print(title, "\n", b.get("mood", ""))
        i = 0
        for g in b["groups"]:
            print(f"\n== {g['name']} ==")
            for it in g["items"]:
                i += 1
                tag = "해외 " if it["foreign"] and not it["section"].startswith("해외") else ""
                print(f"{i:2}. [{tag}{it['section']}] ({it['coverage']}곳) {it['headline']}\n    {it['summary']}")
        print("피드 상태:", b["feeds"])


if __name__ == "__main__":
    main()
