"""
每个交易日收盘后运行：
1. 从新浪财经抓取中证1000股指期权（mo）、沪深300股指期权（io）近月、次月的完整报价
2. 按 Cboe VIX 方法计算 30 天隐含波动率指数（见 vix_calc.py）
3. 从期权论坛下载 QVIX 各版本，用于核对和历史分位
4. 结果写入 data/ 目录（同一天重复运行会覆盖当天那一行）
"""

import calendar
import datetime as dt
import json
import math
import os
import sys
import traceback

import pandas as pd
import requests

from vix_calc import term_variance, vix_from_terms

RISK_FREE = 0.015          # 无风险利率近似（中国1年期国债收益率约1.3%-1.5%），对结果影响很小
ROLL_DAYS = 7              # 与原上交所 iVIX 一致：近月剩余不足7天就换到下一个月
BJ = dt.timezone(dt.timedelta(hours=8))
DATA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Referer": "https://stock.finance.sina.com.cn/",
}

QVIX_COLS = {  # 期权论坛 k.csv 中各版本 QVIX 收盘价所在列（与 AKShare 一致）
    "qvix_50etf": 4, "qvix_300etf": 12, "qvix_500etf": 70, "qvix_cyb": 74,
    "qvix_100etf": 78, "qvix_kcb": 86,
    # 论坛自己的沪深300指数(20)、中证1000指数(28)两列目前是 #NAME? 错误值，暂不使用
}


def third_friday(year: int, month: int) -> dt.date:
    c = calendar.Calendar(firstweekday=calendar.MONDAY)
    fridays = [d for d in c.itermonthdates(year, month) if d.weekday() == 4 and d.month == month]
    return fridays[2]


def add_month(y, m, k):
    m2 = m + k
    return y + (m2 - 1) // 12, (m2 - 1) % 12 + 1


_OFFICIAL_CACHE = {}


def fetch_cffex_official(product: str, ym: str) -> pd.DataFrame:
    """中金所官网实时报价文件，如 http://www.cffex.com.cn/quote_MO.txt"""
    up = product.upper()
    if up not in _OFFICIAL_CACHE:
        from io import BytesIO
        r = requests.get(f"http://www.cffex.com.cn/quote_{up}.txt", headers=HEADERS, timeout=30)
        r.raise_for_status()
        _OFFICIAL_CACHE[up] = pd.read_csv(BytesIO(r.content))
    raw = _OFFICIAL_CACHE[up]
    parts = raw["instrument"].astype(str).str.extract(r"^([A-Z]+)(\d{4})-([CP])-(\d+(?:\.\d+)?)$")
    raw = raw.assign(month=parts[1], cp=parts[2], strike=pd.to_numeric(parts[3], errors="coerce"))
    sub = raw[raw.month == ym]
    if sub.empty:
        return pd.DataFrame()
    num = lambda s: pd.to_numeric(s, errors="coerce").fillna(0)  # noqa: E731
    c = sub[sub.cp == "C"].set_index("strike")
    p = sub[sub.cp == "P"].set_index("strike")
    df = pd.DataFrame({
        "call_bid": num(c["bprice"]), "call_ask": num(c["sprice"]), "call_last": num(c["lastprice"]),
    }).join(pd.DataFrame({
        "put_bid": num(p["bprice"]), "put_ask": num(p["sprice"]), "put_last": num(p["lastprice"]),
    }), how="inner").reset_index().rename(columns={"index": "strike"})
    return df.fillna(0)


def fetch_cffex_chain(product: str, ym: str, notes: list) -> pd.DataFrame:
    """先用中金所官网，失败再用新浪。"""
    try:
        df = fetch_cffex_official(product, ym)
        if not df.empty:
            df["source"] = "cffex"
            return df
    except Exception as e:  # noqa
        notes.append(f"中金所官网 {product}{ym} 失败，改用新浪: {e}")
    df = fetch_sina_chain(product, ym)
    if not df.empty:
        df["source"] = "sina"
    return df


def fetch_sina_chain(product: str, ym: str) -> pd.DataFrame:
    url = "https://stock.finance.sina.com.cn/futures/api/openapi.php/OptionService.getOptionData"
    params = {"type": "futures", "product": product, "exchange": "cffex", "pinzhong": f"{product}{ym}"}
    r = requests.get(url, params=params, headers=HEADERS, timeout=30)
    r.raise_for_status()
    t = r.text
    js = json.loads(t[t.find("{"): t.rfind("}") + 1])
    up = js["result"]["data"]["up"]
    down = js["result"]["data"]["down"]
    if not up or not down:
        return pd.DataFrame()
    c = pd.DataFrame(up).iloc[:, :9]
    c.columns = ["c_bidv", "call_bid", "call_last", "call_ask", "c_askv", "c_oi", "c_chg", "strike", "c_code"]
    p = pd.DataFrame(down).iloc[:, :8]
    p.columns = ["p_bidv", "put_bid", "put_last", "put_ask", "p_askv", "p_oi", "p_chg", "p_code"]
    df = pd.concat([c, p], axis=1)
    for col in ["call_bid", "call_last", "call_ask", "put_bid", "put_last", "put_ask", "strike"]:
        df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0)
    return df[df.strike > 0]


