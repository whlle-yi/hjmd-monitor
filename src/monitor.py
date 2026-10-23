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
import urllib3
from bs4 import BeautifulSoup

import award

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config" / "monitor.json"
STATE_PATH = ROOT / "state" / "state.json"

TZ_CN = timezone(timedelta(hours=8))  # 东八区，仅用于展示时间
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
DATE_RE = re.compile(r"20\d{2}年\d{1,2}月\d{1,2}日")

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


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

def _send(sess: requests.Session, method: str, url: str, **kw):
    """发一次请求；证书校验失败时降级为不校验再试一次。

    原因：cumcm.cnki.net 这类国内站点对境外节点返回的证书与域名不匹配
    （GitHub Actions 上必然踩到，本机在国内则正常）。抓的是公开只读内容，
    降级可接受；日志里会留痕。
    """
    try:
        return sess.request(method, url, **kw)
    except requests.exceptions.SSLError:
        log(f"证书校验不通过，改为不校验证书重试：{url.split('?')[0]}")
        sess.verify = False
        return sess.request(method, url, **kw)


def fetch(url: str, timeout: int, retries: int = 3) -> str:
    """抓取页面：先走系统代理（若配置了），失败自动直连重试。"""
    last_err = "unknown"
    for attempt in range(1, retries + 1):
        for direct in (False, True):
            try:
                sess = requests.Session()
                sess.trust_env = not direct
                r = _send(
                    sess, "GET", url,
                    headers={"User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9"},
                    timeout=timeout,
                )
                if r.status_code == 200:
                    r.encoding = r.apparent_encoding or "utf-8"
                    return r.text
                last_err = f"HTTP {r.status_code}"
            except Exception as exc:  # 网络抖动、代理不可用等
                last_err = repr(exc)
        log(f"抓取失败（第 {attempt}/{retries} 次）：{last_err}")
        time.sleep(3 * attempt)
    raise RuntimeError(f"连续 {retries} 次抓取失败，最后错误：{last_err}")


def dig(obj, path: str):
    """按 "Data.data" 这类点分路径取嵌套字段，取不到返回 None。"""
    cur = obj
    for part in path.split("."):
        if isinstance(cur, dict):
            cur = cur.get(part)
        elif isinstance(cur, list) and part.isdigit() and int(part) < len(cur):
            cur = cur[int(part)]
        else:
            return None
    return cur


def fetch_api(item: dict, timeout: int, retries: int = 3) -> dict:
    """抓取 JSON 接口（用于 Vue/前后端分离页面）：同样先走系统代理、失败直连重试。"""
    api = item["api"]
    url = api["url"]
    method = api.get("method", "POST").upper()
    headers = {
        "User-Agent": UA,
        "Accept": "application/json, text/javascript, */*; q=0.1",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "X-Requested-With": "XMLHttpRequest",
    }
    if api.get("referer"):
        headers["Referer"] = api["referer"]
    data = api.get("data") if method == "POST" else None
    code_ok = api.get("code_ok", 200)

    last_err = "unknown"
    for attempt in range(1, retries + 1):
        for direct in (False, True):
            try:
                sess = requests.Session()
                sess.trust_env = not direct
                sep = "&" if "?" in url else "?"
                bust = f"{url}{sep}_t={int(time.time() * 1000)}"  # 绕开接口/CDN 缓存
                r = _send(sess, method, bust, headers=headers, data=data, timeout=timeout)
                if r.status_code != 200:
                    last_err = f"HTTP {r.status_code}"
                    continue
                payload = r.json()  # JSON 由 requests 按 utf-8 解析，勿手动改 encoding
                if not isinstance(payload, dict):
                    last_err = "接口返回的不是 JSON 对象"
                    continue
                code = payload.get("Code", code_ok)
                if code == code_ok:
                    return payload
                last_err = f"接口返回 Code={code} {payload.get('ErrorMessage') or ''}".strip()
            except Exception as exc:  # 网络抖动、代理不可用、返回体不是 JSON 等
                last_err = repr(exc)
        log(f"接口抓取失败（第 {attempt}/{retries} 次）：{last_err}")
        time.sleep(3 * attempt)
    raise RuntimeError(f"连续 {retries} 次接口抓取失败，最后错误：{last_err}")


