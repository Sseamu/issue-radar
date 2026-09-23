"""Issue Radar - 실행: streamlit run app.py"""
from datetime import date

import pandas as pd
import plotly.express as px
import streamlit as st

from radar import analyze, collect, config, db
from radar.topics import dedupe, extract_topics

st.set_page_config(page_title="Issue Radar", layout="wide")
st.title("Issue Radar")
st.caption("키워드 → 1년 뉴스 수집 → 쟁점 추출 → 요약 → 1·3·6·12개월 예측 → 기한 도래 시 채점")

# ---------------------------------------------------------------- 사이드바
with st.sidebar:
    st.header("검색")
    known = db.list_queries()
    query = st.text_input("키워드 (국내)", value=known[0] if known else "", placeholder="예: 삼성전자, 전세사기")
    foreign_q = st.text_input("영문 키워드 (해외 메이저 언론용, 비우면 생략)", placeholder="예: Samsung Electronics")
    months = st.slider("수집 기간(개월)", 1, 24, 12)
    use_naver = st.checkbox("네이버 API로 최근 기사 보강", value=bool(config.NAVER_CLIENT_ID))
    st.divider()
    st.caption(f"LLM: **{config.LLM_PROVIDER}** / "
               f"{config.OLLAMA_MODEL if config.LLM_PROVIDER == 'ollama' else ''}")
    bk = st.file_uploader("빅카인즈 엑셀 가져오기(선택)", type=["xlsx"])

if not query:
    st.info("왼쪽에 키워드를 입력하세요.")
    st.stop()

c1, c2, c3 = st.columns(3)

if c1.button("① 뉴스 수집", width="stretch"):
    bar = st.progress(0.0)
    stats = collect.collect_google(query, months, foreign_q, progress=lambda p, m: bar.progress(p, m))
    if use_naver:
        stats |= collect.collect_naver(query)
    if bk is not None:
        stats |= collect.import_bigkinds(query, bk)
    bar.empty()
    st.success("수집 완료")
    st.json(stats)
    capped = sum(v.get("capped_slices", 0) for v in stats.values() if isinstance(v, dict))
    if capped:
        st.warning(f"{capped}개 구간이 100건 상한에 걸렸습니다 → .env에서 SLICE_DAYS를 7로 줄이면 더 많이 수집됩니다.")

raw = db.load_articles(query)
if raw.empty:
    st.warning("저장된 기사가 없습니다. ① 뉴스 수집을 누르세요.")
    st.stop()

if c2.button("② 쟁점 분석 + 요약", width="stretch"):
    with st.spinner("중복 제거 → 임베딩 → 군집 → LLM 요약 (수 분 소요)"):
        arts = dedupe(raw)
        res = extract_topics(arts)
        st.session_state["res"] = res
        st.session_state["brief"] = analyze.brief(query, res)

if c3.button("③ 미래 예측", width="stretch", disabled="res" not in st.session_state):
    with st.spinner("예측 생성 중"):
        analyze.forecast(query, st.session_state["res"], st.session_state["brief"])
    st.success("예측 저장 완료 → '예측·채점' 탭")

tab0, tab1, tab2, tab3 = st.tabs(["개요", "쟁점 타임라인", "최근 이슈", "예측·채점"])

# ---------------------------------------------------------------- 개요
with tab0:
    m = raw.assign(month=raw["published"].dt.to_period("M").astype(str))
    cnt = m.groupby(["month", "origin"]).size().reset_index(name="기사 수")
    k1, k2, k3 = st.columns(3)
    k1.metric("저장 기사", f"{len(raw):,}")
    k2.metric("기간", f"{raw['published'].min():%Y-%m-%d} ~ {raw['published'].max():%Y-%m-%d}")
    k3.metric("매체 수", raw["source"].nunique())
    st.plotly_chart(px.bar(cnt, x="month", y="기사 수", color="origin", title="월별 보도량"),
                    width="stretch")
    st.dataframe(raw["source"].value_counts().head(20).rename("건수"))

