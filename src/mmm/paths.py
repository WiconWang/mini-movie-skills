"""路径根：CODE_ROOT（代码/配置资产）与 DATA_ROOT（数据）。

mmm 把目录分成两类：
- CODE_ROOT：源码、配置、模板、字体、ffmpeg、.env、catalog.yaml、logs、.locks
  ——随代码仓库走，不随数据迁移。
- DATA_ROOT：materials/、tasks/、output/、workspace/、pipeline.sqlite
  ——业务数据，外移到统一工作区 $MMM_DATA_ROOT，避免随 Skill 安装进配置目录。

回退链：进程环境变量 MMM_DATA_ROOT > CODE_ROOT/.env 里的 MMM_DATA_ROOT > CODE_ROOT。
无 env 时 DATA_ROOT 回退 CODE_ROOT，保留旧行为，测试/CI 零风险。

注意：editable 安装下 CODE_ROOT 仓库可写，故 .locks 留 CODE_ROOT。
若未来改为 pip 安装（site-packages 只读），需把 .locks 也挪到 DATA_ROOT。
"""

from __future__ import annotations

import os
from pathlib import Path

CODE_ROOT: Path = Path(__file__).resolve().parents[2]


def _from_env_file(env_file: Path, key: str) -> str | None:
    """从 .env 解析单个键值，不依赖 dotenv。"""
    if not env_file.exists():
        return None
    for line in env_file.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        k, v = s.split("=", 1)
        if k.strip() == key:
            return v.strip()
    return None


def _resolve_data_root() -> Path:
    """DATA_ROOT 解析：进程 env > .env > 回退 CODE_ROOT。"""
    env_val = os.environ.get("MMM_DATA_ROOT")
    if not env_val:
        env_val = _from_env_file(CODE_ROOT / ".env", "MMM_DATA_ROOT")
    if env_val:
        return Path(env_val).expanduser().resolve()
    return CODE_ROOT


DATA_ROOT: Path = _resolve_data_root()

# 过渡别名：存量 18 个文件 `from .db import PROJECT_ROOT`，分批替换后可删。
# 语义上 PROJECT_ROOT 历来指代码仓库根，故等价于 CODE_ROOT。
PROJECT_ROOT: Path = CODE_ROOT
