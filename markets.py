"""
每天美股收盘后运行：抓取三地股指、恐慌指数、外围宏观与不确定性指标的日线（含成交量），
计算 1日 / 约7天（5个交易日）/ 约30天（21个交易日）涨跌、成交量对比、均线位置、52周位置，
写入 data/markets.json 供每日简报读取。
"""

import datetime as dt
import json
import math
import os
import time
from io import StringIO

import pandas as pd
import requests

DATA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"}

# (分组, 显示名, Yahoo 代码候选)
# 代码前缀 em: 表示东方财富日线（带成交额，A股/港股用），其余为 Yahoo
SYMBOLS = [
    ("A股", "上证指数", ["qq:sh000001", "em:1.000001", "000001.SS"]),
    ("A股", "深证成指", ["qq:sz399001", "em:0.399001", "399001.SZ"]),
    ("A股", "创业板指", ["qq:sz399006", "em:0.399006"]),
    ("A股", "沪深300", ["qq:sh000300", "em:1.000300"]),
    ("A股", "科创50", ["qq:sh000688", "em:1.000688"]),
    ("A股", "中证1000", ["qq:sh000852", "em:1.000852"]),
    ("港股", "恒生指数", ["qq:hkHSI", "em:100.HSI", "^HSI"]),
    ("港股", "恒生科技", ["qq:hkHSTECH", "em:124.HSTECH"]),
    ("港股", "国企指数", ["qq:hkHSCEI", "em:100.HSCEI", "^HSCE"]),
    ("港股", "恒生医疗保健", ["qq:hkHSHCI", "em:124.HSHCI"]),
    ("美股", "标普500", ["^GSPC"]),
    ("美股", "纳斯达克综合", ["^IXIC"]),
    ("美股", "道琼斯", ["^DJI"]),
    ("美股", "费城半导体", ["^SOX"]),
    ("美股", "纳斯达克生物科技", ["^NBI"]),
    ("美股", "中概股KWEB", ["KWEB"]),
    ("恐慌", "VIX", ["^VIX"]),
    ("恐慌", "VXN", ["^VXN"]),
    ("恐慌", "MOVE美债波动率", ["^MOVE"]),
    ("宏观", "美国10年期国债收益率", ["^TNX"]),
    ("宏观", "美元指数", ["DX-Y.NYB"]),
    ("宏观", "离岸人民币USD/CNH", ["CNH=X", "CNY=X"]),
    ("宏观", "黄金期货", ["GC=F"]),
    ("宏观", "WTI原油期货", ["CL=F"]),
    ("宏观", "比特币", ["BTC-USD"]),
]


def yahoo_daily(sym: str) -> pd.DataFrame:
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}"
    r = requests.get(url, params={"range": "2y", "interval": "1d"}, headers=UA, timeout=30)
    r.raise_for_status()
    res = r.json()["chart"]["result"][0]
    q = res["indicators"]["quote"][0]
    tz = res["meta"].get("exchangeTimezoneName", "UTC")
    idx = pd.to_datetime(res["timestamp"], unit="s", utc=True).tz_convert(tz).date
    df = pd.DataFrame({"close": q["close"], "volume": q.get("volume")}, index=idx)
    df = df[~df.index.duplicated(keep="last")].dropna(subset=["close"])
    return df


def em_daily(secid: str) -> pd.DataFrame:
    """东方财富日K：日期、收盘、成交额（元）。"""
    url = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
    params = {"secid": secid, "klt": "101", "fqt": "1", "lmt": "520", "end": "20500101",
              "fields1": "f1,f2,f3,f4,f5,f6", "fields2": "f51,f52,f53,f54,f55,f56,f57"}
    last = None
    for attempt in range(2):  # 东方财富对海外服务器限流严重，只作备用
        try:
            time.sleep(2 + attempt * 3)
            r = requests.get(url, params=params, headers={**UA, "Connection": "close"}, timeout=30)
            r.raise_for_status()
            kl = (r.json().get("data") or {}).get("klines") or []
            break
        except Exception as e:  # noqa
            last = e
    else:
        raise last
    rows = [k.split(",") for k in kl]
    df = pd.DataFrame(rows, columns=["date", "open", "close", "high", "low", "vol", "amount"])
    df["close"] = pd.to_numeric(df["close"], errors="coerce")
    df["volume"] = pd.to_numeric(df["amount"], errors="coerce")  # 用成交额比较放量缩量
    df.index = pd.to_datetime(df["date"]).dt.date
    return df[["close", "volume"]].dropna(subset=["close"])


