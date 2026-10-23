"""临时探测脚本：比较不同数据源的期权行权价覆盖范围，确认后删除。"""
from io import BytesIO, StringIO

import pandas as pd
import requests

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0 Safari/537.36"}

for prod in ["MO", "IO"]:
    print(f"\n===== 中金所官网 quote_{prod}.txt =====")
    try:
        r = requests.get(f"http://www.cffex.com.cn/quote_{prod}.txt", headers=UA, timeout=30)
        print("status", r.status_code, "bytes", len(r.content))
        df = pd.read_table(BytesIO(r.content), sep=",")
        print("columns:", list(df.columns))
        print(df.head(3).to_string())
        inst = df["instrument"].astype(str)
        for m in ["2611", "2612"]:
            sub = df[inst.str.contains(f"{prod}{m}")]
            ks = sub["instrument"].str.extract(r"-(\d+)$")[0].astype(float)
            print(f"{prod}{m}: n={len(sub)} strike range {ks.min()} - {ks.max()}")
    except Exception as e:
        print("失败:", repr(e))

print("\n===== 东方财富 中金所期权列表 =====")
try:
    r = requests.get("https://futsseapi.eastmoney.com/list/option/221", params={
        "orderBy": "zdf", "sort": "desc", "pageSize": "20000", "pageIndex": "0",
        "token": "58b2fa8f54638b60b87d69b31969089c",
        "field": "dm,sc,name,p,zsjd,zde,zdf,f152,vol,cje,ccl,xqj,syr,rz,zjsj,o"}, headers=UA, timeout=30)
    js = r.json()
    df = pd.DataFrame(js["list"])
    for m in ["mo2611", "io2611", "mo2612"]:
        sub = df[df["dm"].astype(str).str.lower().str.startswith(m)]
        print(f"{m}: n={len(sub)} strike {pd.to_numeric(sub['xqj']).min()} - {pd.to_numeric(sub['xqj']).max()}")
except Exception as e:
    print("失败:", repr(e))

print("\n===== 期权论坛 k.csv 表头 =====")
try:
    r = requests.get("http://1.optbbs.com/d/csv/d/k.csv", headers=UA, timeout=60)
    raw = pd.read_csv(StringIO(r.content.decode("gbk", errors="ignore")))
    print("shape", raw.shape)
    print(list(enumerate(raw.columns)))
    print(raw.dropna(how="all", subset=raw.columns[1:]).tail(3).to_string())
except Exception as e:
    print("失败:", repr(e))
