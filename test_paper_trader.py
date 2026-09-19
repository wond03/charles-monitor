# -*- coding: utf-8 -*-
"""paper_trader.manage_positions 回归测试（审查建议补充）
运行：python test_paper_trader.py
覆盖：SL / TP / LIQ / TIMEOUT / VOL_TP / TREND_EXIT / 保本上移 / short 分支
"""
import time
import paper_trader as pt


CFG = {
    "leverage": 100,
    "fee_taker": 0.0008,
    "cost_per_trade_usdt": 5,
    "sl_pct": 1.0,
    "tp_rr": 5.0,
    "max_hold_hours": 48,
    "use_structure_sl_tp": True,
}


def _state(balance=1000.0):
    return {
        "balance": balance,
        "initial_balance": balance,
        "peak_equity": balance,
        "open_positions": {},
        "closed_trades": [],
        "stats": {"pnl": 0.0, "wins": 0, "losses": 0},
        "consecutive_losses": 0,
        "daily_trades": {},
        "last_active_date": time.strftime("%Y-%m-%d"),
    }


def _sig(direction, price, symbol="BTC"):
    class S:
        pass
    s = S()
    s.symbol = symbol
    s.direction = direction
    s.strategy = "保底"
    s.level = "15M"
    s.price = price
    s.detail = "test"
    s.entry_type = "市价委托"
    s.sl_price = None
    s.tp_price = None
    return s


def _ctx(price, h4="flat", h4_prev="flat", vol=False):
    return {"price": price, "h4_trend": h4, "h4_prev_trend": h4_prev, "vol_surge": vol}


def _open(st, direction="long", price=100.0):
    st["balance"] = 1000.0
    return pt.open_position(st, "BTC", _sig(direction, price), CFG)


def test_long_sl():
    st = _state(); _open(st)
    ev = pt.manage_positions(st, {"BTC": _ctx(99.03)}, CFG)
    assert len(ev) == 1 and ev[0][0] == "SL", ev
    print("test_long_sl OK")


def test_long_tp():
    st = _state(); _open(st)
    pos = st["open_positions"]["BTC"]
    ev = pt.manage_positions(st, {"BTC": _ctx(pos["tp"] + 0.01)}, CFG)
    assert len(ev) == 1 and ev[0][0] == "TP", ev
    print("test_long_tp OK")


def test_short_sl_tp():
    st = _state(); _open(st, "short", 100.0)
    pos = st["open_positions"]["BTC"]
    ev = pt.manage_positions(st, {"BTC": _ctx(pos["sl"] + 0.01)}, CFG)
    assert len(ev) == 1 and ev[0][0] == "SL", ev
    st = _state(); _open(st, "short", 100.0)
    pos = st["open_positions"]["BTC"]
    ev = pt.manage_positions(st, {"BTC": _ctx(pos["tp"] - 0.01)}, CFG)
    assert len(ev) == 1 and ev[0][0] == "TP", ev
    print("test_short_sl_tp OK")


def test_liq():
    st = _state(); _open(st)
    lp = pt.liq_price(st["open_positions"]["BTC"])
    ev = pt.manage_positions(st, {"BTC": _ctx(lp)}, CFG)
    assert len(ev) == 1 and ev[0][0] == "LIQ", ev
    assert st["closed_trades"][0]["reason"] == "爆仓"
    print("test_liq OK")


def test_vol_tp():
    st = _state(); _open(st)
    pos = st["open_positions"]["BTC"]
    ev = pt.manage_positions(st, {"BTC": _ctx(pos["entry"], vol=True)}, CFG)
    assert len(ev) == 1 and ev[0][0] == "VOL_TP", ev
    print("test_vol_tp OK")


def test_trend_exit_long():
    st = _state(); _open(st, "long", 100.0)
    pos = st["open_positions"]["BTC"]
    ev = pt.manage_positions(st, {"BTC": _ctx(pos["entry"], h4="down", h4_prev="up")}, CFG)
    assert len(ev) == 1 and ev[0][0] == "TREND_EXIT", ev
    print("test_trend_exit_long OK")


def test_trend_exit_short():
    st = _state(); _open(st, "short", 100.0)
    pos = st["open_positions"]["BTC"]
    ev = pt.manage_positions(st, {"BTC": _ctx(pos["entry"], h4="up", h4_prev="down")}, CFG)
    assert len(ev) == 1 and ev[0][0] == "TREND_EXIT", ev
    print("test_trend_exit_short OK")


