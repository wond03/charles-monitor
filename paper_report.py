# -*- coding: utf-8 -*-
"""
查尔斯信号监控系统 · 模拟盘周报/月报生成器
从 paper_state.json 读取模拟账户数据，按周/月/全部生成统计报告（Markdown）。

用法：
  python paper_report.py --period weekly   --state paper_state.json --output 周报.md
  python paper_report.py --period monthly  --state paper_state.json --output 月报.md
  python paper_report.py --period all      --state paper_state.json --output 总览.md

统计指标：
  - 概览：余额 / 累计盈亏 / 累计收益率 / 最大回撤
  - 交易：笔数 / 胜率 / 盈亏比(平均盈利÷平均亏损) / 期望值
  - 分布：按策略 / 按品种 / 按平仓原因
"""
import argparse
import json
import os
import time
from collections import defaultdict

# 统一北京时间
os.environ.setdefault("TZ", "Asia/Shanghai")
time.tzset()

PERIOD_HOURS = {"weekly": 7 * 24, "monthly": 30 * 24, "all": None}


def load_state(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def filter_trades(trades: list, period: str) -> list:
    hours = PERIOD_HOURS[period]
    if hours is None:
        return list(trades)
    now = time.time()
    cutoff = now - hours * 3600
    return [t for t in trades if t.get("exit_ts", 0) >= cutoff]


def max_drawdown_from_trades(trades: list, initial: float) -> float:
    """基于逐笔余额重建权益曲线，计算历史最大回撤(%)"""
    balance = initial
    peak = initial
    mdd = 0.0
    for t in trades:
        balance += t.get("pnl", 0.0)
        peak = max(peak, balance)
        if peak > 0:
            mdd = max(mdd, (peak - balance) / peak * 100)
    return mdd


def stat_by(trades: list, key: str):
    """按某字段分组统计：{组名: {n, wins, losses, pnl, avg, win_rate}}"""
    groups = defaultdict(lambda: {"n": 0, "wins": 0, "losses": 0, "pnl": 0.0})
    for t in trades:
        k = t.get(key, "未知")
        g = groups[k]
        g["n"] += 1
        if t.get("pnl", 0) >= 0:
            g["wins"] += 1
        else:
            g["losses"] += 1
        g["pnl"] += t.get("pnl", 0)
    for g in groups.values():
        g["avg"] = g["pnl"] / g["n"] if g["n"] else 0.0
        g["win_rate"] = g["wins"] / g["n"] * 100 if g["n"] else 0.0
    return dict(sorted(groups.items(), key=lambda x: -x[1]["pnl"]))


def build_report(state: dict, trades: list, period: str, now_str: str) -> str:
    initial = state.get("initial_balance", 100.0)
    balance = state.get("balance", initial)
    total_pnl = state.get("stats", {}).get("pnl", balance - initial)
    n = len(trades)
    wins = sum(1 for t in trades if t.get("pnl", 0) >= 0)
    losses = n - wins
    win_rate = wins / n * 100 if n else 0.0
    profits = [t["pnl"] for t in trades if t.get("pnl", 0) > 0]
    losses_amt = [t["pnl"] for t in trades if t.get("pnl", 0) < 0]
    avg_win = sum(profits) / len(profits) if profits else 0.0
    avg_loss = abs(sum(losses_amt) / len(losses_amt)) if losses_amt else 0.0
    rr = avg_win / avg_loss if avg_loss > 0 else 0.0
    exp = sum(t.get("pnl", 0) for t in trades) / n if n else 0.0
    mdd = max_drawdown_from_trades(trades, initial)
    ret_pct = (balance - initial) / initial * 100 if initial else 0.0
    period_cn = {"weekly": "周报", "monthly": "月报", "all": "总览"}[period]

    L = [
        f"# 模拟盘{period_cn}（{now_str}）",
        "",
        "> ⚠️ 模拟单仅作练习记录，不涉及真实资金",
        "",
        "## 一、账户概览",
        "",
        "| 指标 | 数值 |",
        "|------|------|",
        f"| 当前余额 | **{balance:,.2f} USDT** |",
        f"| 初始余额 | {initial:,.2f} USDT |",
        f"| 累计盈亏 | **{total_pnl:+.2f} USDT**（{ret_pct:+.2f}%） |",
        f"| 峰值权益 | {state.get('peak_equity', balance):,.2f} USDT |",
        f"| 最大回撤（区间内） | {mdd:.2f}% |",
        "",
        "## 二、交易统计",
        "",
        "| 指标 | 数值 |",
        "|------|------|",
        f"| 已平仓笔数 | {n} |",
        f"| 盈利 / 亏损 | {wins} / {losses} |",
        f"| 胜率 | **{win_rate:.1f}%** |",
        f"| 平均盈利 | {avg_win:+.2f} USDT |",
        f"| 平均亏损 | {avg_loss:.2f} USDT |",
        f"| 盈亏比 | **{rr:.2f}** |",
        f"| 单笔期望值 | {exp:+.2f} USDT |",
        f"| 区间盈亏合计 | {sum(t.get('pnl', 0) for t in trades):+.2f} USDT |",
        "",
    ]
    if n == 0:
        L += ["📭 该区间暂无已平仓交易，以下分布表为空。", ""]

    by_strategy = stat_by(trades, "strategy")
    if by_strategy:
        L += ["## 三、按策略分布", "", "| 策略 | 笔数 | 胜/负 | 胜率 | 盈亏合计 | 平均盈亏 |", "|------|-----|-------|------|---------|---------|"]
        for k, g in by_strategy.items():
            L.append(f"| {k} | {g['n']} | {g['wins']}/{g['losses']} | {g['win_rate']:.1f}% | {g['pnl']:+.2f} | {g['avg']:+.2f} |")
        L.append("")

    by_symbol = stat_by(trades, "name")
    if by_symbol:
        L += ["## 四、按品种分布", "", "| 品种 | 笔数 | 胜/负 | 胜率 | 盈亏合计 |", "|------|-----|-------|------|---------|"]
        for k, g in by_symbol.items():
            L.append(f"| {k} | {g['n']} | {g['wins']}/{g['losses']} | {g['win_rate']:.1f}% | {g['pnl']:+.2f} |")
        L.append("")

    by_reason = stat_by(trades, "reason")
    if by_reason:
        reason_cn = {"TP": "止盈", "SL": "止损", "TIMEOUT": "超时强平", "REVERSE": "反向平仓", "LIQ": "爆仓"}
        L += ["## 五、按平仓原因分布", "", "| 原因 | 笔数 | 胜/负 | 胜率 | 盈亏合计 |", "|------|-----|-------|------|---------|"]
        for k, g in by_reason.items():
            L.append(f"| {reason_cn.get(k, k)} | {g['n']} | {g['wins']}/{g['losses']} | {g['win_rate']:.1f}% | {g['pnl']:+.2f} |")
        L.append("")

    if trades:
        L += ["## 六、最近交易明细", "", "| 时间 | 品种 | 方向 | 策略 | 原因 | 入场→出场 | 盈亏 |", "|------|------|------|------|------|-----------|------|"]
        for t in reversed(trades[-10:]):
            d = "多" if t.get("direction") == "long" else "空"
            rc = {"TP": "止盈", "SL": "止损", "TIMEOUT": "超时强平", "REVERSE": "反向平仓", "LIQ": "爆仓"}.get(t.get("reason", ""), t.get("reason", ""))
            pnl = t.get("pnl", 0)
            arrow = "+" if pnl >= 0 else ""
            L.append(f"| {t.get('exit_time', '')[:16]} | {t.get('name', '')} | {d} | {t.get('strategy', '')} | {rc} | {t.get('entry', 0):.2f} → {t.get('exit', 0):.2f} | **{arrow}{pnl:.2f}** |")
        L.append("")

    if state.get("open_positions"):
        L += ["## 七、当前持仓", "", "| 品种 | 方向 | 策略 | 入场 | 止损 | 止盈 | 盈亏比 | 开仓时间 |", "|------|------|------|------|------|------|--------|---------|"]
        for sym, p in state["open_positions"].items():
            d = "多" if p.get("direction") == "long" else "空"
            sl_dist = abs(p.get("entry", 0) - p.get("sl", 0))
            tp_dist = abs(p.get("tp", 0) - p.get("entry", 0))
            rr = tp_dist / sl_dist if sl_dist else 0
            L.append(f"| {p.get('name', sym)} | {d} | {p.get('strategy', '')} | {p.get('entry', 0):.2f} | {p.get('sl', 0):.2f} | {p.get('tp', 0):.2f} | {rr:.2f} | {p.get('open_time', '')[:16]} |")
        L.append("")

    return "\n".join(L)


def main():
    ap = argparse.ArgumentParser(description="模拟盘周报/月报生成器")
    ap.add_argument("--period", choices=["weekly", "monthly", "all"], default="weekly")
    ap.add_argument("--state", default="paper_state.json")
    ap.add_argument("--output", default="")
    args = ap.parse_args()

    state = load_state(args.state)
    trades = filter_trades(state.get("closed_trades", []), args.period)
    now_str = time.strftime("%Y-%m-%d %H:%M")
    report = build_report(state, trades, args.period, now_str)
    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(report)
        print(f"已生成: {args.output}")
    else:
        print(report)


if __name__ == "__main__":
    main()