# ---------------------------------------------------------------- 쟁점
brief = st.session_state.get("brief") or db.latest_report(query, "brief")
with tab1:
    res = st.session_state.get("res")
    if not brief:
        st.info("② 쟁점 분석을 실행하세요.")
    else:
        ov = brief.get("overall", {})
        st.subheader("1년 흐름")
        st.write(ov.get("summary", ""))
        st.write("**핵심 쟁점:** " + " › ".join(ov.get("top_issues", [])))
        if ov.get("data_caveats"):
            st.caption("데이터 한계: " + ov["data_caveats"])
        labels = {t["topic"]: t["label"] for t in brief.get("topics", [])}
        if res is not None:
            tl = res["timeline"].rename(columns=lambda c: labels.get(c, f"토픽{c}"))
            long = tl.reset_index().melt(id_vars="month", var_name="쟁점", value_name="기사 수")
            st.plotly_chart(px.area(long, x="month", y="기사 수", color="쟁점", title="쟁점별 보도량 추이"),
                            width="stretch")
        stats = {t["topic"]: t for t in (res["topics"] if res else [])}
        for t in brief.get("topics", []):
            s = stats.get(t["topic"], {})
            head = f"{t.get('label')} · {t.get('status', '')}"
            if s:
                head += f" · {s['size']}건 · 모멘텀 {s['momentum']}"
            with st.expander(head):
                st.write(t.get("summary", ""))
                for e in t.get("key_events", []):
                    st.markdown(f"- `{e.get('date')}` {e.get('event')}")
                if s:
                    st.caption("키워드: " + ", ".join(s["keywords"]))
                    for smp in s["samples"][:5]:
                        st.markdown(f"- {smp['date']} [{smp['title']}]({smp['url']}) · {smp['source']}")

# ---------------------------------------------------------------- 최근 이슈
with tab2:
    if brief and brief.get("now"):
        st.subheader(brief["now"].get("headline", ""))
        for b in brief["now"].get("bullets", []):
            st.markdown(f"- {b}")
    recent = raw.sort_values("published", ascending=False).head(50)
    st.dataframe(recent[["published", "title", "source", "origin", "url"]], hide_index=True,
                 column_config={"url": st.column_config.LinkColumn("링크")})

# ---------------------------------------------------------------- 예측·채점
with tab3:
    sc = db.latest_report(query, "scenarios")
    if sc:
        a, b, c = st.columns(3)
        a.info("**기본**\n\n" + sc.get("base", ""))
        b.success("**긍정**\n\n" + sc.get("bull", ""))
        c.error("**부정**\n\n" + sc.get("bear", ""))
    preds = db.load_predictions(query)
    if preds.empty:
        st.info("③ 미래 예측을 실행하세요.")
    else:
        st.dataframe(preds[["horizon", "statement", "probability", "due_date", "outcome", "basis", "signposts"]],
                     hide_index=True, column_config={"probability": st.column_config.ProgressColumn(
                         "확률", min_value=0, max_value=1, format="%.2f")})
        due = preds[(preds["outcome"].isna()) & (preds["due_date"] <= date.today().isoformat())]
        st.subheader(f"채점 대기: {len(due)}건")
        for p in due.to_dict("records"):
            with st.container(border=True):
                st.write(f"**[{p['horizon']}] {p['statement']}** (예측확률 {p['probability']:.0%}, 기한 {p['due_date']})")
                x, y, z = st.columns([1, 1, 2])
                if z.button("LLM 판정 제안", key=f"s{p['id']}"):
                    titles = raw[raw["published"] >= pd.Timestamp(p["created_at"][:10])]["title"].tolist()
                    st.json(analyze.suggest_resolution(p, titles[::-1]))
                if x.button("발생 ✔", key=f"y{p['id']}"):
                    db.resolve_prediction(p["id"], 1); st.rerun()
                if y.button("미발생 ✘", key=f"n{p['id']}"):
                    db.resolve_prediction(p["id"], 0); st.rerun()
        rep = analyze.brier_report()
        st.subheader("누적 예측 성적 (전체 키워드)")
        if rep.empty:
            st.caption("아직 채점된 예측이 없습니다. Brier 0.25 = 동전 던지기, 0.1 이하 = 우수.")
        else:
            st.dataframe(rep)
