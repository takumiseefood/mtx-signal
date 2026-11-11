"""兩次完整計算之間，每 10 秒抓一次期交所即時價。
價格觸及日K或60分模擬的停損／停利出場價時立刻結束，讓 loop.sh 馬上重算並推播。
用法：python watch.py <最多等待秒數>
"""
import datetime as dt
import json
import os
import sys
import time

from sim60 import fetch_quote, TZ

ROOT = os.path.dirname(os.path.abspath(__file__))
DONE = "/tmp/watch_done.json"     # 已觸發過的價位，避免同一價位一直重算
INTERVAL = 10


def levels():
    out = []
    try:
        d = json.load(open(os.path.join(ROOT, "site", "data.json"), encoding="utf-8"))
    except Exception:
        return out
    s = (d.get("simkd") or {}).get("status") or {}
    if s.get("pos") and s.get("stop"): out.append(("KD", s["pos"], s["stop"]))
    return out


def session(t):
    hm = t.hour * 100 + t.minute
    if 845 <= hm <= 1345: return "0"
    if hm >= 1500 or hm <= 500: return "1"
    return None


def main():
    end = time.time() + float(sys.argv[1] if len(sys.argv) > 1 else 300)
    try: done = set(json.load(open(DONE)))
    except Exception: done = set()
    lv = levels(); fails = 0; prev = None
    while time.time() < end:
        t = dt.datetime.now(TZ); m = session(t)
        if m and lv and t.weekday() < 6:
            try:
                q = fetch_quote(m); fails = 0
                if q:
                    px = float(q["CLastPrice"]); sh = float(q["CHighPrice"]); sl = float(q["CLowPrice"])
                    # 只採計「上次輪詢之後」才創的新高／新低，避免進場前的高低點誤判
                    hi = sh if (prev and prev[0] == q["CDate"] + m and sh > prev[1]) else px
                    lo = sl if (prev and prev[0] == q["CDate"] + m and sl < prev[2]) else px
                    prev = (q["CDate"] + m, sh, sl)
                    for name, pos, stop in lv:
                        key = f"{name}:{pos}:{stop}"
                        if key in done: continue
                        if (pos > 0 and lo <= stop) or (pos < 0 and hi >= stop):
                            done.add(key); json.dump(sorted(done)[-50:], open(DONE, "w"))
                            print(f"⚡ {t:%H:%M:%S} {name} 觸價 {stop:.0f}（目前 {px:.0f}），立即重算")
                            return
            except Exception as ex:
                fails += 1
                if fails in (1, 30): print("即時價失敗：", ex)
        time.sleep(max(0, min(INTERVAL, end - time.time())))


if __name__ == "__main__":
    main()