def norm_date(raw: str) -> str:
    """把 "2026/05/14 16:06:23" / "2026-5-14" 统一成 "2026年05月14日"（与列表页邮件风格一致）。"""
    m = re.search(r"(\d{4})[/\-年.](\d{1,2})[/\-月.](\d{1,2})", raw or "")
    if not m:
        return ""
    return f"{m.group(1)}年{int(m.group(2)):02d}月{int(m.group(3)):02d}日"


def parse_api_entries(item: dict, payload: dict) -> tuple[list[dict], str]:
    """从 JSON 接口返回体解析条目，并生成用于哈希/差异定位的文本。"""
    api = item["api"]
    rows = dig(payload, api.get("list_path", "Data.data")) or []
    if not isinstance(rows, list):
        raise RuntimeError(f"接口字段 {api.get('list_path', 'Data.data')} 不是列表，页面结构可能已变")

    title_field = api.get("title_field", "Title")
    date_field = api.get("date_field", "PublishTime")
    id_field = api.get("id_field", "Id")
    content_field = api.get("content_field")
    href_tpl = api.get("href_template", "{id}")

    entries: list[dict] = []
    lines: list[str] = []
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        title = re.sub(r"\s+", " ", str(row.get(title_field) or "")).strip()
        if not title:
            continue
        rid = str(row.get(id_field) or "")
        href = href_tpl.replace("{id}", rid).replace("{title}", title)
        href = urljoin(item["url"], href)
        if href in seen:
            continue
        seen.add(href)
        date = norm_date(str(row.get(date_field) or ""))
        entries.append({"title": title, "date": date, "href": href})
        line = f"{date} {title}"
        if content_field:
            body = visible_text(str(row.get(content_field) or "")).replace("\n", " ")
            if body:
                line = f"{line} | {body}"
        lines.append(line)

    lines.insert(0, f"共 {len(entries)} 条")
    return entries, "\n".join(lines)


def collect(item: dict, timeout: int) -> tuple[list[dict], str]:
    """抓取单个监控项，返回 (条目列表, 用于哈希与差异定位的文本)。

    - 配置里有 api  -> 走 JSON 接口（前后端分离页面）；
    - 否则          -> 抓 HTML 页面，解析列表并提取可见文本。
    """
    if item.get("api"):
        payload = fetch_api(item, timeout)
        return parse_api_entries(item, payload)

    page_html = fetch(item["url"], timeout)
    soup = BeautifulSoup(page_html, "html.parser")
    entries = parse_entries(
        soup, item["url"],
        require_date=bool(item.get("require_date", True)),
        title_pattern=item.get("title_pattern"),
    )
    return entries, visible_text(page_html)


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


def parse_entries(
    soup: BeautifulSoup, base_url: str,
    require_date: bool = True, title_pattern: str | None = None,
) -> list[dict]:
    """解析公告列表：标题、日期、链接。

    require_date=True  适用于「标题 + 日期」式列表页（如江西赛区获奖名单）；
    require_date=False 适用于纯链接列表页（如竞赛官网历年结果页，条目无日期），
    此时收集页面正文中所有像条目的链接（标题 >= 6 字，跳过自引用与去重）。
    title_pattern     按标题正则过滤（如 "20\\\\d{2}" 只保留带年份的条目）。
    """
    pattern = re.compile(title_pattern) if title_pattern else None
    entries: list[dict] = []
    seen: set[str] = set()
    base = base_url.rstrip("/")

    def add(a, need_date: bool) -> None:
        title = re.sub(r"\s+", " ", a.get_text(" ", strip=True)).strip()
        if len(title) < (5 if need_date else 6):
            return
        if pattern and not pattern.search(title):
            return
        href = urljoin(base_url, a["href"])
        if href.rstrip("/") == base or href in seen:
            return
        container = a.find_parent("tr") or a.find_parent("td") or a.parent
        text_around = container.get_text(" ", strip=True) if container else ""
        m = DATE_RE.search(text_around)
        if need_date and not m:
            return
        seen.add(href)
        entries.append({"title": title, "date": m.group(0) if m else "", "href": href})

    table = soup.find("table", id="DataGrid1")
    if table:
        for tr in table.find_all("tr"):
            a = tr.find("a", href=True)
            if a:
                add(a, require_date)
    if not entries:  # 兜底
        if require_date:  # 整页找「链接 + 日期」的组合
            for a in soup.find_all("a", href=True):
                add(a, True)
        else:  # 无日期模式：收集全部像条目的链接
            for a in soup.find_all("a", href=True):
                add(a, False)
    return entries


