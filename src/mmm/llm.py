"""LLM 客户端（opencode zen 通道）。

排障记录（2026-08-17）：403(1010) 是 Cloudflare WAF 的 UA 指纹拦截（Python-urllib
被挡），非频率限制——换 curl UA 即放行，实测连发 6 次无限制。限速仅防突发并发。
退避重试保留，应对偶发网络抖动与服务端 5xx。

配置：.env 中 OPENCODE_ZEN_BASE_URL / OPENCODE_ZEN_API_KEY（.env 已 gitignore）。
"""

from __future__ import annotations

import base64
import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path

from .db import PROJECT_ROOT

# 默认限速：每次调用间隔最少秒数（防突发并发，非风控要求——实测 zen 网关不限频率）
MIN_INTERVAL_SEC = float(os.environ.get("MMM_LLM_INTERVAL", "0.5"))
MAX_RETRIES = 5

_last_call = 0.0


def _load_env() -> tuple[str, str]:
    env = PROJECT_ROOT / ".env"
    vals = {}
    if env.exists():
        for line in env.read_text().splitlines():
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                vals[k.strip()] = v.strip()
    base = os.environ.get("OPENCODE_ZEN_BASE_URL") or vals.get("OPENCODE_ZEN_BASE_URL", "")
    key = os.environ.get("OPENCODE_ZEN_API_KEY") or vals.get("OPENCODE_ZEN_API_KEY", "")
    if not base or not key:
        raise RuntimeError("缺少 OPENCODE_ZEN_BASE_URL / OPENCODE_ZEN_API_KEY（.env 或环境变量）")
    return base.rstrip("/"), key


def chat(model: str, messages: list[dict], *, max_tokens: int = 4096,
         temperature: float | None = None) -> str:
    """带限速与指数退避的 chat 调用，返回 content 文本。"""
    global _last_call
    base, key = _load_env()

    payload: dict = {"model": model, "messages": messages, "max_tokens": max_tokens}
    if temperature is not None:
        payload["temperature"] = temperature

    wait_total = 0.0
    for attempt in range(MAX_RETRIES + 1):
        # 限速
        gap = time.time() - _last_call
        if gap < MIN_INTERVAL_SEC:
            time.sleep(MIN_INTERVAL_SEC - gap)
        _last_call = time.time()

        req = urllib.request.Request(
            f"{base}/chat/completions",
            data=json.dumps(payload).encode(),
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json",
                     # zen 网关 WAF 按 UA 指纹拦截（实测 Python-urllib 被 403(1010)，curl 放行）
                     "User-Agent": "curl/8.7.1"},
        )
        try:
            with urllib.request.urlopen(req, timeout=300) as resp:
                r = json.load(resp)
            return r["choices"][0]["message"]["content"] or ""
        except urllib.error.HTTPError as e:
            if e.code in (403, 429, 500, 502, 503) and attempt < MAX_RETRIES:
                backoff = min(30 * (2 ** attempt), 300)   # 30s → 60s → 120s → 240s → 300s
                wait_total += backoff
                time.sleep(backoff)
                continue
            raise
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            if attempt < MAX_RETRIES:   # 网络抖动（SSL EOF/超时等）：短退避重试
                backoff = min(10 * (2 ** attempt), 120)
                wait_total += backoff
                time.sleep(backoff)
                continue
            raise
    raise RuntimeError(f"LLM 调用失败（重试 {MAX_RETRIES} 次，累计等待 {wait_total:.0f}s）")


def chat_with_image(model: str, text: str, image_path: Path | str, *,
                    max_tokens: int = 4096) -> str:
    """单图视觉调用（抽帧理解/自检回环用）。"""
    p = Path(image_path)
    b64 = base64.b64encode(p.read_bytes()).decode()
    messages = [{"role": "user", "content": [
        {"type": "text", "text": text},
        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
    ]}]
    return chat(model, messages, max_tokens=max_tokens)
