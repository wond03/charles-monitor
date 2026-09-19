# -*- coding: utf-8 -*-
"""
查尔斯信号监控系统 · 信号引擎
把《查尔斯交易实操手册》的策略规则翻译成可计算的规则化版本：

  - 关键水平：swing 高低点 + 价格聚类（对应手册"标记关键水平，精确到小数点后两位"）
  - 真假突破：实体收穿=真 / 影线穿但收回=假（对应手册 2.4 终极判断标准）
  - 归汤信号：假突破 + 反转迹象（对应手册 3.2 Turtle Soup 六步）
  - BOS：实体收穿最近 swing（对应手册 2.1）
  - CHoCH / MSS：转势信号（对应手册 2.2 / 2.3）
  - FVG：三K线缺口（对应手册 2.5）
  - 0.5回踩：swing 区间斐波那契 0.5（对应手册 3.1 附加筛选）

⚠️ 说明：手册中"数结构""倒V回踩""庄家意愿"等需盘面经验的判断，
本引擎做规则化近似，信号用于提醒人工复核，不构成直接交易指令。
"""
from dataclasses import dataclass, field
from typing import List, Optional
import logging

log = logging.getLogger(__name__)

# ---------- 数据结构 ----------

@dataclass
class Kline:
    ts: int
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0


@dataclass
class Signal:
    symbol: str          # BTC / 黄金
    direction: str       # long / short / wait（做多 / 做空 / 等待）
    strategy: str        # 保底 / 归汤 / 模板 / BOS / MSS / 0.5回踩 / 等待
    level: str           # 1H / 15M / 4H
    price: float
    key_levels: list = field(default_factory=list)
    detail: str = ""
    ts: int = 0
    entry_type: str = ""  # 入场方式：限价委托 / 市价委托 / 条件委托 / 追踪委托
    sl_price: float = 0.0   # 结构止损位（手册条件判定）
    tp_price: float = 0.0   # 目标位（盈亏比目标）
    rr_target: float = 0.0  # 目标盈亏比（区间上限，默认 5R；须落在 1:2~1:5 区间）
    priority: int = 99      # 条件判定优先级：越小越符合（同品种择优依据）


# ---------- 盈亏比目标区间（用户确认口径：1:2 ~ 1:5） ----------
RR_MIN_DEFAULT = 2.0   # 盈亏比下限：目标至少 2R 才有入场意义
RR_MAX_DEFAULT = 5.0   # 盈亏比上限：到 TP 止盈按 5R 计算（对应手册 4.4 到TP止盈）


# ---------- 基础工具 ----------

def to_klines(rows: list) -> List[Kline]:
    return [Kline(ts=r["ts"], open=r["open"], high=r["high"],
                  low=r["low"], close=r["close"], volume=r.get("volume", 0.0))
            for r in rows]


def sma(values: List[float], period: int) -> float:
    if len(values) < period:
        return sum(values) / len(values)
    return sum(values[-period:]) / period


def detect_swings(kl: List[Kline], radius: int = 3):
    """检测 swing 高点/低点。返回 [(index, price, 'high'|'low'), ...]"""
    swings = []
    n = len(kl)
    for i in range(radius, n - radius):
        win_high = [kl[i - r].high for r in range(1, radius + 1)]
        win_low = [kl[i - r].low for r in range(1, radius + 1)]
        if kl[i].high > max(win_high) and kl[i].high > max(k.high for k in kl[i + 1:i + 1 + radius]):
            swings.append((i, kl[i].high, "high"))
        if kl[i].low < min(win_low) and kl[i].low < min(k.low for k in kl[i + 1:i + 1 + radius]):
            swings.append((i, kl[i].low, "low"))
    return swings


def key_levels(kl: List[Kline], radius: int = 3, cluster_pct: float = 0.15) -> List[dict]:
    """swing 价格聚类为关键水平。返回 [{price, kind, count}, ...] 按最近优先"""
    swings = detect_swings(kl, radius)
    if not swings:
        return []
    levels = []  # {price, kind, ts, count}
    for idx, price, kind in swings:
        placed = False
        for lv in levels:
            if abs(lv["price"] - price) / price * 100 <= cluster_pct:
                lv["count"] += 1
                if kl[idx].ts > lv["ts"]:
                    lv["ts"] = kl[idx].ts
                    lv["kind"] = kind
                    lv["price"] = price
                placed = True
                break
        if not placed:
            levels.append({"price": price, "kind": kind, "ts": kl[idx].ts, "count": 1})
    levels.sort(key=lambda x: -x["ts"])
    return levels[:10]


