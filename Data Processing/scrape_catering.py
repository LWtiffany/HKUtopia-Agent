# scrape_catering.py
# HKU CEDARS Catering Outlets - Robust scraper for "Latest Opening Hours"
#
# 功能亮点
# - 多店解析（一个 details 区块内可能有多家店）
# - 与页面上的 “... details” 链接按顺序一一对齐，抓 Outlet Name / Location / Contact / 详情链接
# - 把“星期标签”（Monday–Friday / Saturday & Public Holiday …）拼进每条时段
# - 解析 “Adjusted opening hours from DD/MM/YYYY to DD/MM/YYYY” 的有效期
# - 提供“标准化时段”（Closed→00:00-00:00、All-day→00:00-23:59）并连同原始时段一起写入
# - 断行时间合并（"10:00 -" + 下一行 "17:30" → "10:00 - 17:30"）
# - 并发抓详情（--fast），或完全跳过详情（--no-details），以及 DEBUG 预览（--debug）
# - 使用 timezone-aware UTC 时间
#
# 依赖安装：
#   pip install requests beautifulsoup4 lxml requests-cache
#
# 用法：
#   python scrape_catering.py                     # 默认：抓详情（串行），输出 data/catering.json
#   python scrape_catering.py -o out.json --fast  # 并发抓详情（更快）
#   python scrape_catering.py --no-details        # 跳过详情页，仅抓营业时间（最快）
#   python scrape_catering.py --debug             # 打印 DEBUG 预览

import os
import re
import json
import argparse
import requests
import requests_cache
from bs4 import BeautifulSoup
from datetime import datetime, timezone
from urllib.parse import urljoin
from itertools import zip_longest
from concurrent.futures import ThreadPoolExecutor, as_completed

LIST_URL = "https://www.cedars.hku.hk/campuslife/catering/catering-outlets/"
UA = "HKU-Tour-Agent/1.1 (+contact@example.com)"

# ---------------- 基础工具 ----------------

def norm(s: str) -> str:
    return re.sub(r"[ \t\u00A0]+", " ", s.strip())

def merge_broken_time_lines(lines):
    """把 '10:00 -' + 下一行 '17:30' 合并为 '10:00 - 17:30'。"""
    merged, i = [], 0
    while i < len(lines):
        ln = lines[i]
        if re.match(r"^\s*\d{1,2}:\d{2}\s*-\s*$", ln) and i + 1 < len(lines):
            nxt = lines[i + 1]
            if re.match(r"^\s*\d{1,2}:\d{2}\s*$", nxt):
                merged.append(norm(f"{ln} {nxt}"))
                i += 2
                continue
        merged.append(ln)
        i += 1
    return merged

def is_time_line(ln: str) -> bool:
    return bool(
        re.search(r"\b\d{1,2}:\d{2}\s*-\s*\d{1,2}:\d{2}\b", ln)
        or re.search(r"\bAll-?day\b", ln, re.I)
        or ln.strip().lower().endswith("closed")
    )

# “星期行”检测：Monday - Friday / Saturday & Public Holiday 等
DAY_HDR_RE = re.compile(
    r"^(?:(Mon|Tue|Wed|Thu|Fri|Sat|Sun)(?:day)?)"
    r"(?:\s*-\s*(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)(?:day)?)?"
    r"(?:\s*&\s*Public\s+Holiday(?:s)?)?$",
    re.I
)
def is_day_heading(ln: str) -> bool:
    return bool(DAY_HDR_RE.match(ln.strip()))

def is_noise_line(ln: str) -> bool:
    low = ln.lower()
    if low.startswith("image:"): return True
    if re.match(r"^open(?:ing)?\s*hours\b", low): return True
    if re.match(r"^adjusted opening hours\b", low): return True
    if re.search(r"(will be closed|due to|arrangements during bad weather|order online)", low): return True
    if ln in ("Main Campus","Centennial Campus","Sassoon Road Campus","Sassoon Road Campus Campus"): return True
    if is_day_heading(ln): return True
    return False

TITLE_KW = [
    "restaurant","canteen","kiosk","subway","coffee","nook","hub",
    "vegetarian","gourmet","academics","oori","foodtopia","oliver",
    "bijas","grove","union","pizza express","vending","starbucks",
    "cafe","café","kitchen","bistro","bakery","deli"
]

