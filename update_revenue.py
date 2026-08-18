#!/usr/bin/env python3
"""
台灣電子產業月營收更新腳本
每月10日後執行一次，抓取最新月份營收並更新 industry-revenue.html
資料來源：FinMind TaiwanStockMonthRevenue API
"""

import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import date, datetime
from pathlib import Path

DIR = Path(__file__).parent
HTML_PATH = DIR / "industry-revenue.html"
JSON_PATH = DIR / "revenue-data.json"
NETLIFY_SITE_ID = "cd9aa85d-5847-4e8a-9d1a-34bea93f7acb"
NETLIFY_CLI = os.path.expanduser("~/.npm-global/bin/netlify")

FINMIND_BASE = "https://api.finmindtrade.com/api/v4/data"
HISTORY_START = "2022-01-01"
DISPLAY_START = "2023-01"

# TWSE 七大電子子板塊 + 代表性上市股票
SECTORS = {
    "semiconductor": {
        "name": "半導體",
        "stocks": [
            ("2330", "台積電"), ("3711", "日月光投控"), ("2303", "聯電"),
            ("2454", "聯發科"), ("3034", "聯詠"), ("2379", "瑞昱"),
            ("2344", "華邦電"), ("2337", "旺宏"), ("5347", "世界先進"),
            ("3443", "創意"), ("2408", "南亞科"), ("4966", "譜瑞-KY"),
            ("6770", "力積電"),
            ("6239", "力成科技"), ("2449", "京元電子"), ("6271", "同欣電"), ("8150", "南茂科技"),
            ("8299", "群聯電子"), ("3661", "世芯-KY"), ("5269", "祥碩"),
        ],
    },
    "computer_peripherals": {
        "name": "電腦及周邊設備",
        "stocks": [
            ("2382", "廣達"), ("2357", "華碩"), ("2353", "宏碁"),
            ("3231", "緯創"), ("2324", "仁寶"), ("2356", "英業達"),
            ("2376", "技嘉"), ("2336", "致伸"), ("2365", "昆盈"),
            ("6414", "樺漢"), ("6669", "緯穎"),
        ],
    },
    "optoelectronics": {
        "name": "光電",
        "stocks": [
            ("2409", "友達"), ("3481", "群創"), ("3008", "大立光"),
            ("2393", "億光"), ("2448", "晶電"), ("2352", "佳世達"),
            ("3406", "玉晶光"), ("3698", "隆達"), ("2349", "錸德"),
        ],
    },
    "electronic_components": {
        "name": "電子零組件",
        "stocks": [
            ("2317", "鴻海"), ("2308", "台達電"), ("2327", "國巨"),
            ("2301", "光寶科"), ("3044", "健鼎"), ("3189", "景碩"),
            ("4958", "臻鼎-KY"), ("6269", "台郡"), ("2313", "華通"),
            ("3037", "欣興"), ("6153", "嘉聯益"),
            ("2492", "華新科"), ("3026", "禾伸堂"), ("2383", "台光電"),
            ("3533", "嘉澤"),
        ],
    },
    "electronic_distribution": {
        "name": "電子通路",
        "stocks": [
            ("2347", "聯強"), ("3036", "文曄"), ("3702", "大聯大"),
            ("5434", "崇越"), ("6189", "豐藝"), ("2430", "燦坤"),
            ("6776", "展碁國際"),
        ],
    },
    "it_services": {
        "name": "資訊服務",
        "stocks": [
            ("3130", "一零四"), ("6214", "精誠"), ("6183", "關貿"),
            ("2453", "凌群"), ("2480", "敦陽科"), ("2471", "資通"),
            ("2447", "鼎新"), ("5203", "訊連"),
        ],
    },
    "other_electronics": {
        "name": "其他電子",
        "stocks": [
            ("3367", "英華達"), ("6409", "旭隼"), ("6196", "帆宣"),
            ("6139", "亞翔"), ("6201", "亞弘電"), ("6438", "迅得"),
            ("6215", "和椿"), ("2411", "飛瑞"), ("6192", "巨路"),
        ],
    },
}


