#!/usr/bin/env python3
"""验证指定 LLM route 的配置与真实连通性。"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mmm.llm import chat, load_endpoint  # noqa: E402


def mask_key(value: str) -> str:
    if len(value) <= 8:
        return "***"
    return f"{value[:4]}***{value[-4:]}"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("route", choices=["low", "high", "vision"])
    parser.add_argument("--yes", action="store_true", help="确认发起真实付费请求")
    args = parser.parse_args()

    route = {
        "low": "narrate_low",
        "high": "narrate_high",
        "vision": "vision",
    }[args.route]
    endpoint = load_endpoint(route)
    print("[1/2] 配置解析")
    print(f"      route={endpoint.route}")
    print(f"      profile={endpoint.profile_id}")
    print(f"      model={endpoint.model}")
    print(f"      base_url={endpoint.base_url}")
    print(f"      api_key={mask_key(endpoint.api_key)}")
    print(f"      max_attempts={endpoint.profile.max_retries + 1}")

    if not args.yes:
        print("[2/2] 未发起请求：追加 --yes 才执行真实付费验证")
        return

    print("[2/2] 发起真实请求")
    result = chat(
        endpoint,
        [{"role": "user", "content": "只回复两个字：成功"}],
        max_tokens=512,
        temperature=0,
        label=f"verify:{route}",
    )
    print(f"      attempt={result.attempt}")
    print(f"      duration_ms={result.duration_ms}")
    print(f"      http_status={result.http_status}")
    print(f"      finish_reason={result.finish_reason}")
    print(f"      usage={result.usage}")
    print(f"      reply={result.content.strip()[:80]}")
    print(f"      log={result.log_path}")


if __name__ == "__main__":
    main()