def is_title_candidate(ln: str) -> bool:
    if is_time_line(ln) or is_noise_line(ln): return False
    low = ln.strip().lower()
    if low in ("closed",): return False
    if any(k in low for k in TITLE_KW): return True
    # 读得像名字：首字符为字母/数字，其余允许 &-./()' 和空格
    return bool(re.match(r"^[A-Za-z0-9][A-Za-z0-9&\-\./()' ]{1,79}$", ln))

def campus_from_title(title: str, campus_hint: str | None) -> str | None:
    if campus_hint: return campus_hint
    t = title.lower()
    if "sassoon road" in t: return "Sassoon Road Campus"
    if "centennial campus" in t: return "Centennial Campus"
    if "main campus" in t: return "Main Campus"
    return None

def looks_suspicious_title(title: str) -> bool:
    low = title.lower()
    if re.match(r"^\d{1,2}:\d{2}", title): return True
    if low in ("closed","all-day"): return True
    if "will be closed" in low or "due to" in low: return True
    if is_day_heading(title): return True
    return False

def normalize_segment(seg: str) -> str:
    """标准化时段：Closed → 00:00-00:00 (Closed)；All-day → 00:00-23:59 (All-day)；否则保持原格式"""
    s = seg.strip()
    low = s.lower()
    if low.endswith("closed") or " closed" in low:
        s = re.sub(r"\b\d{1,2}:\d{2}\s*-\s*\d{1,2}:\d{2}\b", "00:00 - 00:00", s)
        if "00:00 - 00:00" not in s:
            s = (s + " 00:00 - 00:00").strip()
        if "(Closed)" not in s:
            s = s + " (Closed)"
        return norm(s)
    if re.search(r"\ball-?day\b", low):
        s = re.sub(r"\ball-?day\b", "", s, flags=re.I).strip()
        if s and not re.search(r"\d{1,2}:\d{2}\s*-\s*\d{1,2}:\d{2}", s):
            s = f"{s} 00:00 - 23:59 (All-day)"
        elif not s:
            s = "00:00 - 23:59 (All-day)"
        return norm(s)
    return norm(s)

# ---------------- 分块与解析 ----------------

def split_outlet_blocks(soup: BeautifulSoup):
    """返回与每个 '* details' 链接一一对应的文本块（跳过页头）。"""
    full = soup.get_text("\n", strip=True).replace("\r", "")
    parts = re.split(r"(?i)(?:\+\s*)?(?:outlet|vending)\s*details\b", full)
    return parts[1:]  # len(blocks) == #details

DATE_RANGE_RE = re.compile(r"from\s+(\d{2}/\d{2}/\d{4})\s+to\s+(\d{2}/\d{2}/\d{4})", re.I)

def backtrack_title(lines, start_idx):
    """在出现 Opening/Adjusted 行时，回溯若干行寻找合适的标题。"""
    for k in range(max(0, start_idx - 12), start_idx)[::-1]:
        cand = norm(lines[k])
        if not cand:
            continue
        if is_time_line(cand) or is_noise_line(cand):
            continue
        if not looks_suspicious_title(cand):
            return cand
    return None

