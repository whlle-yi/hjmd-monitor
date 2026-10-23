#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""获奖名单检索：在名单附件（PDF / Excel）里定位「指定学校 + 指定队员」，推断获奖等级。

只做一件事：给定一条公告（entry）和关注目标（学校、姓名），把它挂的名单文件拉下来、
抽出文字、在目标学校附近找到该队员，再回退最近的等级小标题，得出「X 等奖」。

被 src/monitor.py 调用，不单独运行。

设计要点：
- 只认「文字型」PDF / Excel。扫描件（整页是图片、无文本层）会返回 status="no_text"，
  由邮件提示人工查看，不做 OCR（表格 OCR 误识别姓名风险高，宁可如实说不知道）。
- 学校名与队员名用「字符位置邻近」判定，而不是假定表格行结构，因此对
  「一格一行」（PDF 常见）和「一行一条」（Excel 常见）都成立。
- 等级取目标位置之前最近的等级小标题，兼容「本科组一等奖（共 164 项）」
  「本科组推荐参评名单（47 项）」这类不同写法。
"""

from __future__ import annotations

import difflib
import io
import os
import re
import shutil
import subprocess
import tempfile
import time
from urllib.parse import urlencode, urljoin

import requests

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

FILE_SUFFIX = (".pdf", ".xlsx", ".xls", ".docx", ".doc")
FILE_RE = re.compile(
    r"""href=["']([^"']+?\.(?:pdf|xlsx|xls|docx|doc))["']""", re.I
)
AWARD_WORD_RE = re.compile(r"名单|评奖|评审|获奖|结果")
LEVEL_WORD_RE = re.compile(r"特等奖|一等奖|二等奖|三等奖")
RECOMMEND_RE = re.compile(r"推荐参评|推荐全国|参与全国|全国奖评审")
SCHOOL_TAIL_RE = re.compile(r"(大学|学院|学校|职院|高职|分校|校区)$")
SEQ_RE = re.compile(r"^\d{1,4}$")
TOPIC_RE = re.compile(r"^[A-F]$")

# 报名系统（全国大学生数学建模竞赛报名/评审系统）的附件接口
CNKI_ENCLOSURE_API = "https://cumcm.cnki.net/cumcm/Enclosure/GetEnclosure"
CNKI_DOWNLOAD_API = "https://cumcm.cnki.net/cumcm/Enclosure/Download"


def log(msg: str) -> None:
    print(f"[award] {msg}", flush=True)


def _session(direct: bool) -> requests.Session:
    s = requests.Session()
    s.trust_env = not direct  # direct=True 时绕过系统代理直连
    s.headers["User-Agent"] = UA
    return s


def http(method: str, url: str, timeout: int, referer: str | None = None,
         stream: bool = False, retries: int = 3, **kw):
    """与 monitor.fetch 同样的代理回退策略；返回 Response（调用方负责 status 判断）。"""
    headers = kw.pop("headers", {}) or {}
    if referer:
        headers.setdefault("Referer", referer)
    headers.setdefault("Accept-Language", "zh-CN,zh;q=0.9")
    last_err = "unknown"
    for attempt in range(1, retries + 1):
        for direct in (False, True):
            try:
                r = _session(direct).request(
                    method, url, timeout=timeout, headers=headers, stream=stream, **kw
                )
                if r.status_code == 200:
                    return r
                last_err = f"HTTP {r.status_code}"
            except Exception as exc:
                last_err = repr(exc)
        log(f"请求失败（第 {attempt}/{retries} 次）：{url} <- {last_err}")
        time.sleep(3 * attempt)
    raise RuntimeError(f"连续 {retries} 次请求失败：{url}（{last_err}）")


# ---------------------------------------------------------------- 附件定位

def is_list_file(url: str) -> bool:
    return url.lower().split("?")[0].endswith(FILE_SUFFIX)


CNKI_DETAIL_RE = re.compile(
    r"cumcm\.cnki\.net/cumcm/studentHome/noticeDetail\?id=([\w-]+)", re.I
)


def cnki_enclosures(detail_url: str, timeout: int) -> list[str]:
    """报名系统（cumcm.cnki.net）的附件：详情页是 Vue，HTML 里没有链接，
    要调 GET /cumcm/Enclosure/GetEnclosure?noticeId=... 取文件编码，再拼下载地址。"""
    m = CNKI_DETAIL_RE.search(detail_url)
    if not m:
        return []
    notice_id = m.group(1)
    api = f"{CNKI_ENCLOSURE_API}?noticeId={notice_id}&time={int(time.time() * 1000)}"
    r = http("GET", api, timeout, referer=detail_url, retries=2)
    payload = r.json()
    rows = ((payload.get("Data") or {}).get("date")) or []  # 对方接口字段就叫 date
    out: list[str] = []
    for row in rows:
        code = row.get("EnclosureFileCode") or ""
        name = row.get("EnclosureName") or "attachment"
        ftype = row.get("EnclosureFileTypeName") or ""
        if not code:
            continue
        q = urlencode({"fileCode": code, "fileName": name, "fileType": ftype})
        out.append(f"{CNKI_DOWNLOAD_API}?{q}")
    return out


def list_attachments(entry: dict, timeout: int) -> list[str]:
    """返回该公告挂的名单文件地址（PDF/Excel/Word）。

    公告本身可能就是文件直链；否则抓详情页找附件链接；
    报名系统那种 Vue 详情页 HTML 里没有链接，改走它的附件接口。
    """
    href = entry["href"]
    r = http("GET", href, timeout, referer=urljoin(href, "/"))
    ctype = (r.headers.get("content-type") or "").lower()
    if is_list_file(href) or "pdf" in ctype or "excel" in ctype or "officedocument" in ctype:
        return [href]

    r.encoding = r.apparent_encoding or "utf-8"
    urls: list[str] = []
    for m in FILE_RE.finditer(r.text):
        u = urljoin(href, m.group(1))
        if u not in urls:
            urls.append(u)
    if not urls and CNKI_DETAIL_RE.search(href):
        try:
            urls = cnki_enclosures(href, timeout)
        except Exception as exc:
            log(f"报名系统附件接口取不到：{exc!r}")
    return urls


def fingerprint(url: str, timeout: int) -> str:
    """文件指纹（用于「同一份名单不必每次重下」）：ETag / Last-Modified / 长度。"""
    try:
        r = http("HEAD", url, timeout, retries=1)
        parts = [
            r.headers.get("etag", ""),
            r.headers.get("last-modified", ""),
            r.headers.get("content-length", ""),
        ]
        fp = "|".join(p for p in parts if p)
        return fp
    except Exception:
        return ""


def download(url: str, timeout: int, max_bytes: int) -> bytes:
    r = http("GET", url, timeout, stream=True)
    length = int(r.headers.get("content-length") or 0)
    if length and length > max_bytes:
        r.close()
        raise RuntimeError(f"文件过大（{length / 1048576:.1f}MB > 上限 {max_bytes / 1048576:.0f}MB）")
    buf = io.BytesIO()
    for chunk in r.iter_content(65536):
        buf.write(chunk)
        if buf.tell() > max_bytes:
            r.close()
            raise RuntimeError(f"文件超过 {max_bytes / 1048576:.0f}MB 上限，已中止")
    return buf.getvalue()


# ---------------------------------------------------------------- 文本抽取

def pdf_text(data: bytes) -> tuple[str, int]:
    """返回 (文本, 页数)。无文本层（扫描件）时文本为空串。"""
    import pymupdf

    doc = pymupdf.open(stream=data, filetype="pdf")
    try:
        text = "\n".join(page.get_text() for page in doc)
        return text, doc.page_count
    finally:
        doc.close()


def sheet_text(data: bytes) -> str:
    """Excel 转文本：一格一行、按行优先顺序，和 PDF 的「一格一行」保持同构。"""
    import openpyxl

    wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    out: list[str] = []
    try:
        for ws in wb.worksheets:
            for row in ws.iter_rows(values_only=True):
                for cell in row:
                    if cell is None:
                        continue
                    s = str(cell).strip()
                    if s:
                        out.append(s)
    finally:
        wb.close()
    return "\n".join(out)


def extract_text(url: str, data: bytes) -> tuple[str, str]:
    """按文件内容判断格式再抽文字，返回 (文本, 格式说明)。

    不靠后缀判断——报名系统的下载地址是 /Enclosure/Download?fileCode=xxx，没有扩展名；
    按魔术字节判断（PDF 是 %PDF，xlsx/docx 是 zip 的 PK 开头）更稳。
    """
    head = data[:8]
    if head.startswith(b"%PDF"):
        text, pages = pdf_text(data)
        return text, f"PDF（{pages} 页）"
    if head.startswith(b"PK"):
        try:
            return sheet_text(data), "Excel"
        except Exception as exc:
            raise RuntimeError(f"Office 文件解析失败（可能是 .docx）：{exc}")
    if head.startswith(b"\xd0\xcf\x11\xe0"):
        raise RuntimeError("旧版 .doc/.xls 暂不支持，请人工查看")
    raise RuntimeError(f"不认识的文件格式（不是 PDF/Excel）：{url.rsplit('/', 1)[-1][:60]}")


# ---------------------------------------------------------------- 扫描件 OCR

_TESSDATA = os.environ.get("TESSDATA_PREFIX") or ""

def _env() -> dict:
    env = os.environ.copy()
    if _TESSDATA:
        env["TESSDATA_PREFIX"] = _TESSDATA
    return env


def tesseract_ok(tessdata: str | None = None) -> bool:
    """本机有没有可用的 tesseract + 中文模型。"""
    global _TESSDATA
    if tessdata:
        _TESSDATA = str(tessdata)
    exe = shutil.which("tesseract")
    if not exe:
        return False
    try:
        r = subprocess.run([exe, "--list-langs"], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=30, env=_env())
        return "chi_sim" in ((r.stdout or "") + (r.stderr or ""))
    except Exception:
        return False


def _ocr(pix) -> str:
    exe = shutil.which("tesseract")
    fd, path = tempfile.mkstemp(suffix=".png")
    os.close(fd)
    try:
        pix.save(path)
        r = subprocess.run(
            [exe, path, "stdout", "-l", "chi_sim", "--psm", "6"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=180, env=_env(),
        )
        if r.returncode != 0 and not (r.stdout or "").strip():
            log(f"OCR 失败：{(r.stderr or '').strip()[:160]}")
        return r.stdout or ""
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


_CELL_NOISE = re.compile(r"[^\u4e00-\u9fa5A-Za-z0-9·、\.\(\)（）]")
_LEVEL_RE = re.compile(r"(专科组|本科组|高职高专组|研究生组)?(特等奖|一等奖|二等奖|三等奖)")


def _cell(s: str) -> str:
    """去掉 OCR 在汉字之间插的空格和各种线框噪声，还原成裸文本。"""
    return _CELL_NOISE.sub("", s)


_SCHOOL_TAIL = ("大学", "学院", "学校", "职院", "分校", "校区")


def _table_rows(text: str, min_cells: int = 3) -> list[list[str]]:
    """把 OCR 文本切成单元格。不强制首列是数字——序号列经常被识别成「1 东」之类。"""
    rows = []
    for line in text.split("\n"):
        if line.count("|") < min_cells - 1:
            continue
        cells = [c for c in (_cell(x) for x in line.split("|")) if c]
        if len(cells) >= min_cells:
            rows.append(cells)
    return rows


def _seq_of(cells: list[str]) -> str:
    for c in cells[:3]:
        if c.isdigit():
            return c
    return ""


def _region_cell(cells: list[str], region: str) -> int:
    """返回「赛区」这一格的列号；靠长度排除「江西财经大学」这种校名。"""
    for k, c in enumerate(cells[:5]):
        if region in c and len(c) <= 5 and not c.endswith(_SCHOOL_TAIL):
            return k
    return -1


def _record_from_row(cells: list[str], region: str) -> dict | None:
    """从一行单元格里取出一条记录。

    主锚点是「赛区」格（值等于 region，靠长度排除校名）。若找不到赛区格，
    退一步用「学校」格反推：学校格前一个是赛区、后三个是队员。
    这样即使列顺序调整（比如赛区列被合并/去掉），只要学校名还在，就还能读出来。
    """
    k = _region_cell(cells, region)
    if k >= 1 and k + 3 < len(cells):
        return {
            "seq": _seq_of(cells[:k]),
            "zone": cells[k],
            "school": cells[k + 1],
            "members": cells[k + 2:k + 5],
            "teacher": "、".join(cells[k + 5:k + 7]),
        }
    for j, c in enumerate(cells):
        if len(c) >= 4 and c.endswith(_SCHOOL_TAIL) and j + 3 < len(cells):
            return {
                "seq": _seq_of(cells[:j]),
                "zone": cells[j - 1] if j else "",
                "school": c,
                "members": cells[j + 1:j + 4],
                "teacher": "、".join(cells[j + 4:j + 6]),
            }
    return None


def _level_in_line(line: str) -> str:
    flat = _cell(line)
    if "等奖" not in flat or len(flat) > 26:
        return ""
    m = _LEVEL_RE.search(flat)
    if not m:
        return ""
    return re.sub(r"[（(].*$", "", flat)


def ocr_pdf_records(data: bytes, aw: dict, timeout: int) -> tuple[list[dict], str]:
    """扫描件（无文本层）走 OCR 取表格记录。

    按页顺序整页 OCR 一遍，边读边维护「当前分节标题」：
      「本科组一等奖（294名）」这类标题一出现就更新，之后的行都归到这一节。
    顺序扫描是为了让等级归属天然正确——分节标题和它下面的行必须按文档先后配对，
    跳页抽样会把二等奖的行错挂到一等奖上（实测过）。

    只保留「赛区 = region（默认江西）」的行：省赛区在名单里是连续的一段，
    其余赛区不参与匹配，也就不需要记住整张表。

    返回 (records, note)；note 是给邮件/日志看的一句话说明。
    """
    import pymupdf

    zone = aw.get("region") or "江西"
    max_pages = int(aw.get("ocr_max_pages", 80))
    budget = float(aw.get("ocr_budget_sec", 300))
    dpi = int(aw.get("ocr_dpi", 220))
    if aw.get("tessdata"):
        tesseract_ok(aw["tessdata"])  # 顺手把语言包目录设上
    t0 = time.time()
    doc = pymupdf.open(stream=data, filetype="pdf")
    try:
        if doc.page_count > max_pages:
            return [], f"页数过多（{doc.page_count} 页），跳过 OCR"

        records: list[dict] = []
        level = ""
        for i, page in enumerate(doc):
            if time.time() - t0 > budget:
                return records, f"OCR 超时（预算 {budget:.0f}s），只读到第 {i} 页"
            txt = _ocr(page.get_pixmap(dpi=dpi))
            for line in txt.split("\n"):
                if "|" not in line:
                    lv = _level_in_line(line)  # 分节标题
                    if lv:
                        level = lv
                    continue
                cells = [c for c in (_cell(x) for x in line.split("|")) if c]
                rec = _record_from_row(cells, zone)
                if rec:
                    records.append({**rec, "level": level, "page": i + 1})
        if not records:
            return [], f"OCR 了 {doc.page_count} 页，没找到「{zone}」的记录"
        return records, f"OCR 了 {doc.page_count} 页，取到 {len(records)} 条{zone}记录"
    finally:
        doc.close()


def _similar(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, a, b).ratio()


def match_ocr_records(records: list[dict], school: str, name: str):
    """在 OCR 出来的记录里找目标队伍。

    返回 (hits, probable)：
      hits     学校名与队员名都对上的
      probable 队员名只差一两个字（OCR 常见错字），需要人工确认的
    """
    hits, probable = [], []
    for rec in records:
        if _similar(rec["school"], school) < 0.8:
            continue
        for who in rec["members"]:
            if who == name:
                hits.append({**rec, "ocr_name": who, "probable": False})
                break
            if (len(who) == len(name) == 3 and who[0] == name[0]
                    and sum(a == b for a, b in zip(who, name)) >= 2):
                probable.append({**rec, "ocr_name": who, "probable": True})
                break
    return hits, probable



def _line_of(text: str, pos: int) -> tuple[int, str]:
    """返回 pos 所在的行号与该行内容。"""
    idx = text.count("\n", 0, pos)
    lines = text.split("\n")
    idx = min(idx, len(lines) - 1)
    return idx, lines[idx]


def _level_before(text: str, pos: int) -> tuple[str, int]:
    """目标位置之前最近的等级小标题，如「本科组一等奖（共 164 项）」；找不到返回 ("", -1)。"""
    best, best_pos = "", -1
    for m in LEVEL_WORD_RE.finditer(text, 0, pos):
        _, line = _line_of(text, m.start())
        line = line.strip()
        # 小标题很短；排除「某校获一等奖」这种正文长句
        if line and len(line) <= 30 and not SCHOOL_TAIL_RE.search(line):
            best, best_pos = line, m.start()
    return best, best_pos


def _section_line(text: str, pos: int, pattern: re.Pattern) -> str:
    best = ""
    for m in pattern.finditer(text, 0, pos):
        _, line = _line_of(text, m.start())
        line = line.strip()
        if line and len(line) <= 40:
            best = line
    return best


HEADER_TOKENS = {
    "序号", "学校", "学", "校", "学校名称", "参赛学生", "参赛", "指导教师", "指导",
    "教师", "队员一", "队员二", "队员三", "赛区", "区号", "获奖等级", "等级", "队号", "题目",
}


def find_record(text: str, school: str, name: str, max_hits: int = 3) -> list[dict]:
    """在文本里定位「学校 + 队员」，返回命中记录（通常 0 或 1 条）。

    先按「学校 → 后面三个词是队员」的严格结构匹配；匹配不到再做一次宽松匹配
    （只要学校名附近出现这个姓名就算）。宽松命中的会带 loose=True，邮件里会注明
    「按姓名在名单里出现的位置判断」，避免排版一变就直接返回「未获奖」。
    """
    hits = _strict_records(text, school, name, max_hits)
    return hits or _loose_records(text, school, name, max_hits)


def _strict_records(text: str, school: str, name: str, max_hits: int) -> list[dict]:
    hits: list[dict] = []
    starts = [m.start() for m in re.finditer(re.escape(school), text)]
    for k, pos in enumerate(starts):
        end = starts[k + 1] if k + 1 < len(starts) else pos + 400
        seg = text[pos + len(school):end]
        if name not in seg:
            continue

        tokens = [t for t in re.split(r"\s+", seg) if t]
        tokens = [t for t in tokens if t not in HEADER_TOKENS]
        # 片段末尾是下一条记录的序号/选题，去掉
        while tokens and (SEQ_RE.match(tokens[-1]) or TOPIC_RE.match(tokens[-1])):
            tokens.pop()
        if name not in tokens[:4]:  # 队员名必须落在本条记录的队员位
            continue

        students = tokens[:3]
        # 指导教师最多两位；遇到下一条记录的序号、或翻页后的表头碎片就停
        teachers: list[str] = []
        for t in tokens[3:6]:
            if SEQ_RE.match(t) or TOPIC_RE.match(t) or t in HEADER_TOKENS:
                break
            teachers.append(t)
            if len(teachers) >= 2:
                break

        # 序号 / 选题：紧贴学校名之前的那几个字符
        before = text[max(0, pos - 16):pos]
        ms = re.search(r"(\d{1,4})\s*$", before)
        mt = re.search(r"([A-F])\s*$", before)
        seq = ms.group(1) if ms else ""
        topic = mt.group(1) if mt else ""

        level, _ = _level_before(text, pos)
        recommend = "" if level else _section_line(text, pos, RECOMMEND_RE)

        hits.append({
            "school": school,
            "name": name,
            "seq": seq,
            "topic": topic,
            "members": students,
            "teacher": "、".join(teachers),
            "level": level,
            "recommend": recommend,
            "snippet": " / ".join([school] + tokens),
        })
        if len(hits) >= max_hits:
            break
    return hits


def _loose_records(text: str, school: str, name: str, max_hits: int) -> list[dict]:
    """宽松兜底：学校名与该姓名出现在同一段（到下一个学校名为止）就算命中。

    只在严格匹配失败时使用——排版/列顺序变了以后，队员未必还在「学校后第三个词」的位置，
    但名字通常仍紧跟在同一行附近。宁可给一条带 loose 标记的线索，也不要直接判「没获奖」。
    """
    hits: list[dict] = []
    starts = [m.start() for m in re.finditer(re.escape(school), text)]
    for k, pos in enumerate(starts):
        # 前后各扩到相邻的同名学校，这样即使「队员在学校名之前」（列顺序被调过）也能看到
        lo = starts[k - 1] + len(school) if k else max(0, pos - 300)
        hi = starts[k + 1] if k + 1 < len(starts) else pos + 300
        seg = text[lo:hi]
        if name not in seg:
            continue
        tokens = [t for t in re.split(r"\s+", seg) if t and t not in HEADER_TOKENS][:8]
        level, _ = _level_before(text, pos)
        recommend = "" if level else _section_line(text, pos, RECOMMEND_RE)
        hits.append({
            "school": school,
            "name": name,
            "seq": "",
            "topic": "",
            "members": [],
            "teacher": "",
            "level": level,
            "recommend": recommend,
            "snippet": " / ".join(tokens),
            "loose": True,
        })
        if len(hits) >= max_hits:
            break
    return hits


# ---------------------------------------------------------------- 对外入口

def _unrelated_reason(text: str, aw: dict) -> str:
    """文本读通了，但看着不像我们要的名单时返回原因（用于区分「真没获奖」和「没读懂」）。"""
    region = aw.get("region") or "江西"
    if region not in text:
        return f"文件里没出现「{region}」，可能不是完整名单、或名单格式已变"
    schools = len(re.findall(r"[\u4e00-\u9fa5]{2,12}(?:大学|学院|学校|职院|分校)", text))
    if schools < 3:
        return f"文件里几乎找不到学校名（只有 {schools} 处），可能已经不是名单表格了"
    return ""


def _ocr_quality_reason(records: list[dict], aw: dict) -> str:
    """OCR 出来的记录是否像一张正常的获奖名单表。"""
    region = aw.get("region") or "江西"
    if len(records) < 3:
        return f"只解析出 {len(records)} 条{region}记录，表格结构可能已变"
    n = len(records)
    if sum(1 for r in records if len([m for m in r.get("members", []) if m]) >= 2) / n < 0.6:
        return "多数记录缺队员列，列顺序可能已变"
    if sum(1 for r in records if r.get("school", "").endswith(_SCHOOL_TAIL)) / n < 0.6:
        return "多数记录取不到学校名，列顺序可能已变"
    return ""


def inspect_entry(entry: dict, aw: dict, timeout: int) -> dict:
    """检索一条公告挂的名单文件。

    返回 dict，status 取值：
      found      命中（含 level / members / teacher 等；OCR 命中的带 ocr=True）
      absent     文件读通了、结构也正常，但确实没有该学校该队员
      unparsed   文件读到了，但结构不像预期（格式变了 / 只发了一部分），不敢下结论
      no_text    扫描件，且本机没有可用的 OCR
      no_file    公告里没有可解析的名单文件
      error      下载/解析失败（下一轮会重试）

    「absent」和「unparsed」必须分清：前者是「你没有获奖」，后者是「我看不懂」。
    混淆的代价很大——排版一变就把「看不懂」报成「没获奖」。
    """
    school, name = aw["school"], aw["name"]
    max_bytes = int(aw.get("max_file_mb", 40)) * 1048576
    result: dict = {"status": "no_file", "entry": entry, "files": [], "hits": []}
    try:
        files = list_attachments(entry, timeout)
    except Exception as exc:
        return {**result, "status": "error", "error": f"打开公告失败：{exc}"}
    if not files:
        return result

    readable, scanned, errors, doubts = 0, [], [], []
    for url in files[:4]:  # 一条公告最多看 4 个附件
        try:
            data = download(url, timeout, max_bytes)
            text, kind = extract_text(url, data)
        except Exception as exc:
            errors.append(f"{url.rsplit('/', 1)[-1]}: {exc}")
            continue
        result["files"].append({"url": url, "kind": kind})
        if len(text.strip()) < 50:  # 扫描件：没有文本层
            scanned.append((url, kind, data))
            continue
        readable += 1
        hits = find_record(text, school, name)
        for h in hits:
            result["hits"].append({**h, "file": url, "kind": kind})
        if not hits:
            why = _unrelated_reason(text, aw)
            if why:
                doubts.append(why)

    # 扫描件兜底：OCR 只扫「赛区=江西」那一段，命中就填进去
    if not result["hits"] and scanned:
        if not aw.get("ocr", True):
            result["status"] = "no_text"
            result["note"] = "扫描件，且配置里关了 OCR"
            return result
        if not tesseract_ok():
            result["status"] = "no_text"
            result["files"] = [f for f in result["files"] if f["url"] in {s[0] for s in scanned}]
            result["note"] = "扫描件，且本机没有安装 tesseract（含 chi_sim），无法自动检索"
            return result
        for url, kind, data in scanned:
            records, note = ocr_pdf_records(data, aw, timeout)
            result["note"] = note
            if not records:
                doubts.append(note or "OCR 没取到记录")
                continue
            quality = _ocr_quality_reason(records, aw)
            hits, probable = match_ocr_records(records, school, name)
            readable += 1
            if quality and not (hits or probable):
                doubts.append(quality)
            for h in hits:
                result["hits"].append({
                    "school": school, "name": name, "seq": h["seq"], "topic": "",
                    "members": h["members"], "teacher": h["teacher"], "level": h["level"],
                    "recommend": "", "snippet": " / ".join([h["school"], *h["members"]]),
                    "file": url, "kind": f"{kind}·OCR", "ocr": True,
                    "probable": False, "ocr_name": h["ocr_name"], "page": h["page"],
                })
            for h in probable:
                result["hits"].append({
                    "school": school, "name": name, "seq": h["seq"], "topic": "",
                    "members": h["members"], "teacher": h["teacher"], "level": h["level"],
                    "recommend": "", "snippet": " / ".join([h["school"], *h["members"]]),
                    "file": url, "kind": f"{kind}·OCR", "ocr": True,
                    "probable": True, "ocr_name": h["ocr_name"], "page": h["page"],
                })
            if result["hits"]:
                break

    if result["hits"]:
        result["status"] = "found"
    elif doubts:
        # 读到了内容，但结构不像预期：不敢说「你没获奖」，标成 unparsed 让邮件提示人工看一眼
        result["status"] = "unparsed"
        result["note"] = "；".join(dict.fromkeys(doubts))
    elif readable:
        result["status"] = "absent"
    elif scanned:
        result["status"] = "no_text"
    else:
        result["status"] = "error"
        result["error"] = "; ".join(errors) or "没有可读取的名单文件"
    return result


def is_candidate(entry: dict, aw: dict) -> bool:
    """只认「本年度的国赛获奖名单」，省赛名单 / 拟推荐国奖名单一律不翻。

    两类国赛名单都会翻：
      - 全国版：「2026高教社杯全国大学生数学建模竞赛获奖名单」（官网/报名系统，扫描件）；
      - 江西赛区转发的国奖结果：「关于公布2026年江西省参加全国大学生数学建模竞赛获奖名单的说明」
        （文字版 PDF，能真正读出等级）。
    不翻的：省赛「江西省大学生数学建模竞赛获奖名单」、拟推荐/评审名单。
    """
    title = entry.get("title", "")
    year = str(aw.get("year") or "")
    if year and year not in title:
        return False
    include = aw.get("title_include") or r"获奖名单"
    exclude = aw.get("title_exclude")
    if exclude is None:
        exclude = (
            r"拟推荐|推荐参评|推荐全国|评审|省赛|省级|市赛|赛区"
            r"|江西省大学生数学建模竞赛"
        )
    if not re.search(include, title):
        return False
    return not re.search(exclude, title)


def level_text(hit: dict) -> str:
    """把等级整理成一句人话（纯文本；HTML 版自行加粗）。"""
    level = (hit.get("level") or "").strip()
    if level:
        level = re.sub(r"[（(]\s*共\s*\d+\s*项\s*[)）]", "", level).strip()
        s = f"你们获得了 {level}"
    elif hit.get("recommend"):
        s = f"你们的队伍进入了「{hit['recommend']}」"
    else:
        s = "名单里查到了你们队伍，等级以名单原文为准"
    if hit.get("probability_note"):
        s += f"（{hit['probability_note']}）"
    elif hit.get("probable"):
        s += f"（OCR 识别为「{hit.get('ocr_name', '')}」，一字之差，可能是你们）"
    elif hit.get("loose"):
        s += "（名单排版与预期不同，这是按姓名位置判断的结果，建议顺手核对一下）"
    return s
