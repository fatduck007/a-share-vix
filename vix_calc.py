"""
A股恐慌指数：按 Cboe VIX 白皮书方法，用股指期权完整报价计算 30 天隐含波动率指数。

核心公式（单个到期月）：
    sigma^2 = (2/T) * sum_i( dK_i / K_i^2 * e^{RT} * Q(K_i) ) - (1/T) * (F/K0 - 1)^2
    T    : 到期时间（年），按分钟计 / 525600
    K_i  : 第 i 个虚值期权的行权价
    dK_i : 行权价间隔 = (K_{i+1} - K_{i-1}) / 2（两端用单边间隔）
    Q(K) : 该行权价期权的买卖中间价（K<K0 用看跌，K>K0 用看涨，K0 取两者均值）
    R    : 无风险利率（连续复利）
    F    : 远期价格 = K* + e^{RT} * (C(K*) - P(K*))，K* 为看涨看跌价差最小的行权价
    K0   : 不高于 F 的最大行权价

再把近月、次月方差按时间线性插值到 30 天：
    VIX = 100 * sqrt( [T1*s1^2*(N2-N30)/(N2-N1) + T2*s2^2*(N30-N1)/(N2-N1)] * N365/N30 )
"""

import math
from dataclasses import dataclass
from typing import Optional

import pandas as pd

MIN_PER_YEAR = 365 * 24 * 60
N30 = 30 * 24 * 60


@dataclass
class TermResult:
    sigma2: float
    T: float          # 年
    minutes: float    # 剩余分钟
    F: float
    K0: float
    n_strikes: int


def term_variance(chain: pd.DataFrame, minutes: float, r: float) -> Optional[TermResult]:
    """
    chain 列：strike, call_bid, call_ask, call_last, put_bid, put_ask, put_last
    返回该到期月的方差；数据不足返回 None。
    """
    T = minutes / MIN_PER_YEAR
    df = chain.copy().dropna(subset=["strike"]).sort_values("strike").reset_index(drop=True)
    if len(df) < 5 or T <= 0:
        return None

    def mid(bid, ask, last):
        if bid > 0 and ask > 0 and ask >= bid:
            return (bid + ask) / 2
        if last and last > 0:          # 收盘后偶有买卖价缺失，退而用最新价
            return last
        return float("nan")

    df["call_mid"] = [mid(b, a, l) for b, a, l in zip(df.call_bid, df.call_ask, df.call_last)]
    df["put_mid"] = [mid(b, a, l) for b, a, l in zip(df.put_bid, df.put_ask, df.put_last)]

    both = df.dropna(subset=["call_mid", "put_mid"])
    if both.empty:
        return None
    diff = (both.call_mid - both.put_mid).abs()
    kstar = both.loc[diff.idxmin()]
    erT = math.exp(r * T)
    F = kstar.strike + erT * (kstar.call_mid - kstar.put_mid)

    below = df[df.strike <= F]
    if below.empty:
        return None
    K0 = below.strike.max()

    # 选取虚值期权：从 K0 向外走，遇到连续两个零买价就停止，零买价的行权价不计入
    selected = []  # (strike, Q)
    i0 = df.index[df.strike == K0][0]
    # 看跌（向下）
    zeros = 0
    for i in range(i0 - 1, -1, -1):
        row = df.loc[i]
        if not (row.put_bid > 0):
            zeros += 1
            if zeros >= 2:
                break
            continue
        zeros = 0
        if not math.isnan(row.put_mid):
            selected.append((row.strike, row.put_mid))
    # K0
    k0row = df.loc[i0]
    q0 = pd.Series([k0row.call_mid, k0row.put_mid]).dropna()
    if q0.empty:
        return None
    selected.append((K0, q0.mean()))
    # 看涨（向上）
    zeros = 0
    for i in range(i0 + 1, len(df)):
        row = df.loc[i]
        if not (row.call_bid > 0):
            zeros += 1
            if zeros >= 2:
                break
            continue
        zeros = 0
        if not math.isnan(row.call_mid):
            selected.append((row.strike, row.call_mid))

    sel = pd.DataFrame(selected, columns=["K", "Q"]).sort_values("K").reset_index(drop=True)
    if len(sel) < 3:
        return None
    Ks = sel.K.tolist()
    dK = []
    for j in range(len(Ks)):
        if j == 0:
            dK.append(Ks[1] - Ks[0])
        elif j == len(Ks) - 1:
            dK.append(Ks[-1] - Ks[-2])
        else:
            dK.append((Ks[j + 1] - Ks[j - 1]) / 2)
    sel["dK"] = dK
    contrib = (sel.dK / sel.K ** 2 * erT * sel.Q).sum()
    sigma2 = (2 / T) * contrib - (1 / T) * (F / K0 - 1) ** 2
    return TermResult(sigma2=sigma2, T=T, minutes=minutes, F=F, K0=K0, n_strikes=len(sel))


def vix_from_terms(near: TermResult, nxt: Optional[TermResult]) -> float:
    """把近月、次月方差插值到 30 天。近月剩余期限 >= 30 天或没有次月时，只用近月。"""
    if nxt is None or near.minutes >= N30:
        return 100 * math.sqrt(max(near.sigma2, 0))
    N1, N2 = near.minutes, nxt.minutes
    w1 = (N2 - N30) / (N2 - N1)
    w2 = (N30 - N1) / (N2 - N1)
    var = (near.T * near.sigma2 * w1 + nxt.T * nxt.sigma2 * w2) * (MIN_PER_YEAR / N30)
    return 100 * math.sqrt(max(var, 0))
