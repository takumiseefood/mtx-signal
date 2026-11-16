#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""籌碼與匯率：外資台指期空單、上市融資餘額與維持率（估算）、美元/台幣。寫入 site/data.json 的 macro。
每 30 分鐘才重新抓一次（期交所約 15:00、證交所約 21:00 後公布當日資料）。"""
import datetime as dt
import json
import os
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.abspath(__file__))
SITE = os.path.join(ROOT, "site")
CACHE = os.path.join(SITE, "macro.json")
TZ = dt.timezone(dt.timedelta(hours=8))
UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/130 Safari/537.36"}
num = lambda s: float(str(s).replace(",", "").strip() or 0)


def get(url, data=None, enc="utf-8"):
    req = urllib.request.Request(url, data=urllib.parse.urlencode(data).encode() if data else None, headers=UA)
    return urllib.request.urlopen(req, timeout=30).read().decode(enc, errors="ignore")


def foreign_futures(today):
    """外資及陸資 臺股期貨（大台）未平倉：空方口數、多空淨額，與前一交易日比較"""
    for back in range(0, 6):
        end = today - dt.timedelta(days=back)
        txt = get("https://www.taifex.com.tw/cht/3/futContractsDateDown",
                  dict(queryStartDate=f"{end - dt.timedelta(days=9):%Y/%m/%d}", queryEndDate=f"{end:%Y/%m/%d}", commodityId="TXF"), "big5")
        rows = [r.split(",") for r in txt.splitlines() if r[:2] == "20"]
        rows = [r for r in rows if len(r) >= 15 and r[1].strip() == "臺股期貨" and "外資" in r[2]]
        if len(rows) >= 2:
            rows.sort(key=lambda r: r[0]); a, b = rows[-2], rows[-1]
            return dict(date=b[0], short=int(num(b[11])), short_chg=int(num(b[11]) - num(a[11])),
                        long=int(num(b[9])), net=int(num(b[13])), net_chg=int(num(b[13]) - num(a[13])), prev_date=a[0])
    raise RuntimeError("期交所三大法人資料抓不到")


def margin(today):
    """上市融資餘額（億元）、當日增減、整體維持率估算＝Σ(融資餘額張數×1000×收盤價) ÷ 融資金額"""
    for back in range(0, 7):
        d = today - dt.timedelta(days=back)
        if d.weekday() >= 5: continue
        ds = f"{d:%Y%m%d}"
        j = json.loads(get(f"https://www.twse.com.tw/rwd/zh/marginTrading/MI_MARGN?date={ds}&selectType=ALL&response=json"))
        tb = j.get("tables") or []
        if j.get("stat") != "OK" or len(tb) < 2 or not tb[1].get("data"): continue
        amt = next(r for r in tb[0]["data"] if "融資金額" in r[0])
        prev, now = num(amt[4]) * 1000, num(amt[5]) * 1000          # 元
        out = dict(date=f"{d:%Y/%m/%d}", bal=round(now / 1e8, 1), chg=round((now - prev) / 1e8, 1))
        try:
            p = json.loads(get(f"https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX?date={ds}&type=ALLBUT0999&response=json"))
            px = {}
            for t in p.get("tables") or []:
                f = t.get("fields") or []
                if "證券代號" in f and "收盤價" in f:
                    i, k = f.index("證券代號"), f.index("收盤價")
                    for r in t["data"]:
                        try: px[r[i].strip()] = num(r[k])
                        except ValueError: pass
            mv = sum(num(r[6]) * 1000 * px.get(r[0].strip(), 0) for r in tb[1]["data"])
            if mv and now: out["ratio"] = round(mv / now * 100, 1)
        except Exception as ex:
            out["ratio_err"] = str(ex)
        return out
    raise RuntimeError("證交所融資資料抓不到")


def usdtwd():
    d = json.loads(get("https://ws.api.cnyes.com/ws/api/v1/quote/quotes/FX:USDTWD:FOREX"))["data"][0]
    return dict(px=d["6"], chg=d["11"], time=dt.datetime.fromtimestamp(d["200007"], TZ).strftime("%m/%d %H:%M"))


def main():
    t = dt.datetime.now(TZ)
    m = json.load(open(CACHE, encoding="utf-8")) if os.path.exists(CACHE) else {}
    if not m or (t.timestamp() - m.get("ts", 0)) > 1800:
        m["ts"] = t.timestamp(); m["updated"] = t.strftime("%Y-%m-%d %H:%M"); m["err"] = []
        for key, fn in (("fut", lambda: foreign_futures(t.date())), ("margin", lambda: margin(t.date())), ("fx", usdtwd)):
            try: m[key] = fn()
            except Exception as ex: m["err"].append(f"{key} 失敗：{ex}")      # 失敗時沿用上次的數字
        os.makedirs(SITE, exist_ok=True); json.dump(m, open(CACHE, "w", encoding="utf-8"), ensure_ascii=False)
        print("籌碼更新:", {k: m.get(k) for k in ("fut", "margin", "fx", "err")})
    p = os.path.join(SITE, "data.json")
    if os.path.exists(p):
        page = json.load(open(p, encoding="utf-8")); page["macro"] = m
        json.dump(page, open(p, "w", encoding="utf-8"), ensure_ascii=False)


if __name__ == "__main__":
    main()
