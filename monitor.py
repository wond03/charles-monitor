# -*- coding: utf-8 -*-
"""
查尔斯信号监控系统 · 主程序
每 N 秒扫描一次 BTC / 黄金(PAXG代理) 的 1H+15M+4H 数据，
按查尔斯策略（保底/归汤/模板）规则检测信号，命中后：
  1. 推送企业微信提醒
  2. 自动开模拟单（paper trading，不涉及真实资金）
     - 已有反向持仓时先平仓反手
     - 持仓实时检查止损/止盈/超时，触发即平仓并推送结果

用法：
  python monitor.py            # 前台运行
  python monitor.py --once     # 只扫描一次（便于测试）
"""
import argparse
import json
import logging
import os
import subprocess
import sys
import time

import yaml

# 统一使用北京时间（GitHub runner 默认 UTC）
os.environ.setdefault("TZ", "Asia/Shanghai")
time.tzset()

from datafeed import fetch_klines, last_price, pct_change
from pusher import (load_webhook, send_wecom, send_test, format_signal,
                    format_paper_open, format_paper_close, format_paper_status)
from signal_engine import to_klines, scan_symbol
import paper_trader

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("monitor")


def load_config(path: str = "config.yaml") -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def symbol_signals(sym_cfg: dict, eng_cfg: dict, webhook: str) -> list:
    """扫描单个标的，返回 (signal, extra, price) 列表"""
    results = []
    name = sym_cfg["name"]
    inst = sym_cfg["inst"]
    # 数据源优先级由 config 中 exchange 字段指定（逗号分隔，首个为主源）
    exch = str(sym_cfg.get("exchange", "gate-futures,gate,okx,binance"))
    sources = tuple(s.strip() for s in exch.split(",") if s.strip())
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
    for s in sigs:
        results.append((s, extra, price))
    return results


def commit_paper_state(state_file: str, extra_files: list = None) -> None:
    """在 Git 环境（GitHub Actions）中把模拟账户状态、冷却状态提交回仓库。
    本地运行没有仓库/权限时静默跳过。"""
    files = [state_file] + (extra_files or [])
    try:
        subprocess.run(["git", "config", "user.name", "charles-monitor[bot]"],
                       check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "actions@users.noreply.github.com"],
                       check=True, capture_output=True)
        for f in files:
            if os.path.exists(f):
                subprocess.run(["git", "add", f], check=True, capture_output=True)
        r = subprocess.run(["git", "commit", "-m", "paper: update simulated account"],
                           capture_output=True)
        if r.returncode == 0:
            subprocess.run(["git", "push"], check=True, capture_output=True)
            log.info("状态文件已提交并推送")
        else:
            log.info("状态文件无变化，跳过提交")
    except Exception as e:  # noqa: BLE001
        log.info("状态文件提交跳过（非 Git 环境）: %s", e)


def load_cooldown(path: str) -> dict:
    """加载持久化冷却记录（GitHub Actions 每次运行都是新进程，须落盘跨运行生效）"""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return {}


def save_cooldown(cooldown: dict, path: str) -> None:
    """持久化冷却记录"""
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(cooldown, f, ensure_ascii=False)
    except Exception as e:  # noqa: BLE001
        log.warning("冷却状态保存失败: %s", e)


