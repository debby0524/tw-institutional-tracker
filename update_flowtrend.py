#!/usr/bin/env python3
"""
產業金流動向（flowtrend.html）— 每日更新腳本

由 update.py 的 main() 在每天更新完 index.html/data.json 後呼叫。獨立成本檔，
不動 data.json 既有結構（data.json 只給 index.html 用）。

資料來源：
  TWSE T86（上市三大法人買賣超）＋ TWSE MI_INDEX（上市收盤價）
  TPEx 3itrade_hedge_result（上櫃三大法人買賣超，支援歷史日期查詢）
    ＋ TPEx daily_close_quotes（上櫃收盤價）
  FinMind TaiwanStockInfo（官方 industry_category 產業分類，變動不頻繁，本機快取
    INDUSTRY_MAP_MAX_AGE_DAYS 天才重抓一次）

快取檔：flow_data.json（gitignore，不進 repo，跟 data.json 同層級）
  - industry_map: {股票代號: 產業分類}
  - industry_map_updated: 上次重抓日期
  - days: 最近 FLOW_KEEP 個交易日，每天 {date, twse:[...], tpex:[...], close:{...}}
"""

import json
import os
import re
import time
from collections import defaultdict
from datetime import datetime, timedelta

from update import BASE_DIR, http_get, parse_num, parse_float, fetch_t86, fetch_close_prices

FLOW_CACHE_PATH      = os.path.join(BASE_DIR, "flow_data.json")
FLOWTREND_HTML_PATH  = os.path.join(BASE_DIR, "flowtrend.html")

FLOW_KEEP = 25                    # d20 視窗 + alert 用的過去20日對照，留5天緩衝
INDUSTRY_MAP_MAX_AGE_DAYS = 30    # 產業分類多久重抓一次
VERDICT_STAY = 0.6
VERDICT_MIX  = 0.25
ALERT_MIN_HIST_DAYS = 10
ALERT_LOOKBACK_DAYS = 20
ALERT_RATIO = 3.0

# 這幾類是基金/受益憑證包裝，不是傳統產業分類，「產業金流動向」頁面不收錄
NON_INDUSTRY_CATEGORIES = {
    "ETF", "ETN", "上櫃ETF", "上櫃指數股票型基金(ETF)", "受益證券", "指數投資證券(ETN)",
}


# ──────────────────────────────────────────
# 資料抓取
# ──────────────────────────────────────────
def roc_date(date_str_8):
    y = int(date_str_8[:4]) - 1911
    return f"{y}/{date_str_8[4:6]}/{date_str_8[6:8]}"


def fetch_tpex_insti(date_str):
    """上櫃三大法人買賣超（歷史查詢版）。回傳 (is_ok, stocks_list)，
    stocks 內 f=外資及陸資合計買賣超股數, t=投信買賣超股數（跟 TWSE T86 同單位/語意）。"""
    url = (f"https://www.tpex.org.tw/web/stock/3insti/daily_trade/3itrade_hedge_result.php"
           f"?l=zh-tw&d={roc_date(date_str)}&se=EW&t=D")
    raw = http_get(url)
    if not raw:
        return False, []
    try:
        d = json.loads(raw)
        tables = d.get("tables", [])
        if not tables or not tables[0].get("data"):
            return False, []
        stocks = []
        for row in tables[0]["data"]:
            code = row[0].strip()
            name = row[1].strip()
            foreign_net = parse_num(row[10])  # 外資及陸資合計-買賣超
            trust_net   = parse_num(row[13])  # 投信-買賣超
            if foreign_net != 0 or trust_net != 0:
                stocks.append({"c": code, "n": name, "f": foreign_net, "t": trust_net})
        return True, stocks
    except Exception as e:
        print(f"  [TPEx INSTI ERR] {e}")
        return False, []


def fetch_tpex_close(date_str):
    """上櫃收盤價。回傳 {代號: 收盤價}。"""
    url = (f"https://www.tpex.org.tw/web/stock/aftertrading/daily_close_quotes/stk_quote_result.php"
           f"?l=zh-tw&d={roc_date(date_str)}&se=EW")
    raw = http_get(url)
    if not raw:
        return {}
    try:
        d = json.loads(raw)
        tables = d.get("tables", [])
        if not tables:
            return {}
        prices = {}
        for row in tables[0].get("data", []):
            code = row[0].strip()
            price = parse_float(row[2])
            if price > 0:
                prices[code] = price
        return prices
    except Exception as e:
        print(f"  [TPEx CLOSE ERR] {e}")
        return {}


