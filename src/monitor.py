#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
网页更新监控 + 邮件通知（GitHub Actions 云端版）

功能：
  按 config/monitor.json 配置的页面定时抓取：
  - 页面内容变化   -> 解析公告列表、生成差异，发邮件提醒；
  - 连续抓取失败   -> 达到阈值后发一封异常提醒邮件（恢复后自动复位，不重复骚扰）；
  - GitHub Actions -> 自动把最新状态提交回仓库；长期无变化时自动空提交保活，
                      避免仓库 60 天无活动导致定时任务被 GitHub 停用。

用法：
  python src/monitor.py              # 正常运行（CI 中自动提交状态；本地运行只改本地文件）
  python src/monitor.py --dry-run    # 不发邮件、不推送，邮件内容打印到终端
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import smtplib
import subprocess
import time
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from html import escape as esc
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config" / "monitor.json"
STATE_PATH = ROOT / "state" / "state.json"

TZ_CN = timezone(timedelta(hours=8))  # 东八区，仅用于展示时间
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
DATE_RE = re.compile(r"20\d{2}年\d{1,2}月\d{1,2}日")


def now_cn() -> str:
    return datetime.now(TZ_CN).strftime("%Y-%m-%d %H:%M:%S")


def log(msg: str) -> None:
    print(f"[{now_cn()}] {msg}", flush=True)


def load_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


# ---------------------------------------------------------------- 抓取与解析

def fetch(url: str, timeout: int, retries: int = 3) -> str:
    last_err = "unknown"
    for attempt in range(1, retries + 1):
        try:
            r = requests.get(
                url,
                headers={"User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9"},
                timeout=timeout,
            )
            if r.status_code == 200:
                r.encoding = r.apparent_encoding or "utf-8"
                return r.text
            last_err = f"HTTP {r.status_code}"
        except Exception as exc:  # 网络抖动等
            last_err = repr(exc)
        log(f"抓取失败（第 {attempt}/{retries} 次）：{last_err}")
        time.sleep(3 * attempt)
    raise RuntimeError(f"连续 {retries} 次抓取失败，最后错误：{last_err}")


def visible_text(page_html: str) -> str:
    """提取可见文本并把空白压平，保证哈希只反映真实内容变化。"""
    soup = BeautifulSoup(page_html, "html.parser")
    for tag in soup(["script", "style", "noscript", "meta", "link"]):
        tag.decompose()
    lines = []
    for line in soup.get_text("\n").splitlines():
        line = re.sub(r"\s+", " ", line).strip()
        if line:
            lines.append(line)
    return "\n".join(lines)


def parse_entries(soup: BeautifulSoup, base_url: str) -> list[dict]:
    """解析公告列表：标题、日期、链接。优先取 DataGrid1 表格，取不到则整页兜底。"""
    entries: list[dict] = []
    seen: set[str] = set()

    def add(a) -> None:
        title = re.sub(r"\s+", " ", a.get_text(" ", strip=True)).strip()
        if len(title) < 5:
            return
        href = urljoin(base_url, a["href"])
        if href in seen:
            return
        container = a.find_parent("tr") or a.find_parent("td") or a.parent
        text_around = container.get_text(" ", strip=True) if container else ""
        m = DATE_RE.search(text_around)
        if not m:
            return
        seen.add(href)
        entries.append({"title": title, "date": m.group(0), "href": href})

    table = soup.find("table", id="DataGrid1")
    if table:
        for tr in table.find_all("tr"):
            a = tr.find("a", href=True)
            if a:
                add(a)
    if not entries:  # 兜底：整页找「链接 + 日期」的组合
        for a in soup.find_all("a", href=True):
            add(a)
    return entries


# ---------------------------------------------------------------- 差异与邮件

def changed_snippet(old: str, new: str, ctx: int = 60) -> str:
    """定位两段文本的差异中段，各带前后 ctx 字上下文，用于「条目未变但页面有变」的情况。"""
    n, m = len(old), len(new)
    i = 0
    while i < n and i < m and old[i] == new[i]:
        i += 1
    j = 0
    while j < (n - i) and j < (m - i) and old[n - 1 - j] == new[m - 1 - j]:
        j += 1
    seg_old, seg_new = old[i:n - j], new[i:m - j]
    if not seg_old.strip() and not seg_new.strip():
        return ""
    pre = old[max(0, i - ctx):i]
    post = old[n - j:min(n, n - j + ctx)]
    return f"……{pre}\n  【旧】{seg_old}\n  【新】{seg_new}\n  {post}……"


