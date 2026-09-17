# -*- coding: utf-8 -*-
"""
查尔斯信号监控系统 · 主程序
每 N 秒扫描一次 BTC / 黄金(PAXG代理) 的 1H+15M+4H 数据，
按查尔斯策略（保底/归汤/模板）规则检测信号，命中后推送企业微信。

用法：
  python monitor.py            # 前台运行
  python monitor.py --once     # 只扫描一次（便于测试）
"""
import argparse
import logging
import os
import sys
import time

import yaml

from datafeed import fetch_klines, fetch_gold_snapshot, last_price, pct_change
from pusher import load_webhook, send_wecom, send_test, format_signal
from signal_engine import to_klines, scan_symbol

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("monitor")


def load_config(path: str = "config.yaml") -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def symbol_signals(sym_cfg: dict, eng_cfg: dict, webhook: str) -> list:
    """扫描单个标的，返回 (signal, extra) 列表"""
    results = []
    name = sym_cfg["name"]
    inst = sym_cfg["inst"]
    sources = ("gate", "okx", "binance")  # 数据源优先级：Gate.io 主，OKX/Binance 备
    try:
        h1 = fetch_klines(inst, "1h", eng_cfg["h1_lookback"], sources)
        m15 = fetch_klines(inst, "15m", eng_cfg["m15_lookback"], sources)
        h4 = fetch_klines(inst, "4h", eng_cfg["h4_lookback"], sources)
    except Exception as e:  # noqa: BLE001
        log.warning("[%s] 数据获取失败: %s", name, e)
        return results

    sigs = scan_symbol(to_klines(h1), to_klines(m15), to_klines(h4), name, eng_cfg)
    chg = pct_change(h1, 24)
    price = last_price(h1)
    extra = f"24h涨跌：{chg:+.2f}%"
    # 黄金附带新浪实时伦敦金价校准
    if "黄金" in name or "PAXG" in inst.upper():
        snap = fetch_gold_snapshot()
        if snap:
            extra += f"；伦敦金实时：${snap['price']:.2f}"
    for s in sigs:
        results.append((s, extra))
    return results


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true", help="只扫描一次")
    parser.add_argument("--config", default="config.yaml")
    args = parser.parse_args()

    cfg = load_config(args.config)
    eng_cfg = cfg["engine"]
    scan_cfg = cfg["scanner"]
    webhook = load_webhook(args.config)
    if not webhook:
        log.error("请先配置企业微信 webhook：config.yaml 的 wecom_webhook 或环境变量 WECOM_WEBHOOK")
        sys.exit(1)

    if scan_cfg.get("push_test_on_start") and not args.once:
        send_test(webhook)
        log.info("启动测试消息已发送")

    # 冷却记录：{(symbol, strategy, direction, level): last_ts}
    cooldown = {}
    cool_sec = scan_cfg["cooldown_hours"] * 3600

    while True:
        try:
            for key, sym_cfg in cfg["symbols"].items():
                if not sym_cfg.get("enabled"):
                    continue
                for sig, extra in symbol_signals(sym_cfg, eng_cfg, webhook):
                    ckey = (sig.symbol, sig.strategy, sig.direction, sig.level)
                    now = time.time()
                    if ckey in cooldown and now - cooldown[ckey] < cool_sec:
                        continue
                    cooldown[ckey] = now
                    try:
                        send_wecom(webhook, format_signal(sig, extra))
                        log.info("推送信号: %s %s %s %s @%.2f",
                                 sig.symbol, sig.strategy, sig.direction, sig.level, sig.price)
                    except Exception as e:  # noqa: BLE001
                        log.error("推送失败: %s", e)
            log.info("本轮扫描完成")
        except Exception as e:  # noqa: BLE001
            log.error("扫描异常: %s", e)

        if args.once:
            break
        time.sleep(scan_cfg["scan_interval_seconds"])


if __name__ == "__main__":
    main()
