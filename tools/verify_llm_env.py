#!/usr/bin/env python3
"""验证 .env 中 LLM 配置是否可用。

用法：
    .venv/bin/python tools/verify_llm_env.py [模型名]

默认模型: deepseek-v4-flash（解说模型，与 stage_narrate.NARRATE_MODEL 一致）。
验证内容：配置解析 → 真实 chat 请求 → 检查响应。

注意：llm.chat() 带限速（0.5s）与指数退避，网络异常时可能耗时数十秒。
"""

import sys
import time

def main() -> None:
    model = sys.argv[1] if len(sys.argv) > 1 else "deepseek-v4-flash"

    print(f"[1/3] 解析 .env 配置 ...")
    from mmm import llm
    base, key = llm._load_env()
    print(f"      ✅ 读取成功: {base[:24]}... | key {key[:4]}***{key[-4:]}")

    print(f"[2/3] 发送真实 chat 请求 (model={model}) ...")
    t0 = time.time()
    # 注意：max_tokens 要留足思考型模型的推理预算（reasoning 共享 token），
    # 太小会 finish_reason=length 且 content 为空，造成"模型不可用"误报。
    try:
        reply = llm.chat(
            model,
            [{"role": "user", "content": "只回复两个字：成功"}],
            max_tokens=512, temperature=0,
        )
    except Exception as e:
        print(f"      ❌ 请求失败: {type(e).__name__}: {e}")
        sys.exit(1)

    print(f"[3/3] 响应耗时 {time.time()-t0:.1f}s")
    reply = reply.strip()
    print(f"      ✅ 模型回复: {reply[:80]}")
    if reply:
        print("      ✅ LLM 配置可用：URL / key / 模型 / 网络 全链路正常")
    else:
        print("      ⚠️ 请求成功但回复为空")
        print("      可能原因：思考型模型推理耗尽了 max_tokens，或模型未放行。")
        print("      建议用更大 max_tokens 重试，或换模型验证。")

if __name__ == "__main__":
    main()
