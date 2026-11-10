#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
小台指期 20/120 均線交叉多空 + 2倍ATR 移動停損 —— 雲端訊號程式
==================================================================
每次執行：
  1. 從期交所抓最新日K（日盤+夜盤合併的全盤K棒），補進 data/mtx_daily.csv
  2. 從期交所即時行情抓目前價格（盤中的「當日K棒」）
  3. 從 2022 年起重播整套策略，算出目前部位、移動停損價、下一個動作
  4. 有新訊號（進場/出場/反手/停損觸發）就用 ntfy 推播到手機
  5. 產生 site/data.json 給圖表網頁使用

策略規則（與回測完全一致）：
  - 日K收盤 20MA > 120MA → 做多；20MA < 120MA → 做空（下一個交易時段開盤執行，即夜盤 15:00）
  - 進場後移動停損 = 進場後最有利價格 ∓ 2 × ATR(14)
  - 停損出場後，若均線方向不變，下一個交易時段開盤重新進場
只用 Python 標準函式庫，不需安裝套件。
"""
import csv, json, os, sys, math, urllib.request, urllib.parse, datetime as dt

TZ = dt.timezone(dt.timedelta(hours=8))
ROOT = os.path.dirname(os.path.abspath(__file__))
CSV_PATH = os.path.join(ROOT, "data", "mtx_daily.csv")
STATE_PATH = os.path.join(ROOT, "data", "notified.json")
SITE_DIR = os.path.join(ROOT, "site")
START = "2022-01-01"
FAST, SLOW, ATR_N, ATR_MULT = 20, 120, 14, 2.0
NONE_TREND = 0   # 均線資料不足時不持倉
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "")
PAGE_URL = os.environ.get("PAGE_URL", "")
UA = {"User-Agent": "Mozilla/5.0 (mtx-signal)"}


def now_tw():
    return dt.datetime.now(TZ)


# ───────────── 資料 ─────────────
def load_csv():
    with open(CSV_PATH, encoding="utf-8") as f:
        return [dict(date=r["date"], open=float(r["open"]), high=float(r["high"]), low=float(r["low"]),
                     close=float(r["close"]), source=r.get("source", "")) for r in csv.DictReader(f)]


def save_csv(bars):
    with open(CSV_PATH, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f); w.writerow(["date", "open", "high", "low", "close", "source"])
        for b in bars:
            w.writerow([b["date"], int(b["open"]), int(b["high"]), int(b["low"]), int(b["close"]), b["source"]])


def fetch_taifex_daily(days=20):
    """期交所 期貨每日交易行情下載：回傳 {日期: 全盤K棒}，全盤 = 當日盤後(前一晚夜盤) + 當日一般"""
    end = now_tw().date(); start = end - dt.timedelta(days=days)
    body = urllib.parse.urlencode(dict(down_type="1", commodity_id="MTX", commodity_id2="",
                                       queryStartDate=start.strftime("%Y/%m/%d"),
                                       queryEndDate=end.strftime("%Y/%m/%d"))).encode()
    req = urllib.request.Request("https://www.taifex.com.tw/cht/3/futDataDown", data=body,
                                 headers={**UA, "Content-Type": "application/x-www-form-urlencoded"})
    txt = urllib.request.urlopen(req, timeout=30).read().decode("big5", errors="ignore")
    rows = {}
    for line in txt.splitlines()[1:]:
        a = [x.strip() for x in line.split(",")]
        if len(a) < 18 or a[1] != "MTX" or not a[2].isdigit() or len(a[2]) != 6 or a[3] in ("-", ""):
            continue  # 只取月合約（排除週選、價差）
        date = a[0].replace("/", "-"); sess = a[17]
        rows.setdefault(date, {}).setdefault(a[2], {})[sess] = dict(
            o=float(a[3]), h=float(a[4]), l=float(a[5]), c=float(a[6]), v=float(a[9] or 0))
    out = {}
    for date, cons in rows.items():
        # 近月 = 當日一般盤成交量最大的月合約
        cands = [(v["一般"]["v"], m) for m, v in cons.items() if "一般" in v]
        if not cands: continue
        m = max(cands)[1]; day = cons[m]["一般"]; night = cons[m].get("盤後")
        o = night["o"] if night else day["o"]
        h = max(day["h"], night["h"]) if night else day["h"]
        l = min(day["l"], night["l"]) if night else day["l"]
        out[date] = dict(date=date, open=o, high=h, low=l, close=day["c"], source="taifex")
    return out


def fetch_live():
    """期交所行情資訊網：回傳近月小台 日盤/夜盤 即時報價"""
    res = {}
    for mt, name in (("0", "day"), ("1", "night")):
        body = json.dumps(dict(MarketType=mt, SymbolType="F", KindID="1", CID="MXF", ExpireMonth="",
                               RowSize="全部", PageNo="", SortColumn="", AscDesc="A")).encode()
        req = urllib.request.Request("https://mis.taifex.com.tw/futures/api/getQuoteList", data=body,
                                     headers={**UA, "Content-Type": "application/json"})
        j = json.loads(urllib.request.urlopen(req, timeout=20).read().decode("utf-8"))
        best = None
        for q in (j.get("RtData") or {}).get("QuoteList") or []:
            sid = q.get("SymbolID", "")
            if not sid.startswith("MXF") or sid.startswith("MXF-") or not q.get("CLastPrice"):
                continue
            vol = float(q.get("CTotalVolume") or 0)
            if best is None or vol > best[0]:
                best = (vol, q)
        if best:
            q = best[1]
            res[name] = dict(symbol=q["DispCName"], o=float(q["COpenPrice"]), h=float(q["CHighPrice"]),
                             l=float(q["CLowPrice"]), c=float(q["CLastPrice"]), date=q["CDate"], time=q["CTime"])
    return res


def live_bar(live, last_date):
    """把即時報價組成「目前交易日」的K棒（尚未收盤）"""
    if not live: return None
    t = now_tw(); d, n = live.get("day"), live.get("night")
    hm = t.hour * 100 + t.minute
    if hm >= 1500 or hm < 500:      # 夜盤進行中 → 屬於下一個交易日
        if not n: return None
        return dict(o=n["o"], h=n["h"], l=n["l"], c=n["c"], label="夜盤進行中", time=n["time"])
    today = t.strftime("%Y%m%d")
    if d and d["date"] == today and t.strftime("%Y-%m-%d") > last_date:
        o = n["o"] if (n and n["date"] == today) else d["o"]
        h = max(d["h"], n["h"]) if (n and n["date"] == today) else d["h"]
        l = min(d["l"], n["l"]) if (n and n["date"] == today) else d["l"]
        return dict(o=o, h=h, l=l, c=d["c"], label="日盤" + ("進行中" if hm < 1345 else "已收盤"), time=d["time"])
    if n and hm < 845 and t.strftime("%Y-%m-%d") > last_date:
        return dict(o=n["o"], h=n["h"], l=n["l"], c=n["c"], label="夜盤已收、等待日盤", time=n["time"])
    return None


# ───────────── 指標 ─────────────
def sma(x, n):
    out, s = [None] * len(x), 0.0
    for i, v in enumerate(x):
        s += v
        if i >= n: s -= x[i - n]
        if i >= n - 1: out[i] = s / n
    return out


def ema(x, span=None, alpha=None):
    a = alpha if alpha else 2 / (span + 1); out = []; p = None
    for v in x:
        p = v if p is None else p + a * (v - p); out.append(p)
    return out


def indicators(B):
    c = [b["close"] for b in B]
    ma20, ma120 = sma(c, FAST), sma(c, SLOW)
    tr = [B[0]["high"] - B[0]["low"]] + [max(B[i]["high"] - B[i]["low"], abs(B[i]["high"] - c[i - 1]),
                                              abs(B[i]["low"] - c[i - 1])) for i in range(1, len(B))]
    atr = ema(tr, alpha=1 / ATR_N)
    dif = [a - b for a, b in zip(ema(c, 12), ema(c, 26))]; dea = ema(dif, 9)
    K, D = [], []; k = d = 50.0
    for i, b in enumerate(B):
        w = B[max(0, i - 8):i + 1]; hh = max(x["high"] for x in w); ll = min(x["low"] for x in w)
        rsv = 50.0 if hh == ll else (b["close"] - ll) / (hh - ll) * 100
        k = k * 2 / 3 + rsv / 3; d = d * 2 / 3 + k / 3; K.append(k); D.append(d)
    return dict(ma20=ma20, ma120=ma120, atr=atr, dif=dif, dea=dea, hist=[a - b for a, b in zip(dif, dea)], K=K, D=D)


# ───────────── 策略重播 ─────────────
def replay(B, ind):
    """從 START 起逐日重播，回傳交易紀錄、目前狀態、下一步動作"""
    pos = 0; e = ext = 0.0; want = 0; trades = []; events = []
    def trend(i):
        a, b = ind["ma20"][i], ind["ma120"][i]
        if a is None or b is None: return NONE_TREND
        return 1 if a > b else -1
    s0 = next(i for i, b in enumerate(B) if b["date"] >= START)
    for i in range(s0, len(B)):
        b = B[i]
        if want != pos:
            if pos:
                trades.append(dict(entry_date=ent, exit_date=b["date"], side=pos, entry=e, exit=b["open"], why="訊號")); events.append((b["date"], "exit", pos))
            if want:
                pos, e, ext, ent = want, b["open"], b["open"], b["date"]; events.append((b["date"], "entry", pos))
        if pos:
            stop = ext - pos * ATR_MULT * ind["atr"][i - 1]
            if (pos > 0 and b["low"] <= stop) or (pos < 0 and b["high"] >= stop):
                px = min(b["open"], stop) if pos > 0 else max(b["open"], stop)
                trades.append(dict(entry_date=ent, exit_date=b["date"], side=pos, entry=e, exit=px, why="停損")); events.append((b["date"], "stop", pos))
                pos = 0
        if pos:
            ext = max(ext, b["high"]) if pos > 0 else min(ext, b["low"])
        want = trend(i)
    last = len(B) - 1
    stop_next = (ext - pos * ATR_MULT * ind["atr"][last]) if pos else None
    return dict(pos=pos, entry=e if pos else None, entry_date=ent if pos else None, ext=ext if pos else None,
                stop=stop_next, want=want, trades=trades, events=events, atr=ind["atr"][last])


# ───────────── 通知 ─────────────
def load_state():
    try:
        return json.load(open(STATE_PATH, encoding="utf-8"))
    except Exception:
        return {"sent": []}


def notify(key, title, msg, state, priority=4, tags=None):
    if key in state["sent"]:
        return False
    if NTFY_TOPIC:
        body = json.dumps(dict(topic=NTFY_TOPIC, title=title, message=msg, priority=priority,
                               tags=tags or [], click=PAGE_URL or None)).encode()
        req = urllib.request.Request("https://ntfy.sh/", data=body, headers={**UA, "Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=20).read()
    print("推播:", title, "|", msg)
    state["sent"] = (state["sent"] + [key])[-300:]
    return True


SIDE = {1: "做多", -1: "做空", 0: "空手"}


def main():
    t = now_tw(); log = []
    B = load_csv(); changed = False
    try:
        new = fetch_taifex_daily()
        have = {b["date"]: i for i, b in enumerate(B)}
        for date in sorted(new):
            if date < B[-1]["date"] and date in have:
                continue
            if date in have:
                if B[have[date]]["source"] != "taifex": B[have[date]] = new[date]; changed = True
            elif date > B[-1]["date"]:
                B.append(new[date]); changed = True
        log.append(f"期交所日資料 OK（最新 {B[-1]['date']}）")
    except Exception as ex:
        log.append(f"期交所日資料失敗：{ex}")
    if changed: save_csv(B)

    live = {}
    try:
        live = fetch_live(); log.append("即時行情 OK")
    except Exception as ex:
        log.append(f"即時行情失敗：{ex}")
    # 日盤收盤後（13:45~15:00）期交所日資料可能還沒公布 → 先用即時行情組出今日K棒，確保 15:00 前發出訊號
    today = t.strftime("%Y-%m-%d"); hm = t.hour * 100 + t.minute
    d_q, n_q = live.get("day"), live.get("night")
    if 1345 <= hm < 1500 and B[-1]["date"] < today and d_q and d_q["date"] == t.strftime("%Y%m%d") and d_q["time"] >= "134400":
        night_ok = n_q and n_q["date"] == d_q["date"] and n_q["time"] < "060000"
        B.append(dict(date=today, open=n_q["o"] if night_ok else d_q["o"],
                      high=max(d_q["h"], n_q["h"]) if night_ok else d_q["h"],
                      low=min(d_q["l"], n_q["l"]) if night_ok else d_q["l"],
                      close=d_q["c"], source="live"))
        save_csv(B); log.append("今日K棒暫以即時行情組成（期交所日資料公布後會自動更正）")
    lb = live_bar(live, B[-1]["date"])

    ind = indicators(B); st = replay(B, ind); state = load_state()
    last = B[-1]; i = len(B) - 1
    ma20, ma120 = ind["ma20"][i], ind["ma120"][i]

    # 1) 收盤後：下一個交易時段要做什麼
    if st["want"] != st["pos"]:
        act = st["want"]; cur = st["pos"]
        if cur == 0:
            title = f"📢 小台 {'做多' if act > 0 else '做空'}進場訊號"
            how = f"下一個交易時段開盤（夜盤 15:00）{'買進' if act > 0 else '賣出'}"
        else:
            title = f"🔄 小台 反手{'做多' if act > 0 else '做空'}"
            how = f"下一個交易時段開盤平掉{SIDE[cur]}單，改{'做多' if act > 0 else '做空'}"
        approx_stop = last["close"] - act * ATR_MULT * st["atr"]
        msg = (f"{last['date']} 收盤 {last['close']:.0f}\n20MA {ma20:.0f} {'>' if ma20 > ma120 else '<'} 120MA {ma120:.0f}\n"
               f"{how}\n停損 ≈ 進場價 {'−' if act > 0 else '+'} {ATR_MULT * st['atr']:.0f} 點（約 {approx_stop:.0f}）")
        notify(f"act-{last['date']}-{act}", title, msg, state, 5, ["chart_with_upwards_trend" if act > 0 else "chart_with_downwards_trend"])

    # 2) 盤中：即時價格觸及移動停損
    if lb and st["pos"] and st["stop"]:
        hit = (st["pos"] > 0 and lb["l"] <= st["stop"]) or (st["pos"] < 0 and lb["h"] >= st["stop"])
        if hit:
            notify(f"stop-{last['date']}-{st['pos']}-{int(st['stop'])}", f"⛔ 小台 {SIDE[st['pos']]}單觸及移動停損",
                   f"停損價 {st['stop']:.0f}，目前 {lb['c']:.0f}（{lb['label']}）\n依規則出場；若均線方向不變，下一個交易時段開盤再進場",
                   state, 5, ["no_entry"])

    json.dump(state, open(STATE_PATH, "w", encoding="utf-8"), ensure_ascii=False)

    # 3) 網頁資料
    os.makedirs(SITE_DIR, exist_ok=True)
    n = 260; s = max(0, len(B) - n)
    r1 = lambda v: None if v is None else round(v, 1)
    markers = []
    for d, kind, side in st["events"]:
        if d >= B[s]["date"]:
            markers.append(dict(date=d, kind=kind, side=side))
    page = dict(
        updated=t.strftime("%Y-%m-%d %H:%M"), log=log,
        bars=[dict(d=B[k]["date"], o=B[k]["open"], h=B[k]["high"], l=B[k]["low"], c=B[k]["close"],
                   ma20=r1(ind["ma20"][k]), ma120=r1(ind["ma120"][k]), dif=r1(ind["dif"][k]), dea=r1(ind["dea"][k]),
                   hist=r1(ind["hist"][k]), K=r1(ind["K"][k]), D=r1(ind["D"][k])) for k in range(s, len(B))],
        live=lb, live_quotes=live, markers=markers,
        status=dict(pos=st["pos"], side=SIDE[st["pos"]], entry=st["entry"], entry_date=st["entry_date"],
                    stop=r1(st["stop"]), want=st["want"], next=SIDE[st["want"]], atr=r1(st["atr"]),
                    ma20=r1(ma20), ma120=r1(ma120), last_date=last["date"], last_close=last["close"],
                    K=r1(ind["K"][i]), D=r1(ind["D"][i]), hist=r1(ind["hist"][i])),
        trades=[dict(**{k: (round(v) if isinstance(v, float) else v) for k, v in x.items()},
                     pts=round(x["side"] * (x["exit"] - x["entry"]))) for x in st["trades"][-15:]][::-1],
    )
    json.dump(page, open(os.path.join(SITE_DIR, "data.json"), "w", encoding="utf-8"), ensure_ascii=False)
    print("\n".join(log)); print("狀態:", page["status"])


if __name__ == "__main__":
    main()