# ---------------------------------------------------------------- 获奖名单检索

def check_awards(item: dict, new_entries: list[dict], entries: list[dict],
                 state: dict, cfg: dict, timeout: int):
    """只对「本次新增的」国奖名单公告去翻附件，找「目标学校 + 目标队员」。

    返回 (hits, pending, changed, warnings)：
      hits     本次要写进邮件的新命中
      pending  命中记录，**等邮件发成功再写进 state**，否则发信失败就漏报了
      changed  非命中的检索结果是否已写入 state（需要落盘）
      warnings 名单读到了但结构不像预期（状态 unparsed），要在邮件里如实说明

    规则：
    - 只翻本次 diff 里新增的公告，存量/基线公告一律不翻（各页面各翻各的，文件未必相同）；
    - 标题必须像「2026 … 获奖名单」，且不像省赛/拟推荐名单（见 award.is_candidate）；
    - 例外：上次没读成的（附件还没挂上 / 下载失败）会补试 `retry_attempts` 次，
      有的站点先发公告、过一会儿才挂 PDF；
    - 同一份文件被多个页面挂出来时只提醒一次（按文件 URL 去重），不同名单照常各自检索。
    """
    aw = cfg.get("award_watch") or {}
    if not aw.get("enabled"):
        return [], {}, False, []
    known = state.setdefault("awards", {})
    reported = set(state.get("reported_files") or [])
    retry_max = int(aw.get("retry_attempts", 3))

    todo: list[dict] = []
    for e in new_entries:
        if award.is_candidate(e, aw) and e["href"] not in known:
            todo.append(e)
    for e in entries:  # 补试上次没读成的同一条新公告
        r = known.get(e["href"]) or {}
        if r.get("status") in ("error", "no_file") and int(r.get("attempts", 1)) < retry_max:
            if award.is_candidate(e, aw) and all(x["href"] != e["href"] for x in todo):
                todo.append(e)

    hits: list[dict] = []
    pending: dict = {}
    changed = False
    warnings: list[dict] = []

    for e in todo:
        href = e["href"]
        log(f"检索名单：{e['title'][:40]}")
        try:
            res = award.inspect_entry(e, aw, timeout)
        except Exception as exc:  # 兜底：不让名单检索拖垮整轮监控
            log(f"名单检索异常（下轮再试）：{exc!r}")
            continue

        status = res["status"]
        record = {
            "title": e["title"],
            "status": status,
            "attempts": int((known.get(href) or {}).get("attempts", 0)) + 1,
            "checked_at": now_cn(),
            "files": res.get("files", []),
        }
        if res.get("error"):
            record["error"] = res["error"]

        if status == "found":
            best = res["hits"][0]
            record["hit"] = best
            log(f"命中：{aw['school']} {aw['name']} -> {award.level_text(best)}"
                f"（{best.get('file', '').rsplit('/', 1)[-1]}）")
            fresh = [h for h in res["hits"] if not h.get("file") or h["file"] not in reported]
            if fresh:
                pending[href] = record
                hits.append({
                    "entry_title": e["title"],
                    "entry_href": href,
                    "hit": fresh[0],
                    "extras": fresh[1:],
                })
            else:  # 同一份文件已在别的页面报过，只记录不重复提醒
                known[href] = record
                changed = True
                log("该名单文件此前已提醒过，跳过重复通知")
        else:
            if status == "absent":
                log(f"未命中（结构正常，确实没有）：{e['title'][:40]}")
            elif status == "unparsed":
                note = res.get("note") or "结构不像预期"
                log(f"名单读到但没读懂，不敢下结论：{note}")
                warnings.append({
                    "entry_title": e["title"],
                    "entry_href": href,
                    "note": note,
                })
            elif status == "no_text":
                log(f"名单是扫描件，且本机没有可用 OCR：{e['title'][:40]}")
            else:
                log(f"名单读取失败（{status}），第 {record['attempts']} 次：{record.get('error', '')[:100]}")
            known[href] = record
            changed = True

    return hits, pending, changed, warnings


