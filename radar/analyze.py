"""LLM 기반 요약·쟁점 명명·예측·채점."""
from datetime import date, timedelta

import pandas as pd

from .db import load_predictions, save_predictions, save_report
from .llm import chat_json

HORIZON_DAYS = {"1m": 30, "3m": 91, "6m": 182, "12m": 365}

SYS_ANALYST = (
    "너는 뉴스 데이터를 분석하는 한국어 리서치 애널리스트다. 주어진 기사 제목·통계만 근거로 쓰고, "
    "근거에 없는 사실을 지어내지 마라. 날짜는 YYYY-MM-DD 또는 YYYY-MM 형식으로 쓴다."
)


def _topic_block(topics: list[dict], n_samples: int = 6) -> str:
    lines = []
    for t in topics:
        lines.append(
            f"[토픽 {t['topic']}] 기사 {t['size']}건 | 기간 {t['first']}~{t['last']} | 정점 {t['peak_month']}"
            f"({t['peak_count']}건) | 최근2개월 비중 {t['share_recent']:.0%} vs 이전 {t['share_prior']:.0%}"
            f" (모멘텀 {t['momentum']}) | 해외비중 {t['foreign_ratio']:.0%}\n"
            f"  키워드: {', '.join(t['keywords'])}\n"
            + "\n".join(f"  - {s['date']} {s['title']} ({s['source']})" for s in t["samples"][:n_samples])
        )
    return "\n".join(lines)


def brief(query: str, result: dict, recent_days: int = 7) -> dict:
    df = result["articles"]
    cutoff = df["published"].max() - pd.Timedelta(days=recent_days)
    recent = df[df["published"] >= cutoff].sort_values(["coverage", "published"], ascending=False).head(40)
    recent_txt = "\n".join(f"- {r.published.date()} {r.title} ({r.source}, {r.coverage}개 매체)"
                           for r in recent.itertuples())
    user = f"""분석 대상: "{query}"   오늘: {date.today()}   전체 기사(중복 제거 후): {len(df)}건

## 최근 {recent_days}일 주요 기사 (다룬 매체 수 많은 순)
{recent_txt or '(없음)'}

## 지난 기간 쟁점 토픽 (자동 군집 결과)
{_topic_block(result['topics'])}

아래 JSON 형식으로 답하라:
{{
 "now": {{"headline": "지금 가장 중요한 이슈 한 문장", "bullets": ["최근 이슈 요약 3~5개"]}},
 "topics": [{{"topic": 토픽번호, "label": "쟁점명(15자 이내)", "summary": "2~3문장 요약",
             "status": "부상|지속|소멸|일회성", "key_events": [{{"date": "YYYY-MM-DD", "event": "사건"}}]}}],
 "overall": {{"summary": "분석 기간 전체 흐름 요약 4~6문장",
             "top_issues": ["중요도 순 핵심 쟁점 5개(토픽 label 사용)"],
             "data_caveats": "데이터 한계(예: 제목 위주, 특정 기간 기사 부족 등)"}}
}}
status 판단: 모멘텀>1.5면 부상, <0.67이면 소멸 경향. 모든 토픽을 빠짐없이 포함하라."""
    out = chat_json(SYS_ANALYST, user)
    save_report(query, "brief", out)
    return out


SYS_FORECAST = (
    "너는 보정(calibration)이 잘 된 슈퍼예측가다. 원칙: "
    "(1) 먼저 기저율(비슷한 사건이 과거에 얼마나 자주 일어났나)을 생각하고, 이번 사례의 특수성으로 조정한다. "
    "(2) 예측 문장은 기한 시점에 '발생/미발생'을 명확히 판정할 수 있어야 한다(모호한 '관심이 커질 것' 금지). "
    "(3) 확률은 0.05~0.95 사이로, 과신하지 않는다. 기간이 길수록 불확실성이 커짐을 반영한다. "
    "(4) 뉴스 보도량은 사건 발생의 대리지표일 뿐임을 기억한다. "
    "(5) 주어진 데이터에 없는 사실을 지어내지 않는다."
)


