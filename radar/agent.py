"""Slack 에이전트 로직: 의도 파악 → 작업 실행 → Slack용 텍스트 생성.
bot.py(Slack 입출력)와 분리해 두어 Slack 없이도 테스트할 수 있다."""
import re
from datetime import datetime, timedelta

import numpy as np
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity

from . import analyze, collect, config, db
from .llm import chat_json
from .topics import dedupe, extract_topics

HELP = """*사용법*
• `@radar 삼성전자` : 최근 이슈 → 1년 쟁점 → 1·3·6·12개월 예측 (스레드로 순차 답변)
• `@radar 삼성전자 / Samsung Electronics` : `/` 뒤에 영문을 쓰면 해외 메이저 언론 포함
• 분석 스레드 안에서 `@radar 노조 이슈 더 자세히` : 수집한 기사 근거로 후속 답변
• `@radar 브리핑` : 아침 브리핑 지금 받기
• `@radar 성적` : 예측 적중 성적(Brier)
• `@radar 비용` : 이번 달 LLM API 사용액
• `@radar 관심` / `관심 추가 반도체/semiconductor` / `관심 삭제 반도체` : 매일 자동으로 챙겨 볼 관심 키워드
• `@radar 도움말`
_PC가 꺼져 있을 때 남긴 요청은 다음 날 아침 배치(06:00)에서 처리합니다. 접수되면 :eyes:, 완료되면 :white_check_mark: 반응이 달립니다._"""

STATUS_ICON = {"부상": ":arrow_up:", "지속": ":left_right_arrow:", "소멸": ":arrow_down:", "일회성": ":zap:"}


# ---------------------------------------------------------------- 의도 파악
def parse_intent(text: str, in_thread_query: str | None = None) -> dict:
    t = re.sub(r"<@[A-Z0-9]+>", "", text).strip()
    if not t or t in ("도움말", "help", "?"):
        return {"intent": "help"}
    if re.fullmatch(r"(오늘\s*)?(아침\s*)?(브리핑|뉴스)(\s*보여줘)?", t):
        return {"intent": "brief"}
    m = re.fullmatch(r"관심(?:\s*키워드)?\s*(추가|등록)\s+(.+)", t)
    if m:
        return {"intent": "watch_add", "spec": m.group(2).strip()}
    m = re.fullmatch(r"관심(?:\s*키워드)?\s*(삭제|제거)\s+(.+)", t)
    if m:
        return {"intent": "watch_remove", "q": m.group(2).strip()}
    if re.fullmatch(r"관심(?:\s*키워드)?(?:\s*목록)?", t):
        return {"intent": "watch_list"}
    if re.fullmatch(r"(api\s*)?(비용|사용량|요금)", t, flags=re.I):
        return {"intent": "cost"}
    if re.fullmatch(r"(예측\s*)?(성적|점수|brier)", t, flags=re.I):
        return {"intent": "score"}
    if in_thread_query:
        return {"intent": "followup", "query": in_thread_query, "question": t}
    if "/" in t:  # "삼성전자 / Samsung Electronics"
        ko, en = [x.strip() for x in t.split("/", 1)]
        return {"intent": "analyze", "query": ko, "en": en}
    if len(t) <= 20 and "?" not in t:  # 짧으면 키워드 그대로
        return {"intent": "analyze", "query": t, "en": ""}
    try:  # 문장형이면 LLM으로 키워드 추출 ("요즘 전세사기 문제 어떻게 돼가?")
        r = chat_json("사용자 요청에서 뉴스 검색 키워드를 뽑는다.",
                      f'요청: "{t}"\nJSON: {{"query": "한국어 검색 키워드(2~10자)", '
                      f'"en": "해외 언론 검색용 영문 키워드(국제적 주제일 때만, 아니면 빈 문자열)"}}', fast=True)
        return {"intent": "analyze", "query": r.get("query") or t[:20], "en": r.get("en", "")}
    except Exception:
        return {"intent": "analyze", "query": t[:20], "en": ""}


# ---------------------------------------------------------------- 분석 단계 (각 단계 결과를 Slack 텍스트로 반환)
def step_collect(query: str, en: str, ko_query: str | None = None, foreign_sites: list[str] | None = None) -> str:
    prev = db.latest_report(query, "collect")
    fresh = prev and datetime.fromisoformat(prev["_created_at"]) > datetime.now() - timedelta(hours=20)
    if fresh:
        return f"_최근 20시간 내 수집 이력이 있어 기존 데이터를 사용합니다._"
    months = 1 if prev else config.ANALYZE_MONTHS  # 이미 수집 이력이 있으면 최근 1개월만 추가
    stats = collect.collect_google(query, months, en, ko_query=ko_query, foreign_sites=foreign_sites)
    stats |= collect.collect_naver(query)
    db.save_report(query, "collect", stats)
    n = len(db.load_articles(query, include_dups=True))
    capped = sum(v.get("capped_slices", 0) for v in stats.values() if isinstance(v, dict))
    msg = f"_수집 완료: 누적 {n:,}건 ({months}개월 구간 조회)_"
    if capped:
        msg += (f"\n_{capped}/{sum(v.get('slices', 0) for v in stats.values() if isinstance(v, dict))}개 구간이 "
                f"구간당 100건 상한에 도달 → 보도량이 많은 키워드라 관련도 상위 기사 위주로 표본 수집됨_")
    return msg