def fvg_detect(kl: List[Kline], idx: int, min_pct: float = 0.05) -> Optional[str]:
    """检测第 idx 根K线是否为 FVG 起点。
    看涨FVG: low[i] > high[i-2]；看跌FVG: high[i] < low[i-2]
    """
    if idx < 2:
        return None
    a, b, c = kl[idx - 2], kl[idx - 1], kl[idx]
    gap_pct = abs(a.high - c.low) / c.low * 100
    if c.low > a.high and gap_pct >= min_pct:
        return "bull"
    gap_pct2 = abs(c.high - a.low) / c.low * 100
    if c.high < a.low and gap_pct2 >= min_pct:
        return "bear"
    return None


def last_fvg(kl: List[Kline], n: int = 8, min_pct: float = 0.05) -> Optional[str]:
    """最近 n 根K线内是否存在 FVG 及方向"""
    for idx in range(len(kl) - 1, max(1, len(kl) - n) - 1, -1):
        d = fvg_detect(kl, idx, min_pct)
        if d:
            return d
    return None


def trend_by_ma(kl: List[Kline], period: int = 50) -> str:
    """简单趋势判断：收盘价 vs 均线。返回 up / down / flat"""
    if len(kl) < period:
        return "flat"
    ma = sma([k.close for k in kl], period)
    if kl[-1].close > ma * 1.002:
        return "up"
    if kl[-1].close < ma * 0.998:
        return "down"
    return "flat"


# ---------- 手册条件判定辅助（对应手册 4.2 止损 / 4.4 止盈三原则） ----------

def struct_sl_from_swings(kl: List[Kline], direction: str, radius: int = 3,
                          buffer_pct: float = 0.1) -> Optional[float]:
    """结构止损位：long=最近 swing 低点下方，short=最近 swing 高点上方
    对应手册 4.2：止损设在结构下方/上方（小止损）"""
    swings = detect_swings(kl, radius)
    if not swings:
        return None
    if direction == "long":
        lows = [p for _, p, k in swings if k == "low"]
        if not lows:
            return None
        return lows[-1] * (1 - buffer_pct / 100.0)
    if direction == "short":
        highs = [p for _, p, k in swings if k == "high"]
        if not highs:
            return None
        return highs[-1] * (1 + buffer_pct / 100.0)
    return None


def tp_from_rr(entry: float, sl: float, direction: str, rr: float) -> Optional[float]:
    """按盈亏比计算目标价（手册 4.4：到TP止盈）"""
    if sl is None or sl <= 0:
        return None
    dist = abs(entry - sl)
    if direction == "long":
        return entry + dist * rr
    if direction == "short":
        return entry - dist * rr
    return None


def vol_surge(kl: List[Kline], n: int = 20, mult: float = 2.5) -> bool:
    """出量检测（手册 4.4：任何情况下一旦出量就要吃单）"""
    vols = [k.volume for k in kl if k.volume > 0]
    if len(vols) < 5:
        return False
    window = vols[-n:]
    avg = sum(window) / len(window)
    if avg <= 0:
        return False
    return kl[-1].volume > avg * mult


# ---------- 信号原语 ----------

def detect_bos(kl: List[Kline], radius: int = 3) -> Optional[Signal]:
    """BOS 结构破坏：最近 swing 被实体收穿"""
    if len(kl) < radius * 2 + 3:
        return None
    swings = detect_swings(kl, radius)
    if not swings:
        return None
    last_swing = swings[-1]
    idx, price, kind = last_swing
    cur = kl[-1]
    if kind == "high" and cur.close > price:
        s = Signal(symbol="", direction="long", strategy="BOS", level="",
                   price=cur.close, detail=f"实体收穿前高 {price:.2f}", priority=3)
        sl = struct_sl_from_swings(kl, "long", radius)
        if sl:
            s.sl_price = sl
            s.tp_price = tp_from_rr(cur.close, sl, "long", 5.0) or 0.0
        return s
    if kind == "low" and cur.close < price:
        s = Signal(symbol="", direction="short", strategy="BOS", level="",
                   price=cur.close, detail=f"实体收穿前低 {price:.2f}", priority=3)
        sl = struct_sl_from_swings(kl, "short", radius)
        if sl:
            s.sl_price = sl
            s.tp_price = tp_from_rr(cur.close, sl, "short", RR_MAX_DEFAULT) or 0.0
        return s
    return None


