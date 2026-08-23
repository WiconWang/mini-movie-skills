"""模型适配层：按模型名解析请求参数，隔离各模型网关差异。"""

from __future__ import annotations

from dataclasses import dataclass

import yaml

from .db import PROJECT_ROOT

MODELS_YAML = PROJECT_ROOT / "config" / "models.yaml"

_DEFAULTS = {
    "temperature": "passthrough",
    "max_tokens_cap": 32768,
    "timeout": 300,
    "max_retries": 3,
    "retry_backoff": 5.0,
    "narration_segment_max_tokens": 8192,
    "narration_fuse_max_tokens": 16384,
    "narration_segment_workers": 1,
    "request_extra": None,
}


@dataclass(frozen=True)
class ModelProfile:
    """单个模型的一组请求参数。"""

    name: str
    temperature: str | float | None = "passthrough"
    max_tokens_cap: int | None = None
    timeout: float = 300
    max_retries: int = 3
    retry_backoff: float = 5.0
    narration_segment_max_tokens: int = 8192
    narration_fuse_max_tokens: int = 16384
    narration_segment_workers: int = 1
    request_extra: dict | None = None

    def resolve_temperature(self, requested: float | None) -> float | None:
        """omit/null 不传；passthrough 跟随调用方；数值则强制覆盖。"""
        if self.temperature in ("omit", None):
            return None
        if self.temperature == "passthrough":
            return requested
        return float(self.temperature)

    def resolve_max_tokens(self, requested: int) -> int:
        if self.max_tokens_cap and requested > self.max_tokens_cap:
            return self.max_tokens_cap
        return requested


_CACHE: dict[str, ModelProfile] = {}


def _raw_config() -> dict:
    if not MODELS_YAML.exists():
        return {}
    data = yaml.safe_load(MODELS_YAML.read_text(encoding="utf-8")) or {}
    return data if isinstance(data, dict) else {}


def profile_for(model: str) -> ModelProfile:
    """合并 default + 具体模型配置；模型未配置时只用 default。"""
    if model in _CACHE:
        return _CACHE[model]
    raw = _raw_config()
    merged = {**_DEFAULTS, **raw.get("default", {})}
    specific = raw.get(model, {})
    nar = {**merged.get("narration", {}), **specific.get("narration", {})}
    merged.update(specific)
    merged.pop("narration", None)
    merged["narration_segment_max_tokens"] = nar.get("segment_max_tokens",
                                                      merged["narration_segment_max_tokens"])
    merged["narration_fuse_max_tokens"] = nar.get("fuse_max_tokens",
                                                  merged["narration_fuse_max_tokens"])
    merged["narration_segment_workers"] = nar.get("segment_workers",
                                                  merged["narration_segment_workers"])
    profile = ModelProfile(name=model, **merged)
    _CACHE[model] = profile
    return profile
