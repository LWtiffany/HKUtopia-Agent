# scrape_library_hours.py
# HKU Libraries - Opening Hours scraper with Playwright rendering
# - 支持“今日开放时间”（默认）与“每周开放时间”（--weekly）
# - 自动安装（或兜底使用本机 Chrome/Edge）解决初次安装报错
# - 统一标准化：Closed -> 00:00-00:00 (Closed), 24/7/All-day -> 00:00-23:59 (All-day)
# - 输出：
#     - 今日模式：data/library_hours.json（可 -o 指定）
#     - 每周模式：data/library_hours_weekly.json（可 -o 指定）
#
# 依赖：
#   pip install playwright beautifulsoup4 requests requests-cache
#   （脚本会在需要时自动执行：python -m playwright install chromium）
#
# 用法：
#   python scrape_library_hours.py --debug
#   python scrape_library_hours.py --weekly --debug
#   python scrape_library_hours.py --headful              # 可见浏览器
#   python scrape_library_hours.py -o out.json            # 指定输出文件

import os, re, json, argparse, sys, subprocess
from datetime import datetime, timezone

import requests, requests_cache
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

BASE = "https://lib.hku.hk"
CURRENT_URL = f"{BASE}/general/hours/index.html"      # Current (daily) - JS populated
WEEKLY_URL  = f"{BASE}/general/hours/weekly.html"     # Weekly (Mon–Sun) - JS populated
HOLIDAY_URL = f"{BASE}/general/hours/holiday.html"
WEATHER_URL = f"{BASE}/general/hours/badweather.html"
LIB24_URL   = f"{BASE}/library24/index.html"

UA = "HKU-Tour-Agent/1.4 (+contact@example.com)"

# 常见馆名（按页面展示可自行增删）
CANONICAL_NAMES = [
    "Main Library",
    "Collaboration Zone & Study Zone (Level 3)",
    "Media Services (co-branded LES)",
    "Special Collections",
    "Overnight Open Area (Library Corner)",
    "Fung Ping Shan Library",
    "Tin Ka Ping Education Library",
    "Ko Wong Wai Ching Wendy Fine Arts Digital Library",
    "Dental Library",
    "Lui Che Woo Law Library",
    "Yu Chun Keung Medical Library",
    "Music Library",
    "24-hour Study Room",
    "Library 24",
]

DAYS = [
    ("Mon", "Monday"),
    ("Tue", "Tuesday"),
    ("Wed", "Wednesday"),
    ("Thu", "Thursday"),
    ("Fri", "Friday"),
    ("Sat", "Saturday"),
    ("Sun", "Sunday"),
]
DAY_NAMES = [d[0] for d in DAYS]

# am/pm 时段（容忍中横线/短横/长横）
R_TIME = re.compile(
    r"\b\d{1,2}:\d{2}\s*(?:am|pm)\s*[-–—]\s*\d{1,2}:\d{2}\s*(?:am|pm)\b",
    re.I
)

def normalize_segment(seg: str) -> str:
    s = seg.strip()
    low = s.lower()
    if "closed" in low:
        if not R_TIME.search(s):
            s = (s + " 00:00 - 00:00").strip()
        s = re.sub(R_TIME, "00:00 - 00:00", s)
        if "(Closed)" not in s:
            s += " (Closed)"
        return re.sub(r"\s+", " ", s)
    if "24/7" in low or "24 hours" in low or "24-hour" in low:
        base = re.sub(r"(24/?7|24-?hour[s]?|24\s*hours)", "", s, flags=re.I).strip()
        s = (base + " 00:00 - 23:59 (All-day)").strip() if base else "00:00 - 23:59 (All-day)"
        return re.sub(r"\s+", " ", s)
    if "all-day" in low or "allday" in low:
        base = re.sub(r"all-?day", "", s, flags=re.I).strip()
        s = (base + " 00:00 - 23:59 (All-day)").strip()
        return re.sub(r"\s+", " ", s)
    return re.sub(r"\s+", " ", s)

def make_id(title: str) -> str:
    return "lib_" + re.sub(r"[^a-z0-9]+", "_", title.lower()).strip("_")

def now_ymd() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")

# ---------------- Playwright 渲染（含自动安装 & 兜底） ----------------

def _launch_browser(p, headless, debug):
    try:
        return p.chromium.launch(headless=headless)
    except Exception as e:
        msg = str(e)
        if debug: print("[DEBUG] Chromium launch failed:", msg)
        if "Executable doesn't exist" in msg or "playwright install" in msg:
            if debug: print("[DEBUG] Auto-install chromium…")
            subprocess.run([sys.executable, "-m", "playwright", "install", "chromium"], check=True)
            return p.chromium.launch(headless=headless)
        # Fallback to system Chrome/Edge
        try:
            if debug: print("[DEBUG] Fallback: channel='chrome'")
            return p.chromium.launch(channel="chrome", headless=headless)
        except Exception as e2:
            if debug: print("[DEBUG] Chrome failed:", e2)
            if debug: print("[DEBUG] Fallback: channel='msedge'")
            return p.chromium.launch(channel="msedge", headless=headless)