def detect_fake_breakout(kl: List[Kline], levels: List[dict]) -> Optional[Signal]:
    """归汤·假突破：最新一根影线穿过关键水平但收盘收回"""
    if len(kl) < 2:
        return None
    cur, prev = kl[-1], kl[-2]
    for lv in levels:
        p = lv["price"]
        # 向上假突破：最高价超过水平，但收盘收回水平下方
        if cur.high > p > cur.close and cur.close < p and cur.high >= p * 1.0005:
            s = Signal(symbol="", direction="short", strategy="归汤", level="",
                       price=cur.close, key_levels=[p],
                       detail=f"影线上穿关键位 {p:.2f} 后收回，疑似假突破", priority=4)
            sl = struct_sl_from_swings(kl, "short", 3)
            if sl:
                s.sl_price = sl
                s.tp_price = tp_from_rr(cur.close, sl, "short", RR_MAX_DEFAULT) or 0.0
            return s
        # 向下假突破：最低价低于水平，但收盘收回水平上方
        if cur.low < p < cur.close and cur.low <= p * 0.9995:
            s = Signal(symbol="", direction="long", strategy="归汤", level="",
                       price=cur.close, key_levels=[p],
                       detail=f"影线下穿关键位 {p:.2f} 后收回，疑似假突破", priority=4)
            sl = struct_sl_from_swings(kl, "long", 3)
            if sl:
                s.sl_price = sl
                s.tp_price = tp_from_rr(cur.close, sl, "long", 5.0) or 0.0
            return s
    return None


def detect_mss(kl: List[Kline], radius: int = 3) -> Optional[Signal]:
    """MSS 回踩型转势（简化规则）：
    看涨MSS: 出现更低低点(lower low)后反弹形成回踩，随后实体收穿回踩前的局部高点
    看跌MSS: 出现更高高点(higher high)后回落，随后实体收穿回踩前的局部低点
    """
    if len(kl) < radius * 2 + 5:
        return None
    swings = detect_swings(kl, radius)
    if len(swings) < 3:
        return None
    s1, s2, s3 = swings[-3], swings[-2], swings[-1]
    cur = kl[-1]
    # 看涨MSS：swing 序列 low -> low，s2低点 > s1低点（higher low），s3反弹高点被实体收穿
    if s1[2] == "low" and s2[2] == "low" and s3[2] == "high":
        if s2[1] > s1[1] and cur.close > s3[1]:
            s = Signal(symbol="", direction="long", strategy="MSS", level="",
                       price=cur.close, detail=f"更高低点回踩后实体收穿反弹高点 {s3[1]:.2f}", priority=3)
            sl = struct_sl_from_swings(kl, "long", radius)
            if sl:
                s.sl_price = sl
                s.tp_price = tp_from_rr(cur.close, sl, "long", 5.0) or 0.0
            return s
    # 看跌MSS：swing 序列 high -> high，s2高点 < s1高点（lower high），s3回落低点被实体收穿
    if s1[2] == "high" and s2[2] == "high" and s3[2] == "low":
        if s2[1] < s1[1] and cur.close < s3[1]:
            s = Signal(symbol="", direction="short", strategy="MSS", level="",
                       price=cur.close, detail=f"更低高点回踩后实体收穿回落低点 {s3[1]:.2f}", priority=3)
            sl = struct_sl_from_swings(kl, "short", radius)
            if sl:
                s.sl_price = sl
                s.tp_price = tp_from_rr(cur.close, sl, "short", RR_MAX_DEFAULT) or 0.0
            return s
    return None


def retrace_05_level(kl: List[Kline], radius: int = 3) -> Optional[float]:
    """最近一波 swing 区间的斐波那契 0.5 价位（对应手册 3.1 附加筛选）。
    只取最近两个方向相反的 swing（一高一低）构成区间；返回 None 表示无有效区间。
    注意：本函数只算价位、不作独立信号，由结构信号命中时作为加分项标注。"""
    if len(kl) < radius * 2 + 2:
        return None
    swings = detect_swings(kl, radius)
    if len(swings) < 2:
        return None
    s1, s2 = swings[-2], swings[-1]
    if s1[2] == s2[2]:
        return None
    hi = max(s1[1], s2[1])
    lo = min(s1[1], s2[1])
    if hi == lo:
        return None
    return lo + (hi - lo) * 0.5


def near_level(price: float, level: float, tolerance_pct: float = 0.15) -> bool:
    """价格是否在目标价位附近（容差百分比）"""
    if level is None or level <= 0:
        return False
    return abs(price - level) / level * 100 <= tolerance_pct


