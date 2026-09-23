"""Slack 전송 (봇 토큰 사용)."""
from slack_sdk import WebClient

from . import config

_client = None


def client() -> WebClient:
    global _client
    if _client is None:
        if not config.SLACK_BOT_TOKEN:
            raise RuntimeError("SLACK_BOT_TOKEN이 없습니다 (.env 또는 GitHub Secrets 확인)")
        _client = WebClient(token=config.SLACK_BOT_TOKEN)
    return _client


def post(blocks: list[dict], text: str, channel: str, thread_ts: str | None = None) -> dict:
    # Slack은 메시지당 블록 50개 제한
    return client().chat_postMessage(channel=channel, blocks=blocks[:50], text=text,
                                     thread_ts=thread_ts, unfurl_links=False, unfurl_media=False).data


def md_blocks(text: str) -> list[dict]:
    """긴 mrkdwn 텍스트를 3000자 단위 section 블록으로 분할."""
    chunks, cur = [], ""
    for line in text.split("\n"):
        if len(cur) + len(line) > 2900:
            chunks.append(cur)
            cur = ""
        cur += line + "\n"
    if cur.strip():
        chunks.append(cur)
    return [{"type": "section", "text": {"type": "mrkdwn", "text": c}} for c in chunks]