def render_page(url: str, timeout_ms=20000, headless=True, debug=False) -> str:
    with sync_playwright() as p:
        browser = _launch_browser(p, headless, debug)
        ctx = browser.new_context(user_agent=UA, viewport={"width": 1360, "height": 2400})
        page = ctx.new_page()
        page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
        # 尝试等待关键字和 networkidle
        try: page.wait_for_load_state("networkidle", timeout=timeout_ms)
        except Exception: pass
        # 有些页面会有 "Main Library"、"Monday" 等文本
        try: page.wait_for_selector("text=Main Library", timeout=timeout_ms)
        except Exception: pass
        body_text = page.locator("body").inner_text(timeout=timeout_ms)
        browser.close()
        return body_text

# ---------------- 今日视图：解析 ----------------

def extract_today_from_text(page_text: str, debug=False) -> list[dict]:
    lines = [ln.strip() for ln in page_text.splitlines() if ln.strip()]
    text = "\n".join(lines)

    results = []
    for name in CANONICAL_NAMES:
        idx = text.lower().find(name.lower())
        if idx == -1:
            if debug: print(f"[DEBUG] Name not found (today): {name}")
            continue
        # 小窗口读取（避免跨到下一个馆）
        window = text[idx: idx + 500]
        m = R_TIME.search(window)
        seg = None
        if m:
            seg = m.group(0)
        else:
            if re.search(r"\bClosed\b", window, re.I):
                seg = "Closed"
            elif re.search(r"\b24\s*/?\s*7\b|\b24[- ]?hours?\b|\b24[- ]?hour\b", window, re.I):
                seg = "24/7"
            elif re.search(r"\bAll-?day\b", window, re.I):
                seg = "All-day"
        results.append({"title": name, "hours_today": [seg] if seg else []})

    # Library 24 兜底
    if not any(x["title"] == "Library 24" for x in results):
        results.append({"title": "Library 24", "hours_today": ["24/7"]})

    # 去重
    seen, out = set(), []
    for r in results:
        k = r["title"].lower()
        if k in seen: continue
        seen.add(k); out.append(r)
    return out

# ---------------- 每周视图：解析 ----------------

# 一个“大正则”：匹配 “Mon/Tue/…/Sunday” + （am/pm 时段 | Closed | All-day | 24/7）
DAY_TOKEN = r"(Mon(?:day)?|Tue(?:sday)?|Wed(?:nesday)?|Thu(?:rsday)?|Fri(?:day)?|Sat(?:urday)?|Sun(?:day)?)"
SEG_TOKEN = r"((?:\d{1,2}:\d{2}\s*(?:am|pm)\s*[-–—]\s*\d{1,2}:\d{2}\s*(?:am|pm))|Closed|All-?day|24/?7|24[- ]?hours?)"
DAY_SEG_RE = re.compile(rf"\b{DAY_TOKEN}\b[^\S\r\n]*[:\-]?\s*{SEG_TOKEN}", re.I)

def _day_to_key(day_str: str) -> str:
    ds = day_str.lower()
    for abbr, full in DAYS:
        if abbr.lower() in ds or full.lower() in ds:
            return abbr
    return day_str[:3].title()

def extract_weekly_from_text(page_text: str, debug=False) -> list[dict]:
    # 为了避免“跨馆污染”，先把所有馆名的索引找出来
    text = page_text
    name_positions = []
    for name in CANONICAL_NAMES:
        pos = text.lower().find(name.lower())
        if pos != -1:
            name_positions.append((pos, name))
    name_positions.sort()

    results = []
    for i, (pos, name) in enumerate(name_positions):
        next_cut = name_positions[i + 1][0] if i + 1 < len(name_positions) else len(text)
        window = text[pos: next_cut]

        # 在窗口内按 day+segment 抓取
        day_map = {}
        for m in DAY_SEG_RE.finditer(window):
            day_raw = m.group(1)
            seg_raw = m.group(2)
            day_key = _day_to_key(day_raw)
            # 只取第一条（若同一日多段，可改为列表 append）
            if day_key not in day_map:
                day_map[day_key] = [seg_raw]

        # 如果一个都没抓到，也要留空条目（后续可用默认或回退）
        weekly_list = [{"day": abbr, "segments": day_map.get(abbr, [])} for abbr, _ in DAYS]
        results.append({"title": name, "hours_weekly": weekly_list})

    # Library 24 兜底（全天）
    if not any(x["title"] == "Library 24" for x in results):
        results.append({
            "title": "Library 24",
            "hours_weekly": [{"day": abbr, "segments": ["24/7"]} for abbr, _ in DAYS]
        })

    # 去重
    seen, out = set(), []
    for r in results:
        k = r["title"].lower()
        if k in seen: continue
        seen.add(k); out.append(r)
    return out

