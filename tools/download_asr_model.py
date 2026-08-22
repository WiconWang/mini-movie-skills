#!/usr/bin/env python3
"""下载 faster-whisper 模型到本地缓存并验证可加载。

用法：
    .venv/bin/python tools/download_asr_model.py [模型档位]

默认档位 medium（与 src/mmm/stage_asr.py 的 ASR_MODEL_SIZE 一致）。
模型缓存到 HF 缓存目录（默认 ~/.cache/huggingface），WhisperModel("medium") 会自动命中。

下载源优先级：
    1. 本地缓存已命中 → 直接完成，不联网（离线可用）。
    2. 国内 ModelScope（阿里云 CDN，实测 20MB/s+），下载后自动布局进 HF 缓存。
    3. 直连 HuggingFace（需能访问，配合 HF_ENDPOINT / HF_HUB_DISABLE_XET）。

依赖：modelscope（uv pip install modelscope）
"""

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

# 必须在 import huggingface_hub 之前设置（防御：若走官方源则禁用 Xet，镜像不支持）
os.environ.setdefault("HF_HUB_DISABLE_XET", "1")

# 模型档位 → HF repo_id（faster-whisper 官方仓库，ModelScope 有同步镜像）
REPO_BY_SIZE = {
    "tiny": "Systran/faster-whisper-tiny",
    "base": "Systran/faster-whisper-base",
    "small": "Systran/faster-whisper-small",
    "medium": "Systran/faster-whisper-medium",
    "large-v1": "Systran/faster-whisper-large-v1",
    "large-v2": "Systran/faster-whisper-large-v2",
    "large-v3": "Systran/faster-whisper-large-v3",
}


def hf_cache_dir() -> Path:
    """HF 缓存根目录（与 huggingface_hub 一致）。"""
    base = os.environ.get("HF_HOME") or str(Path.home() / ".cache/huggingface")
    return Path(base)


def cache_repo_dir(repo_id: str) -> Path:
    """仓库在缓存中的目录（models--owner--name）。"""
    return hf_cache_dir() / "hub" / f"models--{repo_id.replace('/', '--')}"


def layout_into_cache(repo_id: str, src: Path) -> None:
    """把本地模型目录布局进 HF 缓存（snapshots/<name> + refs/main 文本文件）。"""
    repo_dir = cache_repo_dir(repo_id)
    rev = "modelscope-mirror"
    snapshot = repo_dir / "snapshots" / rev
    snapshot.mkdir(parents=True, exist_ok=True)

    # 每个文件软链进 snapshot（避免复制 1.5GB）
    for f in src.iterdir():
        if f.is_file():
            link = snapshot / f.name
            link.unlink(missing_ok=True)
            link.symlink_to(f.resolve())

    # refs/main 必须是"无换行"文本，内容 = snapshot 目录名
    (repo_dir / "refs").mkdir(parents=True, exist_ok=True)
    (repo_dir / "refs" / "main").write_text(rev)

    # 清理可能残留的旧 hash snapshot（非本次 rev 的目录）
    for d in (repo_dir / "snapshots").iterdir():
        if d.name != rev:
            shutil.rmtree(d, ignore_errors=True)


def verify_load(model_size: str) -> bool:
    """离线模式尝试加载模型，成功返回 True。"""
    import subprocess

    code = subprocess.run(
        [
            sys.executable, "-c",
            "import os; os.environ['HF_HUB_OFFLINE']='1';"
            "from faster_whisper import WhisperModel;"
            f"WhisperModel('{model_size}', device='cpu', compute_type='int8');"
            "print('OK')",
        ],
        capture_output=True, text=True,
    )
    return "OK" in code.stdout


def main() -> None:
    model_size = sys.argv[1] if len(sys.argv) > 1 else "medium"
    repo_id = REPO_BY_SIZE.get(model_size)
    if not repo_id:
        sys.exit(f"未知档位: {model_size}。可选: {', '.join(REPO_BY_SIZE)}")

    # 1) 缓存已命中？
    if verify_load(model_size):
        print(f"✅ 缓存已命中，无需下载: {model_size}")
        return

    # 2) ModelScope 下载
    print(f"[1/2] 从 ModelScope 下载 {repo_id} ...")
    t0 = time.time()
    work = Path("temp") / "models" / f"faster-whisper-{model_size}"
    work.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["modelscope", "download", "--model", repo_id, "--local_dir", str(work)],
        check=True,
    )
    print(f"      下载完成，耗时 {time.time()-t0:.1f}s")

    # 3) 布局进 HF 缓存
    print("[2/2] 布局进 HF 缓存 ...")
    layout_into_cache(repo_id, work)

    # 4) 验证
    if verify_load(model_size):
        print(f"✅ 模型就绪: {model_size} (device=cpu, compute_type=int8, 离线可用)")
    else:
        sys.exit("❌ 布局后验证失败，请检查模型文件完整性")


if __name__ == "__main__":
    main()
