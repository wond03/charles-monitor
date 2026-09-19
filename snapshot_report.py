#!/usr/bin/env python3
"""生成可对话报告快照（Markdown）：账户状态 + 实时行情 + 当前持仓 + 周/月报摘要。
供企微智能机器人知识集同步使用。
用法: python snapshot_report.py [--out snapshot.md]
"""
import argparse
import json
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

REPORT_DIR = Path("/home/marvis/Marvis/User/oAN1i2Wsuc3QCmguAYwGbiEeTOv8/workspace/conv_25fcb43e01c54de58ec30f74a56f1fc0/output/查尔斯信号监控系统")
if REPORT_DIR.exists():
    sys.path.insert(0, str(REPORT_DIR))

from datafeed import fetch_klines, last_price  # noqa: E402
from paper_report import build_report, load_state  # noqa: E402

SYMBOLS = {
    "btc": ("BTC_USDT", "BTC永续", ("gate-futures", "weex")),
    "gold": ("XAU_USDT", "黄金永续(XAU)", ("gate-futures", "weex")),
}


def fetch_prices():
    prices = {}
    for key, (inst, _name, sources) in SYMBOLS.items():
        try:
            klines = fetch_klines(inst, interval="1m", limit=2, sources=sources)
            prices[key] = last_price(klines)
        except Exception as e:  # noqa: BLE001
            prices[key] = None
            print(f"[warn] {key} price fetch failed: {e}", file=sys.stderr)
    return prices


def fmt_price(v):
    return "--" if v is None else f"{v:,.2f}"


def calc_unrealized(pos, price):
    if price is None:
        return None
    if pos["direction"] == "long":
        return (price - pos["entry"]) / pos["entry"] * pos["size"]
    return (pos["entry"] - price) / pos["entry"] * pos["size"]


def build_snapshot(state_path: str) -> str:
    state = load_state(state_path)
    prices = fetch_prices()
    balance = state.get("balance", 0.0)
    initial = state.get("initial_balance", 100.0)
    peak = state.get("peak_equity", balance)
    open_pos = state.get("open_positions", {})
    closed = state.get("closed_trades", [])
    stats = state.get("stats", {})

    lines = []
    lines.append(f"# 查尔斯模拟盘对话报告")
    lines.append(f"更新时间：{time.strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append("")
    lines.append("## 一、模拟账户状态")
    lines.append(f"- 账户余额：{balance:.2f} USDT（初始 {initial:.2f}）")
    lines.append(f"- 历史峰值权益：{peak:.2f} USDT")
    lines.append(f"- 已平仓笔数：{len(closed)}（胜 {stats.get('wins', 0)} / 负 {stats.get('losses', 0)}）")
    lines.append(f"- 已实现盈亏：{stats.get('pnl', 0.0):.2f} USDT")
    lines.append("")
    lines.append("## 二、实时行情")
    for key, (_inst, name, _sources) in SYMBOLS.items():
        lines.append(f"- {name}：{fmt_price(prices.get(key))} USDT")
    lines.append("")
    lines.append("## 三、当前持仓")
    if not open_pos:
        lines.append("- 当前无持仓")
    for key, pos in open_pos.items():
        p = prices.get(key)
        upnl = calc_unrealized(pos, p)
        upnl_str = "--" if upnl is None else f"{upnl:+.2f} USDT"
        pct_str = "--" if upnl is None else f"{upnl / max(pos['margin'], 1e-9) * 100:+.1f}%（按保证金）"
        lines.append(f"### {pos.get('name', key)}（{pos.get('direction', '')}）")
        lines.append(f"- 开仓价：{pos['entry']:.2f}｜止损：{pos['sl']:.2f}｜止盈：{pos['tp']:.2f}")
        lines.append(f"- 现价：{fmt_price(p)}｜浮动盈亏：{upnl_str}（{pct_str}）")
        lines.append(f"- 仓位名义：{pos.get('size', 0):.2f} USDT｜杠杆 {pos.get('leverage', 0)}x｜保证金 {pos.get('margin', 0):.2f} USDT")
        lines.append(f"- 开仓时间：{pos.get('open_time', '')}")
        lines.append(f"- 依据：{pos.get('detail', '')}")
        lines.append("")
    lines.append("## 四、周报 / 月报摘要")
    try:
        weekly = build_report(state, closed, "weekly", time.strftime("%Y-%m-%d"))
        monthly = build_report(state, closed, "monthly", time.strftime("%Y-%m-%d"))
        lines.append("### 周报")
        lines.append(weekly)
        lines.append("")
        lines.append("### 月报")
        lines.append(monthly)
    except Exception as e:  # noqa: BLE001
        lines.append(f"（报告生成失败：{e}）")
    lines.append("")
    lines.append("> 本快照由查尔斯信号监控系统自动生成，数据仅供参考，非投资建议。")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--state", default=str(HERE / "paper_state.json"))
    ap.add_argument("--out", default=str(HERE / "snapshot.md"))
    args = ap.parse_args()
    text = build_snapshot(args.state)
    Path(args.out).write_text(text, encoding="utf-8")
    print(text)
    print(f"\n[ok] snapshot written: {args.out}", file=sys.stderr)


if __name__ == "__main__":
    main()