def _paper_on_signal(state: dict, sym_key: str, sig, paper_cfg: dict, webhook: str) -> None:
    """信号命中后的模拟单处理：反向持仓先平，再开新仓"""
    try:
        pos = state["open_positions"].get(sym_key)
        if pos and pos["direction"] != sig.direction:
            trade = paper_trader.close_position(state, sym_key, sig.price, "反向平仓")
            if trade:
                send_wecom(webhook, format_paper_close("REVERSE", trade, state["balance"]))
                log.info("模拟平仓(反手): %s", trade["name"])
        new_pos = paper_trader.open_position(state, sym_key, sig, paper_cfg)
        if new_pos:
            send_wecom(webhook, format_paper_open(new_pos, state["balance"]))
            log.info("模拟开仓: %s %s @%.2f",
                     new_pos["name"], new_pos["direction"], new_pos["entry"])
    except Exception as e:  # noqa: BLE001
        log.error("模拟单处理失败: %s", e)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true", help="只扫描一次")
    parser.add_argument("--config", default="config.yaml")
    args = parser.parse_args()

    cfg = load_config(args.config)
    eng_cfg = cfg["engine"]
    scan_cfg = cfg["scanner"]
    paper_cfg = cfg.get("paper_trading", {})
    paper_on = bool(paper_cfg.get("enabled", True))
    state_file = paper_cfg.get("state_file", "paper_state.json")
    webhook = load_webhook(args.config)
    if not webhook:
        log.error("请先配置企业微信 webhook：config.yaml 的 wecom_webhook 或环境变量 WECOM_WEBHOOK")
        sys.exit(1)

    if scan_cfg.get("push_test_on_start") and not args.once:
        send_test(webhook)
        log.info("启动测试消息已发送")

    # 冷却记录：{(symbol, strategy, direction, level): {"ts": last_ts, "price": last_price}}
    # 持久化到 cooldown_state.json，GitHub Actions 每次运行都是新进程，跨运行生效
    cooldown_file = "cooldown_state.json"
    cooldown = load_cooldown(cooldown_file)
    cool_sec = scan_cfg["cooldown_hours"] * 3600
    # 冷却期内价格变化超过该阈值(%) 视为新机会，允许重新推送
    price_rearm_pct = float(scan_cfg.get("cooldown_price_rearm_pct", 0.1))

    while True:
        try:
            state = paper_trader.load_state(state_file) if paper_on else {}
            prices = {}
            for key, sym_cfg in cfg["symbols"].items():
                if not sym_cfg.get("enabled"):
                    continue
                found = symbol_signals(sym_cfg, eng_cfg, webhook)
                for sig, extra, price in found:
                    prices[key] = price
                    ckey = ":".join([sig.symbol, sig.strategy, sig.direction, sig.level])
                    now = time.time()
                    rec = cooldown.get(ckey)
                    if rec:
                        age = now - rec.get("ts", 0)
                        last_price = rec.get("price", 0)
                        # 冷却期内：价格未明显移动则跳过；价格显著变动视为新机会放行
                        price_drift = abs(price - last_price) / last_price * 100 if last_price else 0
                        if age < cool_sec and price_drift < price_rearm_pct:
                            continue
                    cooldown[ckey] = {"ts": now, "price": price}
                    try:
                        send_wecom(webhook, format_signal(sig, extra))
                        log.info("推送信号: %s %s %s %s @%.2f",
                                 sig.symbol, sig.strategy, sig.direction, sig.level, sig.price)
                    except Exception as e:  # noqa: BLE001
                        log.error("推送失败: %s", e)
                    # 模拟单：信号命中即自动开仓（反向持仓先平）
                    if paper_on and paper_cfg:
                        _paper_on_signal(state, key, sig, paper_cfg, webhook)

            if paper_on and paper_cfg:
                # 管理持仓：止盈/止损/超时，触发即推送
                for kind, trade in paper_trader.manage_positions(state, prices, paper_cfg):
                    try:
                        send_wecom(webhook, format_paper_close(kind, trade, state["balance"]))
                        log.info("模拟平仓: %s %s %s (%s)", trade["name"], kind, trade["pnl"], trade["reason"])
                    except Exception as e:  # noqa: BLE001
                        log.error("平仓推送失败: %s", e)
                paper_trader.save_state(state, state_file)
                commit_paper_state(state_file, extra_files=[cooldown_file])
            else:
                save_cooldown(cooldown, cooldown_file)
                commit_paper_state(cooldown_file, extra_files=[])
            log.info("本轮扫描完成")
        except Exception as e:  # noqa: BLE001
            log.error("扫描异常: %s", e)

        if args.once:
            break
        time.sleep(scan_cfg["scan_interval_seconds"])


if __name__ == "__main__":
    main()