# ---------------- 备注（节假日/恶劣天气/Library24 链接） ----------------

def fetch_notes(debug=False):
    requests_cache.install_cache("cache_libhours", expire_after=1800)
    sess = requests.Session(); sess.headers.update({"User-Agent": UA})
    out = {}
    try:
        h = sess.get(HOLIDAY_URL, timeout=15); h.raise_for_status()
        out["holiday_url"] = HOLIDAY_URL
        soup = BeautifulSoup(h.text, "lxml")
        first = soup.get_text("\n", strip=True).split("\n")[0:2]
        out["holiday_note"] = " ".join(first)[:300]
    except Exception as e:
        if debug: print("[DEBUG] holiday fetch failed:", e)
    try:
        w = sess.get(WEATHER_URL, timeout=15); w.raise_for_status()
        out["adverse_weather_url"] = WEATHER_URL
    except Exception as e:
        if debug: print("[DEBUG] weather fetch failed:", e)
    try:
        l24 = sess.get(LIB24_URL, timeout=15); l24.raise_for_status()
        out["library24_url"] = LIB24_URL
    except Exception as e:
        if debug: print("[DEBUG] library24 fetch failed:", e)
    return out

# ---------------- 主程序 ----------------

def main():
    ap = argparse.ArgumentParser(description="HKU Libraries - Opening hours scraper (Playwright)")
    ap.add_argument("-o", "--output", default=None, help="输出文件路径（默认：今日= data/library_hours.json；每周= data/library_hours_weekly.json）")
    ap.add_argument("--weekly", action="store_true", help="抓取“每周开放时间”（Mon–Sun）")
    ap.add_argument("--headful", action="store_true", help="以可见浏览器运行（默认无头）")
    ap.add_argument("--no-notes", action="store_true", help="不抓节假日/天气注释")
    ap.add_argument("--debug", action="store_true", help="打印调试信息")
    args = ap.parse_args()

    out_path = args.output or ("data/library_hours_weekly.json" if args.weekly else "data/library_hours.json")
    if args.debug:
        print(f"[DEBUG] Mode: {'WEEKLY' if args.weekly else 'TODAY'}")
        print(f"[DEBUG] Target: {WEEKLY_URL if args.weekly else CURRENT_URL}")

    # 渲染页面
    url = WEEKLY_URL if args.weekly else CURRENT_URL
    page_text = render_page(url, headless=not args.headful, debug=args.debug)

    # 解析
    if args.weekly:
        items_raw = extract_weekly_from_text(page_text, debug=args.debug)
    else:
        items_raw = extract_today_from_text(page_text, debug=args.debug)

    # 备注（可选）
    notes = {} if args.no_notes else fetch_notes(debug=args.debug)
    today = now_ymd()

    # 组织 JSON
    items = []
    for r in items_raw:
        title = r["title"]
        base = {
            "id": make_id(title),
            "type": "library_hours",
            "lang": "en",
            "title": title,
            "text": "",
            "metadata": {
                "source_url": url,
                "last_scraped": today,
                **notes
            }
        }

        if args.weekly:
            weekly = r["hours_weekly"]  # [{"day":"Mon","segments":[...]}...]
            base["metadata"]["hours_weekly"] = weekly
            base["metadata"]["normalized_hours_weekly"] = [
                {"day": d["day"], "segments": [normalize_segment(s) for s in (d["segments"] or [])]}
                for d in weekly
            ]
            base["text"] = "; ".join(
                f"{d['day']}: {', '.join(d['segments'])}" for d in weekly if d["segments"]
            )
        else:
            segs = r["hours_today"] or []
            base["metadata"]["hours_today"] = segs or None
            base["metadata"]["normalized_hours_today"] = [normalize_segment(s) for s in segs] if segs else None
            base["text"] = "; ".join(segs) if segs else ""

        items.append(base)

    # 保存
    odir = os.path.dirname(out_path)
    if odir: os.makedirs(odir, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, indent=2)

    if args.debug:
        print(f"[DEBUG] Parsed {len(items)} entries. Sample:")
        for x in items[:6]:
            if args.weekly:
                print(" -", x["title"], "=>", {d["day"]: d["segments"] for d in x["metadata"]["hours_weekly"]})
            else:
                print(" -", x["title"], "=>", x["metadata"]["hours_today"])
    print(f"抓到 {len(items)} 个馆/区域的{'每周' if args.weekly else '今日'}开放时间，已写入 {out_path}")

if __name__ == "__main__":
    main()