# 子板塊分類（各大類下的細分，stock_id 必須是上方 SECTORS 的子集）
SUBSECTORS = {
    "semiconductor": {
        "fab":       {"name": "晶圓代工", "stocks": ["2330", "2303", "5347", "6770"]},
        "memory":    {"name": "記憶體",   "stocks": ["2408", "2344", "2337", "8299"]},
        "ic_design": {"name": "IC設計",  "stocks": ["2454", "3034", "2379", "4966", "3443", "3661", "5269"]},
        "ic_pkg":    {"name": "IC封測",  "stocks": ["3711", "6239", "2449", "6271", "8150"]},
    },
    "computer_peripherals": {
        "server_odm": {"name": "AI Server/ODM", "stocks": ["2382", "3231", "6669"]},
        "nb_odm":     {"name": "筆電ODM",        "stocks": ["2324", "2356"]},
        "brand_pc":   {"name": "品牌PC",          "stocks": ["2357", "2353"]},
        "other_pc":   {"name": "其他周邊",        "stocks": ["2376", "2365", "6414"]},
    },
    "optoelectronics": {
        "display":    {"name": "面板",    "stocks": ["2409", "3481"]},
        "led":        {"name": "LED",     "stocks": ["2393", "2448", "3698"]},
        "optics":     {"name": "光學鏡頭", "stocks": ["3008", "3406"]},
        "other_opto": {"name": "其他光電", "stocks": ["2352", "2349"]},
    },
    "electronic_components": {
        "ems":        {"name": "EMS",     "stocks": ["2317"]},
        "pcb":        {"name": "PCB",     "stocks": ["3044", "3189", "4958", "6269", "2313", "3037", "6153", "2383"]},
        "passive":    {"name": "被動元件", "stocks": ["2327", "2492", "3026"]},
        "power":      {"name": "電源/模組", "stocks": ["2308", "2301"]},
        "connector":  {"name": "連接器",   "stocks": ["3533"]},
    },
    "electronic_distribution": {
        "large_dist": {"name": "大型通路", "stocks": ["2347", "3036", "3702"]},
        "other_dist": {"name": "其他通路", "stocks": ["5434", "6189", "2430", "6776"]},
    },
}


def aggregate_subsectors(sector_key: str, stock_rev: dict, all_months: list, stock_names: dict) -> dict:
    """從已抓取的 stock_rev 計算子板塊聚合，無需額外 API 呼叫"""
    if sector_key not in SUBSECTORS:
        return {}
    result = {}
    for sub_key, sub_cfg in SUBSECTORS[sector_key].items():
        sub_ids = set(sub_cfg["stocks"])
        sub_by_month: dict = {}
        for m in all_months:
            total = sum(stock_rev.get(sid, {}).get(m, 0) for sid in sub_ids)
            if total > 0:
                sub_by_month[m] = total
        all_sub_months = sorted(sub_by_month.keys())
        sub_months = [m for m in all_sub_months if m >= DISPLAY_START]
        sub_last = all_sub_months[-1] if all_sub_months else ""
        # top5
        top5 = []
        if sub_last:
            stock_data = []
            for sid in sub_ids:
                rev = stock_rev.get(sid, {}).get(sub_last, 0)
                if rev <= 0:
                    continue
                prev_yr = f"{int(sub_last[:4])-1}-{sub_last[5:]}"
                prev_rev = stock_rev.get(sid, {}).get(prev_yr, 0)
                yoy = round((rev - prev_rev) / prev_rev * 100, 1) if prev_rev > 0 else None
                stock_data.append({
                    "id": sid,
                    "name": stock_names.get(sid, sid),
                    "rev": round(rev / 1e8, 1),
                    "yoy": yoy,
                })
            top5 = sorted(stock_data, key=lambda x: x["rev"], reverse=True)[:5]
        result[sub_key] = {
            "name": sub_cfg["name"],
            "months": sub_months,
            "total_rev": [round(sub_by_month[m] / 1e8, 1) for m in sub_months],
            "yoy": [compute_yoy(sub_by_month, m) for m in sub_months],
            "last_month": sub_last,
            "top5": top5,
        }
    return result