def commit_awards(state: dict, pending: dict, hits: list[dict]) -> None:
    """邮件发出去了才把命中结果落库，并记下已提醒过的名单文件（跨页面去重用）。"""
    state.setdefault("awards", {}).update(pending)
    files = set(state.get("reported_files") or [])
    files |= {h["hit"]["file"] for h in hits if h["hit"].get("file")}
    state["reported_files"] = sorted(files)


def award_text_block(hits: list[dict]) -> list[str]:
    out = ["", "=" * 46, "🎉 名单里查到你们队伍", "=" * 46]
    for h in hits:
        hit = h["hit"]
        out.append(f"◆ {award.level_text(hit)}")
        out.append(f"  名单：{h['entry_title']}")
        out.append(f"  队伍：{hit['school']}　{'、'.join(hit['members'])}")
        if hit.get("teacher"):
            out.append(f"  指导教师：{hit['teacher']}")
        if hit.get("seq"):
            out.append(f"  序号：{hit['seq']}")
        if hit.get("file"):
            out.append(f"  文件：{hit['file']}")
        for e in h.get("extras", []):
            out.append(f"  另外也出现在：{e.get('file', '')}")
    out.append("")
    return out


def award_html_block(hits: list[dict]) -> str:
    rows = []
    for h in hits:
        hit = h["hit"]
        level = award.level_text(hit)
        # 「你们获得了 本科组一等奖」→ 加粗等级部分
        m = re.match(r"你们获得了\s*(.+)$", level)
        shown = f"你们获得了 <b>{esc(m.group(1))}</b>" if m else esc(level)
        detail = [
            f"名单：<a href=\"{esc(h['entry_href'])}\">{esc(h['entry_title'])}</a>",
            f"队伍：{esc(hit['school'])}　{esc('、'.join(hit['members']))}",
        ]
        if hit.get("teacher"):
            detail.append(f"指导教师：{esc(hit['teacher'])}")
        if hit.get("seq"):
            detail.append(f"序号：{esc(hit['seq'])}")
        if hit.get("file"):
            detail.append(f"文件：<a href=\"{esc(hit['file'])}\">原名单</a>")
        rows.append(
            f'<div style="margin:10px 0;padding:10px 12px;background:#fff8e1;'
            f'border-left:4px solid #f0a020;border-radius:4px">'
            f'<div style="font-size:16px">🎉 {shown}</div>'
            f'<div style="color:#555;font-size:13px;margin-top:6px">'
            + "<br>".join(detail)
            + "</div></div>"
        )
    return "".join(rows)


def award_subject(hits: list[dict]) -> str:
    """提示邮件标题：把等级直接写进主题，不用点开就知道结果。"""
    h = hits[0]["hit"]
    lv = award.level_text(h).replace("你们获得了 ", "")
    s = f"【获奖名单】{h['school']}·{h['name']}：{lv}"
    return s if len(s) <= 62 else s[:60] + "…"


def award_mail(hits: list[dict], page_name: str):
    """只有名单命中、页面本身没变化时单独发的邮件。"""
    first = hits[0]["hit"]
    subject = award_subject(hits)
    text = "\n".join(
        ["你监控的页面里出现了一份可以检索的获奖名单，其中查到了你们队伍：", ""]
        + [f"页面：{page_name}", f"检测时间：{now_cn()}", ""]
        + award_text_block(hits)
        + ["—— 本邮件由 GitHub Actions 自动发送"]
    )
    html_body = (
        '<div style="font-family:sans-serif;max-width:640px;line-height:1.6">'
        f"<h2 style=\"margin:0 0 4px\">🎉 名单里有你们</h2>"
        f'<p style="color:#555;margin:0 0 12px">页面：{esc(page_name)} · 检测时间：{esc(now_cn())}</p>'
        + award_html_block(hits)
        + '<hr><p style="color:#999;font-size:12px">'
          "本邮件由 hjmd-monitor（GitHub Actions）自动发送</p></div>"
    )
    return subject, text, html_body


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


def fmt_date(date: str) -> str:
    return f"【{date}】" if date else ""