def test_timeout():
    st = _state(); _open(st)
    pos = st["open_positions"]["BTC"]
    pos["open_ts"] = int(time.time()) - int(CFG["max_hold_hours"] * 3600) - 1
    ev = pt.manage_positions(st, {"BTC": _ctx(pos["entry"])}, CFG)
    assert len(ev) == 1 and ev[0][0] == "TIMEOUT", ev
    print("test_timeout OK")


def test_breakeven():
    st = _state(); _open(st, "long", 100.0)
    pos = st["open_positions"]["BTC"]
    risk = pos["risk"]
    # 浮盈 >= 3R 时止损上移入场价（略超阈值，避免浮点边界）
    target = pos["entry"] + (risk * 3.0) / pos["size"] * pos["entry"] * 1.001
    ev = pt.manage_positions(st, {"BTC": _ctx(target)}, CFG)
    assert not ev, ev
    pos2 = st["open_positions"]["BTC"]
    assert pos2["breakeven_applied"] and pos2["sl"] == pos2["entry"], pos2
    print("test_breakeven OK")


def test_no_exit_when_flat():
    st = _state(); _open(st, "long", 100.0)
    pos = st["open_positions"]["BTC"]
    ev = pt.manage_positions(st, {"BTC": _ctx(pos["entry"])}, CFG)
    assert not ev and "BTC" in st["open_positions"], ev
    print("test_no_exit_when_flat OK")


def test_daily_filter():
    # 单日过滤：当日开仓数达上限后不再开仓
    st = _state()
    today = time.strftime("%Y-%m-%d")
    st["daily_trades"][today] = 10
    p = pt.open_position(st, "BTC", _sig("long", 100.0), CFG)
    assert p is None, "单日达上限应拒绝开仓"
    # 未达上限时正常开仓并计数
    st2 = _state()
    p2 = pt.open_position(st2, "BTC", _sig("long", 100.0), CFG)
    assert p2 is not None and st2["daily_trades"][today] == 1, st2
    print("test_daily_filter OK")


def test_consecutive_losses_filter():
    # 连亏过滤：连亏达3单暂停开仓；盈利清零；下个自然日自动重置
    st = _state()
    st["consecutive_losses"] = 3
    p = pt.open_position(st, "BTC", _sig("long", 100.0), CFG)
    assert p is None, "连亏达上限应暂停开仓"
    st["consecutive_losses"] = 2
    p = pt.open_position(st, "BTC", _sig("long", 100.0), CFG)
    assert p is not None, "连亏2单未达上限可开仓"
    # 盈利单清零连亏计数
    pos = st["open_positions"]["BTC"]
    ev = pt.manage_positions(st, {"BTC": _ctx(pos["tp"] + 0.01)}, CFG)
    assert ev and ev[0][0] == "TP" and st["consecutive_losses"] == 0, (ev, st)
    # 亏损单累加连亏
    st2 = _state(); _open(st2, "long", 100.0)
    pos2 = st2["open_positions"]["BTC"]
    ev2 = pt.manage_positions(st2, {"BTC": _ctx(pos2["sl"] - 0.01)}, CFG)
    assert ev2 and ev2[0][0] == "SL" and st2["consecutive_losses"] == 1, (ev2, st2)
    # 自然日切换自动重置：last_active_date 跨天后连亏清零
    st3 = _state(); st3["consecutive_losses"] = 3
    st3["last_active_date"] = "2000-01-01"
    pt._rollover_daily(st3)
    assert st3["consecutive_losses"] == 0 and st3["last_active_date"] == time.strftime("%Y-%m-%d"), st3
    # 同日调用无副作用
    st3["consecutive_losses"] = 2
    pt._rollover_daily(st3)
    assert st3["consecutive_losses"] == 2, st3
    print("test_consecutive_losses_filter OK")


if __name__ == "__main__":
    test_long_sl()
    test_long_tp()
    test_short_sl_tp()
    test_liq()
    test_vol_tp()
    test_trend_exit_long()
    test_trend_exit_short()
    test_timeout()
    test_breakeven()
    test_no_exit_when_flat()
    test_daily_filter()
    test_consecutive_losses_filter()
    print("\n全部回归测试通过")
