#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""接力触发：本运行结束后，隔约一个周期唤起下一次监控运行。

用于绕开 GitHub Actions 定时调度器不可靠的问题（新建工作流可能数小时不注册、
高峰期丢弃运行）。要点：

- 自调节：唤起前检查最近的其他运行，若有运行在本周期内刚启动，则跳过本次唤起，
  保证无论定时器、手动触发还是多条历史链并发，始终只有一条链在维持节奏；
- 健壮：对 GitHub API 的调用带重试；拿不到本运行的启动时间时按「刚启动」处理，
  宁可多等也不会断链或加速；
- 兜底：monitor.yml 中的 cron 保持不变，即使这条链意外断掉，定时器也会把监控拉起来。
"""

from __future__ import annotations

import datetime
import json
import os
import time
import urllib.request

REPO = os.environ["GITHUB_REPOSITORY"]
TOKEN = os.environ["GH_TOKEN"]
BRANCH = os.environ.get("GITHUB_REF_NAME", "main")
RUN_ID = os.environ.get("GITHUB_RUN_ID", "")
TARGET = int(os.environ.get("TARGET_INTERVAL", "280"))  # 相邻两次运行的启动间隔（秒）
API = f"https://api.github.com/repos/{REPO}"


def api(method: str, path: str, body: dict | None = None, retries: int = 3):
    last_err: Exception | None = None
    for attempt in range(1, retries + 1):
        req = urllib.request.Request(
            API + path,
            method=method,
            data=json.dumps(body).encode() if body is not None else None,
            headers={
                "Authorization": f"Bearer {TOKEN}",
                "Accept": "application/vnd.github+json",
                "User-Agent": "hjmd-monitor-chain",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read().decode()
                return json.loads(raw) if raw.strip() else {}
        except Exception as exc:  # 瞬时故障重试
            last_err = exc
            time.sleep(15 * attempt)
    raise RuntimeError(f"GitHub API 调用失败 {method} {path}: {last_err!r}")


def parse_ts(s: str) -> datetime.datetime:
    return datetime.datetime.fromisoformat(s.replace("Z", "+00:00"))


def my_started_at() -> datetime.datetime | None:
    """本运行的启动时间；拿不到就返回 None（调用方按「刚启动」处理）。"""
    if RUN_ID:
        obj = api("GET", f"/actions/runs/{RUN_ID}")
        if isinstance(obj, dict) and obj.get("started_at"):
            return parse_ts(obj["started_at"])
        # 兜底：从运行列表里找自己
        lst = api("GET", "/actions/runs?per_page=20")
        for r in lst.get("workflow_runs", []) if isinstance(lst, dict) else []:
            if str(r.get("id")) == str(RUN_ID) and r.get("started_at"):
                return parse_ts(r["started_at"])
    print(f"警告：未能确定本运行启动时间（GITHUB_RUN_ID={RUN_ID or '缺失'}），按刚启动处理")
    return None


def main() -> int:
    # 1) 间距控制：从本运行启动时刻算起，凑满一个周期再触发
    started = my_started_at() or datetime.datetime.now(datetime.timezone.utc)
    now = datetime.datetime.now(datetime.timezone.utc)
    wait = max(0.0, TARGET - (now - started).total_seconds())
    print(f"本运行已进行 {(now - started).total_seconds():.0f}s，"
          f"再等 {wait:.0f}s 后触发下一次运行", flush=True)
    time.sleep(wait)

    # 2) 自调节：若周期内有其他运行刚启动，说明已有链在维持节奏，跳过
    runs = api("GET", "/actions/runs?per_page=5").get("workflow_runs", [])
    now = datetime.datetime.now(datetime.timezone.utc)
    recent = [
        r for r in runs
        if str(r.get("id")) != str(RUN_ID)
        and r.get("created_at")
        and 0 <= (now - parse_ts(r["created_at"])).total_seconds() < TARGET + 60
    ]
    if recent:
        print(f"检测到 {len(recent)} 个刚启动的运行（最近：{recent[0]['created_at']}），"
              "跳过本次唤起，避免多条链并行")
        return 0

    # 3) 唤起下一次监控
    api("POST", "/actions/workflows/monitor.yml/dispatches", {"ref": BRANCH})
    print("已触发下一次监控运行")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
