#!/usr/bin/env python3
"""
台股三大法人追蹤 — 每日自動更新腳本

首次執行：自動補抓最近 15 個交易日的 T86 歷史資料（一次性）
之後每次：只抓當天 1 次 T86 + BFI82U + 期貨 + 匯率 = 共 4 次請求

檔案位置（與本腳本同目錄）：
  data.json   — T86 近15交易日原始資料（不上傳，只留在本機）
  index.html  — 顯示頁面（更新後 git push 到 GitHub Pages）

部署：git push 到 github.com/debby0524/tw-institutional-tracker（main 分支，GitHub Pages
      直接從 main 的 / (root) 建置），改自 Netlify（2026-08 因帳單額度暫停 production deploy
      而換掉）。推送用的 Personal Access Token 存在 ~/.github_token，只需要 Contents
      read/write 權限，不會存進 repo 或 log。
"""

import json
import os
import re
import subprocess
import time
import requests
from collections import defaultdict
from datetime import datetime, timedelta

# ===== 路徑設定 =====
BASE_DIR   = os.path.dirname(os.path.abspath(__file__))
HTML_PATH  = os.path.join(BASE_DIR, "index.html")
DATA_PATH  = os.path.join(BASE_DIR, "data.json")
GITHUB_TOKEN_PATH = os.path.expanduser("~/.github_token")
GITHUB_REPO_URL   = "github.com/debby0524/tw-institutional-tracker.git"

T86_KEEP    = 15   # d15 需要的天數
CHART_KEEP  = 30   # TOTAL/FUT/FX 圖表保留天數
MARGIN_KEEP = 30   # 融資餘額圖表保留天數


# ──────────────────────────────────────────
# 網路工具
# ──────────────────────────────────────────
def http_get(url, timeout=12):
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36",
        "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.8",
        "Referer": "https://www.twse.com.tw/",
    }
    try:
        r = requests.get(url, headers=headers, timeout=timeout)
        r.raise_for_status()
        return r.text
    except Exception as e:
        print(f"  [HTTP ERR] {url[:70]}: {e}")
        return None

def parse_num(s):
    try:
        return int(str(s).replace(",", "").strip())
    except:
        return 0

def parse_float(s):
    try:
        return float(str(s).replace(",", "").strip())
    except:
        return 0.0


# ──────────────────────────────────────────
# T86 — 單日抓取與解析
# ──────────────────────────────────────────
def fetch_t86(date_str):
    """回傳 (is_ok, stocks_list)"""
    url = (f"https://www.twse.com.tw/rwd/zh/fund/T86"
           f"?response=json&date={date_str}&selectType=ALLBUT0999")
    raw = http_get(url)
    if not raw:
        return False, []
    d = json.loads(raw)
    if d.get("stat") != "OK" or not d.get("data"):
        return False, []
    stocks = []
    for row in d["data"]:
        f = parse_num(row[4])
        t = parse_num(row[10])
        if f != 0 or t != 0:
            stocks.append({"c": row[0].strip(), "n": row[1].strip(), "f": f, "t": t})
    return True, stocks


# ──────────────────────────────────────────
# MI_INDEX — 每日收盤行情（用來估算個股買賣超金額）
# ──────────────────────────────────────────
def fetch_close_prices(date_str):
    """回傳 {證券代號: 收盤價}。用於「買賣超金額」估算（張數 × 收盤價），
    非三大法人實際成交金額。抓不到時回傳 {}，呼叫端需自行處理缺值。"""
    url = f"https://www.twse.com.tw/exchangeReport/MI_INDEX?response=json&date={date_str}&type=ALLBUT0999"
    raw = http_get(url)
    if not raw:
        return {}
    try:
        d = json.loads(raw)
        if d.get("stat") != "OK":
            return {}
        table = next((t for t in d.get("tables", []) if "每日收盤行情" in (t.get("title") or "")), None)
        if not table:
            return {}
        prices = {}
        for row in table.get("data", []):
            code = row[0].strip()
            price = parse_float(row[8])
            if price > 0:
                prices[code] = price
        return prices
    except Exception as e:
        print(f"  [MI_INDEX ERR] {e}")
        return {}


