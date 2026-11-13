#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
60 分 20/120 定方向＋KD 挑進場點 模擬（推播標示【60分＋KD模擬】，與日K實單分開）
================================================================================
- 方向：最近一根已收盤 60 分K 的 20MA > 120MA 偏多、< 偏空
- 進場（偏多）：60 分 KD 的 K < 30 且上彎（準備進場），同時 5 分 KD 在 50 以下黃金交叉 → 下一根 5 分K 開盤做多
  偏空相反：60 分 K > 70 且下彎，同時 5 分 KD 在 50 以上死亡交叉 → 做空
- 出場：2 倍 ATR(60 分) 移動停損，或 60 分均線反轉（下一根 5 分K 開盤平倉）
- 停損後不立即再進場，等下一次 KD 條件成立
"""
import bisect
import datetime as dt
import json
import os

import signal as S
import sim60 as M

SIM_START = "2026-11-11 12:00"
MULT = 2.0
K60_L, K60_S = 30, 70   # 60分K 門檻：做多要低於 30 且上彎；做空要高於 70 且下彎
ENTRY = "both"        # 進場時機："both"＝60分KD<50上彎＋5分KD<50黃金交叉（現行）；"5m50"＝只看5分；"60kd"＝60分KD＋5分交叉（不限50）
ADJ_MIN = 50          # 防守價變動達 50 點才記一筆調整紀錄
SIDE = {1: "做多", -1: "做空", 0: "空手"}
TAG = "🎯【60分＋KD】"


def kd(B):
    K, D = [], []; k = d = 50.0
    for i, b in enumerate(B):
        w = B[max(0, i - 8):i + 1]; hh = max(x["h"] for x in w); ll = min(x["l"] for x in w)
        rsv = 50.0 if hh == ll else (b["c"] - ll) / (hh - ll) * 100
        k = k * 2 / 3 + rsv / 3; d = d * 2 / 3 + k / 3; K.append(k); D.append(d)
    return K, D


def ts(k): return dt.datetime.strptime(k, "%Y-%m-%d %H:%M").replace(tzinfo=M.TZ)


def cost(px): return 2 * (50 + px * 50 * 0.00002) / 50 + 4


def replay(F, H):
    """F＝已收盤 5 分K、H＝已收盤 60 分K"""
    hk = [b["t"] for b in H]
    ind = S.indicators([dict(date=b["t"], open=b["o"], high=b["h"], low=b["l"], close=b["c"]) for b in H])
    K60, _ = kd(H); K5, D5 = kd(F)

    def c60(i):     # 5 分K i 收盤當下，最近一根已收盤 60 分K 的索引
        s = ts(F[i]["t"]); k = M.bar_start(s)
        if k is None: return -1
        j = bisect.bisect_left(hk, k)
        done = s + dt.timedelta(minutes=5) >= ts(k) + dt.timedelta(hours=1)
        return j if (done and j < len(hk) and hk[j] == k) else j - 1

    pos = 0; e = ext = 0.0; act = 0; ent = None; trades = []; ev = []; adj = []; rec = None   # adj＝目前部位的防守價調整紀錄
    for i in range(1, len(F)):
        b = F[i]
        if act != pos and (act or pos):
            if pos:
                k = "exit" if pos * (b["o"] - e) > 0 else "stop"
                trades.append(dict(entry_t=ent, exit_t=b["t"], side=pos, entry=e, exit=b["o"],
                                   why=("出場" if k == "exit" else "停損") + "（均線反轉）")); ev.append((b["t"], k, pos)); pos = 0
            if act: pos, e, ext, ent = act, b["o"], b["o"], b["t"]; ev.append((b["t"], "entry", pos)); adj = []; rec = None
        c = c60(i - 1)
        if pos and c >= 0 and ind["atr"][c]:
            st = ext - pos * MULT * ind["atr"][c]
            if (pos > 0 and b["l"] <= st) or (pos < 0 and b["h"] >= st):
                px = min(b["o"], st) if pos > 0 else max(b["o"], st)
                k = "exit" if pos * (px - e) > 0 else "stop"
                trades.append(dict(entry_t=ent, exit_t=b["t"], side=pos, entry=e, exit=px, why="出場" if k == "exit" else "停損"))
                ev.append((b["t"], k, pos)); pos = 0
        if pos: ext = max(ext, b["h"]) if pos > 0 else min(ext, b["l"])
        c = c60(i); act = pos
        if pos and c >= 0 and ind["atr"][c]:
            cur = ext - pos * MULT * ind["atr"][c]
            if rec is None: rec = cur; adj.append(dict(t=b["t"], frm=None, to=cur))
            elif abs(cur - rec) >= ADJ_MIN: adj.append(dict(t=b["t"], frm=rec, to=cur)); rec = cur
        a20, a120 = (ind["ma20"][c], ind["ma120"][c]) if c >= 1 else (None, None)
        tr = 0 if a20 is None or a120 is None else (1 if a20 > a120 else -1)
        if pos and tr != pos: act = 0
        if pos == 0 and tr:
            gx = K5[i] > D5[i] and K5[i - 1] <= D5[i - 1]; dx = K5[i] < D5[i] and K5[i - 1] >= D5[i - 1]
            if ENTRY == "5m50":        # 5 分 KD 在 50 以下黃金交叉做多；50 以上死亡交叉做空
                if tr > 0 and gx and K5[i] < 50: act = 1
                if tr < 0 and dx and K5[i] > 50: act = -1
            elif ENTRY == "both":
                if tr > 0 and K60[c] < K60_L and K60[c] > K60[c - 1] and gx and K5[i] < 50: act = 1
                if tr < 0 and K60[c] > K60_S and K60[c] < K60[c - 1] and dx and K5[i] > 50: act = -1
            else:
                if tr > 0 and K60[c] < 50 and K60[c] > K60[c - 1] and gx: act = 1
                if tr < 0 and K60[c] > 50 and K60[c] < K60[c - 1] and dx: act = -1
    c = c60(len(F) - 1)
    atr = ind["atr"][c]
    return dict(pos=pos, entry=e if pos else None, entry_t=ent if pos else None,
                stop=(ext - pos * MULT * atr) if pos else None, act=act, trend=tr, atr=atr,
                k60=K60[c], k60p=K60[c - 1], k60pp=K60[c - 2], h60=H[c]["t"], ma20=ind["ma20"][c], ma120=ind["ma120"][c],
                adj=adj if pos else [], K5=K5, D5=D5, K60=K60, ind=ind, trades=trades, events=ev)


def main():
    t = M.now(); log = []
    live = json.load(open(M.LIVE)) if os.path.exists(M.LIVE) else {}
    # 60 分K：逐筆檔＋本時段 1 分K，只取已收盤
    h = M.load60(); h.update({k: v for k, v in (live.get("bars") or {}).items() if k not in h})
    H = [h[k] for k in sorted(h) if ts(k) + dt.timedelta(hours=1) <= t]
    f = M.load5(); f.update({k: v for k, v in (live.get("bars5") or {}).items() if k not in f})
    keys = sorted(f)
    F = [f[k] for k in keys if ts(k) + dt.timedelta(minutes=5) <= t]
    forming = next((f[k] for k in reversed(keys) if ts(k) + dt.timedelta(minutes=5) > t), None)
    if len(H) < 130 or len(F) < 50:
        print("K棒不足"); return
    st = replay(F, H)
    last = F[-1]
    fresh = t - (ts(last["t"]) + dt.timedelta(minutes=5)) <= dt.timedelta(minutes=15)
    state = json.load(open(M.STATE_PATH)) if os.path.exists(M.STATE_PATH) else {"sent": []}
    G = lambda p, price, entry: "停利出場價" if p * (price - entry) > 0 else "停損價"
    # 60分K 條件剛成立（上一根還不成立）→「準備進場」提醒
    def ok60(tr, k, kp): return (tr > 0 and k < K60_L and k > kp) or (tr < 0 and k > K60_S and k < kp)
    tr = st["trend"]
    if fresh and not st["pos"] and tr and ok60(tr, st["k60"], st["k60p"]) and not ok60(tr, st["k60p"], st["k60pp"]):
        L = tr > 0
        M.notify(f"kd-ready-{st['h60']}", f"🟡【60分＋KD】準備{SIDE[tr]}：60分K條件成立",
                 f"{st['h60'][5:]} 這根60分K收盤：K {st['k60']:.0f}（{'<30 上彎' if L else '>70 下彎'}），60分均線{'偏多' if L else '偏空'}\n"
                 f"接下來等 5分KD 在50以{'下黃金' if L else '上死亡'}交叉才進場（目前 5分K {st['K5'][-1]:.0f}、D {st['D5'][-1]:.0f}）\n"
                 f"※ 還不是進場訊號；條件可能在下一根60分K失效", state, 3, ["hourglass_flowing_sand"])
    if fresh and st["act"] != st["pos"]:
        a, p = st["act"], st["pos"]; ref = last["c"]
        if a:
            dist = MULT * st["atr"]
            M.notify(f"kd-act-{last['t']}-{a}", f"{TAG}{SIDE[a]}進場",
                     f"{last['t'][5:]} 5分K收盤 {ref:.0f}\n方向：60分 20MA {'>' if a > 0 else '<'} 120MA\n"
                     f"時機：60分K {st['k60']:.0f}（{'<30 上彎' if a > 0 else '>70 下彎'}）＋5分KD 在50以{'下黃金' if a > 0 else '上死亡'}交叉（K {st['K5'][-1]:.0f}）\n"
                     f"▶ 模擬：下一根5分K開盤{'買進' if a > 0 else '賣出'}\n⛔ 停損＝成交價 {'−' if a > 0 else '+'} {dist:.0f} 點（約 {ref - a * dist:.0f}）\n"
                     f"※ 策略訊號，僅供參考", state, 4, ["dart"])
        else:
            g = "出場（停利）" if p * (ref - st["entry"]) > 0 else "停損"
            M.notify(f"kd-flat-{last['t']}", f"{TAG}{SIDE[p]}單{g}",
                     f"60分均線反轉（20MA {'<' if p > 0 else '>'} 120MA）\n▶ 模擬：下一根5分K開盤平倉（目前約 {ref:.0f}，進場 {st['entry']:.0f}）\n※ 策略訊號，僅供參考",
                     state, 4, ["dart"])
    if fresh and st["events"] and st["events"][-1][0] == last["t"] and st["events"][-1][1] in ("stop", "exit") and not st["pos"]:
        x = st["trades"][-1]
        if x["exit_t"] == last["t"] and "均線" not in x["why"]:
            M.notify(f"kd-stop-{x['exit_t']}", f"{TAG}{SIDE[x['side']]}單{'停利出場' if x['why'] == '出場' else '停損'}",
                     f"觸及 {x['exit']:.0f}，進場 {x['entry']:.0f}，約 {x['side'] * (x['exit'] - x['entry']):+.0f} 點\n之後等下一次 KD 條件再進場\n※ 策略訊號，僅供參考",
                     state, 4, ["dart"])
    if forming and st["pos"] and st["stop"]:
        if (st["pos"] > 0 and forming["l"] <= st["stop"]) or (st["pos"] < 0 and forming["h"] >= st["stop"]):
            g = G(st["pos"], st["stop"], st["entry"])
            M.notify(f"kd-stop-{forming['t']}", f"{TAG}{SIDE[st['pos']]}單{'停利出場' if g == '停利出場價' else '停損'}",
                     f"{g} {st['stop']:.0f}，進場 {st['entry']:.0f}，約 {st['pos'] * (st['stop'] - st['entry']):+.0f} 點\n之後等下一次 KD 條件再進場\n※ 策略訊號，僅供參考",
                     state, 4, ["dart"])
    json.dump(state, open(M.STATE_PATH, "w", encoding="utf-8"), ensure_ascii=False)

    real = [x for x in st["trades"] if x["entry_t"] >= SIM_START]
    pts = sum(x["side"] * (x["exit"] - x["entry"]) - cost(x["entry"]) for x in real)
    if st["pos"] and st["entry_t"] >= SIM_START: pts += st["pos"] * ((forming or last)["c"] - st["entry"])
    hist = sum(x["side"] * (x["exit"] - x["entry"]) - cost(x["entry"]) for x in st["trades"])
    r1 = lambda v: None if v is None else round(v, 1)
    n5 = 180; s5 = len(F) - n5
    n6 = 120; s6 = len(H) - n6
    if st["act"] != st["pos"]:
        nxt = (f"{SIDE[st['act']]}進場" if st["act"] else "平倉") + "（下一根5分K開盤）"
    elif st["pos"]:
        nxt = f"續抱{SIDE[st['pos']]}"
    elif st["trend"] > 0:
        nxt = "等做多時機：60分K<30上彎＋5分KD在50以下黃金交叉"
    elif st["trend"] < 0:
        nxt = "等做空時機：60分K>70下彎＋5分KD在50以上死亡交叉"
    else:
        nxt = "觀望"
    page_path = os.path.join(M.SITE, "data.json")
    page = json.load(open(page_path, encoding="utf-8")) if os.path.exists(page_path) else {}
    page["simkd"] = dict(
        updated=t.strftime("%Y-%m-%d %H:%M"), sim_start=SIM_START,
        bars5=[dict(t=F[i]["t"], o=F[i]["o"], h=F[i]["h"], l=F[i]["l"], c=F[i]["c"], K=r1(st["K5"][i]), D=r1(st["D5"][i])) for i in range(s5, len(F))],
        forming=forming,
        k60=[dict(t=H[i]["t"], K=r1(st["K60"][i])) for i in range(s6, len(H))],
        markers=[dict(t=x, kind=k, side=sd) for x, k, sd in st["events"] if x >= F[s5]["t"]],
        markers60=[dict(t=M.bar_start(ts(x)), kind=k, side=sd) for x, k, sd in st["events"] if x >= H[max(0, len(H) - 180)]["t"]],
        status=dict(pos=st["pos"], entry=st["entry"], entry_t=st["entry_t"], stop=r1(st["stop"]), act=st["act"], next=nxt,
                    trend=st["trend"], ma20=r1(st["ma20"]), ma120=r1(st["ma120"]), atr=r1(st["atr"]),
                    k60=r1(st["k60"]), k60p=r1(st["k60p"]), h60=st["h60"], k5=r1(st["K5"][-1]), d5=r1(st["D5"][-1]),
                    last_t=last["t"], last_c=last["c"], sim_pts=round(pts), sim_trades=len(real),
                    hist_pts=round(hist), hist_trades=len(st["trades"]), hist_from=F[0]["t"]),
        open=(dict(entry_t=st["entry_t"], side=st["pos"], entry=round(st["entry"]), last=round((forming or last)["c"]),
                   adj=[dict(t=x["t"], frm=None if x["frm"] is None else round(x["frm"]), to=round(x["to"]),
                             lock=round(st["pos"] * (x["to"] - st["entry"]))) for x in st["adj"][-10:]][::-1]) if st["pos"] else None),
        trades=[dict(entry_t=x["entry_t"], exit_t=x["exit_t"], side=x["side"], entry=round(x["entry"]), exit=round(x["exit"]),
                     why=x["why"], pts=round(x["side"] * (x["exit"] - x["entry"]))) for x in st["trades"][-12:]][::-1])
    json.dump(page, open(page_path, "w", encoding="utf-8"), ensure_ascii=False)
    print("60分＋KD模擬:", {k: v for k, v in page["simkd"]["status"].items()})


if __name__ == "__main__":
    main()
