"""模型路由与供应商适配配置。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import yaml

from .db import PROJECT_ROOT

MODELS_YAML = PROJECT_ROOT / "config" / "models.yaml"
ROUTES = ("narrate_low", "narrate_high", "vision")
PROTOCOLS = {"openai_chat_completions"}
MAX_TOKENS_FIELDS = {"max_tokens", "max_completion_tokens"}
REQUEST_RESERVED_FIELDS = {
    "model", "messages", "stream", "temperature", "max_tokens",
    "max_completion_tokens",
}


@dataclass(frozen=True)
class ModelProfile:
    """一个流水线路由在一个供应商接入方式下的请求策略。"""

    route: str
    profile_id: str
    protocol: str
    temperature_mode: str | float | None
    max_tokens_field: str
    input_context_tokens: int
    safety_margin_tokens: int
    capabilities: frozenset[str]
    timeout_seconds: float
    retry_backoff_seconds: float
    min_interval_seconds: float
    max_tokens: int
    temperature: float | None
    narration_segment_workers: int = 1
    max_retries: int = 0
    retryable_status_codes: frozenset[int] = frozenset()
    retry_network_errors: bool = False
    request_extra: dict | None = None

    def resolve_temperature(self, requested: float | None) -> float | None:
        """按 profile 决定是否发送 temperature。"""
        if self.temperature_mode in ("omit", None):
            return None
        if self.temperature_mode == "passthrough":
            return requested if requested is not None else self.temperature
        return float(self.temperature_mode)

    def resolve_max_tokens(self, requested: int | None = None) -> int:
        return requested if requested is not None else self.max_tokens

    def available_input_tokens(self) -> int:
        return max(0, self.input_context_tokens - self.safety_margin_tokens - self.max_tokens)


def _raw_config() -> dict:
    if not MODELS_YAML.exists():
        raise RuntimeError(f"缺少模型配置文件: {MODELS_YAML}")
    data = yaml.safe_load(MODELS_YAML.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise RuntimeError(f"{MODELS_YAML} 必须是 YAML 对象")
    return data


def _section(raw: dict, name: str) -> dict:
    value = raw.get(name, {})
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise RuntimeError(f"models.yaml 的 {name} 必须是对象")
    return dict(value)


def _positive_int(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise RuntimeError(f"{field} 必须是大于 0 的整数")
    return value


def _non_negative_number(value: object, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        raise RuntimeError(f"{field} 必须是大于等于 0 的数字")
    return float(value)


def _validate_common(merged: dict, label: str) -> None:
    if merged.get("protocol") not in PROTOCOLS:
        raise RuntimeError(f"{label}.protocol 未实现，当前支持: {sorted(PROTOCOLS)}")
    if merged.get("max_tokens_field") not in MAX_TOKENS_FIELDS:
        raise RuntimeError(f"{label}.max_tokens_field 必须是 {sorted(MAX_TOKENS_FIELDS)} 之一")
    _positive_int(merged.get("input_context_tokens"), f"{label}.input_context_tokens")
    _positive_int(merged.get("safety_margin_tokens"), f"{label}.safety_margin_tokens")
    _non_negative_number(merged.get("timeout_seconds"), f"{label}.timeout_seconds")
    _non_negative_number(merged.get("retry_backoff_seconds"), f"{label}.retry_backoff_seconds")
    _non_negative_number(merged.get("min_interval_seconds"), f"{label}.min_interval_seconds")
    capabilities = merged.get("capabilities", [])
    if not isinstance(capabilities, list) or not all(isinstance(x, str) for x in capabilities):
        raise RuntimeError(f"{label}.capabilities 必须是字符串列表")
    temperature_mode = merged.get("temperature_mode")
    if temperature_mode not in ("passthrough", "omit", None) and not (
        isinstance(temperature_mode, (int, float))
        and not isinstance(temperature_mode, bool)
        and 0 <= float(temperature_mode) <= 2
    ):
        raise RuntimeError(f"{label}.temperature_mode 必须是 passthrough/omit 或 0~2 数值")


def _validate_route(merged: dict, route: str) -> None:
    label = f"routes.{route}"
    _positive_int(merged.get("max_tokens"), f"{label}.max_tokens")
    retries = merged.get("max_retries")
    if isinstance(retries, bool) or not isinstance(retries, int) or retries < 0:
        raise RuntimeError(f"{label}.max_retries 必须是大于等于 0 的整数")
    statuses = merged.get("retryable_status_codes", [])
    if not isinstance(statuses, list):
        raise RuntimeError(f"{label}.retryable_status_codes 必须是数组")
    for status in statuses:
        if isinstance(status, bool) or not isinstance(status, int) or not 100 <= status <= 599:
            raise RuntimeError(f"{label}.retryable_status_codes 包含非法 HTTP 状态码: {status}")
    if route == "narrate_high" and retries != 0:
        raise RuntimeError("routes.narrate_high.max_retries 必须固定为 0")
    if route == "narrate_low":
        workers = merged.get("narration_segment_workers", 1)
        if isinstance(workers, bool) or not isinstance(workers, int) or workers <= 0:
            raise RuntimeError(f"{label}.narration_segment_workers 必须是大于 0 的整数")
    temperature = merged.get("temperature")
    if temperature is not None and (
        isinstance(temperature, bool)
        or not isinstance(temperature, (int, float))
        or not 0 <= float(temperature) <= 2
    ):
        raise RuntimeError(f"{label}.temperature 必须为空或 0~2 数值")
    if not isinstance(merged.get("retry_network_errors"), bool):
        raise RuntimeError(f"{label}.retry_network_errors 必须是布尔值")
    if merged["max_tokens"] + merged["safety_margin_tokens"] >= merged["input_context_tokens"]:
        raise RuntimeError(
            f"{label}.max_tokens + safety_margin_tokens 必须小于 input_context_tokens"
        )

    extra = merged.get("request_extra") or {}
    if not isinstance(extra, dict):
        raise RuntimeError(f"{label}.request_extra 必须是对象")
    forbidden = sorted(set(extra) & REQUEST_RESERVED_FIELDS)
    if forbidden:
        raise RuntimeError(
            f"{label}.request_extra 不允许覆盖协议核心字段: {forbidden}"
        )


def _profile_data(raw: dict, route: str, profile_id: str) -> dict:
    defaults = _section(raw, "defaults")
    profiles = _section(raw, "profiles")
    routes = _section(raw, "routes")
    if route not in routes:
        raise RuntimeError(f"models.yaml 缺少 route: {route}")
    if profile_id not in profiles:
        raise RuntimeError(f"models.yaml 缺少 profile: {profile_id}")

    route_base = _section(routes[route], "defaults")
    merged = {
        **defaults,
        **profiles[profile_id],
        **route_base,
    }
    merged["route"] = route
    merged["profile_id"] = profile_id
    label = f"profiles.{profile_id} + routes.{route}"
    _validate_common(merged, label)
    _validate_route(merged, route)
    merged["capabilities"] = frozenset(merged.get("capabilities", []))
    merged["retryable_status_codes"] = frozenset(merged.get("retryable_status_codes", []))
    merged["request_extra"] = merged.get("request_extra") or None
    return merged


def validate_base_url(base_url: str, env_name: str) -> str:
    if not base_url:
        raise RuntimeError(f"缺少 {env_name}（.env 或环境变量）")
    parsed = urlparse(base_url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise RuntimeError(f"{env_name} 必须是合法 HTTP(S) 根地址: {base_url}")
    if parsed.query or parsed.fragment:
        raise RuntimeError(f"{env_name} 不应携带 query 或 fragment")
    normalized = base_url.rstrip("/")
    if normalized.endswith("/chat/completions"):
        raise RuntimeError(f"{env_name} 应填 OpenAI compatible 根地址，不要包含 /chat/completions")
    return normalized


def route_profile(route: str, profile_id: str) -> ModelProfile:
    """按流水线路由与供应商 profile 解析合并后的请求策略。"""
    if route not in ROUTES:
        raise RuntimeError(f"未知模型路由: {route}，支持: {ROUTES}")
    raw = _raw_config()
    data = _profile_data(raw, route, profile_id)
    if route == "vision" and "image" not in data["capabilities"]:
        raise RuntimeError(f"vision route 使用的 profile 必须声明 image capability: {profile_id}")
    return ModelProfile(**data)


_CACHE: dict[tuple[str, str], ModelProfile] = {}


def cached_route_profile(route: str, profile_id: str) -> ModelProfile:
    key = (route, profile_id)
    if key not in _CACHE:
        _CACHE[key] = route_profile(route, profile_id)
    return _CACHE[key]