def parse_block_multi(block_text: str, campus_hint: str | None):
    """一个块里可能有多家店：Title -> (Adjusted?) -> Opening Hours -> 时间行（带星期标签），并解析 Adjusted 有效期。"""
    raw_lines = [x for x in block_text.split("\n")]
    lines = [norm(x) for x in raw_lines if norm(x)]
    lines = merge_broken_time_lines(lines)

    items, i, cur = [], 0, None  # cur = {"title","adj","hours","campus","adjusted_range"}

    while i < len(lines):
        ln = lines[i]

        # 新标题
        if is_title_candidate(ln):
            if cur: items.append(cur)
            cur = {"title": ln, "adj": [], "hours": [], "campus": campus_from_title(ln, campus_hint)}
            i += 1
            # 标题后直接跟时间（无显式 Opening Hours）
            while i < len(lines) and is_time_line(lines[i]):
                cur["hours"].append(lines[i]); i += 1
            continue

        # Adjusted opening hours（带星期标签）
        if re.match(r"^adjusted opening hours\b", ln, re.I):
            if not cur:
                bt = backtrack_title(lines, i)
                if bt:
                    cur = {"title": bt, "adj": [], "hours": [], "campus": campus_from_title(bt, campus_hint)}
            # 解析有效期
            m = DATE_RANGE_RE.search(ln)
            if m and cur:
                cur["adjusted_range"] = {"from": m.group(1), "to": m.group(2)}
            j = i + 1
            current_label = None
            while j < len(lines) \
                and not re.match(r"^open(?:ing)?\s*hours\b", lines[j], re.I) \
                and not is_title_candidate(lines[j]) \
                and not re.match(r"(Main Campus|Centennial Campus|Sassoon Road Campus)", lines[j], re.I):
                if is_day_heading(lines[j]):
                    current_label = lines[j]; j += 1; continue
                if cur and is_time_line(lines[j]):
                    seg = f"{current_label} {lines[j]}" if current_label else lines[j]
                    cur["adj"].append(seg)
                j += 1
            i = j
            continue

        # Opening Hours（带星期标签）
        if re.match(r"^open(?:ing)?\s*hours\b", ln, re.I):
            if not cur:
                bt = backtrack_title(lines, i)
                if bt:
                    cur = {"title": bt, "adj": [], "hours": [], "campus": campus_from_title(bt, campus_hint)}
            j = i + 1
            current_label = None
            while j < len(lines) \
                and not is_title_candidate(lines[j]) \
                and not re.match(r"(Main Campus|Centennial Campus|Sassoon Road Campus)", lines[j], re.I):
                if is_day_heading(lines[j]):
                    current_label = lines[j]; j += 1; continue
                if cur and is_time_line(lines[j]):
                    seg = f"{current_label} {lines[j]}" if current_label else lines[j]
                    cur["hours"].append(seg)
                j += 1
            i = j
            continue

        i += 1

    if cur: items.append(cur)

    # 清洗
    clean = []
    for c in items:
        title = c["title"].strip()
        if looks_suspicious_title(title):
            continue
        if not (c["hours"] or c["adj"]):
            continue
        out = {
            "title": title,
            "campus": c["campus"] or campus_hint,
            "adjusted": c["adj"] or None,
            "hours": c["hours"] or None
        }
        if c.get("adjusted_range"):
            out["adjusted_range"] = c["adjusted_range"]
        clean.append(out)
    return clean

# ---------------- 校区 hint & 详情页 ----------------

def guess_campus_hints(soup: BeautifulSoup):
    """顺序扫描页面文本：每遇到一个 '* details'，记录最近的校区名为该块的 hint。"""
    txt = soup.get_text("\n", strip=True).split("\n")
    hints, cur = [], None
    for ln in txt:
        ln = norm(ln)
        if ln in ("Main Campus","Centennial Campus","Sassoon Road Campus"):
            cur = ln
        elif re.search(r"(?i)(?:\+\s*)?(?:outlet|vending)\s*details\b", ln):
            hints.append(cur)
    return hints  # len == len(blocks)

def _fetch_detail(url: str) -> dict:
    """抓取单个详情页（Outlet Name / Location / Contact / URL）。"""
    name = loc = tel = None
    if url:
        try:
            r = requests.get(url, headers={"User-Agent": UA}, timeout=20)
            r.raise_for_status()
            s2 = BeautifulSoup(r.text, "lxml")
            block = s2.get_text("\n", strip=True)
            m = re.search(r"Outlet Name\s+([^\n]+)", block, re.I)
            if m: name = norm(m.group(1))
            m = re.search(r"Location\s+([^\n]+)", block, re.I)
            if m: loc = norm(m.group(1))
            m = re.search(r"Contact\s+([0-9\- ]{6,})", block, re.I)
            if m: tel = norm(m.group(1))
        except Exception:
            pass
    return {"name": name, "location": loc, "contact": tel, "detail_url": url}