def fetch_industry_map():
    """FinMind TaiwanStockInfo，回傳 {股票代號: 產業分類}（只留上市／上櫃）。"""
    raw = http_get("https://api.finmindtrade.com/api/v4/data?dataset=TaiwanStockInfo", timeout=20)
    if not raw:
        return {}
    try:
        d = json.loads(raw)
        if d.get("status") != 200:
            return {}
        m = {}
        for row in d.get("data", []):
            cat = row.get("industry_category")
            if row.get("type") in ("twse", "tpex") and cat and cat not in NON_INDUSTRY_CATEGORIES:
                m[row["stock_id"]] = cat
        return m
    except Exception as e:
        print(f"  [FINMIND ERR] {e}")
        return {}


def fetch_one_day(date_str):
    """抓單日 TWSE+TPEx 三大法人買賣超 + 收盤價。任一市場成功即算 ok。"""
    ok_tw, tw_stocks = fetch_t86(date_str)
    time.sleep(0.3)
    ok_tp, tp_stocks = fetch_tpex_insti(date_str)
    if not ok_tw and not ok_tp:
        return False, None
    time.sleep(0.3)
    tw_close = fetch_close_prices(date_str) if ok_tw else {}
    time.sleep(0.3)
    tp_close = fetch_tpex_close(date_str) if ok_tp else {}
    close = {**tw_close, **tp_close}
    return True, {"date": date_str, "twse": tw_stocks, "tpex": tp_stocks, "close": close}


# ──────────────────────────────────────────
# 快取讀寫 / 補抓歷史
# ──────────────────────────────────────────
def load_flow_cache():
    if os.path.exists(FLOW_CACHE_PATH):
        with open(FLOW_CACHE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"industry_map": {}, "industry_map_updated": None, "days": []}


def save_flow_cache(cache):
    with open(FLOW_CACHE_PATH, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, separators=(",", ":"))


def backfill_flow_days(days, today_str, need=FLOW_KEEP):
    existing = {d["date"] for d in days}
    if len(existing) >= need:
        return days
    print(f"  產業金流歷史不足（{len(existing)}/{need}），補抓...")
    candidate = datetime.strptime(today_str, "%Y%m%d") - timedelta(days=1)
    tries, max_try = 0, 60
    while len(existing) < need and tries < max_try:
        ds = candidate.strftime("%Y%m%d")
        if candidate.weekday() < 5 and ds not in existing:
            ok, rec = fetch_one_day(ds)
            if ok:
                days.append(rec)
                existing.add(ds)
                print(f"    補抓 {ds}: TWSE {len(rec['twse'])} / TPEx {len(rec['tpex'])} 筆")
            else:
                print(f"    {ds}: 無資料（非交易日或抓取失敗）")
        candidate -= timedelta(days=1)
        tries += 1
    days.sort(key=lambda x: x["date"])
    return days


# ──────────────────────────────────────────
# 聚合計算
# ──────────────────────────────────────────
def daily_industry_nets(day, industry_map):
    """單日、依產業聚合。回傳 (lots_by_ind, value_by_ind, codes_lots_by_ind, codes_value_by_ind)。"""
    merged = {}
    for s in day["twse"] + day["tpex"]:
        c = s["c"]
        pf, pt = merged.get(c, (0, 0))
        merged[c] = (pf + s["f"], pt + s["t"])

    close = day.get("close", {})
    lots, value = defaultdict(float), defaultdict(float)
    codes_lots, codes_value = defaultdict(set), defaultdict(set)

    for c, (f, t) in merged.items():
        ind = industry_map.get(c)
        if not ind:
            continue
        net_shares = f + t
        if net_shares == 0:
            continue
        lots[ind] += net_shares / 1000
        codes_lots[ind].add(c)
        price = close.get(c)
        if price:
            value[ind] += net_shares * price / 1e8
            codes_value[ind].add(c)

    return lots, value, codes_lots, codes_value


def build_series(days, industry_map):
    daily_lots, daily_value = [], []
    codes_lots, codes_value = [], []
    for day in days:
        l, v, cl, cv = daily_industry_nets(day, industry_map)
        daily_lots.append(l)
        daily_value.append(v)
        codes_lots.append(cl)
        codes_value.append(cv)
    return daily_lots, daily_value, codes_lots, codes_value


def persistence_and_verdict(vals):
    if not vals:
        return 0.0, "只是經過"
    s = sum(vals)
    a = sum(abs(v) for v in vals)
    p = abs(s) / a if a > 0 else 0.0
    if p >= VERDICT_STAY:
        v = "堆著"
    elif p >= VERDICT_MIX:
        v = "混合"
    else:
        v = "只是經過"
    return round(p, 2), v


def union_members(codes_list):
    """codes_list: list of {產業: set(代號)}（每天一筆）。回傳 {產業: set(代號)}（跨所有天聯集）。"""
    out = defaultdict(set)
    for day_map in codes_list:
        for ind, s in day_map.items():
            out[ind] |= s
    return out


