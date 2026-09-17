# -*- coding: utf-8 -*-
"""
查尔斯信号监控系统 · 自测脚本
用真实行情（Gate.io）跑一遍信号引擎，验证数据链路与规则代码。
用法：python test_signal.py
"""
import yaml
from datafeed import fetch_klines, last_price, pct_change
from signal_engine import to_klines, scan_symbol


def main():
    with open("config.yaml", "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    eng = cfg["engine"]

    for key, sym in cfg["symbols"].items():
        if not sym.get("enabled"):
            continue
        print("=" * 60)
        print(f"[{sym['name']}] {sym['inst']}")
        try:
            h1 = fetch_klines(sym["inst"], "1h", eng["h1_lookback"])
            m15 = fetch_klines(sym["inst"], "15m", eng["m15_lookback"])
            h4 = fetch_klines(sym["inst"], "4h", eng["h4_lookback"])
            print(f"  1H x{len(h1)} 15M x{len(m15)} 4H x{len(h4)} 最新 {last_price(h1):.2f} "
                  f"(24h {pct_change(h1, 24):+.2f}%)")
            sigs = scan_symbol(to_klines(h1), to_klines(m15), to_klines(h4), sym["name"], eng)
            if sigs:
                for s in sigs:
                    print(f"  >> 信号: {s.strategy} | {s.direction} | {s.level} | {s.price:.2f} | {s.detail}")
            else:
                print("  >> 当前无信号")
        except Exception as e:  # noqa: BLE001
            print(f"  !! 失败: {e}")


if __name__ == "__main__":
    main()
