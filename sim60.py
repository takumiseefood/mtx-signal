#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
60 分 K「20/120 均線多空 + 2 倍 ATR」模擬訊號（與日K實單訊號分開，推播標示【60分模擬】）
=====================================================================================
K 棒來源：
  1. 期交所「期貨每筆成交資料」（近 30 個交易日、每日收盤後公布）→ 精確 60 分 K，存 data/mtx_60m.csv
  2. 當天尚未公布的部分 → 用即時行情每 5 分鐘輪詢組成（近似值），存 site/sim60_live.json，
     期交所檔案公布後自動以精確資料取代
K 棒時間：日盤 08:45、09:45 … 12:45；夜盤 15:00、16:00 … 04:00（以開始時間標示）
"""
import csv, io, json, os, sys, zipfile, urllib.request, datetime as dt

TZ = dt.timezone(dt.timedelta(hours=8))
ROOT = os.path.dirname(os.path.abspath(__file__))
CSV60 = os.path.join(ROOT, "data", "mtx_60m.csv")
FILES = os.path.join(ROOT, "data", "mtx_60m_files.json")
STATE_PATH = os.path.join(ROOT, "data", "notified.json")
SITE = os.path.join(ROOT, "site")
LIVE = os.path.join(SITE, "sim60_live.json")
FAST, SLOW, ATR_N, MULT = 20, 120, 14, 2.0
SIM_START = "2026-11-11 00:00"      # 模擬「正式開始」計分時間（之前的僅作歷史重播）
UA = {"User-Agent": "Mozilla/5.0 (mtx-signal)"}
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", ""); PAGE_URL = os.environ.get("PAGE_URL", "")
SIDE = {1: "做多", -1: "做空", 0: "空手"}


def now(): return dt.datetime.now(TZ)


def bar_start(d: dt.datetime):
    """回傳這個時間所屬 60 分 K 的開始時間字串；非交易時段回傳 None"""
    hm = d.hour * 100 + d.minute
    if 845 <= hm < 1345 or hm == 1345:
        mins = (d.hour * 60 + d.minute) - (8 * 60 + 45); k = min(mins // 60, 4)
        s = d.replace(hour=8, minute=45, second=0, microsecond=0) + dt.timedelta(hours=k)
        return s.strftime("%Y-%m-%d %H:%M")
    if hm >= 1500 or hm < 500 or hm == 500:
        h = d.hour if hm != 500 else 4
        return d.replace(hour=h, minute=0, second=0, microsecond=0).strftime("%Y-%m-%d %H:%M")
    return None


# ───────────── 期交所逐筆 → 60 分 K ─────────────
def fetch_tick_day(date: dt.date):
    url = f"https://www.taifex.com.tw/file/taifex/Dailydownload/DailydownloadCSV/Daily_{date:%Y_%m_%d}.zip"
    try:
        raw = urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=60).read()
    except Exception:
        return None
    if raw[:2] != b"PK": return None
    z = zipfile.ZipFile(io.BytesIO(raw)); txt = z.read(z.namelist()[0]).decode("big5", errors="ignore")
    ticks = {}; vol = {}
    for line in txt.splitlines()[1:]:
        a = [x.strip() for x in line.split(",")]
        if len(a) < 6 or a[1] != "MTX" or not (a[2].isdigit() and len(a[2]) == 6): continue
        try: px = float(a[4]); q = float(a[5])
        except ValueError: continue
        ticks.setdefault(a[2], []).append((a[0], a[3].zfill(6), px)); vol[a[2]] = vol.get(a[2], 0) + q
    if not vol: return None
    m = max(vol, key=vol.get); bars = {}
    for d8, t6, px in ticks[m]:
        x = dt.datetime.strptime(d8 + t6, "%Y%m%d%H%M%S").replace(tzinfo=TZ); k = bar_start(x)
        if not k: continue
        b = bars.get(k)
        if b is None: bars[k] = dict(t=k, o=px, h=px, l=px, c=px)
        else: b["h"] = max(b["h"], px); b["l"] = min(b["l"], px); b["c"] = px
    return bars


def load60():
    if not os.path.exists(CSV60): return {}
    with open(CSV60, encoding="utf-8") as f:
        return {r["t"]: dict(t=r["t"], o=float(r["o"]), h=float(r["h"]), l=float(r["l"]), c=float(r["c"])) for r in csv.DictReader(f)}


def save60(bars):
    with open(CSV60, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f); w.writerow(["t", "o", "h", "l", "c"])
        for k in sorted(bars): b = bars[k]; w.writerow([k, int(b["o"]), int(b["h"]), int(b["l"]), int(b["c"])])


def update_ticks(bars, log):
    done = set(json.load(open(FILES))) if os.path.exists(FILES) else set()
    t = now(); changed = False; got = 0
    days = 45 if not done else 7
    for i in range(days, -1, -1):
        d = (t - dt.timedelta(days=i)).date()
        if d.weekday() >= 5 or d.isoformat() in done: continue
        if d == t.date() and t.hour * 100 + t.minute < 1400: continue   # 今天的檔要等收盤後
        nb = fetch_tick_day(d)
        if nb is None: continue
        bars.update(nb); done.add(d.isoformat()); changed = True; got += 1
    if changed:
        save60(bars); json.dump(sorted(done)[-60:], open(FILES, "w"))
    log.append(f"60分逐筆檔 +{got} 天（共 {len(bars)} 根）")
    return bars


# ───────────── 即時輪詢組成近似 K 棒 ─────────────
def fetch_quote(market):
    body = json.dumps(dict(MarketType=market, SymbolType="F", KindID="1", CID="MXF", ExpireMonth="",
                           RowSize="全部", PageNo="", SortColumn="", AscDesc="A")).encode()
    req = urllib.request.Request("https://mis.taifex.com.tw/futures/api/getQuoteList", data=body,
                                 headers={**UA, "Content-Type": "application/json"})
    j = json.loads(urllib.request.urlopen(req, timeout=20).read().decode())
    best = None
    for q in (j.get("RtData") or {}).get("QuoteList") or []:
        sid = q.get("SymbolID", "")
        if sid.startswith("MXF") and not sid.startswith("MXF-") and q.get("CLastPrice"):
            v = float(q.get("CTotalVolume") or 0)
            if best is None or v > best[0]: best = (v, q)
    return best[1] if best else None


def update_live(bars, log):
    st = json.load(open(LIVE)) if os.path.exists(LIVE) else {"bars": {}, "sess": {}}
    t = now(); k = bar_start(t)
    # 已有精確資料的近似K棒丟掉
    st["bars"] = {kk: b for kk, b in st["bars"].items() if kk not in bars}
    if k:
        hm = t.hour * 100 + t.minute
        q = fetch_quote("0" if 845 <= hm <= 1345 else "1")
        if q:
            px = float(q["CLastPrice"]); sh = float(q["CHighPrice"]); sl = float(q["CLowPrice"])
            sess_key = q["CDate"] + ("D" if 845 <= hm <= 1345 else "N")
            prev = st["sess"].get(sess_key)
            b = st["bars"].get(k)
            if b is None:
                b = st["bars"][k] = dict(t=k, o=px, h=px, l=px, c=px)
                if prev is None and k[11:] in ("08:45", "15:00") and q.get("COpenPrice"):
                    b["o"] = float(q["COpenPrice"])    # 本時段第一根：用時段開盤價
            b["h"] = max(b["h"], px); b["l"] = min(b["l"], px); b["c"] = px
            if prev:   # 兩次輪詢之間創的新高/新低，一定落在這根K棒
                if sh > prev["h"]: b["h"] = max(b["h"], sh)
                if sl < prev["l"]: b["l"] = min(b["l"], sl)
            st["sess"] = {sess_key: dict(h=sh, l=sl)}
            log.append(f"60分即時 {k} 近似K棒 {b['c']:.0f}")
    os.makedirs(SITE, exist_ok=True); json.dump(st, open(LIVE, "w"))
    return st["bars"], k


# ───────────── 指標與策略重播 ─────────────
def ema(x, a):
    out, p = [], None
    for v in x: p = v if p is None else p + a * (v - p); out.append(p)
    return out


def sma(x, n):
    out, s = [], 0.0
    for i, v in enumerate(x):
        s += v
        if i >= n: s -= x[i - n]
        out.append(s / n if i >= n - 1 else None)
    return out


def replay(B):
    C = [b["c"] for b in B]; a, s = sma(C, FAST), sma(C, SLOW)
    tr = [B[0]["h"] - B[0]["l"]] + [max(B[i]["h"] - B[i]["l"], abs(B[i]["h"] - C[i - 1]), abs(B[i]["l"] - C[i - 1])) for i in range(1, len(B))]
    atr = ema(tr, 1 / ATR_N)
    dif = [x - y for x, y in zip(ema(C, 2 / 13), ema(C, 2 / 27))]; dea = ema(dif, 0.2)
    K, D = [], []; kk = dd = 50.0                      # KD(9,3,3)
    for i, b in enumerate(B):
        w = B[max(0, i - 8):i + 1]; hh = max(x["h"] for x in w); ll = min(x["l"] for x in w)
        rsv = 50.0 if hh == ll else (b["c"] - ll) / (hh - ll) * 100
        kk = kk * 2 / 3 + rsv / 3; dd = dd * 2 / 3 + kk / 3; K.append(kk); D.append(dd)
    pos = 0; e = ext = 0.0; want = 0; trades = []; ev = []; ent = None
    start = next((i for i in range(len(B)) if s[i] is not None), len(B))
    for i in range(start, len(B)):
        b = B[i]
        if want != pos:
            if pos:
                k = "exit" if pos * (b["o"] - e) > 0 else "stop"
                trades.append(dict(entry_t=ent, exit_t=b["t"], side=pos, entry=e, exit=b["o"], why=("出場" if k == "exit" else "停損") + "*")); ev.append((b["t"], k, pos))
            if want: pos, e, ext, ent = want, b["o"], b["o"], b["t"]; ev.append((b["t"], "entry", pos))
        if pos:
            stop = ext - pos * MULT * atr[i - 1]
            if (pos > 0 and b["l"] <= stop) or (pos < 0 and b["h"] >= stop):
                px = min(b["o"], stop) if pos > 0 else max(b["o"], stop); k = "exit" if pos * (px - e) > 0 else "stop"
                trades.append(dict(entry_t=ent, exit_t=b["t"], side=pos, entry=e, exit=px, why="出場" if k == "exit" else "停損")); ev.append((b["t"], k, pos))
                pos = 0
        if pos: ext = max(ext, b["h"]) if pos > 0 else min(ext, b["l"])
        want = 1 if a[i] > s[i] else -1
    L = len(B) - 1
    return dict(pos=pos, entry=e if pos else None, entry_t=ent if pos else None,
                stop=(ext - pos * MULT * atr[L]) if pos else None, want=want, atr=atr[L],
                ma20=a, ma120=s, dif=dif, dea=dea, hist=[x - y for x, y in zip(dif, dea)], K=K, D=D, trades=trades, events=ev)


def notify(key, title, msg, state, prio=3, tags=None):
    if key in state["sent"]: return
    if NTFY_TOPIC:
        body = json.dumps(dict(topic=NTFY_TOPIC, title=title, message=msg, priority=prio, tags=tags or [], click=PAGE_URL or None)).encode()
        urllib.request.urlopen(urllib.request.Request("https://ntfy.sh/", data=body, headers={**UA, "Content-Type": "application/json"}), timeout=20).read()
    print("推播:", title, "|", msg); state["sent"] = (state["sent"] + [key])[-300:]


def main():
    log = []; t = now()
    bars = load60()
    try: bars = update_ticks(bars, log)
    except Exception as ex: log.append(f"60分逐筆檔失敗：{ex}")
    try: live, cur = update_live(bars, log)
    except Exception as ex: live, cur = {}, None; log.append(f"60分即時失敗：{ex}")
    allb = dict(bars); allb.update({k: v for k, v in live.items() if k not in bars})
    keys = sorted(allb)
    if len(keys) < SLOW + 5:
        log.append("60分K棒不足，暫不計算"); print(log); return
    # 已收盤的K棒（目前形成中的那根不算入訊號）
    closed = [allb[k] for k in keys if k != cur]
    forming = allb.get(cur) if cur else None
    st = replay(closed)
    state = json.load(open(STATE_PATH)) if os.path.exists(STATE_PATH) else {"sent": []}
    last = closed[-1]; G = lambda p, price, entry: "停利出場價" if p * (price - entry) > 0 else "停損價"
    last_end = dt.datetime.strptime(last["t"], "%Y-%m-%d %H:%M").replace(tzinfo=TZ) + dt.timedelta(hours=1)
    fresh = (t - last_end) <= dt.timedelta(hours=3)          # 資料太舊就不推播，避免誤報
    if not fresh: log.append(f"60分最新K棒 {last['t']} 過舊，本次不推播")
    if fresh and st["want"] != st["pos"]:
        act = st["want"]; ref = forming["c"] if forming else last["c"]; dist = MULT * st["atr"]
        old = (f"{SIDE[st['pos']]}單{'出場（停利）' if st['pos'] * (ref - st['entry']) > 0 else '停損'}平倉，並" if st["pos"] else "")
        notify(f"s60-act-{last['t']}-{act}", f"🧪【60分模擬】{'反手' if st['pos'] else ''}{SIDE[act]}",
               f"{last['t']} 收盤 {last['c']:.0f}｜60分 20MA {'>' if act > 0 else '<'} 120MA\n▶ 模擬：下一根K棒開盤 {old}{'買進' if act > 0 else '賣出'}（目前約 {ref:.0f}）\n"
               f"⛔ 停損＝成交價 {'−' if act > 0 else '+'} {dist:.0f} 點（約 {ref - act * dist:.0f}）\n※ 模擬訊號，僅供觀察，與日K實單訊號分開",
               state, 3, ["test_tube"])
    if forming and st["pos"] and st["stop"]:
        if (st["pos"] > 0 and forming["l"] <= st["stop"]) or (st["pos"] < 0 and forming["h"] >= st["stop"]):
            g = G(st["pos"], st["stop"], st["entry"])
            notify(f"s60-stop-{cur}-{int(st['stop'])}", f"🧪【60分模擬】{SIDE[st['pos']]}單{'停利出場' if g == '停利出場價' else '停損'}",
                   f"{g} {st['stop']:.0f}，進場 {st['entry']:.0f}，約 {st['pos'] * (st['stop'] - st['entry']):+.0f} 點\n※ 模擬訊號", state, 3, ["test_tube"])
    json.dump(state, open(STATE_PATH, "w", encoding="utf-8"), ensure_ascii=False)

    # 模擬績效（自 SIM_START 起，含未平倉）
    cost = lambda px: 2 * (50 + px * 50 * 0.00002) / 50 + 4
    real = [x for x in st["trades"] if x["entry_t"] >= SIM_START]
    pts = sum(x["side"] * (x["exit"] - x["entry"]) - cost(x["exit"]) for x in real)
    if st["pos"] and st["entry_t"] >= SIM_START:
        pts += st["pos"] * ((forming or last)["c"] - st["entry"])
    hist_pts = sum(x["side"] * (x["exit"] - x["entry"]) - cost(x["exit"]) for x in st["trades"])
    n = 180; s0 = max(0, len(closed) - n); r1 = lambda v: None if v is None else round(v, 1)
    page_path = os.path.join(SITE, "data.json")
    page = json.load(open(page_path, encoding="utf-8")) if os.path.exists(page_path) else {}
    page["sim60"] = dict(
        updated=t.strftime("%Y-%m-%d %H:%M"), log=log, sim_start=SIM_START,
        bars=[dict(t=closed[i]["t"], o=closed[i]["o"], h=closed[i]["h"], l=closed[i]["l"], c=closed[i]["c"],
                   ma20=r1(st["ma20"][i]), ma120=r1(st["ma120"][i]), dif=r1(st["dif"][i]), dea=r1(st["dea"][i]),
                   hist=r1(st["hist"][i]), K=r1(st["K"][i]), D=r1(st["D"][i])) for i in range(s0, len(closed))],
        forming=forming, markers=[dict(t=x, kind=k, side=sd) for x, k, sd in st["events"] if x >= closed[s0]["t"]],
        status=dict(pos=st["pos"], side=SIDE[st["pos"]], entry=st["entry"], entry_t=st["entry_t"], stop=r1(st["stop"]),
                    want=st["want"], next=SIDE[st["want"]], atr=r1(st["atr"]), last_t=last["t"], last_c=last["c"],
                    ma20=r1(st["ma20"][-1]), ma120=r1(st["ma120"][-1]),
                    sim_pts=round(pts), sim_trades=len(real), hist_pts=round(hist_pts), hist_trades=len(st["trades"]),
                    hist_from=closed[0]["t"]),
        trades=[dict(entry_t=x["entry_t"], exit_t=x["exit_t"], side=x["side"], entry=round(x["entry"]), exit=round(x["exit"]),
                     why=x["why"], pts=round(x["side"] * (x["exit"] - x["entry"]))) for x in st["trades"][-12:]][::-1])
    json.dump(page, open(page_path, "w", encoding="utf-8"), ensure_ascii=False)
    print("\n".join(log)); print("60分模擬狀態:", {k: v for k, v in page["sim60"]["status"].items()})


if __name__ == "__main__":
    main()
