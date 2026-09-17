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
import logging
import os
import time

log = logging.getLogger(__name__)

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
    止损止盈按手册条件判定：优先使用 sig.sl_price（结构位）/sig.tp_price（目标位），
    无结构位或结构止损过宽（超爆仓线内）时回退固定百分比。
    """
    if sym in state["open_positions"]:
        return None
    if sig.direction not in ("long", "short"):
        return None
    balance = state["balance"]
    risk = balance * cfg["risk_per_trade_pct"] / 100.0
    sl_default, tp_default = _sl_tp(sig.price, sig.direction, cfg["sl_pct"], cfg["tp_rr"])
    sl, tp = sl_default, tp_default
    detail = sig.detail
    use_struct = bool(cfg.get("use_structure_sl_tp", True))
    if use_struct:
        s_sl = getattr(sig, "sl_price", None)
        s_tp = getattr(sig, "tp_price", None)
        if s_sl and s_tp:
            liq_dist = 100.0 / max(int(cfg.get("leverage", 100) or 1), 1)
            dist_pct = abs(sig.price - s_sl) / sig.price * 100
            if dist_pct <= liq_dist * 0.95:  # 结构止损须在爆仓线内留余量
                sl, tp = s_sl, s_tp
                detail = (detail or "") + "；止损=结构位，止盈=目标位（手册条件判定）"
            else:
                detail = (detail or "") + f"；结构止损过宽({dist_pct:.2f}%)，回退固定止损{cfg['sl_pct']}%"
    size = _size(sig.price, sl, risk)
    if size <= 0:
        return None
    min_size = float(cfg.get("min_size_usdt", 5) or 5)
    if size < min_size:
        log.info("仓位低于最小下单量(%.2f USDT)，跳过开仓 %s size=%.2f", min_size, sig.symbol, size)
        return None
    fee_taker = float(cfg.get("fee_taker", 0.0008) or 0.0008)
    fee_open = size * fee_taker  # 开仓手续费（Weex 市价单 Taker 0.08%）
    if state["balance"] < fee_open:
        log.info("模拟余额不足以支付开仓手续费(%.4f)，跳过开仓 %s", fee_open, sig.symbol)
        return None
    state["balance"] = round(state["balance"] - fee_open, 2)
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
        "initial_sl": round(sl, 2),   # 初始止损位（保本上移的基准）
        "sl_protected": False,         # 是否已上移保本
        "size": round(size, 2),
        "margin": round(margin, 4),
        "leverage": leverage,
        "risk": round(risk, 2),
        "fee_taker": fee_taker,
        "fee_open": round(fee_open, 4),
        "open_ts": now,
        "open_time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now)),
        "entry_type": getattr(sig, "entry_type", "") or "市价委托",
        "detail": detail,
    }
    state["open_positions"][sym] = pos
    log.info("模拟开仓: %s %s %s %s @%.2f sl=%.2f tp=%.2f size=%.2f %s",
             pos["name"], pos["direction"], pos["strategy"], pos["level"], pos["entry"],
             pos["sl"], pos["tp"], pos["size"], pos["detail"] or "")
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
    fee_taker = pos.get("fee_taker", 0.0008)
    fee_open = pos.get("fee_open", 0.0)
    # 平仓手续费按平仓时的名义价值计（开仓名义 × 出场/入场）
    fee_close = pos["size"] * price / pos["entry"] * fee_taker
    if realized_pnl is not None:
        pnl = realized_pnl - fee_close  # 爆仓亏光保证金 + 平仓手续费
        pnl_pct = -100.0  # 爆仓即亏光保证金
    else:
        pnl = (price - pos["entry"]) / pos["entry"] * sign * pos["size"] - fee_open - fee_close
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
        "fee_close": round(fee_close, 4),
        "fee_total": round(fee_open + fee_close, 4),
        "pnl": round(pnl, 2),
        "pnl_pct": round(pnl_pct, 3),
        "reason": reason,
    }
    state["closed_trades"].append(trade)
    state["closed_trades"] = state["closed_trades"][-100:]
    return trade


def _maybe_breakeven(pos: dict, price: float, cfg: dict) -> None:
    """保本保护（手册 4.3）：
    - 浮盈 >= 1.5 倍风险 → 止损移到入场价（保本）
    - 浮盈 >= 3 倍风险（1:3）→ 止损移到盈利位，锁 1 倍风险利润
    """
    risk = pos.get("risk", 0.0)
    if risk <= 0:
        return
    pnl = mark_price(pos, price)
    be_rr = float(cfg.get("breakeven_rr", 1.5))
    lock_rr = float(cfg.get("lock_profit_rr", 3.0))
    lock_ratio = float(cfg.get("lock_profit_ratio", 1.0))
    if not pos.get("sl_protected") and pnl >= risk * be_rr:
        pos["sl"] = pos["entry"]
        pos["sl_protected"] = True
        log.info("保本上移: %s %s 浮盈 %.2f >= %.2f×risk，止损移至入场价 %.2f",
                 pos["name"], pos["direction"], pnl, be_rr, pos["entry"])
    elif pos.get("sl_protected") and pnl >= risk * lock_rr:
        dist = abs(pos["entry"] - pos.get("initial_sl", pos["entry"]))
        if pos["direction"] == "long":
            pos["sl"] = pos["entry"] + dist * lock_ratio
        else:
            pos["sl"] = pos["entry"] - dist * lock_ratio
        log.info("锁利上移: %s %s 浮盈 %.2f >= %.2f×risk，止损移至 %.2f",
                 pos["name"], pos["direction"], pnl, lock_rr, pos["sl"])


def manage_positions(state: dict, ctxs: dict, cfg: dict) -> list:
    """
    按现价+上下文管理持仓（对应手册 4.3/4.4）：
      ctxs: {sym: {"price": 现价, "h4_trend": up/down/flat, "vol_surge": bool}}
    触发类型：
      LIQ 爆仓 / SL 止损 / TP 止盈 / TIMEOUT 超时强平 /
      VOL_TP 出量止盈（4.4 出量吃单）/ TREND_EXIT 趋势转换出场（4.4 大级别反转离场）
    prices 参数废弃，由 ctxs 承载。
    """
    events = []
    max_hold = cfg["max_hold_hours"] * 3600
    for sym, pos in list(state["open_positions"].items()):
        ctx = ctxs.get(sym, {})
        price = ctx.get("price")
        if price is None:
            continue
        now = time.time()
        lp = liq_price(pos)
        margin = pos.get("margin", 0.0)
        # 出量止盈：手册"任何情况下一旦出量就要吃单"
        if ctx.get("vol_surge"):
            ev = close_position(state, sym, price, "出量止盈")
            if ev:
                events.append(("VOL_TP", ev))
                continue
        # 趋势转换出场：手册"大级别趋势反转，直接出场"
        # 仅当 4H 趋势刚发生反转（up->down / down->up）时平掉旧方向持仓；
        # 趋势持续（含 flat）时不平仓，避免"逆势即平"导致刚开仓被秒平、止盈止损永远等不到
        h4t = ctx.get("h4_trend")
        h4_prev = ctx.get("h4_prev_trend")
        if h4_prev == "up" and h4t == "down" and pos["direction"] == "long":
            ev = close_position(state, sym, price, "趋势转换出场")
            if ev:
                events.append(("TREND_EXIT", ev))
                continue
        if h4_prev == "down" and h4t == "up" and pos["direction"] == "short":
            ev = close_position(state, sym, price, "趋势转换出场")
            if ev:
                events.append(("TREND_EXIT", ev))
                continue
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
                _maybe_breakeven(pos, price, cfg)
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
            else:
                _maybe_breakeven(pos, price, cfg)
        if sym in state["open_positions"] and now - pos["open_ts"] >= max_hold:
            ev = close_position(state, sym, price, "超时强平")
            if ev:
                events.append(("TIMEOUT", ev))
    return events