def compute_period(daily_maps, window, decimals, members_map):
    window = min(window, len(daily_maps))
    recent = daily_maps[-window:] if window > 0 else []
    industries = set(members_map.keys())
    for m in recent:
        industries.update(m.keys())

    rows = []
    for ind in industries:
        members = len(members_map.get(ind, set()))
        if members == 0:
            continue
        vals = [m.get(ind, 0.0) for m in recent]
        cum = round(sum(vals), decimals)
        if window <= 1:
            p, verdict = 1.0, "單日"
        else:
            p, verdict = persistence_and_verdict(vals)
        rows.append({"label": ind, "members": members, "days": window,
                      "cum": cum, "persistence": p, "verdict": verdict})

    rows.sort(key=lambda r: -r["cum"])
    return rows


def compute_flow_data(days, industry_map):
    daily_lots, daily_value, codes_lots, codes_value = build_series(days, industry_map)
    members_lots  = union_members(codes_lots)
    members_value = union_members(codes_value)

    periods = [("d1", 1), ("d2", 2), ("d3", 3), ("d5", 5), ("d20", 20)]
    flow = {}
    for key, window in periods:
        flow[key] = {
            "lots":  compute_period(daily_lots,  window, 0, members_lots),
            "value": compute_period(daily_value, window, 2, members_value),
        }
    return flow, members_lots, members_value, daily_lots


def compute_sample_meta(industry_map, members_lots, members_value):
    universe_total = len(set(industry_map.keys()))
    members_total = len(set().union(*members_lots.values())) if members_lots else 0
    value_members_total = len(set().union(*members_value.values())) if members_value else 0
    n_industries = len(set(industry_map.values()))
    return {
        "members_total": members_total,
        "value_members_total": value_members_total,
        "universe_total": universe_total,
        "n_industries": n_industries,
    }


def compute_alerts(daily_lots, members_lots):
    """今日剛啟動：今日淨流入(張) >= 自身近20日均值(不含今日) 3倍以上，且方向為買超，
    過去20日(不含今日)尚未列「堆著」。歷史不足 ALERT_MIN_HIST_DAYS 天不判斷。"""
    alerts = []
    if len(daily_lots) < ALERT_MIN_HIST_DAYS + 1:
        return alerts

    today_map = daily_lots[-1]
    hist = daily_lots[:-1]
    window = hist[-ALERT_LOOKBACK_DAYS:] if len(hist) > ALERT_LOOKBACK_DAYS else hist
    if len(window) < ALERT_MIN_HIST_DAYS:
        return alerts

    for ind, today_val in today_map.items():
        if today_val <= 0:
            continue
        past_vals = [d.get(ind, 0.0) for d in window]
        avg_abs = sum(abs(v) for v in past_vals) / len(past_vals)
        if avg_abs <= 0:
            continue
        ratio = today_val / avg_abs
        if ratio < ALERT_RATIO:
            continue
        _, past_verdict = persistence_and_verdict(past_vals)
        if past_verdict == "堆著":
            continue
        alerts.append({
            "label": ind,
            "members": len(members_lots.get(ind, set())),
            "ratio": round(ratio, 1),
            "cum": round(today_val, 0),
        })

    alerts.sort(key=lambda a: -a["ratio"])
    return alerts