# ──────────────────────────────────────────
# T86 — 聚合與排行
# ──────────────────────────────────────────
def aggregate(days_stocks_list):
    fg = defaultdict(lambda: [None, 0])
    tr = defaultdict(lambda: [None, 0])
    for stocks in days_stocks_list:
        for s in stocks:
            fg[s["c"]][0] = s["n"]; fg[s["c"]][1] += s["f"]
            tr[s["c"]][0] = s["n"]; tr[s["c"]][1] += s["t"]
    return fg, tr

def top15(net_dict, direction, close_prices=None):
    close_prices = close_prices or {}
    items = []
    for c, v in net_dict.items():
        if v[1] == 0:
            continue
        lots = v[1] // 1000
        price = close_prices.get(c)
        amt = round(v[1] * price) if price else None  # 估算金額（股數×收盤價），非實際成交金額
        items.append((c, v[0], lots, amt))
    items = [x for x in items if (x[2] > 0 if direction == "buy" else x[2] < 0)]
    items.sort(key=lambda x: -x[2] if direction == "buy" else x[2])
    return [list(x) for x in items[:15]]

def compute_demo(t86_days, close_prices=None):
    all_s = [d["stocks"] for d in t86_days]
    results = {}
    for label, window in [("d1", 1), ("d5", 5), ("d15", 15)]:
        fn, tn = aggregate(all_s[-window:])
        results[label] = {
            "foreign_buy":  top15(fn, "buy", close_prices),  "foreign_sell": top15(fn, "sell", close_prices),
            "trust_buy":    top15(tn, "buy", close_prices),  "trust_sell":   top15(tn, "sell", close_prices),
        }
    return results


# ──────────────────────────────────────────
# BFI82U — 三大法人總買賣超
# ──────────────────────────────────────────
def fetch_bfi82u(date_str):
    url = (f"https://www.twse.com.tw/rwd/zh/fund/BFI82U"
           f"?response=json&dayDate={date_str}&type=day")
    raw = http_get(url)
    if not raw:
        return None
    d = json.loads(raw)
    if d.get("stat") != "OK" or not d.get("data"):
        return None
    fg = trust = ds = dh = 0
    for row in d["data"]:
        v = parse_num(row[3])
        name = row[0]
        if name == "外資及陸資(不含外資自營商)": fg = v
        elif name == "投信":                    trust = v
        elif name == "自營商(自行買賣)":        ds = v
        elif name == "自營商(避險)":            dh = v
    return {"foreign": fg, "trust": trust, "dealer": ds + dh}


# ──────────────────────────────────────────
# TAIFEX — 外資臺股期貨淨未平倉
# ──────────────────────────────────────────
def fetch_futures():
    raw = http_get("https://www.taifex.com.tw/cht/3/futContractsDate", timeout=15)
    if not raw:
        return None, None
    date_m = re.search(r"value='(\d{4}/\d{2}/\d{2})'", raw)
    page_date = date_m.group(1) if date_m else None
    txf_idx = raw.find("臺股期貨</div>")
    if txf_idx == -1:
        return None, page_date
    wz_idx = raw.find("外資", txf_idx)
    if wz_idx == -1:
        return None, page_date
    wz_area = raw[wz_idx:wz_idx + 3000]
    nums = re.findall(r'<span class="blue">\s*(-?[\d,]+)\s*</span>', wz_area)
    if not nums:
        return None, page_date
    return parse_num(nums[-1]), page_date


# ──────────────────────────────────────────
# TWSE — 全市場融資餘額（MI_MARGN）
# ──────────────────────────────────────────
def fetch_margin_by_date(date_str):
    """抓指定日期的全市場融資餘額（張）。英文版 API 支援歷史查詢，直接回傳總和。"""
    url = f"https://www.twse.com.tw/en/exchangeReport/MI_MARGN?response=json&date={date_str}&selectType=MS"
    raw = http_get(url)
    if not raw:
        return None
    try:
        d = json.loads(raw)
        if d.get("stat") != "OK":
            return None
        tables = d.get("tables", [])
        if not tables:
            return None
        data = tables[0].get("data", [])
        if not data:
            return None
        # Row 0 = Margin Purchase (Trading unit), col4 = Balance of the Day (張)
        bal = parse_num(data[0][4])
        return bal if bal > 0 else None
    except Exception as e:
        print(f"  [MARGIN ERR] {e}")
        return None