def award_missing_block(warnings: list[dict]) -> list[str]:
    """名单读到了、但结构不像预期时的提示——宁可说「没读懂」，也不说「没获奖」。"""
    out = ["", "-" * 46, "⚠️ 新名单我没读懂，需要你瞄一眼", "-" * 46]
    for w in warnings:
        out.append(f"  · {w['entry_title']}")
        out.append(f"    原因：{w['note']}")
        out.append(f"    {w['entry_href']}")
    out.append("  这一条我没敢下「有没有你」的结论，别当成没获奖。")
    out.append("")
    return out


def award_missing_html(warnings: list[dict]) -> str:
    items = "".join(
        f'<li style="margin:6px 0"><a href="{esc(w["entry_href"])}">{esc(w["entry_title"])}</a>'
        f'<div style="color:#888;font-size:12px">{esc(w["note"])}</div></li>'
        for w in warnings
    )
    return (
        '<div style="margin:12px 0;padding:10px 12px;background:#fff4f4;'
        'border-left:4px solid #e06060;border-radius:4px">'
        '<div style="font-size:15px">⚠️ 新名单我没读懂</div>'
        f'<ul style="margin:6px 0 0;padding-left:18px;font-size:13px">{items}</ul>'
        '<div style="color:#666;font-size:12px;margin-top:6px">'
        "这一条我没敢下「有没有你」的结论，别当成没获奖。</div></div>"
    )


def change_mail(name: str, url: str, diff: dict, entries: list[dict],
                awards: list[dict] | None = None, warnings: list[dict] | None = None):
    """内容变化提醒邮件，返回 (subject, text, html)。"""
    awards = awards or []
    warnings = warnings or []
    subject = f"【网页更新提醒】{name} 检测到更新"
    if awards:
        subject = award_subject(awards)
    lines = [
        "你监控的页面检测到更新。",
        "",
        f"页面：{name}",
        f"地址：{url}",
        f"检测时间：{now_cn()}",
        "",
    ]
    if awards:
        lines += award_text_block(awards)
    if warnings:
        lines += award_missing_block(warnings)
    if diff["added"]:
        lines.append(f"新增公告 {len(diff['added'])} 条：")
        for e in diff["added"]:
            lines.append(f"  · {fmt_date(e['date'])}{e['title']}")
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
        lines.append(f"  · {fmt_date(e['date'])}{e['title']}")
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
    if awards:
        parts.append(award_html_block(awards))
    if warnings:
        parts.append(award_missing_html(warnings))
    if diff["added"]:
        items = "".join(
            f'<li style="margin:6px 0">{esc(fmt_date(e["date"]))}'
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
        f'<li style="margin:4px 0">{esc(fmt_date(e["date"]))}'
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
    state.setdefault("awards", {})

    threshold = int(cfg.get("failure_alert_after", 6))
    state_dirty = False

    for item in cfg["urls"]:
        name, url = item["name"], item["url"]

        # ---- 抓取与解析
        try:
            entries, text = collect(item, int(cfg.get("request_timeout", 30)))
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

        # ---- 对比
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
            # 只补试「上次没读成」的新公告（例如刚发公告时 PDF 还没挂上），不翻存量名单
            awards, pending_awards, aw_changed, _warn = check_awards(
                item, [], entries, state, cfg, int(cfg.get("request_timeout", 30))
            )
            if aw_changed:
                state_dirty = True
            if awards:
                subject, t_body, h_body = award_mail(awards, name)
                if send_mail(subject, t_body, h_body, dry):
                    commit_awards(state, pending_awards, awards)
                    state_dirty = True
            continue

        # ---- 有变化：先发邮件，成功后才更新状态（失败则下一轮重发）
        diff = build_diff(page, entries, text)
        log(
            f"{name} 检测到更新：新增 {len(diff['added'])} 条，"
            f"移除 {len(diff['removed'])} 条，改题 {len(diff['retitled'])} 条"
        )
        # 只有这次新增的公告才去翻 PDF，且必须是本年度国赛获奖名单
        awards, pending_awards, aw_changed, aw_warnings = check_awards(
            item, diff["added"], entries, state, cfg, int(cfg.get("request_timeout", 30))
        )
        if aw_changed:
            state_dirty = True
        subject, text_body, html_body = change_mail(name, url, diff, entries, awards, aw_warnings)
        if not send_mail(subject, text_body, html_body, dry):
            log("邮件发送失败，本轮不更新状态，下一轮将重发提醒")
            continue
        commit_awards(state, pending_awards, awards)
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