def build_diff(old_page: dict, new_entries: list[dict], new_text: str) -> dict:
    old_map = {e["href"]: e for e in old_page.get("entries", [])}
    new_map = {e["href"]: e for e in new_entries}
    return {
        "added": [e for h, e in new_map.items() if h not in old_map],
        "removed": [e for h, e in old_map.items() if h not in new_map],
        "retitled": [
            {"old": old_map[h]["title"], "new": new_map[h]["title"], "href": h}
            for h in new_map
            if h in old_map and old_map[h]["title"] != new_map[h]["title"]
        ],
        "snippet": changed_snippet(old_page.get("text", ""), new_text),
    }


def change_mail(name: str, url: str, diff: dict, entries: list[dict]):
    """内容变化提醒邮件，返回 (subject, text, html)。"""
    subject = f"【网页更新提醒】{name} 检测到更新"
    lines = [
        "你监控的页面检测到更新。",
        "",
        f"页面：{name}",
        f"地址：{url}",
        f"检测时间：{now_cn()}",
        "",
    ]
    if diff["added"]:
        lines.append(f"新增公告 {len(diff['added'])} 条：")
        for e in diff["added"]:
            lines.append(f"  · {e['date']}  {e['title']}")
            lines.append(f"    {e['href']}")
        lines.append("")
    if diff["retitled"]:
        lines.append("标题有修改：")
        for r in diff["retitled"]:
            lines.append(f"  · {r['old']}  ->  {r['new']}")
        lines.append("")
    if diff["removed"]:
        lines.append("移除条目：")
        for e in diff["removed"]:
            lines.append(f"  · {e['title']}")
        lines.append("")
    if not (diff["added"] or diff["removed"] or diff["retitled"]):
        lines.append("公告条目未变，但页面其他内容有变化：")
        lines.append(diff["snippet"] or "（未定位到具体差异）")
        lines.append("")
    lines.append("当前最新公告（前 10 条）：")
    for e in entries[:10]:
        lines.append(f"  · {e['date']}  {e['title']}")
    if len(entries) > 10:
        lines.append(f"  ……共 {len(entries)} 条")
    lines.append("")
    lines.append("—— 本邮件由 GitHub Actions 自动发送")
    text = "\n".join(lines)

    parts = [
        f'<h2 style="margin:0 0 4px">🔔 {esc(name)} 检测到更新</h2>',
        f'<p style="color:#555;margin:0 0 12px">检测时间：{esc(now_cn())} · '
        f'<a href="{esc(url)}">打开页面</a></p>',
    ]
    if diff["added"]:
        items = "".join(
            f'<li style="margin:6px 0">【{esc(e["date"])}】'
            f'<a href="{esc(e["href"])}">{esc(e["title"])}</a></li>'
            for e in diff["added"]
        )
        parts.append(f'<h3>新增公告（{len(diff["added"])} 条）</h3><ul>{items}</ul>')
    if diff["retitled"]:
        items = "".join(
            f'<li>{esc(r["old"])} → <b>{esc(r["new"])}</b></li>' for r in diff["retitled"]
        )
        parts.append(f"<h3>标题修改</h3><ul>{items}</ul>")
    if diff["removed"]:
        items = "".join(f"<li>{esc(e['title'])}</li>" for e in diff["removed"])
        parts.append(f"<h3>移除条目</h3><ul>{items}</ul>")
    if not (diff["added"] or diff["removed"] or diff["retitled"]):
        snip = esc(diff["snippet"] or "（未定位到具体差异）").replace("\n", "<br>")
        parts.append(
            '<h3>页面其他内容变化</h3>'
            f'<pre style="white-space:pre-wrap;background:#f6f6f6;padding:10px">{snip}</pre>'
        )
    items = "".join(
        f'<li style="margin:4px 0">【{esc(e["date"])}】'
        f'<a href="{esc(e["href"])}">{esc(e["title"])}</a></li>'
        for e in entries[:10]
    )
    parts.append(f"<h3>当前最新公告（前 10 条）</h3><ol>{items}</ol>")
    html_body = (
        '<div style="font-family:sans-serif;max-width:640px;line-height:1.6">'
        + "".join(parts)
        + '<hr><p style="color:#999;font-size:12px">'
          "本邮件由 hjmd-monitor（GitHub Actions）自动发送</p></div>"
    )
    return subject, text, html_body