def step_recent(query: str, days: int = 7) -> str:
    df = db.load_articles(query)
    recent = df[df["published"] >= df["published"].max() - np.timedelta64(days, "D")]
    if recent.empty:
        return "최근 기사가 없습니다."
    titles = "\n".join(f"- {r.published.date()} {r.title} ({r.source})" for r in recent.tail(60).itertuples())
    r = chat_json(analyze.SYS_ANALYST,
                  f'"{query}" 최근 {days}일 기사:\n{titles}\n\nJSON: {{"headline": "지금 가장 중요한 이슈 한 문장", '
                  f'"bullets": ["핵심 이슈 3~5개, 각 1문장"]}}', fast=True)
    out = f"*[1/3] 최근 {days}일 이슈* ({len(recent)}건)\n*{r.get('headline', '')}*\n"
    out += "\n".join(f"• {b}" for b in r.get("bullets", []))
    return out


def step_topics(query: str) -> tuple[str, dict, dict]:
    df = db.load_articles(query)
    df = df[df["published"] >= df["published"].max() - np.timedelta64(int(config.ANALYZE_MONTHS * 30.5), "D")]
    res = extract_topics(dedupe(df))
    brief = analyze.brief(query, res)
    stats = {t["topic"]: t for t in res["topics"]}
    ov = brief.get("overall", {})
    lines = [f"*[2/3] 최근 {config.ANALYZE_MONTHS}개월 쟁점 흐름* (기사 {len(res['articles']):,}건 → 쟁점 {res['k']}개)", ov.get("summary", ""), ""]
    for t in sorted(brief.get("topics", []), key=lambda x: -stats.get(x.get("topic"), {}).get("size", 0)):
        s = stats.get(t.get("topic"), {})
        spark = _spark(res["timeline"][t["topic"]].tolist()) if t.get("topic") in res["timeline"] else ""
        lines.append(f"{STATUS_ICON.get(t.get('status'), '•')} *{t.get('label')}* "
                     f"`{s.get('size', '?')}건` `정점 {s.get('peak_month', '?')}` `{spark}`")
        lines.append(f"      {t.get('summary', '')}")
        if s.get("samples"):
            smp = s["samples"][0]
            lines.append(f"      <{smp['url']}|{smp['title'][:40]}> ({smp['date']})")
    if ov.get("data_caveats"):
        lines.append(f"\n_데이터 한계: {ov['data_caveats']}_")
    return "\n".join(lines), res, brief


def step_forecast(query: str, res: dict, brief: dict) -> str:
    preds = analyze.forecast(query, res, brief)
    sc = db.latest_report(query, "scenarios") or {}
    lines = ["*[3/3] 향후 전망* _(확률은 모델 추정치 · 기한이 되면 채점 알림이 옵니다)_"]
    for h, name in (("1m", "1개월"), ("3m", "3개월"), ("6m", "6개월"), ("12m", "1년")):
        ps = [p for p in preds if p["horizon"] == h]
        if ps:
            lines.append(f"\n*{name}* (~{ps[0]['due_date']})")
            lines += [f"• `{p['probability']:.0%}` {p['statement']}" for p in ps]
    if sc:
        lines.append(f"\n*시나리오*\n• 기본: {sc.get('base', '')}\n• 긍정: {sc.get('bull', '')}\n• 부정: {sc.get('bear', '')}")
    return "\n".join(lines)


def _spark(vals: list[int]) -> str:
    bars = "▁▂▃▄▅▆▇█"
    m = max(vals) or 1
    return "".join(bars[min(7, int(v / m * 7))] for v in vals[-12:])


# ---------------------------------------------------------------- 후속 질문 (수집 기사 기반 RAG)
def followup(query: str, question: str, k: int = 40) -> str:
    df = db.load_articles(query)
    if df.empty:
        return "이 스레드의 수집 데이터가 없습니다."
    docs = (df["title"] + " " + df["snippet"].fillna("")).tolist()
    vec = TfidfVectorizer(analyzer="char_wb", ngram_range=(2, 3)).fit(docs + [question])
    sim = cosine_similarity(vec.transform([question]), vec.transform(docs))[0]
    top = df.iloc[np.argsort(-sim)[:k]].sort_values("published")
    ctx = "\n".join(f"[{i}] {r.published.date()} {r.title} ({r.source})" for i, r in enumerate(top.itertuples(), 1))
    r = chat_json(analyze.SYS_ANALYST,
                  f'주제 "{query}"에 대한 질문: {question}\n\n관련 기사:\n{ctx}\n\n'
                  'JSON: {"answer": "기사 근거로 답변(5문장 이내, 근거 기사 번호를 [3]처럼 표기)", "cited": [번호들]}')
    urls = {i: (r_.title, r_.url) for i, r_ in enumerate(top.itertuples(), 1)}
    refs = [f"[{i}] <{urls[i][1]}|{urls[i][0][:40]}>" for i in r.get("cited", [])[:5] if i in urls]
    return r.get("answer", "") + ("\n" + "\n".join(refs) if refs else "")


def score_text() -> str:
    rep = analyze.brier_report()
    preds = db.load_predictions()
    open_n = int(preds["outcome"].isna().sum()) if not preds.empty else 0
    if rep.empty:
        return f"아직 채점된 예측이 없습니다. (대기 중 예측 {open_n}건)\nBrier 0.25 = 동전 던지기 수준, 0.10 이하 = 우수"
    return f"*예측 성적* (대기 {open_n}건)\n```{rep.to_string()}```\n_Brier 0.25 = 동전 던지기, 0.10 이하 = 우수_"


def cost_text() -> str:
    u = db.usage_summary()
    if u.empty:
        return "기록된 API 사용량이 없습니다 (로컬 LLM 사용 중이거나 아직 호출 없음)."
    this = u[u["month"] == u["month"].max()]
    return (f"*{this['month'].iloc[0]} LLM 사용액: ${this['usd'].sum():.2f}* "
            f"(약 {this['usd'].sum() * 1400:,.0f}원)\n```{u.head(12).to_string(index=False)}```")
