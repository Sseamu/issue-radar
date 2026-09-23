"""쟁점(토픽) 추출.

BERTopic과 같은 원리(임베딩 → 군집 → c-TF-IDF 키워드 → 시간축 집계)를 직접 구현했다.
이유: BERTopic은 hdbscan/umap 의존성 때문에 Windows에서 설치가 자주 깨지고,
뉴스 제목(평균 30~40자) 수천 건 규모에서는 KMeans로도 충분하다.
"""
import re
from collections import Counter

import numpy as np
import pandas as pd
from sklearn.cluster import KMeans
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics import silhouette_score
from sklearn.metrics.pairwise import cosine_similarity

from . import config
from .db import mark_dups

# ---------------------------------------------------------------- 토크나이저
_kiwi = None
STOP = set("기자 뉴스 오늘 이번 관련 지난 올해 내년 때문 가운데 대해 통해 위해 경우 이후 전망 발표 대한 "
           "것 등 및 수 중 명 개 년 월 일 억 조 만 원 달러 the a an of to in and for on with is as by at".split())


def tokenize(text: str) -> list[str]:
    global _kiwi
    try:
        if _kiwi is None:
            from kiwipiepy import Kiwi
            _kiwi = Kiwi()
        toks = [t.form for t in _kiwi.tokenize(text) if t.tag in ("NNG", "NNP", "SL", "SH")]
    except ImportError:
        toks = re.findall(r"[가-힣]{2,}|[A-Za-z]{3,}", text)
    return [t.lower() for t in toks if len(t) > 1 and t.lower() not in STOP]


# ---------------------------------------------------------------- 중복 제거
def dedupe(df: pd.DataFrame, threshold: float = 0.8) -> pd.DataFrame:
    """제목 문자 n-gram 코사인 유사도 ≥ threshold 이고 7일 이내면 같은 기사(재전송/받아쓰기)로 본다.
    대표 기사에 'coverage'(몇 개 매체가 다뤘나)를 붙인다 → 중요도 신호."""
    if len(df) < 2:
        return df.assign(coverage=1)
    df = df.sort_values("published").reset_index(drop=True)
    X = TfidfVectorizer(analyzer="char_wb", ngram_range=(2, 3)).fit_transform(df["title"])
    days = df["published"].values.astype("datetime64[D]").astype(np.int64)
    parent = np.arange(len(df))
    cov = np.ones(len(df), dtype=int)
    for s in range(0, len(df), 1000):  # 메모리 보호용 청크
        sim = cosine_similarity(X[s:s + 1000], X)
        for li, row in enumerate(sim):
            i = s + li
            if parent[i] != i:
                continue
            cand = np.where((row >= threshold) & (np.arange(len(df)) > i) & (np.abs(days - days[i]) <= 7))[0]
            for j in cand:
                if parent[j] == j:
                    parent[j] = i
                    cov[i] += 1
    dup_mask = parent != np.arange(len(df))
    if "id" in df and dup_mask.any():
        mark_dups(df.loc[dup_mask, "id"].astype(int).tolist())
    return df.loc[~dup_mask].assign(coverage=cov[~dup_mask]).reset_index(drop=True)


# ---------------------------------------------------------------- 임베딩
_ST = None


def _st_model():
    """임베딩 모델은 한 번만 로드해 재사용 (주제마다 로드하면 10~20초씩 낭비)."""
    global _ST
    if _ST is None:
        from sentence_transformers import SentenceTransformer
        import torch
        _ST = SentenceTransformer(config.EMBED_MODEL, device="cuda" if torch.cuda.is_available() else "cpu")
    return _ST


def embed(texts: list[str]) -> np.ndarray:
    try:
        if config.EMBED_BACKEND == "tfidf":
            raise RuntimeError("EMBED_BACKEND=tfidf")
        model = _st_model()
        return model.encode(texts, batch_size=64, normalize_embeddings=True, show_progress_bar=False)
    except Exception as ex:  # 모델/torch 미설치 시 TF-IDF+SVD로 대체 (품질 ↓, 동작은 함)
        if config.EMBED_BACKEND != "tfidf":
            print(f"[embed] sentence-transformers 사용 불가 → TF-IDF 대체: {ex}")
        from sklearn.decomposition import TruncatedSVD
        X = TfidfVectorizer(tokenizer=tokenize, lowercase=False, token_pattern=None, min_df=2).fit_transform(texts)
        k = max(2, min(100, X.shape[1] - 1, X.shape[0] - 1))
        Z = TruncatedSVD(n_components=k, random_state=0).fit_transform(X)
        return Z / (np.linalg.norm(Z, axis=1, keepdims=True) + 1e-9)


