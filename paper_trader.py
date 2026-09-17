# -*- coding: utf-8 -*-
"""
查尔斯信号监控系统 · 模拟交易模块（Paper Trading）
信号命中后自动开模拟仓，不涉及真实资金。

规则（对应查尔斯手册风控，可配置）：
  - 每笔风险 = 账户余额 × risk_per_trade_pct（默认 1%）
  - 杠杆 = leverage（默认 100x），保证金 = 名义仓位 / 杠杆
  - 爆仓：价格反向波动达到 100/杠杆 % 时亏光保证金强平（100x → 1%）
  - 止损 = 入场价 ± sl_pct（默认 0.8%，须小于爆仓线 1%）
  - 止盈 = 入场价 ± sl_pct × tp_rr（默认 0.8% × 2 = 1.6%）
  - 反向信号 → 平旧仓并反手开新仓
  - 超时未平 → 按市价强平（默认 24h，日内交易模式）

状态持久化：paper_state.json（本地部署写本地文件；GitHub Actions 运行结束后
由 monitor.py 提交回仓库，保证云端状态不丢）。
"""
import json
import os
import time

# 统一使用北京时间（GitHub runner 默认 UTC）
os.environ.setdefault("TZ", "Asia/Shanghai")
time.tzset()

DEFAULT_STATE = {
    "balance": 100.0,            # 当前余额(USDT)
    "initial_balance": 100.0,    # 初始资金
    "peak_equity": 100.0,        # 峰值权益（用于回撤统计）
    "open_positions": {},        # symbol -> position dict
    "closed_trades": [],         # 最近100笔已平仓记录
    "stats": {"wins": 0, "losses": 0, "pnl": 0.0},
}


def load_state(state_file: str) -> dict:
    """读取模拟账户状态，文件缺失/损坏时用默认初始状态"""
    if os.path.exists(state_file):
        try:
            with open(state_file, "r", encoding="utf-8") as f:
                st = json.load(f)
            for k, v in DEFAULT_STATE.items():
                st.setdefault(k, v)
            return st
        except Exception:  # noqa: BLE001
            pass
    return dict(DEFAULT_STATE)