def fetch_revenue(stock_id: str, start_date: str = HISTORY_START) -> list:
    """抓取單一股票月營收，回傳 [{date, revenue_month, revenue_year, revenue}, ...]"""
    params = urllib.parse.urlencode({
        "dataset": "TaiwanStockMonthRevenue",
        "data_id": stock_id,
        "start_date": start_date,
        "end_date": date.today().strftime("%Y-%m-%d"),
    })
    url = f"{FINMIND_BASE}?{params}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=15) as r:
            d = json.loads(r.read())
            return d.get("data", [])
    except Exception as e:
        print(f"  [WARN] {stock_id} fetch failed: {e}")
        return []


def month_key(date_str: str) -> str:
    """'2026-07-01' → '2026-07'"""
    return date_str[:7]


def compute_yoy(rev_by_month: dict, month: str):
    """計算年增率：(當月 - 去年同月) / 去年同月 × 100"""
    yr, mo = month.split("-")
    prev_year = f"{int(yr)-1}-{mo}"
    curr = rev_by_month.get(month)
    prev = rev_by_month.get(prev_year)
    if curr is None or prev is None or prev == 0:
        return None
    return round((curr - prev) / prev * 100, 1)


def build_sector_data(sector_key: str, sector_cfg: dict) -> dict:
    """抓取一個子板塊所有股票的歷史月營收，回傳聚合結果"""
    print(f"\n[{sector_cfg['name']}] 抓取 {len(sector_cfg['stocks'])} 檔股票...")

    # stock_id → {month_key → revenue}
    stock_rev: dict[str, dict[str, int]] = {}
    stock_names: dict[str, str] = {}

    for stock_id, stock_name in sector_cfg["stocks"]:
        stock_names[stock_id] = stock_name
        rows = fetch_revenue(stock_id)
        if rows:
            stock_rev[stock_id] = {
                f"{r['revenue_year']}-{int(r['revenue_month']):02d}": r["revenue"]
                for r in rows
            }
            print(f"  {stock_id} {stock_name}: {len(rows)} 個月")
        else:
            stock_rev[stock_id] = {}
            print(f"  {stock_id} {stock_name}: 無資料")
        time.sleep(0.35)

    # 收集所有月份
    all_months = sorted(set(
        m for rev_dict in stock_rev.values() for m in rev_dict.keys()
    ))

    # 月份 → 各股票加總
    total_by_month: dict[str, int] = {}
    for m in all_months:
        total = sum(rev.get(m, 0) for rev in stock_rev.values())
        if total > 0:
            total_by_month[m] = total

    months = sorted(total_by_month.keys())

    # 子板塊（重用 stock_rev，無額外 API 呼叫）
    subsectors = aggregate_subsectors(sector_key, stock_rev, months, stock_names)

    # 過濾顯示月份（保留完整資料供 YoY 計算）
    display_months = [m for m in months if m >= DISPLAY_START]

    # YoY
    yoy_list = [compute_yoy(total_by_month, m) for m in display_months]

    # 最新月份 top5
    last_month = months[-1] if months else None
    top5 = []
    if last_month:
        stock_last = []
        for sid, rev_dict in stock_rev.items():
            rev = rev_dict.get(last_month, 0)
            if rev > 0:
                prev_yr = f"{int(last_month[:4])-1}-{last_month[5:]}"
                prev_rev = rev_dict.get(prev_yr, 0)
                yoy = round((rev - prev_rev) / prev_rev * 100, 1) if prev_rev > 0 else None
                stock_last.append({
                    "id": sid,
                    "name": stock_names[sid],
                    "rev": round(rev / 1e8, 1),
                    "yoy": yoy,
                })
        top5 = sorted(stock_last, key=lambda x: x["rev"], reverse=True)[:5]

    return {
        "name": sector_cfg["name"],
        "months": display_months,
        "total_rev": [round(total_by_month[m] / 1e8, 1) for m in display_months],
        "yoy": yoy_list,
        "last_month": last_month or "",
        "top5": top5,
        "subsectors": subsectors,
    }