def retrace_05_note(kl: List[Kline], direction: str, radius: int = 3) -> str:
    """0.5 回踩位加持标注：结构信号命中且价格恰处于 0.5 位附近时返回标注文本。
    顺势校验——long 须价格回踩到 0.5 位（close 不高于 0.5 位 + 容差），
    short 须价格反弹到 0.5 位（close 不低于 0.5 位 - 容差）。"""
    fib_05 = retrace_05_level(kl, radius)
    if fib_05 is None:
        return ""
    tol = fib_05 * 0.15 / 100
    if direction == "long" and kl[-1].close <= fib_05 + tol:
        return f"；0.5回踩位加持({fib_05:.2f})"
    if direction == "short" and kl[-1].close >= fib_05 - tol:
        return f"；0.5回踩位加持({fib_05:.2f})"
    return ""


# ---------- 策略组装 ----------

def scan_symbol(klines_h1: List[Kline], klines_m15: List[Kline], klines_h4: List[Kline],
                symbol: str, cfg: dict) -> List[Signal]:
    """对一个标的多级别扫描，返回信号列表"""
    out: List[Signal] = []
    h1_ma = cfg.get("trend_ma", 50)
    radius_h1 = cfg.get("pivot_radius_h1", 3)
    radius_m15 = cfg.get("pivot_radius_m15", 5)
    cluster = cfg.get("level_cluster_pct", 0.15)
    fvg_min = cfg.get("fvg_min_pct", 0.05)
    # 盈亏比目标区间（用户确认口径：1:2 ~ 1:5；兼容旧配置 tp_rr 单值）
    rr_min = float(cfg.get("tp_rr_min", RR_MIN_DEFAULT))
    rr_max = float(cfg.get("tp_rr_max", cfg.get("tp_rr", RR_MAX_DEFAULT)))
    rr_max = max(rr_max, rr_min)

    # --- 1H 级别信号 ---
    h1_trend = trend_by_ma(klines_h1, h1_ma)
    h1_levels = key_levels(klines_h1, radius_h1, cluster)
    # 0.5 回踩位附加确认（对应手册 3.1）：不单独推送，结构信号命中且价格恰在 0.5 位附近时标注加分
    fib_05_note = {d: retrace_05_note(klines_h1, d, radius_h1) for d in ("long", "short")}

    # BOS（结构破坏）
    s_bos = detect_bos(klines_h1, radius_h1)
    if s_bos:
        s_bos.symbol, s_bos.level = symbol, "1H"
        s_bos.entry_type = "市价委托"
        s_bos.detail += fib_05_note.get(s_bos.direction, "")
        out.append(s_bos)

    # MSS（回踩型转势）
    s_mss = detect_mss(klines_h1, radius_h1)
    if s_mss:
        s_mss.symbol, s_mss.level = symbol, "1H"
        s_mss.entry_type = "市价委托"
        s_mss.detail += fib_05_note.get(s_mss.direction, "")
        out.append(s_mss)

    # 归汤·假突破（1H 关键水平）
    s_fake = detect_fake_breakout(klines_h1, h1_levels)
    if s_fake:
        s_fake.symbol, s_fake.level = symbol, "1H"
        s_fake.entry_type = "市价委托"
        s_fake.detail += fib_05_note.get(s_fake.direction, "")
        out.append(s_fake)

    # --- 保底策略组合：1H 趋势方向 + 15M 转势确认 + FVG 过滤 ---
    m15_levels = key_levels(klines_m15, radius_m15, cluster)
    m15_trend = trend_by_ma(klines_m15, min(h1_ma // 2, 30))
    m15_fvg = last_fvg(klines_m15, 8, fvg_min)

    s_m15_mss = detect_mss(klines_m15, radius_m15)
    s_m15_bos = detect_bos(klines_m15, radius_m15)

    for s in (s_m15_mss, s_m15_bos):
        if not s:
            continue
        # 方向与 1H 趋势同向 且 15M 有 FVG 支持（保底：新建信号对象，不污染原 MSS/BOS 供模板复用）
        if h1_trend == "up" and s.direction == "long":
            if m15_fvg == "bull" or m15_trend == "up":
                s2 = Signal(symbol=symbol, direction="long", strategy="保底", level="15M",
                            price=s.price, key_levels=[lv["price"] for lv in h1_levels[:4]],
                            detail=f"1H趋势向上 + 15M {s.strategy}确认 + FVG支持；"
                                   f"止损=1H结构下方，目标1:2~1:5（到TP止盈）"
                                   f"{fib_05_note.get('long', '')}",
                            entry_type="条件委托", priority=2)
                sl = struct_sl_from_swings(klines_h1, "long", radius_h1)
                s2.sl_price = sl
                s2.tp_price = tp_from_rr(s.price, sl, "long", rr_max)
                s2.rr_target = rr_max
                out.append(s2)
        elif h1_trend == "down" and s.direction == "short":
            if m15_fvg == "bear" or m15_trend == "down":
                s2 = Signal(symbol=symbol, direction="short", strategy="保底", level="15M",
                            price=s.price, key_levels=[lv["price"] for lv in h1_levels[:4]],
                            detail=f"1H趋势向下 + 15M {s.strategy}确认 + FVG支持；"
                                   f"止损=1H结构上方，目标1:2~1:5（到TP止盈）"
                                   f"{fib_05_note.get('short', '')}",
                            entry_type="条件委托", priority=2)
                sl = struct_sl_from_swings(klines_h1, "short", radius_h1)
                s2.sl_price = sl
                s2.tp_price = tp_from_rr(s.price, sl, "short", rr_max)
                s2.rr_target = rr_max
                out.append(s2)

    # --- 模板策略：4H 趋势 + 15M 共振 ---
    h4_trend = trend_by_ma(klines_h4, min(h1_ma * 2, 60)) if len(klines_h4) > 20 else "flat"
    for s in (s_m15_mss, s_m15_bos):
        if not s:
            continue
        if h4_trend == "up" and s.direction == "long":
            sl15 = struct_sl_from_swings(klines_m15, "long", radius_m15)
            s2 = Signal(symbol=symbol, direction="long", strategy="模板", level="4H+15M",
                        price=s.price, key_levels=s.key_levels,
                        detail=f"4H趋势向上 + 15M {s.strategy}共振；"
                               f"大级别定趋势、小级别找共振；极小止损抓大结构，目标1:2~1:5"
                               f"{fib_05_note.get('long', '')}",
                        entry_type="追踪委托", priority=1)
            s2.sl_price = sl15
            s2.tp_price = tp_from_rr(s.price, sl15, "long", rr_max)
            s2.rr_target = rr_max
            out.append(s2)
        elif h4_trend == "down" and s.direction == "short":
            sl15 = struct_sl_from_swings(klines_m15, "short", radius_m15)
            s2 = Signal(symbol=symbol, direction="short", strategy="模板", level="4H+15M",
                        price=s.price, key_levels=s.key_levels,
                        detail=f"4H趋势向下 + 15M {s.strategy}共振；"
                               f"大级别定趋势、小级别找共振；极小止损抓大结构，目标1:2~1:5"
                               f"{fib_05_note.get('short', '')}",
                        entry_type="追踪委托", priority=1)
            s2.sl_price = sl15
            s2.tp_price = tp_from_rr(s.price, sl15, "short", rr_max)
            s2.rr_target = rr_max
            out.append(s2)

    # --- 1H 级结构信号（BOS/MSS/归汤）统一盈亏比目标 ---
    # 止盈目标按区间上限 rr_max 计算（对应手册 4.4：到TP止盈），目标盈亏比须落在 1:2~1:5
    for s in out:
        if s.direction in ("long", "short") and s.sl_price and s.price > 0:
            s.tp_price = tp_from_rr(s.price, s.sl_price, s.direction, rr_max)
            s.rr_target = rr_max

    # --- 等待信号：无入场信号时透出观望状态（不触发开仓，仅提醒人工等待） ---
    if not out:
        trend_cn = {"up": "偏多", "down": "偏空", "flat": "震荡"}.get(h1_trend, "震荡")
        m15_note = ""
        if m15_fvg:
            m15_note = f"；15M 存在FVG({'看涨' if m15_fvg == 'bull' else '看跌'})，观察回踩确认"
        wait = Signal(symbol=symbol, direction="wait", strategy="等待", level="1H+15M",
                      price=klines_h1[-1].close if klines_h1 else 0.0,
                      key_levels=[lv["price"] for lv in h1_levels[:2]],
                      detail=f"当前无入场信号，等待状态；1H趋势{trend_cn}"
                             f"{'，等待15M结构确认' if m15_note else '，等待假突破/结构破坏出现明确方向'}"
                             f"{m15_note}；入场须满足目标盈亏比1:2~1:5",
                      priority=99)
        out.append(wait)

    # push_structure_signals=false 时：BOS/MSS/归汤 结构信号仅作标记，
    # 不独立推送、不进模拟开单流程（保底/模板/等待不受影响）
    if not cfg.get("push_structure_signals", True):
        out = [s for s in out if s.strategy not in ("BOS", "MSS", "归汤")]

    # 附：1H 趋势状态（不推送，供日志）
    log.debug("[%s] 引擎扫描完成：%d 个信号 %s", symbol, len(out),
              [(f"{s.strategy}/{s.level}/{s.direction}@{s.price:.2f}") for s in out])
    return out
