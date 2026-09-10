"""路径根：CODE_ROOT（代码/配置资产）与 DATA_ROOT（数据）。

mmm 把目录分成两类：
- CODE_ROOT：源码、配置、模板、字体、logs、.locks
  ——随代码仓库走，不随数据迁移。
- DATA_ROOT：ledger.sqlite、{game}/、workspace/、tasks/、output/、_sources/
  ——业务数据，落统一工作区 $MINIMOVIE_DATA_ROOT（6 个 skill 共享，非 mmm 专属）。

数据根唯一来源 ~/.minimovie（KEY=VALUE 格式），进程环境变量仅作临时覆盖。
找不到即 fail-fast 报错，不回退 CODE_ROOT（静默写进代码目录是历史漂移的根源）。
"""

from __future__ import annotations

import os
from pathlib import Path

CODE_ROOT: Path = Path(__file__).resolve().parents[2]

MINIMOVIE_RC = Path.home() / ".minimovie"


def _from_rc_file(rc_file: Path, key: str) -> str | None:
    """从 ~/.minimovie 解析单个键值，不依赖 dotenv。"""
    if not rc_file.exists():
        return None
    for line in rc_file.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        k, v = s.split("=", 1)
        if k.strip() == key:
            return v.strip()
    return None


def _resolve_data_root() -> Path:
    """DATA_ROOT 解析：进程 env（临时覆盖）> ~/.minimovie；缺失则报错。

    旧变量 MMM_DATA_ROOT / CODE_ROOT/.env 已废除：新变量缺席但旧变量存在时
    直接报错指路，避免静默写错盘。
    """
    env_val = os.environ.get("MINIMOVIE_DATA_ROOT") or _from_rc_file(
        MINIMOVIE_RC, "MINIMOVIE_DATA_ROOT"
    )
    if env_val:
        return Path(env_val).expanduser().resolve()
    if os.environ.get("MMM_DATA_ROOT"):
        raise RuntimeError(
            "检测到已废除的 MMM_DATA_ROOT，请迁移到 MINIMOVIE_DATA_ROOT：\n"
            "  export MINIMOVIE_DATA_ROOT=$MMM_DATA_ROOT（临时）并写入 ~/.minimovie（持久），"
            "然后 unset MMM_DATA_ROOT"
        )
    raise RuntimeError(
        "未找到数据根：请 export MINIMOVIE_DATA_ROOT 或写入 ~/.minimovie，"
        "格式：MINIMOVIE_DATA_ROOT=/path/to/workdir"
    )


DATA_ROOT: Path = _resolve_data_root()
