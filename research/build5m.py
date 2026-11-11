"""研究用：下載期交所近 30 個交易日逐筆成交，組成小台近月 5 分K（含夜盤）"""
import csv, datetime as dt, io, sys, urllib.request, zipfile
sys.path.insert(0, ".")
from sim60 import TZ, UA

def b5(x):
    hm = x.hour * 100 + x.minute
    if hm == 1345: x -= dt.timedelta(minutes=5)
    elif hm == 500: x -= dt.timedelta(minutes=5)
    elif not (845 <= hm < 1345 or hm >= 1500 or hm < 500): return None
    return x.replace(minute=x.minute - x.minute % 5, second=0).strftime("%Y-%m-%d %H:%M")

def day(date):
    url = f"https://www.taifex.com.tw/file/taifex/Dailydownload/DailydownloadCSV/Daily_{date:%Y_%m_%d}.zip"
    try: raw = urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=60).read()
    except Exception: return {}
    if raw[:2] != b"PK": return {}
    txt = zipfile.ZipFile(io.BytesIO(raw)).read(zipfile.ZipFile(io.BytesIO(raw)).namelist()[0]).decode("big5", errors="ignore")
    ticks, vol = {}, {}
    for line in txt.splitlines()[1:]:
        a = [x.strip() for x in line.split(",")]
        if len(a) < 6 or a[1] != "MTX" or not (a[2].isdigit() and len(a[2]) == 6): continue
        try: px = float(a[4]); q = float(a[5])
        except ValueError: continue
        ticks.setdefault(a[2], []).append((a[0] + a[3].zfill(6), px)); vol[a[2]] = vol.get(a[2], 0) + q
    if not vol: return {}
    out = {}
    for ts, px in sorted(ticks[max(vol, key=vol.get)], key=lambda z: z[0]):
        k = b5(dt.datetime.strptime(ts, "%Y%m%d%H%M%S"))
        if not k: continue
        b = out.get(k)
        if b is None: out[k] = [k, px, px, px, px]
        else: b[2] = max(b[2], px); b[3] = min(b[3], px); b[4] = px
    print(date, len(out), flush=True)
    return out

bars = {}
today = dt.datetime.now(TZ).date()
for i in range(50, -1, -1):
    d = today - dt.timedelta(days=i)
    if d.weekday() < 5: bars.update(day(d))
with open("mtx_5m.csv", "w", newline="") as f:
    w = csv.writer(f); w.writerow(["t", "o", "h", "l", "c"])
    for k in sorted(bars): w.writerow(bars[k])
print("total", len(bars))
