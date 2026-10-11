#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""通过 GitHub API 部署本项目（本机 git clone/push 不可用时的替代通道）。

用法（GITHUB_TOKEN 需具备 repo + workflow 权限）：
  python scripts/deploy.py upload [文件...]            # 上传/更新全部或指定项目文件
  python scripts/deploy.py secret NAME VALUE          # 设置仓库 Actions Secret
  python scripts/deploy.py trigger                    # 手动触发一次监控工作流
  python scripts/deploy.py status                     # 查看最近几次运行结果

环境变量：
  GITHUB_TOKEN   GitHub Personal Access Token
  GITHUB_REPO    仓库，默认 whlle-yi/hjmd-monitor
"""

from __future__ import annotations

import base64
import hashlib
import os
import sys
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
REPO = os.environ.get("GITHUB_REPO", "whlle-yi/hjmd-monitor")
API = "https://api.github.com"

SKIP_DIRS = {"__pycache__", ".git", "temp", ".idea", ".vscode"}
SKIP_SUFFIX = (".tmp", ".pyc")


def _session(direct: bool) -> requests.Session:
    s = requests.Session()
    s.trust_env = not direct  # direct=True 时绕过系统代理直连
    s.headers["Authorization"] = f"Bearer {os.environ['GITHUB_TOKEN']}"
    s.headers["Accept"] = "application/vnd.github+json"
    return s


def call(method: str, path: str, **kw) -> requests.Response:
    """优先走系统代理，连接失败时直连重试。"""
    last: requests.Response | None = None
    for direct in (False, True):
        try:
            r = _session(direct).request(method, f"{API}{path}", timeout=30, **kw)
            if r.status_code < 500:
                return r
            last = r
        except requests.exceptions.RequestException:
            continue
    if last is None:
        raise RuntimeError(f"请求失败：{method} {path}")
    return last


def collect_files() -> list[Path]:
    files = []
    for p in sorted(ROOT.rglob("*")):
        if not p.is_file():
            continue
        parts = set(p.relative_to(ROOT).parts)
        if parts & SKIP_DIRS or p.name.endswith(SKIP_SUFFIX):
            continue
        files.append(p)
    return files


def upload(only: list[str] | None = None) -> None:
    ok = fail = 0
    for p in collect_files():
        rel = p.relative_to(ROOT).as_posix()
        if only and rel not in only:
            continue
        raw = p.read_bytes()
        blob_sha = hashlib.sha1(b"blob " + str(len(raw)).encode() + b"\0" + raw).hexdigest()
        body: dict = {"message": f"deploy: 更新 {rel}", "branch": "main"}
        r = call("GET", f"/repos/{REPO}/contents/{rel}")
        if r.status_code == 200:
            if r.json().get("sha") == blob_sha:
                print(f"SKIP {rel}（内容未变）")
                continue
            body["sha"] = r.json()["sha"]
        body["content"] = base64.b64encode(raw).decode()
        r = call("PUT", f"/repos/{REPO}/contents/{rel}", json=body)
        if r.status_code in (200, 201):
            ok += 1
            print(f"OK   {rel}")
        else:
            fail += 1
            print(f"FAIL {rel} -> {r.status_code} {r.text[:200]}")
    print(f"\n完成：成功 {ok} 个，失败 {fail} 个")


def secret(name: str, value: str) -> None:
    from nacl import encoding
    from nacl.public import PublicKey, SealedBox

    r = call("GET", f"/repos/{REPO}/actions/secrets/public-key")
    r.raise_for_status()
    data = r.json()
    key = PublicKey(data["key"].encode(), encoding.Base64Encoder())
    sealed = SealedBox(key).encrypt(value.encode())
    r = call(
        "PUT",
        f"/repos/{REPO}/actions/secrets/{name}",
        json={"encrypted_value": base64.b64encode(sealed).decode(), "key_id": data["key_id"]},
    )
    print(("OK  " if r.status_code in (201, 204) else f"FAIL {r.status_code}") + f" secret {name}")


def trigger() -> None:
    r = call("POST", f"/repos/{REPO}/actions/workflows/monitor.yml/dispatches", json={"ref": "main"})
    print("触发工作流:", r.status_code, "(204=成功)")


def status() -> None:
    r = call("GET", f"/repos/{REPO}/actions/runs?per_page=5")
    r.raise_for_status()
    runs = r.json().get("workflow_runs", [])
    if not runs:
        print("尚无运行记录")
        return
    for run in runs:
        print(
            f"#{run['run_number']}  {run['status']:<10} {str(run['conclusion']):<8} "
            f"{run['created_at']}  {run['html_url']}"
        )


def main() -> int:
    if not os.environ.get("GITHUB_TOKEN"):
        print("请先设置环境变量 GITHUB_TOKEN")
        return 2
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "upload":
        upload(sys.argv[2:])
    elif cmd == "secret":
        secret(sys.argv[2], sys.argv[3])
    elif cmd == "trigger":
        trigger()
    elif cmd == "status":
        status()
    else:
        print(__doc__)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