def fetch_margin(today_str=None):
    """抓今日全市場融資餘額。優先用英文版（有歷史查詢），備用 OpenAPI 即時資料。
    回傳 {today, prev} 或 None。"""
    today_bal = fetch_margin_by_date(today_str) if today_str else None

    # 備用：OpenAPI 即時資料（忽略日期，永遠回傳最新盤後）
    if today_bal is None:
        url = "https://openapi.twse.com.tw/v1/exchangeReport/MI_MARGN?selectType=MS"
        raw = http_get(url)
        if raw:
            try:
                rows = json.loads(raw)
                if rows and isinstance(rows, list):
                    today_bal = sum(parse_num(row.get("融資今日餘額", "")) for row in rows)
                    prev_bal  = sum(parse_num(row.get("融資前日餘額", "")) for row in rows)
                    return {"today": today_bal, "prev": prev_bal} if today_bal > 0 else None
            except:
                pass
        return None

    # 若英文 API 成功，也抓前一交易日（間隔 sleep，避免連續打同一端點觸發 WAF）
    today_dt = datetime.strptime(today_str, "%Y%m%d")
    prev_dt  = today_dt - timedelta(days=1)
    while prev_dt.weekday() >= 5:
        prev_dt -= timedelta(days=1)
    time.sleep(1)
    prev_val = fetch_margin_by_date(prev_dt.strftime("%Y%m%d"))
    return {"today": today_bal, "prev": prev_val or 0} if today_bal else None

def backfill_margin(margin_days, today_str, need=MARGIN_KEEP):
    """補抓歷史融資餘額資料（英文版 API 支援，每次 sleep 0.4s）。"""
    existing = {d["date"] for d in margin_days}
    if len(existing) >= need:
        return margin_days
    print(f"  融資餘額歷史不足（{len(existing)}/{need}），補抓...")
    candidate = datetime.strptime(today_str, "%Y%m%d") - timedelta(days=1)
    max_try, tries = 50, 0
    while len(existing) < need and tries < max_try:
        ds = candidate.strftime("%Y%m%d")
        if candidate.weekday() < 5 and ds not in existing:
            val = fetch_margin_by_date(ds)
            if val:
                margin_days.append({"date": ds, "balance": val})
                existing.add(ds)
                print(f"    補抓 {ds}: {val/10000:.0f} 萬張")
                time.sleep(0.4)
            else:
                print(f"    {ds}: 無資料")
        candidate -= timedelta(days=1)
        tries += 1
    margin_days.sort(key=lambda x: x["date"])
    return margin_days


# ──────────────────────────────────────────
# TAIFEX — USD/TWD 匯率
# ──────────────────────────────────────────
def fetch_fx():
    raw = http_get("https://www.taifex.com.tw/cht/3/dailyFXRate", timeout=15)
    if not raw:
        return None, None
    idx = raw.find("美元／新台幣", 240000)
    if idx == -1:
        return None, None
    seg = raw[idx:idx + 40000]
    rows = re.findall(
        r"align=center>(\d{4}/\d{2}/\d{2})</td>.*?bgcolor=\"#FFFFFF\">([\d.]+)</td>",
        seg, re.DOTALL)
    if not rows:
        return None, None
    last_date, last_rate = rows[-1]
    return float(last_rate), last_date


# ──────────────────────────────────────────
# data.json 讀寫
# ──────────────────────────────────────────
def load_data():
    if os.path.exists(DATA_PATH):
        with open(DATA_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"t86_days": []}

