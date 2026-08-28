"""TTS 供应商适配器注册表。"""

from ..types import TtsProfile
from .edge import EdgeTTSProvider
from .minimax import MiniMaxTTSProvider


def get_provider(provider_id: str, options: dict | None = None) -> object:
    providers = {
        EdgeTTSProvider.provider_id: EdgeTTSProvider,
        MiniMaxTTSProvider.provider_id: MiniMaxTTSProvider,
    }
    if provider_id not in providers:
        raise ValueError(f"未知 TTS provider: {provider_id}，支持: {sorted(providers)}")
    if provider_id == "minimax":
        return MiniMaxTTSProvider(
            base_url=(options or {}).get("base_url", "https://api.minimaxi.com")
        )
    return providers[provider_id]()


def estimate_plan_cost(provider_id: str, segments: list, profile: TtsProfile) -> dict:
    if provider_id == "edge":
        return {
            "currency": "CNY", "billing_characters": 0,
            "price_per_10000": 0.0, "amount": 0.0,
        }
    provider = get_provider(provider_id, profile.options)
    return provider.estimate_cost(segments, profile)
