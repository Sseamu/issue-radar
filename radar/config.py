"""설정: .env 파일 또는 환경변수에서 읽는다."""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
except ImportError:
    pass


def env(key: str, default: str = "") -> str:
    return os.getenv(key, default).strip()


DB_PATH = Path(env("RADAR_DB", str(ROOT / "data" / "radar.db")))

# 수집
NAVER_CLIENT_ID = env("NAVER_CLIENT_ID")
NAVER_CLIENT_SECRET = env("NAVER_CLIENT_SECRET")
SLICE_DAYS = int(env("SLICE_DAYS", "7"))           # Google News RSS 기간 분할 단위(쿼리당 최대 ~100건)
ANALYZE_MONTHS = int(env("ANALYZE_MONTHS", "12"))   # 키워드 분석 기간(개월)
REQUEST_SLEEP = float(env("REQUEST_SLEEP", "1.0"))  # 요청 간 대기(초) - 차단 방지

# 해외 메이저 언론 (Google News site: 필터)
FOREIGN_SITES = [s for s in env(
    "FOREIGN_SITES",
    "reuters.com,bloomberg.com,ft.com,wsj.com,nytimes.com,cnbc.com,apnews.com,bbc.com,economist.com,nikkei.com",
).split(",") if s]

# 분석
EMBED_MODEL = env("EMBED_MODEL", "BAAI/bge-m3")  # 한/영 혼합 임베딩, GPU 권장
EMBED_BACKEND = env("EMBED_BACKEND", "auto")     # auto | local | tfidf (서버 RAM 1GB면 tfidf)

# LLM: ollama | anthropic | openai (OpenAI 호환 엔드포인트: Gemini, vLLM 등)
LLM_PROVIDER = env("LLM_PROVIDER", "ollama")
OLLAMA_URL = env("OLLAMA_URL", "http://localhost:11434")
OLLAMA_MODEL = env("OLLAMA_MODEL", "qwen3:14b")
ANTHROPIC_API_KEY = env("ANTHROPIC_API_KEY")
ANTHROPIC_MODEL = env("ANTHROPIC_MODEL", "claude-sonnet-5")          # 쟁점 분석·예측
ANTHROPIC_MODEL_FAST = env("ANTHROPIC_MODEL_FAST", "claude-haiku-4-5")  # 의도 파악·짧은 요약·브리핑
OPENAI_API_KEY = env("OPENAI_API_KEY")
OPENAI_BASE_URL = env("OPENAI_BASE_URL", "https://api.openai.com/v1")
OPENAI_MODEL = env("OPENAI_MODEL", "gpt-4o-mini")

# Slack
SLACK_BOT_TOKEN = env("SLACK_BOT_TOKEN")          # xoxb-...
SLACK_APP_TOKEN = env("SLACK_APP_TOKEN")          # xapp-... (Socket Mode)
SLACK_BRIEF_CHANNEL = env("SLACK_BRIEF_CHANNEL", "#daily-brief")
SLACK_ALERT_CHANNEL = env("SLACK_ALERT_CHANNEL", "#radar")   # 예측 채점 알림
