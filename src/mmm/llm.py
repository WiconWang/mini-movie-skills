"""多供应商 LLM 客户端与调用账本。"""

from __future__ import annotations

import base64
import hashlib
import http.client
import json
import math
import os
import re
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from . import models
from .db import PROJECT_ROOT
from .models import ModelProfile

ENV_PREFIXES = {
    "narrate_low": "MMM_NARRATE_LOW_",
    "narrate_high": "MMM_NARRATE_HIGH_",
    "vision": "MMM_VISION_",
}

LOG_PATH = PROJECT_ROOT / "logs" / "llm_calls.jsonl"
_LOG_LOCK = threading.Lock()
_RATE_LOCK = threading.Lock()
_RATE_LAST: dict[tuple[str, str], float] = {}


def _load_env_file() -> dict[str, str]:
    """读取 .env，不覆盖 shell 已提供的环境变量。"""
    env = PROJECT_ROOT / ".env"
    values: dict[str, str] = {}
    if not env.exists():
        return values
    for line in env.read_text(encoding="utf-8").splitlines():
        if "=" not in line or line.lstrip().startswith("#"):
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip()
    return values


for _key, _value in _load_env_file().items():
    os.environ.setdefault(_key, _value)
del _key, _value


@dataclass(frozen=True)
class LLMEndpoint:
    route: str
    profile_id: str
    model: str
    base_url: str
    api_key: str
    profile: ModelProfile


@dataclass(frozen=True)
class LLMCallResult:
    content: str
    route: str
    profile_id: str
    model: str
    attempt: int
    duration_ms: int
    http_status: int | None
    finish_reason: str | None
    usage: dict | None
    response_chars: int
    response_preview: str
    response_hash: str
    log_path: str


class LLMCallError(RuntimeError):
    def __init__(self, message: str, *, route: str, attempt: int,
                 log_path: str, http_status: int | None = None):
        super().__init__(message)
        self.route = route
        self.attempt = attempt
        self.log_path = log_path
        self.http_status = http_status


def _required_env(route: str, field: str) -> str:
    prefix = ENV_PREFIXES[route]
    name = f"{prefix}{field}"
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"缺少 {name}（.env 或环境变量）")
    return value


def load_endpoint(route: str) -> LLMEndpoint:
    """按 route 读取独立 endpoint，并解析供应商适配策略。"""
    if route not in ENV_PREFIXES:
        raise RuntimeError(f"未知模型路由: {route}，支持: {sorted(ENV_PREFIXES)}")
    profile_id = _required_env(route, "PROFILE")
    model = _required_env(route, "MODEL")
    base_url = models.validate_base_url(
        _required_env(route, "BASE_URL"), f"{ENV_PREFIXES[route]}BASE_URL"
    )
    api_key = _required_env(route, "API_KEY")
    profile = models.cached_route_profile(route, profile_id)
    return LLMEndpoint(
        route=route,
        profile_id=profile_id,
        model=model,
        base_url=base_url,
        api_key=api_key,
        profile=profile,
    )


def _prompt_chars(messages: list[dict]) -> int:
    return sum(len(str(m.get("content", ""))) for m in messages)


def estimate_tokens(text: str) -> int:
    """保守估算 prompt token，不引入 tokenizer 依赖。"""
    cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
    other = len(text) - cjk
    return math.ceil(cjk + other * 0.75)


