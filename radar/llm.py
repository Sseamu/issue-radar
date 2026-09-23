"""LLM 클라이언트: 로컬 Ollama(기본) / Anthropic / OpenAI 호환(Gemini·vLLM 등). JSON 응답을 강제한다."""
import json
import re

import requests

from . import config


def _extract_json(text: str):
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip()
    m = re.search(r"```(?:json)?\s*(.*?)```", text, flags=re.S)
    if m:
        text = m.group(1)
    start = min([i for i in (text.find("{"), text.find("[")) if i >= 0], default=0)
    return json.loads(text[start:])


# 달러/100만 토큰 (입력, 출력) - 비용 추적용. 가격 변동 시 수정
PRICES = {"claude-haiku-4-5": (1, 5), "claude-sonnet-5": (2, 10), "claude-sonnet-4-6": (3, 15),
          "claude-opus-5-5": (4, 20)}


def _log_usage(model: str, tin: int, tout: int):
    try:
        from .db import log_usage
        pin, pout = PRICES.get(model, (0, 0))
        log_usage(model, tin, tout, (tin * pin + tout * pout) / 1e6)
    except Exception:
        pass


def chat(system: str, user: str, provider: str | None = None, temperature: float = 0.2,
         max_tokens: int = 4000, fast: bool = False) -> str:
    """fast=True: 의도 파악·짧은 요약 등 가벼운 작업은 저렴한 모델 사용."""
    p = provider or config.LLM_PROVIDER
    if p == "ollama":
        r = requests.post(f"{config.OLLAMA_URL}/api/chat", timeout=600, json=dict(
            model=config.OLLAMA_MODEL, stream=False, format="json", think=False,
            options=dict(temperature=temperature, num_ctx=16384, num_predict=max_tokens),
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}]))
        r.raise_for_status()
        return r.json()["message"]["content"]
    if p == "anthropic":
        import anthropic
        model = config.ANTHROPIC_MODEL_FAST if fast else config.ANTHROPIC_MODEL
        msg = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY, max_retries=4).messages.create(
            model=model, max_tokens=max_tokens, temperature=temperature,
            system=system + "\n반드시 JSON만 출력하라.", messages=[{"role": "user", "content": user}])
        _log_usage(model, msg.usage.input_tokens, msg.usage.output_tokens)
        return "".join(b.text for b in msg.content if b.type == "text")
    if p == "openai":
        r = requests.post(f"{config.OPENAI_BASE_URL}/chat/completions", timeout=300,
                          headers={"Authorization": f"Bearer {config.OPENAI_API_KEY}"},
                          json=dict(model=config.OPENAI_MODEL, temperature=temperature, max_tokens=max_tokens,
                                    response_format={"type": "json_object"},
                                    messages=[{"role": "system", "content": system},
                                              {"role": "user", "content": user}]))
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"]
    raise ValueError(f"알 수 없는 LLM_PROVIDER: {p}")


def chat_json(system: str, user: str, provider: str | None = None, retries: int = 2, **kw):
    last = None
    for _ in range(retries + 1):
        raw = chat(system, user, provider, **kw)
        try:
            return _extract_json(raw)
        except Exception as ex:
            last = ex
            user += "\n\n(이전 응답이 올바른 JSON이 아니었다. 설명 없이 JSON 객체 하나만 출력하라.)"
    raise ValueError(f"LLM JSON 파싱 실패: {last}")
