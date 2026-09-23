"""명령줄 실행: python cli.py "삼성전자" --en "Samsung Electronics" [--forecast]"""
import argparse
import json

from radar import analyze, collect, db
from radar.topics import dedupe, extract_topics

ap = argparse.ArgumentParser()
ap.add_argument("query")
ap.add_argument("--en", default="", help="해외 메이저 언론용 영문 키워드")
ap.add_argument("--months", type=int, default=12)
ap.add_argument("--skip-collect", action="store_true")
ap.add_argument("--forecast", action="store_true")
a = ap.parse_args()

if not a.skip_collect:
    print(collect.collect_google(a.query, a.months, a.en, progress=lambda p, m: print(f"{p:5.0%} {m}")))
    print(collect.collect_naver(a.query))
res = extract_topics(dedupe(db.load_articles(a.query)))
print(f"토픽 {res['k']}개, 기사 {len(res['articles'])}건")
b = analyze.brief(a.query, res)
print(json.dumps(b, ensure_ascii=False, indent=2))
if a.forecast:
    for p in analyze.forecast(a.query, res, b):
        print(f"[{p['horizon']}] {p['probability']:.0%}  {p['statement']}  (기한 {p['due_date']})")