def compute_index(product: str, now: dt.datetime, notes: list, save_chain: bool = True):
    """返回 (vix, 诊断信息dict)；失败返回 (None, info)。"""
    terms = []
    info = {}
    for k in range(0, 4):
        y, m = add_month(now.year, now.month, k)
        exp_dt = dt.datetime.combine(third_friday(y, m), dt.time(15, 0), tzinfo=BJ)
        minutes = (exp_dt - now).total_seconds() / 60
        if minutes < ROLL_DAYS * 1440:
            continue
        ym = f"{y % 100:02d}{m:02d}"
        try:
            chain = fetch_cffex_chain(product, ym, notes)
        except Exception as e:  # noqa
            notes.append(f"{product}{ym} 抓取失败: {e}")
            continue
        if chain.empty:
            notes.append(f"{product}{ym} 无数据")
            continue
        if save_chain:
            os.makedirs(os.path.join(DATA, "chains"), exist_ok=True)
            chain.to_csv(os.path.join(DATA, "chains", f"{now:%Y%m%d}_{product}{ym}.csv"), index=False)
        res = term_variance(chain, minutes, RISK_FREE)
        if res is None:
            notes.append(f"{product}{ym} 有效报价不足")
            continue
        terms.append((ym, res, chain["source"].iloc[0]))
        if len(terms) == 2:
            break
    if not terms:
        return None, info
    near = terms[0][1]
    nxt = terms[1][1] if len(terms) > 1 else None
    vix = vix_from_terms(near, nxt)
    info = {
        "near": f"{product}{terms[0][0]}", "near_days": round(near.minutes / 1440, 2), "near_n": near.n_strikes,
        "near_F": round(near.F, 2), "source": terms[0][2],
        "next": f"{product}{terms[1][0]}" if nxt else "", "next_days": round(nxt.minutes / 1440, 2) if nxt else None,
        "next_n": nxt.n_strikes if nxt else None,
    }
    return round(vix, 2), info


def fetch_qvix(notes: list):
    url = "http://1.optbbs.com/d/csv/d/k.csv"
    try:
        r = requests.get(url, headers={"User-Agent": HEADERS["User-Agent"]}, timeout=60)
        r.raise_for_status()
        from io import StringIO
        raw = pd.read_csv(StringIO(r.content.decode("gbk", errors="ignore")))
    except Exception as e:  # noqa
        notes.append(f"QVIX 下载失败: {e}")
        return None
    out = pd.DataFrame({"date": pd.to_datetime(raw.iloc[:, 0], errors="coerce").dt.date})
    for name, idx in QVIX_COLS.items():
        if idx < raw.shape[1]:
            v = pd.to_numeric(raw.iloc[:, idx], errors="coerce")
            out[name] = v.where(v > 0)
    out = out.dropna(subset=["date"])
    return out.dropna(how="all", subset=[c for c in out.columns if c != "date"])


def upsert(path: str, row: dict):
    df = pd.read_csv(path) if os.path.exists(path) else pd.DataFrame()
    if not df.empty and "date" in df.columns:
        df = df[df["date"].astype(str) != row["date"]]
    df = pd.concat([df, pd.DataFrame([row])], ignore_index=True).sort_values("date")
    df.to_csv(path, index=False)


def main():
    os.makedirs(DATA, exist_ok=True)
    now = dt.datetime.now(BJ)
    notes = []
    row = {"date": now.strftime("%Y-%m-%d"), "run_time_bj": now.strftime("%Y-%m-%d %H:%M")}

    for product, label in [("mo", "vix_csi1000"), ("io", "vix_csi300")]:
        try:
            v, info = compute_index(product, now, notes)
        except Exception:  # noqa
            notes.append(f"{product} 计算出错: {traceback.format_exc(limit=1)}")
            v, info = None, {}
        row[label] = v
        for k, val in info.items():
            row[f"{label}_{k}"] = val

    q = fetch_qvix(notes)
    if q is not None and not q.empty:
        q.to_csv(os.path.join(DATA, "qvix_history.csv"), index=False)
        last = q.iloc[-1]
        row["qvix_date"] = str(last["date"])
        for name in QVIX_COLS:
            if name in q.columns:
                row[name] = None if pd.isna(last[name]) else round(float(last[name]), 2)

    row["notes"] = " | ".join(notes)
    upsert(os.path.join(DATA, "a_share_vix.csv"), row)
    with open(os.path.join(DATA, "latest.json"), "w", encoding="utf-8") as f:
        json.dump(row, f, ensure_ascii=False, indent=2, default=str)
    print(json.dumps(row, ensure_ascii=False, indent=2, default=str))

    if row.get("vix_csi1000") is None and row.get("vix_csi300") is None and q is None:
        sys.exit("所有数据源都失败")


if __name__ == "__main__":
    main()