def failure_mail(name: str, url: str, count: int, last_err: str):
    """连续抓取失败提醒邮件。"""
    subject = f"【监控异常】{name} 连续 {count} 次抓取失败"
    text = "\n".join([
        f"监控页面已连续 {count} 次抓取失败（每次间隔约 5 分钟），请留意：",
        "",
        f"页面：{name}",
        f"地址：{url}",
        f"最近错误：{last_err}",
        "",
        "可能是对方服务器临时维护或网络故障。恢复后将自动继续监控，本异常只提醒一次。",
        "",
        "—— 本邮件由 GitHub Actions 自动发送",
    ])
    html_body = (
        '<div style="font-family:sans-serif;max-width:640px;line-height:1.6">'
        f'<h2 style="margin:0 0 4px">⚠️ {esc(name)} 连续 {count} 次抓取失败</h2>'
        f'<p>地址：<a href="{esc(url)}">{esc(url)}</a></p>'
        f"<p>最近错误：<code>{esc(last_err)}</code></p>"
        "<p>可能是对方服务器临时维护或网络故障。恢复后将自动继续监控，本异常只提醒一次。</p>"
        '<hr><p style="color:#999;font-size:12px">本邮件由 hjmd-monitor（GitHub Actions）自动发送</p></div>'
    )
    return subject, text, html_body


def send_mail(subject: str, text: str, html_body: str, dry: bool) -> bool:
    if dry:
        print("=" * 70)
        print(f"To: {os.environ.get('MAIL_TO', '(未配置)')}")
        print(f"Subject: {subject}")
        print("-" * 70)
        print(text)
        print("=" * 70)
        return True

    host = os.environ.get("SMTP_HOST")
    user = os.environ.get("SMTP_USER")
    password = os.environ.get("SMTP_PASS")
    to_raw = os.environ.get("MAIL_TO", "")
    port = int(os.environ.get("SMTP_PORT") or 465)
    if not (host and user and password and to_raw):
        log("SMTP 配置不完整（需要 SMTP_HOST/SMTP_PORT/SMTP_USER/SMTP_PASS/MAIL_TO），无法发信")
        return False
    recipients = [x.strip() for x in to_raw.split(",") if x.strip()]

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = os.environ.get("MAIL_FROM") or user
    msg["To"] = ", ".join(recipients)
    msg.set_content(text)
    msg.add_alternative(html_body, subtype="html")

    try:
        if port == 465:
            with smtplib.SMTP_SSL(host, port, timeout=30) as s:
                s.login(user, password)
                s.send_message(msg)
        else:  # 587 等端口走 STARTTLS
            with smtplib.SMTP(host, port, timeout=30) as s:
                s.starttls()
                s.login(user, password)
                s.send_message(msg)
        log(f"邮件已发送 -> {', '.join(recipients)}")
        return True
    except Exception as exc:
        log(f"邮件发送失败：{exc!r}")
        return False


# ---------------------------------------------------------------- Git（仅 CI）

def git(*args, check: bool = True):
    return subprocess.run(
        ["git", *args], capture_output=True, text=True,
        encoding="utf-8", errors="replace", check=check,
    )


def git_push(message: str, allow_empty: bool = False) -> None:
    repo = os.environ["GITHUB_REPOSITORY"]
    token = os.environ.get("GH_TOKEN", "")
    branch = os.environ.get("GITHUB_REF_NAME", "main")
    git("config", "user.name", "github-actions[bot]")
    git("config", "user.email", "41898282+github-actions[bot]@users.noreply.github.com")
    git("remote", "set-url", "origin", f"https://x-access-token:{token}@github.com/{repo}.git")
    git("add", str(STATE_PATH), check=False)
    args = ["commit", "-m", message]
    if allow_empty:
        args.append("--allow-empty")
    r = git(*args, check=False)
    if r.returncode != 0:
        log("状态无变化，跳过提交")
        return
    git("pull", "--rebase", "origin", branch, check=False)
    git("push", "origin", f"HEAD:{branch}")
    log(f"已推送：{message}")