def _request_hash(payload: dict) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _response_hash(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def _safe_url(endpoint: LLMEndpoint) -> str:
    parsed = urlparse(endpoint.base_url)
    return f"{parsed.netloc}{parsed.path}".rstrip("/")


def _redact(text: str, api_key: str) -> str:
    return text.replace(api_key, "***")


def _wait_rate_limit(endpoint: LLMEndpoint) -> None:
    parsed = urlparse(endpoint.base_url)
    key = (endpoint.profile_id, f"{parsed.scheme}://{parsed.netloc}")
    interval = endpoint.profile.min_interval_seconds
    if interval <= 0:
        return
    with _RATE_LOCK:
        now = time.monotonic()
        last = _RATE_LAST.get(key, 0.0)
        wait = last + interval - now
        if wait > 0:
            time.sleep(wait)
            now = time.monotonic()
        _RATE_LAST[key] = now


def _write_log(record: dict) -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
    with _LOG_LOCK:
        with LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(line + "\n")


def _base_log(endpoint: LLMEndpoint, payload: dict, attempt: int,
              label: str | None) -> dict:
    from datetime import datetime
    messages = payload.get("messages", [])
    prompt = "\n".join(str(m.get("content", "")) for m in messages)
    return {
        "ts": datetime.now().astimezone().isoformat(timespec="seconds"),
        "route": endpoint.route,
        "profile": endpoint.profile_id,
        "model": endpoint.model,
        "base_url_host": _safe_url(endpoint),
        "label": label,
        "attempt": attempt + 1,
        "max_retries": endpoint.profile.max_retries,
        "prompt_chars": _prompt_chars(messages),
        "estimated_prompt_tokens": estimate_tokens(prompt),
        "max_tokens": payload.get(endpoint.profile.max_tokens_field),
        "temperature_mode": endpoint.profile.temperature_mode,
        "request_hash": _request_hash(payload),
    }


def _error_body(e: urllib.error.HTTPError, api_key: str) -> str:
    try:
        body = e.read().decode("utf-8", errors="replace")
    except Exception:
        body = ""
    return _redact(body[:500], api_key)


def chat(endpoint: LLMEndpoint, messages: list[dict], *, max_tokens: int,
         temperature: float | None = None,
         label: str | None = None) -> LLMCallResult:
    """调用 OpenAI compatible chat 接口，并记录每次 HTTP attempt。"""
    profile = endpoint.profile
    payload: dict = {
        "model": endpoint.model,
        "messages": messages,
        profile.max_tokens_field: profile.resolve_max_tokens(max_tokens),
    }
    resolved_temperature = profile.resolve_temperature(temperature)
    if resolved_temperature is not None:
        payload["temperature"] = resolved_temperature
    if profile.request_extra:
        payload.update(profile.request_extra)

    max_attempts = profile.max_retries + 1
    for attempt_index in range(max_attempts):
        _wait_rate_limit(endpoint)
        base_log = _base_log(endpoint, payload, attempt_index, label)
        started = time.monotonic()
        content = ""
        http_status = None
        finish_reason = None
        usage = None
        error_type = None
        error_message = None
        error_body = ""
        will_retry = False

        request = urllib.request.Request(
            f"{endpoint.base_url}/chat/completions",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {endpoint.api_key}",
                "Content-Type": "application/json",
                "User-Agent": "curl/8.7.1",
            },
        )
        retryable = False
        try:
            with urllib.request.urlopen(request, timeout=profile.timeout_seconds) as response:
                http_status = response.status
                raw_response = json.load(response)
            choice = raw_response.get("choices", [{}])[0]
            content = choice.get("message", {}).get("content") or ""
            finish_reason = choice.get("finish_reason")
            usage = raw_response.get("usage") if isinstance(raw_response.get("usage"), dict) else None
            if not content.strip():
                error_type = "EmptyContent"
                error_message = "模型返回空内容"
        except urllib.error.HTTPError as e:
            http_status = e.code
            error_type = "HTTPError"
            error_message = str(e)
            error_body = _error_body(e, endpoint.api_key)
            retryable = e.code in profile.retryable_status_codes
        except (json.JSONDecodeError, KeyError, IndexError, TypeError, ValueError) as e:
            error_type = "ResponseSchemaError"
            error_message = f"{type(e).__name__}: {e}"
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            error_type = "NetworkError"
            error_message = f"{type(e).__name__}: {e}"
            retryable = profile.retry_network_errors
        except (http.client.IncompleteRead, http.client.HTTPException) as e:
            error_type = "NetworkError"
            error_message = f"{type(e).__name__}: {e}"
            retryable = profile.retry_network_errors

        attempts_left = attempt_index + 1 < max_attempts
        will_retry = bool(retryable and attempts_left and error_type)
        duration_ms = int((time.monotonic() - started) * 1000)
        record = {
            **base_log,
            "will_retry": will_retry,
            "http_status": http_status,
            "duration_ms": duration_ms,
            "finish_reason": finish_reason,
            "usage": usage,
            "response_chars": len(content),
            "response_preview": content[:500],
            "response_hash": _response_hash(content),
            "error_type": error_type,
            "error_message": _redact(error_message or "", endpoint.api_key),
            "error_body": _redact(error_body or "", endpoint.api_key),
        }
        _write_log(record)

        if error_type is None:
            return LLMCallResult(
                content=content,
                route=endpoint.route,
                profile_id=endpoint.profile_id,
                model=endpoint.model,
                attempt=attempt_index + 1,
                duration_ms=duration_ms,
                http_status=http_status,
                finish_reason=finish_reason,
                usage=usage,
                response_chars=len(content),
                response_preview=content[:500],
                response_hash=record["response_hash"],
                log_path=str(LOG_PATH),
            )

        if will_retry:
            backoff = min(
                profile.retry_backoff_seconds * (2 ** attempt_index),
                60,
            )
            time.sleep(backoff)
            continue

        detail = f"{error_type}: {error_message}"
        if error_body:
            detail += f"；响应片段: {error_body}"
        raise LLMCallError(
            f"LLM 调用失败 route={endpoint.route} attempt={attempt_index + 1}: {detail}；"
            f"调用日志: {LOG_PATH}",
            route=endpoint.route,
            attempt=attempt_index + 1,
            log_path=str(LOG_PATH),
            http_status=http_status,
        )

    raise LLMCallError(
        f"LLM 调用失败 route={endpoint.route}：重试循环异常退出；调用日志: {LOG_PATH}",
        route=endpoint.route,
        attempt=max_attempts,
        log_path=str(LOG_PATH),
    )


def chat_with_image(endpoint: LLMEndpoint, text: str,
                    image_path: Path | str, *, max_tokens: int,
                    label: str | None = None) -> LLMCallResult:
    """调用视觉模型分析单张图片。"""
    path = Path(image_path)
    image_b64 = base64.b64encode(path.read_bytes()).decode("ascii")
    messages = [{
        "role": "user",
        "content": [
            {"type": "text", "text": text},
            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image_b64}"}},
        ],
    }]
    return chat(
        endpoint,
        messages,
        max_tokens=max_tokens,
        temperature=endpoint.profile.temperature,
        label=label,
    )