# ---------------------------------------------------------------- 군집 + 키워드
def _choose_k(E: np.ndarray, kmin: int, kmax: int) -> int:
    best_k, best_s = kmin, -1
    sample = min(len(E), 3000)
    for k in range(kmin, kmax + 1):
        labels = KMeans(k, n_init=5, random_state=0).fit_predict(E)
        s = silhouette_score(E, labels, sample_size=sample, random_state=0)
        if s > best_s:
            best_k, best_s = k, s
    return best_k


def c_tfidf_keywords(docs_per_topic: dict[int, list[str]], topn: int = 10) -> dict[int, list[str]]:
    """클래스 기반 TF-IDF: 토픽 하나를 문서 하나로 보고 그 토픽에만 특징적인 단어를 뽑는다."""
    tf = {t: Counter(w for d in docs for w in tokenize(d)) for t, docs in docs_per_topic.items()}
    df_ = Counter(w for c in tf.values() for w in c)
    n = len(tf)
    avg = np.mean([sum(c.values()) for c in tf.values()]) or 1
    out = {}
    for t, c in tf.items():
        # 모든 토픽에 등장하는 단어(검색어 자체 등)는 변별력이 없으므로 제외
        scores = {w: f * np.log(1 + avg / (df_[w] * 1.0)) * np.log(1 + n / df_[w])
                  for w, f in c.items() if n <= 2 or df_[w] < n}
        out[t] = [w for w, _ in sorted(scores.items(), key=lambda x: -x[1])[:topn]]
    return out


def extract_topics(df: pd.DataFrame, k: int | None = None) -> dict:
    """반환: articles(토픽 번호 포함), topics(키워드/대표기사/통계), timeline(월×토픽 건수)."""
    df = df.dropna(subset=["published"]).copy()
    if len(df) < 20:
        raise ValueError(f"기사 수가 너무 적습니다({len(df)}건). 최소 20건 이상 필요.")
    texts = (df["title"] + ". " + df["snippet"].fillna("").str[:120]).tolist()
    E = embed(texts)
    if k is None:
        kmax = int(np.clip(np.sqrt(len(df) / 8), 4, 15))
        k = _choose_k(E, 4, kmax)
    km = KMeans(k, n_init=10, random_state=0).fit(E)
    df["topic"] = km.labels_
    df["centrality"] = (E * km.cluster_centers_[km.labels_]).sum(1)

    kw = c_tfidf_keywords({t: g["title"].tolist() for t, g in df.groupby("topic")})
    df["month"] = df["published"].dt.to_period("M").astype(str)
    months = sorted(df["month"].unique())
    timeline = df.pivot_table(index="month", columns="topic", values="title", aggfunc="count",
                              fill_value=0).reindex(months, fill_value=0)
    share = timeline.div(timeline.sum(1).replace(0, 1), axis=0)

    topics = []
    for t, g in df.groupby("topic"):
        # 대표 기사: 중심에 가깝고 + 많은 매체가 다룬 기사
        score = g["centrality"] + 0.05 * np.log1p(g.get("coverage", pd.Series(1, index=g.index)))
        reps = g.loc[score.sort_values(ascending=False).index[:8]]
        s = share[t]
        recent, prior = s.iloc[-2:].mean(), s.iloc[:-2].mean() if len(s) > 2 else s.mean()
        peak_month = timeline[t].idxmax()
        topics.append(dict(
            topic=int(t), size=int(len(g)), keywords=kw[t],
            first=g["published"].min().date().isoformat(), last=g["published"].max().date().isoformat(),
            peak_month=peak_month, peak_count=int(timeline[t].max()),
            share_recent=round(float(recent), 3), share_prior=round(float(prior), 3),
            momentum=round(float((recent + 1e-3) / (prior + 1e-3)), 2),  # >1.5 부상, <0.67 소멸
            foreign_ratio=round(float((g["lang"] == "en").mean()), 2),
            samples=[dict(date=r.published.date().isoformat(), title=r.title, source=r.source, url=r.url)
                     for r in reps.itertuples()],
        ))
    topics.sort(key=lambda x: -x["size"])
    return dict(articles=df, topics=topics, timeline=timeline, k=k)