def maybe_keepalive(cfg: dict) -> None:
    """距最近一次提交超过 keepalive_days 天时，做一次空提交，
    防止仓库 60 天无活动导致 GitHub 停用定时工作流。"""
    days = int(cfg.get("keepalive_days", 7))
    r = git("log", "-1", "--format=%ct", check=False)
    if r.returncode != 0 or not r.stdout.strip():
        return
    try:
        last = int(r.stdout.strip())
    except ValueError:
        return
    if time.time() - last > days * 86400:
        git_push("chore: 定时空提交保活 [skip ci]", allow_empty=True)
        log("已提交 keepalive，避免定时任务被 GitHub 停用")


# ---------------------------------------------------------------- 主流程

def main() -> int:
    ap = argparse.ArgumentParser(description="网页更新监控")
    ap.add_argument("--dry-run", action="store_true", help="不发邮件、不推送，邮件内容打印到终端")
    args = ap.parse_args()
    dry = args.dry_run

    cfg = load_json(CONFIG_PATH, None)
    if not cfg or not cfg.get("urls"):
        log(f"配置缺失或没有监控地址：{CONFIG_PATH}")
        return 2

    in_ci = bool(os.environ.get("GITHUB_REPOSITORY"))
    if dry:
        log("dry-run 模式：不发邮件、不推送")

    state = load_json(STATE_PATH, {"pages": {}, "failures": {}, "failure_alerted": {}})
    state.setdefault("pages", {})
    state.setdefault("failures", {})
    state.setdefault("failure_alerted", {})

    threshold = int(cfg.get("failure_alert_after", 6))
    state_dirty = False

    for item in cfg["urls"]:
        name, url = item["name"], item["url"]

        # ---- 抓取
        try:
            page_html = fetch(url, int(cfg.get("request_timeout", 30)))
        except Exception as exc:
            count = state["failures"].get(url, 0) + 1
            state["failures"][url] = count
            log(f"{name} 抓取失败（连续第 {count} 次）：{exc}")
            if count >= threshold and not state["failure_alerted"].get(url):
                subject, text, html_body = failure_mail(name, url, count, str(exc))
                if send_mail(subject, text, html_body, dry):
                    state["failure_alerted"][url] = True
            state_dirty = True
            continue

        if state["failures"].get(url):
            log(f"{name} 抓取已恢复")
        if state["failures"].get(url) != 0:
            state["failures"][url] = 0
            state_dirty = True
        if state["failure_alerted"].pop(url, None) is not None:
            state_dirty = True

        # ---- 解析与对比
        soup = BeautifulSoup(page_html, "html.parser")
        text = visible_text(page_html)
        entries = parse_entries(soup, url)
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        page = state["pages"].get(url)

        if page is None:
            log(f"{name} 首次运行，建立基线（{len(entries)} 条公告），不发提醒")
            state["pages"][url] = {
                "text_hash": digest,
                "entries": entries,
                "text": text,
                "last_change": now_cn(),
            }
            state_dirty = True
            continue

        if page.get("text_hash") == digest:
            log(f"{name} 无变化")
            continue

        # ---- 有变化：先发邮件，成功后才更新状态（失败则下一轮重发）
        diff = build_diff(page, entries, text)
        log(
            f"{name} 检测到更新：新增 {len(diff['added'])} 条，"
            f"移除 {len(diff['removed'])} 条，改题 {len(diff['retitled'])} 条"
        )
        subject, text_body, html_body = change_mail(name, url, diff, entries)
        if not send_mail(subject, text_body, html_body, dry):
            log("邮件发送失败，本轮不更新状态，下一轮将重发提醒")
            continue
        page.update({
            "text_hash": digest,
            "entries": entries,
            "text": text,
            "last_change": now_cn(),
        })
        state_dirty = True

    if state_dirty:
        write_json(STATE_PATH, state)
        if in_ci and not dry:
            try:
                git_push("chore: 更新监控状态 [skip ci]")
            except Exception as exc:
                log(f"状态提交推送失败：{exc}")
                return 1
    elif in_ci and not dry:
        maybe_keepalive(cfg)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