def save_state(state: dict, state_file: str) -> None:
    with open(state_file, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def _sl_tp(entry: float, direction: str, sl_pct: float, tp_rr: float):
    """计算止损/止盈价格。long: 止损下方、止盈上方；short 相反"""
    sign = 1 if direction == "long" else -1
    sl = entry * (1 - sign * sl_pct / 100.0)
    tp = entry * (1 + sign * sl_pct * tp_rr / 100.0)
    return sl, tp


def _size(entry: float, sl: float, risk_usdt: float) -> float:
    """按止损距离计算名义仓位(USDT)，使止损亏损≈风险额"""
    dist = abs(entry - sl) / entry
    if dist <= 0:
        return 0.0
    return risk_usdt / dist


def open_position(state: dict, sym: str, sig, cfg: dict):
    """
    开模拟仓。返回 position dict；已有持仓或参数异常返回 None。
    sig 需含 symbol/direction/strategy/level/price/detail。
    """
    if sym in state["open_positions"]:
        return None
    balance = state["balance"]
    risk = balance * cfg["risk_per_trade_pct"] / 100.0
    sl, tp = _sl_tp(sig.price, sig.direction, cfg["sl_pct"], cfg["tp_rr"])
    size = _size(sig.price, sl, risk)
    if size <= 0:
        return None
    now = int(time.time())
    leverage = int(cfg.get("leverage", 100) or 1)
    margin = size / leverage
    pos = {
        "symbol": sym,
        "name": sig.symbol,
        "direction": sig.direction,
        "strategy": sig.strategy,
        "level": sig.level,
        "entry": round(sig.price, 2),
        "sl": round(sl, 2),
        "tp": round(tp, 2),
        "size": round(size, 2),
        "margin": round(margin, 4),
        "leverage": leverage,
        "risk": round(risk, 2),
        "open_ts": now,
        "open_time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now)),
        "detail": sig.detail,
    }
    state["open_positions"][sym] = pos
    return pos


def mark_price(pos: dict, price: float) -> float:
    """按现价估算浮动盈亏 USDT"""
    sign = 1 if pos["direction"] == "long" else -1
    return (price - pos["entry"]) / pos["entry"] * sign * pos["size"]


def liq_price(pos: dict) -> float:
    """爆仓价：价格反向波动 100/杠杆 % 时亏光保证金"""
    dist = 100.0 / pos.get("leverage", 100)
    if pos["direction"] == "long":
        return pos["entry"] * (1 - dist / 100.0)
    return pos["entry"] * (1 + dist / 100.0)


def close_position(state: dict, sym: str, price: float, reason: str, realized_pnl=None):
    """平仓，返回平仓记录 dict；无持仓返回 None。
    realized_pnl 非空时按指定盈亏结算（爆仓 = 亏光保证金）。"""
    pos = state["open_positions"].pop(sym, None)
    if pos is None:
        return None
    now = int(time.time())
    sign = 1 if pos["direction"] == "long" else -1
    if realized_pnl is not None:
        pnl = realized_pnl
        pnl_pct = -100.0  # 爆仓即亏光保证金
    else:
        pnl = (price - pos["entry"]) / pos["entry"] * sign * pos["size"]
        # 收益率 = 价格变动% × 杠杆
        pnl_pct = (price - pos["entry"]) / pos["entry"] * 100 * sign * pos.get("leverage", 100)
    state["balance"] = round(state["balance"] + pnl, 2)
    state["peak_equity"] = max(state["peak_equity"], state["balance"])
    if pnl >= 0:
        state["stats"]["wins"] += 1
    else:
        state["stats"]["losses"] += 1
    state["stats"]["pnl"] = round(state["balance"] - state["initial_balance"], 2)
    trade = {
        **pos,
        "exit": round(price, 2),
        "exit_ts": now,
        "exit_time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now)),
        "pnl": round(pnl, 2),
        "pnl_pct": round(pnl_pct, 3),
        "reason": reason,
    }
    state["closed_trades"].append(trade)
    state["closed_trades"] = state["closed_trades"][-100:]
    return trade


def manage_positions(state: dict, prices: dict, cfg: dict) -> list:
    """
    按现价管理持仓：止盈/止损/超时强平。
    prices: {symbol: current_price}
    返回 [(触发类型, 平仓记录), ...]
    """
    events = []
    max_hold = cfg["max_hold_hours"] * 3600
    for sym, pos in list(state["open_positions"].items()):
        price = prices.get(sym)
        if price is None:
            continue
        now = time.time()
        lp = liq_price(pos)
        margin = pos.get("margin", 0.0)
        if pos["direction"] == "long":
            if price <= lp:
                ev = close_position(state, sym, price, "爆仓", realized_pnl=-margin)
                if ev:
                    events.append(("LIQ", ev))
                    continue
            if price <= pos["sl"]:
                ev = close_position(state, sym, price, "止损")
                if ev:
                    events.append(("SL", ev))
            elif price >= pos["tp"]:
                ev = close_position(state, sym, price, "止盈")
                if ev:
                    events.append(("TP", ev))
        else:
            if price >= lp:
                ev = close_position(state, sym, price, "爆仓", realized_pnl=-margin)
                if ev:
                    events.append(("LIQ", ev))
                    continue
            if price >= pos["sl"]:
                ev = close_position(state, sym, price, "止损")
                if ev:
                    events.append(("SL", ev))
            elif price <= pos["tp"]:
                ev = close_position(state, sym, price, "止盈")
                if ev:
                    events.append(("TP", ev))
        if sym in state["open_positions"] and now - pos["open_ts"] >= max_hold:
            ev = close_position(state, sym, price, "超时强平")
            if ev:
                events.append(("TIMEOUT", ev))
    return events
