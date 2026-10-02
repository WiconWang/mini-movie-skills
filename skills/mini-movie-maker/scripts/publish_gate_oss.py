#!/usr/bin/env python3
"""把闸口 storyboard.html + 它引用的帧图发布到 OSS，并输出可直接打开的签名链接。

为什么要这个脚本（踩过的坑）：
  1. storyboard.html 里的图片是相对路径 ../../workspace/<key>/frames/…（相对数据根），
     单独把 html 发出去必然全是空图 —— 必须连图一起按【数据根结构】上传。
  2. 桶默认是私有且开了「阻止公共访问」(Put public object acl is not allowed)，
     对象改 public-read 会被拒绝 —— 所以只能走【签名 URL】，把 html 里的路径改写成绝对签名 URL。
  3. OSS 签名把 HTTP 方法算进签名串：GET 签的 URL 发 HEAD 一定 403。
     验证链接时必须用 GET（curl -r 0-255 走 Range GET），否则会误判为失败。

用法：
  python3 publish_gate_oss.py <task_dir> [--bucket mini-movie] [--endpoint oss-cn-beijing.aliyuncs.com]
  # task_dir 例：/home/share/mini-movie-materials/tasks/genshin-1.5-cuishizhiyuhu
  # 数据根从 task_dir 往上两级推断（与 html 里的 ../../ 语义一致）
"""
from __future__ import annotations

import argparse
import base64
import configparser
import hashlib
import hmac
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor

IMG_RE = re.compile(r'"((?:\.\./)+workspace/[^"]+\.(?:jpg|jpeg|png|webp))"', re.I)
TEN_YEARS = 315_360_000


def load_creds() -> tuple[str, str]:
    cfg = pathlib.Path.home() / ".ossutilconfig"
    if not cfg.exists():
        sys.exit(f"找不到 {cfg}，请先 ossutil config 写入 accessKeyID/accessKeySecret")
    cp = configparser.ConfigParser()
    cp.read(cfg)
    for sec in cp.sections():
        low = {k.lower(): v for k, v in cp[sec].items()}
        if "accesskeyid" in low and "accesskeysecret" in low:
            return low["accesskeyid"], low["accesskeysecret"]
    sys.exit("~/.ossutilconfig 里没有 accessKeyID/accessKeySecret")


def signer(bucket: str, endpoint: str, ak: str, sk: str, expires: int):
    base = f"https://{bucket}.{endpoint}"

    def sign(key: str, verb: str = "GET") -> str:
        res = f"/{bucket}/{key}"
        to_sign = f"{verb}\n\n\n{expires}\n{res}"
        sig = base64.b64encode(hmac.new(sk.encode(), to_sign.encode(), hashlib.sha1).digest()).decode()
        q = urllib.parse.quote(key, safe="/")
        return f"{base}/{q}?Expires={expires}&OSSAccessKeyId={ak}&Signature={urllib.parse.quote(sig, safe='')}"

    return sign


def get(url: str, rng: str | None = None, timeout: int = 25) -> tuple[str, int]:
    """真 GET（Range 可选）。绝不使用 HEAD —— 签名含方法，HEAD 必 403。"""
    cmd = ["curl", "-s", "-o", os.devnull, "-w", "%{http_code} %{size_download}",
           "--connect-timeout", "8", "--max-time", str(timeout)]
    if rng:
        cmd += ["-r", rng]
    cmd.append(url)
    out = subprocess.run(cmd, capture_output=True, text=True).stdout.strip().split()
    return (out[0] if out else "000"), (int(out[1]) if len(out) > 1 else 0)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("task_dir")
    ap.add_argument("--bucket", default=os.environ.get("MMM_OSS_BUCKET", "mini-movie"))
    ap.add_argument("--endpoint", default=os.environ.get("MMM_OSS_ENDPOINT", "oss-cn-beijing.aliyuncs.com"))
    ap.add_argument("--html", default="storyboard.html")
    ap.add_argument("--no-upload", action="store_true", help="只生成链接，不上传")
    a = ap.parse_args()

    task = pathlib.Path(a.task_dir).resolve()
    root = task.parent.parent          # tasks/<task>/ → 数据根
    html = task / a.html
    if not html.exists():
        sys.exit(f"找不到 {html}")

    ossutil = shutil.which("ossutil") or str(pathlib.Path.home() / ".local/bin/ossutil")
    ak, sk = load_creds()
    sign = signer(a.bucket, a.endpoint, ak, sk, int(time.time()) + TEN_YEARS)

    src = html.read_text(encoding="utf-8")
    rels = sorted(set(IMG_RE.findall(src)))
    print(f"① HTML 相对路径：{len(rels)} 条（去重）")

    keys, missing = [], []
    for rel in rels:
        k = re.sub(r"^(\.\./)+", "", rel)
        (keys if (root / k).exists() else missing).append(k)
    print(f"② 本地命中 {len(keys)} 张" + (f"，缺失 {len(missing)} 张 → {missing[:3]}" if missing else "，无缺失"))

    if not a.no_upload:
        with ThreadPoolExecutor(max_workers=8) as ex:
            list(ex.map(lambda k: subprocess.run(
                [ossutil, "cp", str(root / k), f"oss://{a.bucket}/{k}", "-e", a.endpoint, "-f"],
                capture_output=True), keys))
        print(f"③ 已上传 {len(keys)} 张到 oss://{a.bucket}/（结构同本地数据根）")

    # 改写 html：相对路径 → 绝对签名 URL，上传到 OSS（本地原文件不动）
    out = src
    for rel in rels:
        k = re.sub(r"^(\.\./)+", "", rel)
        out = out.replace(f'"{rel}"', f'"{sign(k)}"')
    left = len(IMG_RE.findall(out))
    stage = pathlib.Path("/tmp/mmm-gate-oss") / task.name
    stage.mkdir(parents=True, exist_ok=True)
    patched = stage / a.html
    patched.write_text(out, encoding="utf-8")
    print(f"④ 改写完成，残留相对路径 {left} 条" + (" ✓" if left == 0 else " ✗"))

    if not a.no_upload:
        subprocess.run([ossutil, "cp", str(patched),
                        f"oss://{a.bucket}/tasks/{task.name}/{a.html}", "-e", a.endpoint, "-f"],
                       capture_output=True)
        print("⑤ 已上传改写后的 HTML")

    link = sign(f"tasks/{task.name}/{a.html}")
    code, size = get(link, rng="0-200")
    print(f"\n⑥ 自检（GET ✓ 不是 HEAD）：HTTP {code}  {size} bytes")
    print(f"\n{'='*70}\n板子链接（10 年有效）：\n{link}\n{'='*70}")
    pathlib.Path("/tmp/mmm-gate-oss/last_link.txt").write_text(link)
    return 0 if code in ("200", "206") and left == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