def build_all_data() -> dict:
    result = {}
    for key, cfg in SECTORS.items():
        result[key] = build_sector_data(key, cfg)
    return result


def load_existing() -> dict:
    if JSON_PATH.exists():
        return json.loads(JSON_PATH.read_text())
    return {}


def update_existing(existing: dict) -> dict:
    """增量更新：只抓取自上次更新後的新月份"""
    today = date.today()
    current_month = today.strftime("%Y-%m")

    for key, cfg in SECTORS.items():
        if key not in existing:
            existing[key] = build_sector_data(key, cfg)
            continue

        sec = existing[key]
        last = sec.get("last_month", "")
        if last >= current_month:
            print(f"[{cfg['name']}] 已是最新 ({last})，跳過")
            continue

        # 找出需要補的月份起始點
        start_date = f"{last}-01" if last else HISTORY_START
        print(f"\n[{cfg['name']}] 增量更新 from {start_date}...")

        stock_names = dict(cfg["stocks"])
        new_rev_by_stock: dict[str, dict[str, int]] = {}

        # 同時也需要去年同月的資料來算 YoY，所以從前13個月開始抓
        yr, mo = start_date[:4], start_date[5:7]
        fetch_from = f"{int(yr)-1}-{mo}-01"

        for stock_id, stock_name in cfg["stocks"]:
            rows = fetch_revenue(stock_id, start_date=fetch_from)
            new_rev_by_stock[stock_id] = {
                f"{r['revenue_year']}-{int(r['revenue_month']):02d}": r["revenue"]
                for r in rows
            }
            time.sleep(0.35)

        # 合併到現有月份資料
        existing_months = set(sec["months"])
        existing_total_by_month = dict(zip(sec["months"], [int(r * 1e8) for r in sec["total_rev"]]))

        # 計算新月份
        all_new_months = sorted(set(
            m for rd in new_rev_by_stock.values() for m in rd.keys()
            if m > last
        ))

        for m in all_new_months:
            total = sum(rd.get(m, 0) for rd in new_rev_by_stock.values())
            if total > 0:
                existing_total_by_month[m] = total

        all_months_full = sorted(existing_total_by_month.keys())
        months = [m for m in all_months_full if m >= DISPLAY_START]
        yoy_list = [compute_yoy(existing_total_by_month, m) for m in months]

        # top5
        new_last = months[-1] if months else last
        top5 = []
        if new_last:
            stock_last = []
            for sid, rd in new_rev_by_stock.items():
                rev = rd.get(new_last, 0)
                if rev == 0:
                    # 可能此月份在舊資料裡
                    continue
                prev_yr = f"{int(new_last[:4])-1}-{new_last[5:]}"
                prev_rev = rd.get(prev_yr, 0)
                yoy = round((rev - prev_rev) / prev_rev * 100, 1) if prev_rev > 0 else None
                stock_last.append({
                    "id": sid,
                    "name": stock_names[sid],
                    "rev": round(rev / 1e8, 1),
                    "yoy": yoy,
                })
            top5 = sorted(stock_last, key=lambda x: x["rev"], reverse=True)[:5]

        sec["months"] = months
        sec["total_rev"] = [round(existing_total_by_month[m] / 1e8, 1) for m in months]
        sec["yoy"] = yoy_list
        sec["last_month"] = new_last
        sec["top5"] = top5

        # 增量更新子板塊
        if key in SUBSECTORS:
            existing_subs = sec.setdefault("subsectors", {})
            for sub_key, sub_cfg in SUBSECTORS[key].items():
                sub_ids = set(sub_cfg["stocks"])
                existing_sub = existing_subs.setdefault(sub_key, {
                    "name": sub_cfg["name"], "months": [], "total_rev": [], "yoy": [], "last_month": ""
                })
                sub_total: dict = dict(zip(
                    existing_sub.get("months", []),
                    [int(r * 1e8) for r in existing_sub.get("total_rev", [])]
                ))
                for m in all_new_months:
                    total = sum(new_rev_by_stock.get(sid, {}).get(m, 0) for sid in sub_ids)
                    if total > 0:
                        sub_total[m] = total
                sub_months = sorted(sub_total.keys())
                all_sub_months_full = sorted(sub_total.keys())
                sub_last = all_sub_months_full[-1] if all_sub_months_full else ""
                sub_months = [m for m in all_sub_months_full if m >= DISPLAY_START]
                # top5
                sub_top5 = []
                if sub_last:
                    stock_data = []
                    for sid in sub_ids:
                        rev = new_rev_by_stock.get(sid, {}).get(sub_last, 0)
                        if rev <= 0:
                            continue
                        prev_yr = f"{int(sub_last[:4])-1}-{sub_last[5:]}"
                        prev_rev = new_rev_by_stock.get(sid, {}).get(prev_yr, 0)
                        yoy = round((rev - prev_rev) / prev_rev * 100, 1) if prev_rev > 0 else None
                        stock_data.append({
                            "id": sid,
                            "name": stock_names.get(sid, sid),
                            "rev": round(rev / 1e8, 1),
                            "yoy": yoy,
                        })
                    sub_top5 = sorted(stock_data, key=lambda x: x["rev"], reverse=True)[:5]
                existing_sub["name"] = sub_cfg["name"]
                existing_sub["months"] = sub_months
                existing_sub["total_rev"] = [round(sub_total[m] / 1e8, 1) for m in sub_months]
                existing_sub["yoy"] = [compute_yoy(sub_total, m) for m in sub_months]
                existing_sub["last_month"] = sub_last
                existing_sub["top5"] = sub_top5

    return existing