def _s(x) -> str:
    """로컬 LLM이 문자열 자리에 리스트를 넣는 경우가 있어 문자열로 통일."""
    if isinstance(x, (list, tuple)):
        return "; ".join(str(i) for i in x)
    return "" if x is None else str(x)


def forecast(query: str, result: dict, brief_out: dict, per_horizon: int = 3) -> list[dict]:
    labels = {t["topic"]: t["label"] for t in brief_out.get("topics", [])}
    tl = result["timeline"].rename(columns=lambda c: labels.get(c, f"토픽{c}"))
    df = result["articles"]
    latest = df.sort_values("published", ascending=False).head(30)
    user = f"""대상: "{query}"   오늘: {date.today()}

## 쟁점별 월간 기사 수 (행=월, 열=쟁점)
{tl.to_string()}

## 쟁점 요약
{chr(10).join(f"- {t.get('label')} [{t.get('status')}]: {t.get('summary')}" for t in brief_out.get('topics', []))}

## 최근 기사 30건
{chr(10).join(f"- {r.published.date()} {r.title} ({r.source})" for r in latest.itertuples())}

기간 1m(1개월), 3m(3개월), 6m(6개월), 12m(12개월) 각각에 대해 예측 {per_horizon}개씩 작성하라.
JSON 형식:
{{"predictions": [{{"horizon": "1m|3m|6m|12m",
   "statement": "기한 내 발생 여부를 판정 가능한 구체적 사건 (예: 'OO가 X월 말까지 Y를 공식 발표한다')",
   "probability": 0.0~1.0,
   "basis": "근거(기저율 + 데이터상 신호) 2~3문장",
   "signposts": "확률을 올리거나 내릴 선행 신호"}}],
 "scenarios": {{"base": "기본 시나리오", "bull": "긍정 시나리오", "bear": "부정 시나리오"}}}}"""
    out = chat_json(SYS_FORECAST, user, temperature=0.4, max_tokens=5000)
    preds = []
    for p in out.get("predictions", []):
        h = p.get("horizon", "").strip()
        if h not in HORIZON_DAYS or not p.get("statement"):
            continue
        try:
            prob = min(0.99, max(0.01, float(p.get("probability", 0.5))))
        except (TypeError, ValueError):
            prob = 0.5
        preds.append(dict(horizon=h, statement=_s(p["statement"]), probability=prob, basis=_s(p.get("basis")),
                          signposts=_s(p.get("signposts")),
                          due_date=(date.today() + timedelta(days=HORIZON_DAYS[h])).isoformat()))
    save_predictions(query, preds)
    save_report(query, "scenarios", out.get("scenarios", {}))
    return preds


def suggest_resolution(pred: dict, recent_titles: list[str]) -> dict:
    """기한이 지난 예측에 대해 최근 기사로 발생 여부 '제안'. 최종 판정은 사람이 한다."""
    user = f"""예측: "{pred['statement']}" (작성 {pred['created_at'][:10]}, 기한 {pred['due_date']})
관련 최근 기사 제목:
{chr(10).join('- ' + t for t in recent_titles[:60])}

JSON: {{"verdict": "발생|미발생|불명확", "evidence": "판단 근거가 된 기사 제목 인용", "confidence": 0~1}}"""
    return chat_json(SYS_ANALYST, user)


def brier_report(query: str | None = None) -> pd.DataFrame:
    """Brier = (확률 - 실제결과)^2 평균. 0이 완벽, 0.25는 항상 50%라고 찍은 수준."""
    df = load_predictions(query)
    done = df[df["outcome"].notna()].copy()
    if done.empty:
        return pd.DataFrame()
    done["brier"] = (done["probability"] - done["outcome"]) ** 2
    rep = done.groupby("horizon").agg(n=("brier", "size"), brier=("brier", "mean"),
                                      hit_rate=("outcome", "mean"), avg_prob=("probability", "mean"))
    rep.loc["전체"] = [len(done), done["brier"].mean(), done["outcome"].mean(), done["probability"].mean()]
    return rep.round(3)