def qq_daily(code: str) -> pd.DataFrame:
    """腾讯行情日K：[日期, 开, 收, 高, 低, 成交量, ...]，成交量用于放量缩量比较。"""
    url = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
    r = requests.get(url, params={"param": f"{code},day,,,520,"}, headers=UA, timeout=30)
    r.raise_for_status()
    node = (r.json().get("data") or {}).get(code) or {}
    rows = node.get("day") or node.get("qfqday") or []
    if not rows:
        raise ValueError("腾讯无数据")
    df = pd.DataFrame([x[:6] for x in rows], columns=["date", "open", "close", "high", "low", "vol"])
    df["close"] = pd.to_numeric(df["close"], errors="coerce")
    df["volume"] = pd.to_numeric(df["vol"], errors="coerce")
    df.index = pd.to_datetime(df["date"]).dt.date
    return df[["close", "volume"]].dropna(subset=["close"])


def fetch(sym: str) -> pd.DataFrame:
    if sym.startswith("qq:"):
        return qq_daily(sym[3:])
    if sym.startswith("em:"):
        return em_daily(sym[3:])
    return yahoo_daily(sym)


def pct(a, b):
    if a is None or b is None or b == 0 or any(map(lambda x: isinstance(x, float) and math.isnan(x), [a, b])):
        return None
    return round((a / b - 1) * 100, 2)


def summarize(df: pd.DataFrame) -> dict:
    c = df["close"]
    v = df["volume"] if "volume" in df else pd.Series(dtype=float)
    last = float(c.iloc[-1])

    def back(n):
        return float(c.iloc[-1 - n]) if len(c) > n else None

    w52 = c.iloc[-250:]
    out = {
        "date": str(df.index[-1]),
        "close": round(last, 4),
        "chg_1d_pct": pct(last, back(1)),
        "chg_5d_pct": pct(last, back(5)),
        "chg_21d_pct": pct(last, back(21)),
        "ma20": round(float(c.iloc[-20:].mean()), 4) if len(c) >= 20 else None,
        "ma60": round(float(c.iloc[-60:].mean()), 4) if len(c) >= 60 else None,
        "low_52w": round(float(w52.min()), 4),
        "high_52w": round(float(w52.max()), 4),
        "pos_52w_pct": round((last - w52.min()) / (w52.max() - w52.min()) * 100, 1) if w52.max() > w52.min() else None,
        "is_52w_low": bool(last <= w52.min()),
        "last5": [round(float(x), 4) for x in c.iloc[-5:]],
    }
    vv = pd.to_numeric(v, errors="coerce").fillna(0) if len(v) else pd.Series(dtype=float)
    if len(vv) >= 21 and vv.iloc[-1] > 0 and vv.iloc[-21:-1].mean() > 0:
        out["vol_last"] = float(vv.iloc[-1])
        out["vol_vs_5d_avg"] = round(float(vv.iloc[-1] / vv.iloc[-6:-1].mean()), 2) if vv.iloc[-6:-1].mean() > 0 else None
        out["vol_vs_20d_avg"] = round(float(vv.iloc[-1] / vv.iloc[-21:-1].mean()), 2)
        out["vol_5d_vs_20d"] = round(float(vv.iloc[-5:].mean() / vv.iloc[-25:-5].mean()), 2) if len(vv) >= 25 and vv.iloc[-25:-5].mean() > 0 else None
    return out


