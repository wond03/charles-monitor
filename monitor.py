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

import logger

from datafeed import fetch_klines, last_price, pct_change
from pusher import (load_webhook, send_wecom, send_test, format_signal,
                    format_paper_open, format_paper_close, format_paper_status)
from signal_engine import to_klines, scan_symbol, trend_by_ma, vol_surge
import paper_trader

# 可触发严谨反手的结构确认类策略（手册 4.5 换边规则）
REVERSE_STRATEGIES = ("模板", "保底", "BOS", "MSS", "归汤")
# 级别权重：数值越大级别越高（反手要求反向信号级别 >= 持仓级别）
LEVEL_RANK = {"15M": 1, "1H": 2, "4H+15M": 3, "1H+15M": 3, "4H": 4}


def _level_rank_of(sig_or_pos) -> int:
    """取信号/持仓的级别权重；未知级别按 0 处理（不满足反手条件）"""
    lv = getattr(sig_or_pos, "level", None) or (sig_or_pos.get("level") if isinstance(sig_or_pos, dict) else None)
    return LEVEL_RANK.get(lv, 0)

log = logging.getLogger(__name__)


def load_config(path: str = "config.yaml") -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def symbol_signals(sym_cfg: dict, eng_cfg: dict, webhook: str) -> dict:
    """扫描单个标的，返回：
    {"signals": [(sig, extra, price)...], "h4_trend": str, "vol_surge": bool, "price": float}
    """
    result = {"signals": [], "h4_trend": "flat", "h4_prev_trend": "flat", "vol_surge": False, "price": 0.0}
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
        return result

    sigs = scan_symbol(to_klines(h1), to_klines(m15), to_klines(h4), name, eng_cfg)
    price = last_price(h1)
    extra = ""  # 额外说明行（当前不附加，保持消息精简）
    h1_ma = eng_cfg.get("trend_ma", 50)
    vol_mult = float(eng_cfg.get("vol_surge_mult", 2.5))
    if len(h4) > 20:
        result["h4_trend"] = trend_by_ma(to_klines(h4), min(h1_ma * 2, 60))
        # 前一状态：供趋势转换出场判定（仅当趋势刚反转时平仓，避免趋势持续时反复平仓）
        if len(h4) > 21:
            result["h4_prev_trend"] = trend_by_ma(to_klines(h4[:-1]), min(h1_ma * 2, 60))
    result["vol_surge"] = vol_surge(to_klines(h1), mult=vol_mult) or vol_surge(to_klines(m15), mult=vol_mult)
    result["price"] = price
    for s in sigs:
        result["signals"].append((s, extra, price))
    return result


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
    """信号命中后的模拟单处理（对应手册 4.5 换边规则）：
    - 无持仓：直接开新仓
    - 反向持仓：严谨反手——仅当反向信号 级别>=持仓级别 且为结构确认类策略
      （模板/保底/BOS/MSS）才平旧仓反手；否则保留旧仓，只推送信号供人工判断
    """
    try:
        pos = state["open_positions"].get(sym_key)
        if pos and pos["direction"] != sig.direction:
            same_or_higher = _level_rank_of(sig) >= _level_rank_of(pos)
            struct_confirm = getattr(sig, "strategy", "") in REVERSE_STRATEGIES
            if same_or_higher and struct_confirm:
                trade = paper_trader.close_position(state, sym_key, sig.price, "反向平仓")
                if trade:
                    try:
                        send_wecom(webhook, format_paper_close("REVERSE", trade, state["balance"]))
                    except Exception as we:  # noqa: BLE001
                        log.warning("反手平仓推送失败: %s", we)
                    log.info("模拟平仓(反手): %s", trade["name"])
            else:
                log.info("反向信号不满足反手条件(级别/策略)，保留持仓: %s %s，仅推送信号提醒",
                         pos["name"], pos["direction"])
        new_pos = paper_trader.open_position(state, sym_key, sig, paper_cfg)
        if new_pos:
            try:
                send_wecom(webhook, format_paper_open(new_pos, state["balance"]))
            except Exception as we:  # noqa: BLE001
                log.warning("开仓推送失败: %s", we)
            log.info("模拟开仓: %s %s @%.2f",
                     new_pos["name"], new_pos["direction"], new_pos["entry"])
    except Exception as e:  # noqa: BLE001
        log.error("模拟单处理失败: %s", e)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true", help="只扫描一次")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--log-level", default=None,
                        help="日志级别 DEBUG/INFO/WARNING/ERROR（默认读 CHARLES_LOG_LEVEL，再默认 INFO）")
    parser.add_argument("--no-console", action="store_true", help="关闭控制台日志输出（仅写文件）")
    args = parser.parse_args()

    logger.setup("monitor", level=getattr(logging, (args.log_level or "INFO").upper(), logging.INFO),
                 console=not args.no_console)

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

    syms_on = [f"{k}:{v.get('name')}({v.get('inst')})" for k, v in cfg["symbols"].items() if v.get("enabled")]
    log.info("启动：标的=%s，数据源=%s，扫描间隔=%ss，冷却=%sh",
             ", ".join(syms_on),
             {k: v.get("exchange") for k, v in cfg["symbols"].items() if v.get("enabled")},
             scan_cfg["scan_interval_seconds"], scan_cfg["cooldown_hours"])
    log.info("模拟盘：enabled=%s，初始余额=%s，杠杆=%sx，止损=%s%%，止盈=%sR，结构位判定=%s",
             paper_on, paper_cfg.get("initial_balance"), paper_cfg.get("leverage"),
             paper_cfg.get("sl_pct"), paper_cfg.get("tp_rr"), paper_cfg.get("use_structure_sl_tp", True))

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
        t0 = time.time()
        try:
            state = paper_trader.load_state(state_file) if paper_on else {}
            prices = {}
            ctxs = {}  # {sym: {"price", "h4_trend", "vol_surge"}} 供持仓管理做条件判定
            for key, sym_cfg in cfg["symbols"].items():
                if not sym_cfg.get("enabled"):
                    continue
                res = symbol_signals(sym_cfg, eng_cfg, webhook)
                ctxs[key] = {"price": res["price"], "h4_trend": res["h4_trend"],
                             "h4_prev_trend": res["h4_prev_trend"],
                             "vol_surge": res["vol_surge"]}
                log.debug("[%s] 扫描结果：%d 个信号，h4_trend=%s，vol_surge=%s，price=%.2f",
                          sym_cfg["name"], len(res["signals"]), res["h4_trend"],
                          res["vol_surge"], res["price"])
                for sig, extra, price in res["signals"]:
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
                            log.debug("冷却跳过：%s（age=%.0fs < %ss, drift=%.3f%%）",
                                      ckey, age, cool_sec, price_drift)
                            continue
                    cooldown[ckey] = {"ts": now, "price": price}
                    try:
                        send_wecom(webhook, format_signal(sig, extra))
                        log.info("推送信号: %s %s %s %s @%.2f",
                                 sig.symbol, sig.strategy, sig.direction, sig.level, sig.price)
                    except Exception as e:  # noqa: BLE001
                        log.error("推送失败: %s", e, exc_info=True)
                    # 模拟单：信号命中即自动开仓（反向持仓先平）
                    if paper_on and paper_cfg:
                        _paper_on_signal(state, key, sig, paper_cfg, webhook)

            if paper_on and paper_cfg:
                # 管理持仓：止盈/止损/超时/出量/趋势转换，触发即推送
                for kind, trade in paper_trader.manage_positions(state, ctxs, paper_cfg):
                    try:
                        send_wecom(webhook, format_paper_close(kind, trade, state["balance"]))
                        log.info("模拟平仓: %s %s %s (%s)", trade["name"], kind, trade["pnl"], trade["reason"])
                    except Exception as e:  # noqa: BLE001
                        log.error("平仓推送失败: %s", e, exc_info=True)
                paper_trader.save_state(state, state_file)
                save_cooldown(cooldown, cooldown_file)
                commit_paper_state(state_file, extra_files=[cooldown_file])
            else:
                save_cooldown(cooldown, cooldown_file)
                commit_paper_state(cooldown_file, extra_files=[])
            log.info("本轮扫描完成（%.1fs）", time.time() - t0)
        except Exception as e:  # noqa: BLE001
            log.error("扫描异常: %s", e, exc_info=True)

        if args.once:
            break
        time.sleep(scan_cfg["scan_interval_seconds"])


if __name__ == "__main__":
    main()