# ──────────────────────────────────────────
# HTML 寫入
# ──────────────────────────────────────────
def render_flowtrend_html(flow_data, sample_meta, alerts, industry_map_updated):
    with open(FLOWTREND_HTML_PATH, "r", encoding="utf-8") as f:
        html = f.read()

    flow_js  = json.dumps(flow_data, ensure_ascii=False, separators=(",", ":"))
    meta_js  = json.dumps(sample_meta, ensure_ascii=False, separators=(",", ":"))
    alerts_js = json.dumps(alerts, ensure_ascii=False, separators=(",", ":"))
    period_label_js = json.dumps(
        {"d1": "1天", "d2": "2天", "d3": "3天", "d5": "近一週累積", "d20": "近一月累積"},
        ensure_ascii=False, separators=(",", ":"))

    html = re.sub(r'const FLOW_DATA = .*?;',    f'const FLOW_DATA = {flow_js};',    html, count=1, flags=re.DOTALL)
    html = re.sub(r'const SAMPLE_META = .*?;',  f'const SAMPLE_META = {meta_js};',  html, count=1, flags=re.DOTALL)
    html = re.sub(r'const ALERTS = .*?;',       f'const ALERTS = {alerts_js};',     html, count=1, flags=re.DOTALL)
    html = re.sub(r'const PERIOD_LABEL = .*?;', f'const PERIOD_LABEL = {period_label_js};', html, count=1, flags=re.DOTALL)

    html = re.sub(
        r'<div class="seg pill" id="segPeriod">.*?</div>',
        '<div class="seg pill" id="segPeriod">\n'
        '    <button data-v="d1">1天</button>\n'
        '    <button data-v="d2">2天</button>\n'
        '    <button data-v="d3">3天</button>\n'
        '    <button data-v="d5">近一週</button>\n'
        '    <button data-v="d20" class="active">近一月</button>\n'
        '  </div>',
        html, count=1, flags=re.DOTALL)

    # 讓5個按鈕在窄螢幕能換行，不會被裁切
    html = html.replace(
        '.seg{\n    display:flex; background: var(--panel); border:1px solid var(--line);\n'
        '    border-radius: 8px; overflow:hidden; width:fit-content; margin-top:14px;\n  }',
        '.seg{\n    display:flex; flex-wrap:wrap; background: var(--panel); border:1px solid var(--line);\n'
        '    border-radius: 8px; overflow:hidden; width:fit-content; margin-top:14px;\n  }')

    html = html.replace(
        '可切換今日／近一週（5日）／近一月（20日）三種累積區間；',
        '可切換1天／2天／3天／近一週（5日）／近一月（20日）五種累積區間；')

    html = re.sub(
        r'產業分類為 FinMind TaiwanStockInfo 官方 industry_category（分類本身不常變動，不需每日重抓）。',
        f'產業分類為 FinMind TaiwanStockInfo 官方 industry_category（本機快取，最近更新 {industry_map_updated}）。',
        html)

    with open(FLOWTREND_HTML_PATH, "w", encoding="utf-8") as f:
        f.write(html)


# ──────────────────────────────────────────
# 主流程（給 update.py 呼叫）
# ──────────────────────────────────────────
def update_flowtrend(today_str):
    print("\n【產業金流動向】更新中...")
    cache = load_flow_cache()

    # 1. 產業分類快取（不常變動，過期才重抓）
    updated = cache.get("industry_map_updated")
    stale = True
    if updated:
        age = (datetime.strptime(today_str, "%Y%m%d") - datetime.strptime(updated, "%Y%m%d")).days
        stale = age > INDUSTRY_MAP_MAX_AGE_DAYS
    if stale or not cache.get("industry_map"):
        print("  重新抓取產業分類（FinMind TaiwanStockInfo）...")
        m = fetch_industry_map()
        if m:
            cache["industry_map"] = m
            cache["industry_map_updated"] = today_str
            print(f"    取得 {len(m)} 檔股票的產業分類")
        else:
            print("    抓取失敗，沿用舊快取")
    industry_map = cache.get("industry_map", {})
    if not industry_map:
        print("  無產業分類資料，略過本次更新")
        return

    # 2. 今天的資料
    days = [d for d in cache.get("days", []) if d["date"] != today_str]
    ok, today_rec = fetch_one_day(today_str)
    if not ok:
        print("  今日 TWSE/TPEx 資料皆抓取失敗，略過本次更新")
        return
    print(f"  今日：TWSE {len(today_rec['twse'])} / TPEx {len(today_rec['tpex'])} 筆，收盤價 {len(today_rec['close'])} 檔")
    days.append(today_rec)
    days.sort(key=lambda x: x["date"])

    # 3. 補齊歷史（首次執行 / 缺資料時）
    if len(days) < FLOW_KEEP:
        days = backfill_flow_days(days, today_str, need=FLOW_KEEP)
        days = [d for d in days if d["date"] != today_str]
        days.append(today_rec)
        days.sort(key=lambda x: x["date"])

    days = days[-FLOW_KEEP:]
    cache["days"] = days
    save_flow_cache(cache)
    print(f"  產業金流資料範圍：{days[0]['date']} – {days[-1]['date']}（{len(days)} 天）")

    # 4. 計算
    flow_data, members_lots, members_value, daily_lots = compute_flow_data(days, industry_map)
    sample_meta = compute_sample_meta(industry_map, members_lots, members_value)
    alerts = compute_alerts(daily_lots, members_lots)

    print(f"  {sample_meta['n_industries']} 個產業別，市場涵蓋率 {sample_meta['members_total']}/{sample_meta['universe_total']}")
    if alerts:
        print(f"  今日剛啟動訊號：{'、'.join(a['label'] for a in alerts)}")

    # 5. 寫入 flowtrend.html
    render_flowtrend_html(flow_data, sample_meta, alerts, cache.get("industry_map_updated") or today_str)
    print("  flowtrend.html 已更新")


if __name__ == "__main__":
    today = datetime.now().strftime("%Y%m%d")
    update_flowtrend(today)
