"""뉴스 수집기.

- Google News RSS  : 키 불필요. 쿼리당 최대 ~100건이라 기간을 SLICE_DAYS 단위로 잘라서 1년치를 모은다.
- Naver 뉴스 API   : 키 필요. 최신순 최대 ~1,100건 → 대기업은 며칠치밖에 안 되므로 '최근 이슈' 보강용.
- BigKinds 엑셀    : 웹에서 직접 내려받은 파일을 가져온다(1년치 국내 기사의 가장 좋은 소스).
"""
import hashlib
import html
import re
import time
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta
from email.utils import parsedate_to_datetime
from urllib.parse import quote_plus

import requests

from . import config
from .db import insert_articles

UA = {"User-Agent": "Mozilla/5.0 (issue-radar personal research tool)"}
_TAG = re.compile(r"<[^>]+>")


def clean(text: str) -> str:
    return html.unescape(_TAG.sub("", text or "")).strip()


def norm_hash(title: str) -> str:
    t = title.lower()
    t = re.sub(r"\[[^\]]*\]|\([^)]*\)|【[^】]*】", " ", t)  # [단독], (종합) 등 제거
    t = re.sub(r"[^0-9a-z가-힣]", "", t)
    return hashlib.sha1(t.encode()).hexdigest()


def _row(query, title, url, source, published, snippet, lang, origin):
    return dict(query=query, title=title, url=url, source=source, published=published,
                snippet=snippet, lang=lang, origin=origin, norm_hash=norm_hash(title))


def _to_date(s: str) -> str | None:
    try:
        return parsedate_to_datetime(s).date().isoformat()
    except Exception:
        return None


# ---------------------------------------------------------------- Google News RSS
def _google_rss(q: str, start: date, end: date, lang: str) -> list[dict]:
    full_q = f"{q} after:{start.isoformat()} before:{end.isoformat()}"
    if lang == "ko":
        params = "hl=ko&gl=KR&ceid=KR:ko"
    else:
        params = "hl=en-US&gl=US&ceid=US:en"
    url = f"https://news.google.com/rss/search?q={quote_plus(full_q)}&{params}"
    r = requests.get(url, headers=UA, timeout=20)
    r.raise_for_status()
    root = ET.fromstring(r.content)
    out = []
    for it in root.iter("item"):
        title = clean(it.findtext("title"))
        src_el = it.find("source")
        source = src_el.text.strip() if src_el is not None and src_el.text else ""
        if source and title.endswith(" - " + source):
            title = title[: -len(" - " + source)]
        out.append(dict(title=title, url=it.findtext("link"), source=source,
                        published=_to_date(it.findtext("pubDate") or ""),
                        snippet=clean(it.findtext("description"))[:300]))
    return out


def _slices(months: int, slice_days: int):
    end = date.today() + timedelta(days=1)
    start = end - timedelta(days=int(months * 30.5))
    cur = start
    while cur < end:
        nxt = min(cur + timedelta(days=slice_days), end)
        yield cur, nxt
        cur = nxt


def collect_google(query: str, months: int = 12, foreign_query: str = "", progress=None,
                   ko_query: str | None = None, foreign_sites: list[str] | None = None) -> dict:
    """국내(한국어) + 해외 메이저(영문 키워드가 있을 때) 수집.
    query 는 DB 저장 키, ko_query 가 있으면 한국어 검색에는 그 검색식을 쓴다 (예: 해외 바이오 중심 검색)."""
    jobs = [("ko", ko_query or query, "google_ko")]
    if foreign_query:
        sites = " OR ".join(f"site:{s}" for s in (foreign_sites or config.FOREIGN_SITES))
        jobs.append(("en", f"{foreign_query} ({sites})", "google_foreign"))

    slices = list(_slices(months, config.SLICE_DAYS))
    total_steps, step, stats = len(jobs) * len(slices), 0, {}
    for lang, q, origin in jobs:
        got = saved = capped = 0
        for s, e in slices:
            step += 1
            try:
                items = _google_rss(q, s, e, lang)
            except Exception as ex:  # 네트워크/차단 등: 해당 구간만 건너뛴다
                print(f"[google:{origin}] {s}~{e} 실패: {ex}")
                items = []
            if len(items) >= 95:
                capped += 1  # 100건 상한에 걸림 → 해당 구간 누락 가능성
            rows = [_row(query, i["title"], i["url"], i["source"], i["published"] or s.isoformat(),
                         i["snippet"], lang, origin) for i in items if i["title"]]
            got += len(rows)
            saved += insert_articles(rows)
            if progress:
                progress(step / total_steps, f"{origin} {s}~{e}: {len(rows)}건")
            time.sleep(config.REQUEST_SLEEP)
        stats[origin] = dict(fetched=got, new=saved, capped_slices=capped, slices=len(slices))
    return stats


# ---------------------------------------------------------------- Naver
def collect_naver(query: str, max_items: int = 1000, progress=None) -> dict:
    if not (config.NAVER_CLIENT_ID and config.NAVER_CLIENT_SECRET):
        return {"naver": "키 없음 - 건너뜀"}
    headers = {"X-Naver-Client-Id": config.NAVER_CLIENT_ID,
               "X-Naver-Client-Secret": config.NAVER_CLIENT_SECRET}
    got = saved = 0
    for start in range(1, min(max_items, 1000) + 1, 100):
        r = requests.get("https://openapi.naver.com/v1/search/news.json", headers=headers, timeout=20,
                         params=dict(query=query, display=100, start=start, sort="date"))
        r.raise_for_status()
        items = r.json().get("items", [])
        rows = []
        for i in items:
            link = i.get("originallink") or i.get("link")
            src = re.sub(r"^https?://(www\.)?", "", link or "").split("/")[0]
            rows.append(_row(query, clean(i["title"]), link, src, _to_date(i.get("pubDate", "")),
                             clean(i.get("description"))[:300], "ko", "naver"))
        got += len(rows)
        saved += insert_articles(rows)
        if progress:
            progress(min(1.0, start / max_items), f"naver {start}~{start + 99}")
        if len(items) < 100:
            break
        time.sleep(0.2)
    return {"naver": dict(fetched=got, new=saved)}


# ---------------------------------------------------------------- BigKinds
def import_bigkinds(query: str, file) -> dict:
    """빅카인즈 '뉴스 분석 > 검색 결과 엑셀 다운로드' 파일 가져오기."""
    import pandas as pd
    df = pd.read_excel(file)
    col = {c.strip(): c for c in df.columns}

    def pick(*names):
        for n in names:
            if n in col:
                return col[n]
        return None

    c_date, c_src, c_title = pick("일자"), pick("언론사"), pick("제목")
    c_body, c_url, c_kw = pick("본문"), pick("URL"), pick("키워드")
    if not (c_date and c_title):
        raise ValueError(f"빅카인즈 형식이 아닙니다. 컬럼: {list(df.columns)[:10]}")
    rows = []
    for _, r in df.iterrows():
        d = str(r[c_date]).split(".")[0]
        try:
            pub = datetime.strptime(d[:8], "%Y%m%d").date().isoformat()
        except ValueError:
            pub = str(r[c_date])[:10]
        snippet = str(r[c_body])[:300] if c_body and pd.notna(r[c_body]) else ""
        if not snippet and c_kw and pd.notna(r[c_kw]):
            snippet = str(r[c_kw])[:300]
        rows.append(_row(query, str(r[c_title]), str(r[c_url]) if c_url else "",
                         str(r[c_src]) if c_src else "", pub, snippet, "ko", "bigkinds"))
    return {"bigkinds": dict(fetched=len(rows), new=insert_articles(rows))}