def save_json(data: dict) -> None:
    payload = {
        "updated": date.today().strftime("%Y-%m"),
        "sectors": data,
    }
    JSON_PATH.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
    print(f"\n已儲存 {JSON_PATH} ({JSON_PATH.stat().st_size // 1024} KB)")


_DATA_START = "/* [REVENUE_DATA_START] */"
_DATA_END = "/* [REVENUE_DATA_END] */"


def update_html(data: dict) -> None:
    if not HTML_PATH.exists():
        print("[WARN] industry-revenue.html 不存在，跳過更新 HTML")
        return

    html = HTML_PATH.read_text()

    js_data = json.dumps({"updated": date.today().strftime("%Y-%m"), "sectors": data},
                         ensure_ascii=False, separators=(",", ":"))
    new_block = f"const REVENUE_DATA = {js_data};"

    # 優先用 comment-marker 方式（安全，不受 JSON 內容影響）
    if _DATA_START in html and _DATA_END in html:
        s = html.index(_DATA_START) + len(_DATA_START)
        e = html.index(_DATA_END)
        html = html[:s] + "\n" + new_block + "\n" + html[e:]
    else:
        # fallback: regex（用 lambda 避免 \u 等字元被誤解讀）
        pattern = r"const REVENUE_DATA = \{.*?\};"
        if re.search(pattern, html, re.DOTALL):
            html = re.sub(pattern, lambda _: new_block, html, flags=re.DOTALL)
        else:
            print("[WARN] 找不到 REVENUE_DATA 區塊，跳過 HTML 更新")
            return

    HTML_PATH.write_text(html)
    print(f"已更新 {HTML_PATH}")


def deploy() -> None:
    token_path = os.path.expanduser("~/.netlify_token")
    env = os.environ.copy()
    if os.path.exists(token_path):
        env["NETLIFY_AUTH_TOKEN"] = open(token_path).read().strip()

    import subprocess
    cmd = [NETLIFY_CLI, "deploy", "--prod",
           "--dir", str(DIR),
           "--site", NETLIFY_SITE_ID]
    result = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=120)
    if result.returncode == 0:
        print("Netlify 部署成功")
    else:
        print(f"Netlify 部署失敗:\n{result.stderr[:500]}")


def main():
    existing = load_existing()

    if not existing:
        print("=== 首次執行：抓取全部歷史資料 ===")
        data = build_all_data()
    else:
        print("=== 增量更新 ===")
        data = update_existing(existing.get("sectors", existing))

    save_json(data)
    update_html(data)
    if "--no-deploy" not in sys.argv:
        deploy()
    print("\n完成！")


if __name__ == "__main__":
    main()
