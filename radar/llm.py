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
        return _ollama(system, user, temperature, max_tokens)
    if p == "claude_code":
        return _claude_code(system, user)
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


OLLAMA_CTX = [int(x) for x in config.env("OLLAMA_CTX", "16384,8192").split(",")]


class OllamaError(RuntimeError):
    pass


def _ollama(system: str, user: str, temperature: float, max_tokens: int) -> str:
    """Ollama 호출. 500(대개 GPU 메모리 부족·모델 로딩 중 러너 종료)이나 연결 끊김이면
    잠시 쉬었다 재시도하고, 두 번째부터는 컨텍스트를 줄여(메모리 ↓) 다시 부른다."""
    import time
    last = ""
    waits = [0, 20, 60]
    for i, wait in enumerate(waits):
        if wait:
            time.sleep(wait)
        ctx = OLLAMA_CTX[min(i, len(OLLAMA_CTX) - 1)]
        try:
            r = requests.post(f"{config.OLLAMA_URL}/api/chat", timeout=600, json=dict(
                model=config.OLLAMA_MODEL, stream=False, format="json", think=False, keep_alive="30m",
                options=dict(temperature=temperature, num_ctx=ctx, num_predict=max_tokens),
                messages=[{"role": "system", "content": system}, {"role": "user", "content": user}]))
        except (requests.ConnectionError, requests.Timeout) as e:
            last = f"연결 실패: {type(e).__name__}"
            continue
        if r.ok:
            return r.json()["message"]["content"]
        try:
            detail = r.json().get("error", "")
        except Exception:
            detail = r.text[:200]
        last = f"HTTP {r.status_code}: {detail}"
        print(f"[ollama] {last} (시도 {i + 1}/{len(waits)}, num_ctx={ctx})")
        if r.status_code == 404:
            last += " → 이 Ollama 서버에 모델이 없음 (PowerShell에서 `ollama list` 확인, 모델 저장 위치가 다르면 .env 에 OLLAMA_MODELS)"
        if r.status_code < 500:
            break
    raise OllamaError(last)


def _claude_code(system: str, user: str) -> str:
    """PC에 설치된 Claude Code CLI(구독 계정)로 실행. API 비용 없음.
    인증: .env 의 CLAUDE_CODE_OAUTH_TOKEN(`claude setup-token`으로 발급) 또는 PC에서 claude 로그인 상태."""
    import os
    import shutil
    import subprocess
    exe = config.env("CLAUDE_EXE") or shutil.which("claude") or shutil.which("claude.cmd")
    if not exe:
        raise RuntimeError("claude CLI 없음 (npm install -g @anthropic-ai/claude-code)")
    env = os.environ.copy()
    if config.env("CLAUDE_CODE_OAUTH_TOKEN"):
        env["CLAUDE_CODE_OAUTH_TOKEN"] = config.env("CLAUDE_CODE_OAUTH_TOKEN")
    cmd = [exe, "-p", "--output-format", "text", "--max-turns", "1",
           "--append-system-prompt", system + "\n도구를 쓰지 말고, 설명 없이 JSON 객체 하나만 출력하라."]
    if config.env("CLAUDE_BRIEF_MODEL"):
        cmd += ["--model", config.env("CLAUDE_BRIEF_MODEL")]
    r = subprocess.run(cmd, input=user, capture_output=True, text=True, encoding="utf-8", errors="replace",
                       timeout=int(config.env("CLAUDE_TIMEOUT", "420")), env=env)
    if r.returncode != 0 or not r.stdout.strip():
        raise RuntimeError(f"claude CLI 실패({r.returncode}): {(r.stderr or r.stdout).strip()[:200]}")
    return r.stdout


def chat_json_prefer(provider: str | None, system: str, user: str, **kw):
    """provider(예: claude_code)를 먼저 쓰고, 실패하면 기본 LLM(.env 의 LLM_PROVIDER)으로 다시 시도."""
    if provider and provider != config.LLM_PROVIDER:
        try:
            return chat_json(system, user, provider=provider, **kw)
        except Exception as e:
            print(f"[llm] {provider} 실패 → {config.LLM_PROVIDER} 로 대체: {str(e)[:200]}")
    return chat_json(system, user, **kw)


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