def fetch_detail_info_list(soup: BeautifulSoup, fast: bool = False):
    """按页面顺序抓取每个 '* details' 的信息；fast=True 时并发抓取。"""
    links = []
    for a in soup.find_all("a"):
        if re.search(r"(?i)(?:outlet|vending)\s*details\b", a.get_text(" ", strip=True)):
            href = a.get("href")
            links.append(urljoin(LIST_URL, href) if href else None)

    if not fast:
        return [_fetch_detail(u) for u in links]

    out = [None] * len(links)
    with ThreadPoolExecutor(max_workers=min(8, max(2, len(links)))) as ex:
        futs = {ex.submit(_fetch_detail, u): i for i, u in enumerate(links)}
        for fut in as_completed(futs):
            out[futs[fut]] = fut.result()
    return out

# ---------------- 主流程 ----------------

def main():
    ap = argparse.ArgumentParser(description="HKU CEDARS catering hours scraper")
    ap.add_argument("-o", "--output", default="data/catering.json", help="输出文件路径 (默认: data/catering.json)")
    ap.add_argument("--fast", action="store_true", help="并发抓详情页，加速")
    ap.add_argument("--no-details", action="store_true", help="跳过详情页（不抓名字/地址/电话），最快")
    ap.add_argument("--debug", action="store_true", help="打印 DEBUG 预览信息")
    args = ap.parse_args()

    requests_cache.install_cache("cache", expire_after=1800)
    r = requests.get(LIST_URL, headers={"User-Agent": UA}, timeout=30)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "lxml")

    blocks = split_outlet_blocks(soup)
    campus_hints = guess_campus_hints(soup)

    if args.no_details:
        detail_infos = [{}] * len(blocks)
    else:
        detail_infos = fetch_detail_info_list(soup, fast=args.fast)

    if args.debug:
        print(f"[DEBUG] blocks={len(blocks)}, campus_hints={len(campus_hints)}, details={len(detail_infos)}")
        for bi, b in enumerate(blocks[:2]):
            prev = [x for x in merge_broken_time_lines([norm(y) for y in b.split("\n") if norm(y)])][:18]
            print(f"[DEBUG] block#{bi} preview:", "\\n".join(prev))

    items = []
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    # 用 zip_longest 容忍三者数量略有偏差
    for idx, (b, campus_hint, di) in enumerate(zip_longest(blocks, campus_hints, detail_infos, fillvalue=None)):
        if b is None:
            break
        parsed = parse_block_multi(b, campus_hint)

        # 用详情页权威 “Outlet Name” 覆盖块内第一家店的标题（通常一块=1店）
        if parsed and di and isinstance(di, dict) and di.get("name"):
            parsed[0]["title"] = di["name"]

        for j, p in enumerate(parsed):
            hours_adj = p["adjusted"] or []
            hours_reg = p["hours"] or []
            norm_adj = [normalize_segment(x) for x in hours_adj]
            norm_reg = [normalize_segment(x) for x in hours_reg]

            meta = {
                "hours_adjusted": hours_adj or None,
                "hours_regular": hours_reg or None,
                "normalized_hours_adjusted": norm_adj or None,
                "normalized_hours_regular": norm_reg or None,
                "campus": p["campus"],
                "source_url": LIST_URL,
                "last_scraped": today
            }
            if p.get("adjusted_range"):
                meta["adjusted_valid_from"] = p["adjusted_range"]["from"]
                meta["adjusted_valid_to"]   = p["adjusted_range"]["to"]

            if j == 0 and di and isinstance(di, dict):
                if di.get("location"):   meta["location"]   = di["location"]
                if di.get("contact"):    meta["contact"]    = di["contact"]
                if di.get("detail_url"): meta["detail_url"] = di["detail_url"]

            key = re.sub(r"[^a-z0-9]+", "_", p["title"].lower()).strip("_")
            items.append({
                "id": f"dining_{key}",
                "type": "dining",
                "lang": "en",
                "title": p["title"],
                "text": "; ".join((hours_adj or []) + (hours_reg or [])) or "",
                "metadata": meta
            })

    # 去重 & 保存
    seen, deduped = set(), []
    for x in items:
        if x["id"] in seen:
            continue
        seen.add(x["id"]); deduped.append(x)

    odir = os.path.dirname(args.output)
    if odir:
        os.makedirs(odir, exist_ok=True)
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(deduped, f, ensure_ascii=False, indent=2)

    print(f"抓到 {len(deduped)} 家门店，已写入 {args.output}")

if __name__ == "__main__":
    main()