def fred_series(sid: str) -> pd.Series:
    url = "https://fred.stlouisfed.org/graph/fredgraph.csv"
    last = None
    for _ in range(3):
        try:
            r = requests.get(url, params={"id": sid}, headers=UA, timeout=120)
            r.raise_for_status()
            break
        except Exception as e:  # noqa
            last = e
            time.sleep(5)
    else:
        raise last
    df = pd.read_csv(StringIO(r.text))
    df.columns = ["date", "value"]
    s = pd.to_numeric(df["value"], errors="coerce")
    s.index = pd.to_datetime(df["date"]).dt.date
    return s.dropna()


def epu_official() -> pd.Series:
    url = "https://www.policyuncertainty.com/media/All_Daily_Policy_Data.csv"
    r = requests.get(url, headers=UA, timeout=60)
    r.raise_for_status()
    df = pd.read_csv(StringIO(r.text))
    df = df.dropna(subset=["day", "month", "year"])
    idx = pd.to_datetime(dict(year=df.year.astype(int), month=df.month.astype(int), day=df.day.astype(int))).dt.date
    col = [c for c in df.columns if "index" in c.lower() or "policy" in c.lower()][0]
    s = pd.to_numeric(df[col], errors="coerce")
    s.index = idx
    return s.dropna()


def main():
    os.makedirs(DATA, exist_ok=True)
    out = {"generated_utc": dt.datetime.utcnow().strftime("%Y-%m-%d %H:%M"), "series": {}, "errors": []}

    for group, name, cands in SYMBOLS:
        for sym in cands:
            try:
                df = fetch(sym)
                if len(df) < 25:
                    raise ValueError("数据太少")
                s = summarize(df)
                s.update({"group": group, "symbol": sym,
                          "volume_unit": "成交额(元)" if sym.startswith("em:") else "成交量"})
                out["series"][name] = s
                break
            except Exception as e:  # noqa
                out["errors"].append(f"{name} {sym}: {e}")
            time.sleep(0.5)

    # 沪深两市合计成交额（上证 + 深证成指成交额，东方财富口径）
    try:
        a = em_daily("1.000001")["volume"]
        b = em_daily("0.399001")["volume"]
        tot = (a + b).dropna()
        out["series"]["沪深两市成交额"] = {
            "group": "A股", "symbol": "em:上证+深成", "date": str(tot.index[-1]),
            "close": round(float(tot.iloc[-1]) / 1e8, 0), "unit": "亿元",
            "avg_5d": round(float(tot.iloc[-6:-1].mean()) / 1e8, 0),
            "avg_20d": round(float(tot.iloc[-21:-1].mean()) / 1e8, 0),
            "max_1y": round(float(tot.iloc[-250:].max()) / 1e8, 0),
            "min_1y": round(float(tot.iloc[-250:].min()) / 1e8, 0),
        }
    except Exception as e:  # noqa
        out["errors"].append(f"两市成交额: {e}")

    # 美国经济政策不确定性指数（日度，Baker-Bloom-Davis）：先试官网 CSV，再试 FRED: USEPUINDXD
    try:
        try:
            s = epu_official()
        except Exception as e0:  # noqa
            out["errors"].append(f"EPU官网: {e0}")
            s = fred_series("USEPUINDXD")
        last250 = s.iloc[-365:]
        out["series"]["美国经济政策不确定性EPU(日度)"] = {
            "group": "不确定性", "symbol": "FRED:USEPUINDXD", "date": str(s.index[-1]),
            "close": round(float(s.iloc[-1]), 1),
            "avg_7d": round(float(s.iloc[-7:].mean()), 1),
            "avg_30d": round(float(s.iloc[-30:].mean()), 1),
            "avg_1y": round(float(last250.mean()), 1),
            "pctile_1y_of_7d_avg": round(float((last250.rolling(7).mean() <= s.iloc[-7:].mean()).mean() * 100), 1),
            "note": "EPU 日度数据噪声大，看7日均值；数据通常滞后1-2天",
        }
    except Exception as e:  # noqa
        out["errors"].append(f"EPU: {e}")

    with open(os.path.join(DATA, "markets.json"), "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print(json.dumps({k: (v if k != "series" else list(v.keys())) for k, v in out.items()}, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