def save_data(data):
    with open(DATA_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"))


# ──────────────────────────────────────────
# 補抓歷史 T86（首次執行 / 缺資料時）
# ──────────────────────────────────────────
def backfill_t86(t86_days, today_str, need=T86_KEEP):
    """補齊至少 need 天的 T86 資料（不含今天，今天主流程已抓）"""
    existing_dates = {d["date"] for d in t86_days}
    if len(existing_dates) >= need:
        return t86_days

    print(f"  首次執行或歷史不足，補抓最近 {need} 個交易日的 T86...")
    # 從 today-1 往回找，跳過週末
    candidate = datetime.strptime(today_str, "%Y%m%d") - timedelta(days=1)
    fetched = 0
    max_try = 30
    tries = 0
    while len(existing_dates) < need and tries < max_try:
        ds = candidate.strftime("%Y%m%d")
        if candidate.weekday() < 5 and ds not in existing_dates:
            ok, stocks = fetch_t86(ds)
            if ok:
                t86_days.append({"date": ds, "stocks": stocks})
                existing_dates.add(ds)
                fetched += 1
                print(f"    補抓 {ds}: {len(stocks)} 筆")
                time.sleep(0.3)
        candidate -= timedelta(days=1)
        tries += 1

    t86_days.sort(key=lambda x: x["date"])
    print(f"  補抓完成，共 {fetched} 天，總計 {len(t86_days)} 天")
    return t86_days


# ──────────────────────────────────────────
# HTML 陣列 append（正則，保留 CHART_KEEP 筆）
# ──────────────────────────────────────────
def append_array(html, var_name, new_val, is_str=False):
    pattern = rf"(const {var_name}\s*=\s*\[)([^\]]+)(\];)"
    m = re.search(pattern, html)
    if not m:
        print(f"  WARNING: {var_name} not found in HTML")
        return html, False
    items = [x.strip() for x in m.group(2).split(",") if x.strip()]
    new_item = f'"{new_val}"' if is_str else str(new_val)
    # 防重複：若最後一筆已是今天的值，跳過
    if items and items[-1] == new_item:
        return html, True
    items.append(new_item)
    if len(items) > CHART_KEEP:
        items = items[-CHART_KEEP:]
    return html.replace(m.group(0), m.group(1) + ",".join(items) + m.group(3), 1), True


# ──────────────────────────────────────────
# 格式化輔助
# ──────────────────────────────────────────
def fmt_yi(n):
    return ("+" if n >= 0 else "-") + f"{abs(n)/1e8:.1f}億"

def mmdd(date_str_8):
    return f"{date_str_8[4:6]}/{date_str_8[6:8]}"

def slash_date(date_str_8):
    return f"{date_str_8[:4]}/{date_str_8[4:6]}/{date_str_8[6:8]}"

def dash_date(date_str_8):
    return f"{date_str_8[:4]}-{date_str_8[4:6]}-{date_str_8[6:8]}"

def fmt_demo_js(demo, today_slash):
    def arr_js(period, key):
        arr = demo[period][key]
        return json.dumps(arr, ensure_ascii=False, separators=(",", ":"))

    return (
        f"// ===== 真實資料 {today_slash}（來源：TWSE T86，1日/5日/15日累積）=====\n"
        f"const DEMO = {{\n"
        f"  foreign: {{\n"
        f"    buy: {{\n"
        f"      d1: {arr_js('d1','foreign_buy')},\n"
        f"      d5: {arr_js('d5','foreign_buy')},\n"
        f"      d15: {arr_js('d15','foreign_buy')}\n"
        f"    }},\n"
        f"    sell: {{\n"
        f"      d1: {arr_js('d1','foreign_sell')},\n"
        f"      d5: {arr_js('d5','foreign_sell')},\n"
        f"      d15: {arr_js('d15','foreign_sell')}\n"
        f"    }}\n"
        f"  }},\n"
        f"  trust: {{\n"
        f"    buy: {{\n"
        f"      d1: {arr_js('d1','trust_buy')},\n"
        f"      d5: {arr_js('d5','trust_buy')},\n"
        f"      d15: {arr_js('d15','trust_buy')}\n"
        f"    }},\n"
        f"    sell: {{\n"
        f"      d1: {arr_js('d1','trust_sell')},\n"
        f"      d5: {arr_js('d5','trust_sell')},\n"
        f"      d15: {arr_js('d15','trust_sell')}\n"
        f"    }}\n"
        f"  }}\n"
        f"}};"
    )


# ──────────────────────────────────────────
# 主程式
# ──────────────────────────────────────────
def main():
    today = datetime.now().strftime("%Y%m%d")
    print(f"\n=== 台股三大法人追蹤更新 {dash_date(today)} ===\n")

    # 1. 確認今天是否為交易日（同時取回 T86 資料）
    print("【1】確認交易日 + 抓取今日 T86...")
    is_trading, today_stocks = fetch_t86(today)
    if not is_trading:
        print("   今天非交易日或資料尚未更新，跳過。")
        return
    print(f"   OK，{len(today_stocks)} 筆個股資料")

    # 2. 載入 data.json
    data = load_data()
    t86_days = data.get("t86_days", [])

    # 移除今天的舊資料（避免重複），加入今天
    t86_days = [d for d in t86_days if d["date"] != today]
    t86_days.append({"date": today, "stocks": today_stocks})
    t86_days.sort(key=lambda x: x["date"])

    # 3. 若歷史不足，補抓（首次執行時）
    if len(t86_days) < T86_KEEP:
        print(f"\n【2】歷史資料不足（{len(t86_days)}/{T86_KEEP}），補抓...")
        t86_days = backfill_t86(t86_days, today, need=T86_KEEP)
        t86_days = [d for d in t86_days if d["date"] != today]
        t86_days.append({"date": today, "stocks": today_stocks})
        t86_days.sort(key=lambda x: x["date"])
    else:
        print(f"【2】歷史資料充足（{len(t86_days)} 天），跳過補抓")

    # 保留最近 T86_KEEP 天
    t86_days = t86_days[-T86_KEEP:]
    data["t86_days"] = t86_days
    print(f"   T86 日期範圍：{t86_days[0]['date']} – {t86_days[-1]['date']}")

    # 4. 計算 d1/d5/d15 排行
    print("\n【3】計算排行...")
    close_prices = fetch_close_prices(today)
    if close_prices: print(f"   收盤價：取得 {len(close_prices)} 檔（用於估算買賣超金額）")
    else:            print("   收盤價：抓取失敗，本次買賣超金額欄位將留空")
    demo = compute_demo(t86_days, close_prices)
    print(f"   d1 外資買超 #1: {demo['d1']['foreign_buy'][0] if demo['d1']['foreign_buy'] else 'N/A'}")

    # 5. 抓 BFI82U / 期貨 / 匯率 / 融資餘額
    print("\n【4】抓取 BFI82U + 期貨 + 匯率 + 融資餘額...")
    bfi      = fetch_bfi82u(today)
    fut_val, fut_date  = fetch_futures()
    fx_val,  fx_date   = fetch_fx()
    margin_data        = fetch_margin(today)

    if bfi:         print(f"   BFI82U: 外資{fmt_yi(bfi['foreign'])} 投信{fmt_yi(bfi['trust'])} 自營{fmt_yi(bfi['dealer'])}")
    else:           print("   BFI82U: 抓取失敗，保留舊值")
    if fut_val is not None: print(f"   期貨淨未平倉: {fut_val:,} 口（{fut_date}）")
    else:           print(f"   期貨: 抓取失敗（{fut_date}），保留舊值")
    if fx_val:      print(f"   USD/TWD: {fx_val}（{fx_date}）")
    else:           print("   匯率: 抓取失敗，保留舊值")
    if margin_data: print(f"   融資餘額: 今日 {margin_data['today']/10000:.0f} 萬張，前日 {margin_data['prev']/10000:.0f} 萬張")
    else:           print("   融資餘額: 抓取失敗，保留舊值")

    # 5b. 更新 margin_days（今日 + 前日兩筆）
    margin_days = data.get("margin_days", [])
    if margin_data:
        today_bal = margin_data["today"]
        prev_bal  = margin_data["prev"]
        # 計算前一交易日（跳過週末）
        today_dt = datetime.strptime(today, "%Y%m%d")
        prev_dt  = today_dt - timedelta(days=1)
        while prev_dt.weekday() >= 5:
            prev_dt -= timedelta(days=1)
        prev_str = prev_dt.strftime("%Y%m%d")
        # 移除今天的舊記錄，寫入新記錄
        existing_dates = {d["date"] for d in margin_days}
        margin_days = [d for d in margin_days if d["date"] != today]
        margin_days.append({"date": today, "balance": today_bal})
        # 前日如果尚未存在，補入
        if prev_str not in existing_dates and prev_bal > 0:
            margin_days.append({"date": prev_str, "balance": prev_bal})
        margin_days.sort(key=lambda x: x["date"])
        # 若歷史不足 MARGIN_KEEP，補抓（英文版 API 支援）
        if len(margin_days) < MARGIN_KEEP:
            margin_days = backfill_margin(margin_days, today, need=MARGIN_KEEP)
        margin_days = margin_days[-MARGIN_KEEP:]
        data["margin_days"] = margin_days

    # 6. 更新 HTML
    print("\n【5】更新 HTML...")
    with open(HTML_PATH, "r", encoding="utf-8") as f:
        html = f.read()

    today_mmdd   = mmdd(today)
    today_slash  = slash_date(today)
    today_dash   = dash_date(today)

    # dateLabel
    html = re.sub(r'id="dateLabel">[^<]+<', f'id="dateLabel">{today_dash}<', html)

    # demo-flag
    html = re.sub(
        r'(✅ 外資／投信買賣超、期貨淨未平倉、USD/TWD 匯率均為 <b>)[^<]*(</b> 真實資料)',
        rf'\g<1>{today_slash}\g<2>', html)

    # TOTAL constant
    if bfi:
        html = re.sub(
            r'const TOTAL = \{[^}]+\};',
            f'const TOTAL = {{ date:"{today_slash}", foreign:{bfi["foreign"]}, trust:{bfi["trust"]}, dealer:{bfi["dealer"]} }};',
            html)

    # TOTAL arrays（append）
    if bfi:
        html, _ = append_array(html, "TOTAL_DATES",   today_mmdd,       is_str=True)
        html, _ = append_array(html, "TOTAL_FOREIGN",  bfi["foreign"])
        html, _ = append_array(html, "TOTAL_TRUST",    bfi["trust"])
        html, _ = append_array(html, "TOTAL_DEALER",   bfi["dealer"])

    # DEMO object（整塊替換）
    new_demo = fmt_demo_js(demo, today_slash)
    html = re.sub(
        r'// ===== 真實資料[^\n]*\nconst DEMO = \{.*?\};',
        new_demo, html, flags=re.DOTALL)

    # FUT arrays
    if fut_val is not None:
        html, _ = append_array(html, "FUT_DATES", today_mmdd, is_str=True)
        html, _ = append_array(html, "FUT_DEMO",  fut_val)

    # FX_DEMO
    if fx_val is not None:
        html, _ = append_array(html, "FX_DEMO", fx_val)

    # MARGIN_DATES / MARGIN_BAL（整塊替換，保持與 data.json 同步）
    if margin_data and len(margin_days) >= 2:
        m_dates_js = json.dumps([mmdd(d["date"]) for d in margin_days], ensure_ascii=False)
        m_bals_js  = json.dumps([d["balance"] for d in margin_days])
        html = re.sub(r'const MARGIN_DATES = \[.*?\];', f'const MARGIN_DATES = {m_dates_js};', html, flags=re.DOTALL)
        html = re.sub(r'const MARGIN_BAL = \[.*?\];',   f'const MARGIN_BAL = {m_bals_js};',   html, flags=re.DOTALL)

    # Footer
    d15_start = t86_days[-15]["date"] if len(t86_days) >= 15 else t86_days[0]["date"]
    d5_start  = t86_days[-5]["date"]  if len(t86_days) >= 5  else t86_days[0]["date"]

    if margin_data and len(margin_days) >= 2:
        chg_wan = (margin_data["today"] - margin_data["prev"]) / 10000
        bal_wan  = margin_data["today"] / 10000
        html = re.sub(
            r'<b>全市場融資餘額</b>：.*?<br>',
            f'<b>全市場融資餘額</b>：來源 TWSE MI_MARGN，資料日期 <b>{today_slash}</b>'
            f'（{bal_wan:.0f} 萬張，當日增減 {chg_wan:+.0f} 萬張）<br>',
            html)

    if bfi:
        html = re.sub(
            r'<b>三大法人總買賣超金額</b>：來源 TWSE BFI82U，資料日期 <b>[^<]+</b>（[^）]+）',
            f'<b>三大法人總買賣超金額</b>：來源 TWSE BFI82U，資料日期 <b>{today_slash}</b>'
            f'（外資 {fmt_yi(bfi["foreign"])}、投信 {fmt_yi(bfi["trust"])}、自營商 {fmt_yi(bfi["dealer"])}）',
            html)

    html = re.sub(
        r'<b>外資／投信買賣超排行</b>：來源 TWSE T86；[^<]+',
        f'<b>外資／投信買賣超排行</b>：來源 TWSE T86；'
        f'1日 = {today_slash}，5日 = {mmdd(d5_start)}–{today_mmdd}，15日 = {mmdd(d15_start)}–{today_mmdd}',
        html)

    if fut_val is not None:
        m_fut = re.search(r'const FUT_DATES = \[(.*?)\]', html, re.DOTALL)
        fut_dates = [x.strip().strip('"') for x in m_fut.group(1).split(',') if x.strip()] if m_fut else []
        fut_start_mmdd = fut_dates[0] if fut_dates else "07/01"
        fut_count = len(fut_dates)
        abs_fut = abs(fut_val)
        fut_dir = "淨空單" if fut_val < 0 else "淨多單"
        html = re.sub(
            r'<b>外資臺股期貨淨未平倉</b>：來源 TAIFEX，資料日期 <b>[^<]+</b>（[^）]+）',
            f'<b>外資臺股期貨淨未平倉</b>：來源 TAIFEX，資料日期 <b>2026/{fut_start_mmdd}–{today_mmdd}</b>'
            f'（{fut_count}個交易日真實資料，07/10為非交易日；最新 {abs_fut:,} 口{fut_dir}）',
            html)

    if fx_val:
        html = re.sub(
            r'<b>USD/TWD 匯率</b>：來源 TAIFEX，資料日期 <b>[^<]+</b>（[\d.]+ 元）',
            f'<b>USD/TWD 匯率</b>：來源 TAIFEX，資料日期 <b>{today_slash}</b>（{fx_val} 元）',
            html)

    # 7. 寫入檔案
    with open(HTML_PATH, "w", encoding="utf-8") as f:
        f.write(html)
    save_data(data)
    print("   index.html 與 data.json 已儲存")

    # 8. 部署 — git push 到 GitHub Pages
    print("\n【6】部署至 GitHub Pages...")
    deploy_to_github_pages(today)

    print(f"\n=== 完成 {dash_date(today)} ===\n")


def deploy_to_github_pages(today):
    def run(args, **kw):
        return subprocess.run(args, cwd=BASE_DIR, capture_output=True, text=True, timeout=30, **kw)

    run(["git", "add", "-A"])
    diff = run(["git", "diff", "--cached", "--quiet"])
    if diff.returncode == 0:
        print("   沒有變動，跳過 commit/push")
        return

    commit = run(["git", "commit", "-m", f"Update {dash_date(today)} 資料"])
    if commit.returncode != 0:
        print(f"   ❌ commit 失敗：{commit.stderr[:300]}")
        return

    try:
        token = open(GITHUB_TOKEN_PATH).read().strip()
    except FileNotFoundError:
        print(f"   ❌ 找不到 GitHub token（{GITHUB_TOKEN_PATH}），已 commit 但沒有 push")
        return

    push_url = f"https://oauth2:{token}@{GITHUB_REPO_URL}"
    result = run(["git", "push", push_url, "main"])
    if result.returncode == 0:
        print("   ✅ 部署成功 → https://debby0524.github.io/tw-institutional-tracker/")
    else:
        # stderr 可能含 token，僅顯示不含 push_url 的部分
        err = result.stderr.replace(token, "***")[:300]
        print(f"   ❌ push 失敗：{err}")
    return


if __name__ == "__main__":
    main()
