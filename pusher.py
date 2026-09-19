# -*- coding: utf-8 -*-
"""
查尔斯信号监控系统 · 企业微信机器人推送
"""
import logging
import os
import time
import requests
import yaml

log = logging.getLogger(__name__)

# 统一使用北京时间
os.environ.setdefault("TZ", "Asia/Shanghai")
time.tzset()

TIMEOUT = 10


def load_webhook(config_path: str = "config.yaml") -> str:
    """从 config.yaml 或环境变量 WECOM_WEBHOOK 读取 webhook"""
    env = os.environ.get("WECOM_WEBHOOK", "").strip()
    if env:
        return env
    with open(config_path, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    return (cfg.get("wecom_webhook") or "").strip()


def send_wecom(webhook: str, content: str, msgtype: str = "markdown") -> bool:
    """发送企业微信消息。content 为 markdown 或 text 内容。"""
    if not webhook:
        raise ValueError("未配置企业微信 webhook，请在 config.yaml 填入或设置环境变量 WECOM_WEBHOOK")
    payload = {msgtype: {"content": content}}
    r = requests.post(webhook, json={"msgtype": msgtype, **payload}, timeout=TIMEOUT)
    r.raise_for_status()
    data = r.json()
    if data.get("errcode") != 0:
        raise RuntimeError(f"企业微信推送失败: {data}")
    return True


def send_test(webhook: str) -> None:
    """发送测试消息"""
    send_wecom(webhook, "✅ 查尔斯信号监控系统已启动！\n> 监控标的：BTC / 黄金(PAXG)\n> 策略：保底 / 归汤 / 模板\n> 信号命中后将实时推送提醒")


def format_signal(sig, extra: str = "") -> str:
    """把 Signal 对象格式化为企业微信 markdown 消息"""
    dir_cn = "📈 看多 (long)" if sig.direction == "long" else "📉 看空 (short)"
    if sig.direction not in ("long", "short"):
        dir_cn = "🔍 关注"
    key_lines = ""
    if sig.key_levels:
        kls = " / ".join(f"**{k:.2f}**" for k in sig.key_levels[:4])
        key_lines = f"> 关键位：{kls}\n"
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    lines = [
        f"**{sig.symbol} {dir_cn}**",
        f"> 信号时间：{ts}（北京时间）",
        f"> 策略：{sig.strategy}（{sig.level}）",
    ]
    if getattr(sig, "entry_type", ""):
        lines.append(f"> 入场方式：{sig.entry_type}")
    lines.append(f"> 现价：**{sig.price:.2f}**")
    if extra:
        lines.append(f"> {extra}")
    if key_lines:
        lines.append(key_lines.rstrip("\n"))
    lines.append(f"> 说明：{sig.detail}")
    lines.append("> ⚠️ 规则化信号仅作提醒，实盘请人工复核")
    return "\n".join(lines)


def _calc_rr(pos: dict) -> float:
    """计算盈亏比 = 止盈距离 / 止损距离（按方向取正距离）"""
    direction = pos.get("direction", "long")
    if direction == "short":
        tp_dist = pos["entry"] - pos["tp"]
        sl_dist = pos["sl"] - pos["entry"]
    else:
        tp_dist = pos["tp"] - pos["entry"]
        sl_dist = pos["entry"] - pos["sl"]
    if sl_dist <= 0:
        return 0.0
    return tp_dist / sl_dist


def _fmt_duration(seconds: float) -> str:
    """持仓时长格式化：秒 → 分 → 小时 → 天"""
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}秒"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}分钟"
    hours = minutes // 60
    rem_min = minutes % 60
    if hours < 24:
        return f"{hours}小时{rem_min}分" if rem_min else f"{hours}小时"
    days = hours // 24
    rem_h = hours % 24
    return f"{days}天{rem_h}小时" if rem_h else f"{days}天"


def format_paper_open(pos: dict, balance: float) -> str:
    """模拟开仓消息"""
    dir_cn = "📈 看多 (long)" if pos["direction"] == "long" else "📉 看空 (short)"
    return "\n".join([
        f"🟢 **模拟开仓 · {pos['name']}**",
        f"> 开仓时间：{pos.get('open_time') or time.strftime('%Y-%m-%d %H:%M:%S')}（北京时间）",
        f"> 方向：{dir_cn}",
        f"> 策略：{pos['strategy']}（{pos['level']}）",
        f"> 入场方式：{pos.get('entry_type') or '市价委托'}",
        f"> 入场：**{pos['entry']:.2f}**",
        f"> 保证金：{pos.get('margin', 0):.2f} USDT @ {pos.get('leverage', 100)}x",
        f"> 止损：**{pos['sl']:.2f}**",
        f"> 止盈：**{pos['tp']:.2f}**",
        "> ⚠️ 模拟单仅作练习记录，不涉及真实资金",
    ])


def format_paper_close(kind: str, trade: dict, balance: float) -> str:
    """模拟平仓消息。kind: TP/SL/TIMEOUT/REVERSE"""
    kind_cn = {"TP": "止盈", "SL": "止损", "TIMEOUT": "超时强平", "REVERSE": "反向平仓", "LIQ": "爆仓",
               "VOL_TP": "出量止盈", "TREND_EXIT": "趋势转换出场"}.get(kind, kind)
    dir_cn = "📈 多单" if trade["direction"] == "long" else "📉 空单"
    arrow = "+" if trade["pnl"] >= 0 else ""
    duration = _fmt_duration(trade.get("exit_ts", 0) - trade.get("open_ts", trade.get("exit_ts", 0)))
    return "\n".join([
        f"🔴 **模拟平仓 · {trade['name']}**",
        f"> 平仓时间：{trade.get('exit_time') or time.strftime('%Y-%m-%d %H:%M:%S')}（北京时间）",
        f"> 原因：{kind_cn}（{dir_cn} {trade['strategy']}）",
        f"> 持仓时长：{duration}",
        f"> 入场 {trade['entry']:.2f} → 出场 **{trade['exit']:.2f}**",
        f"> 盈亏：**{arrow}{trade['pnl']:.2f} USDT**（{arrow}{trade['pnl_pct']:.2f}%）",
        f"> 模拟余额：**{balance:,.2f} USDT**",
    ])


def format_paper_status(state: dict) -> str:
    """模拟账户状态（附在信号推送末尾，当存在持仓时）"""
    stats = state["stats"]
    now_str = time.strftime("%Y-%m-%d %H:%M:%S")
    lines = [
        f"📊 **模拟账户**（{now_str} 北京时间）：余额 **{state['balance']:,.2f} USDT**",
        f"> 累计盈亏：{stats['pnl']:+.2f}（胜 {stats['wins']} / 负 {stats['losses']}）",
    ]
    for sym, pos in state["open_positions"].items():
        dir_cn = "多" if pos["direction"] == "long" else "空"
        lines.append(f"> 持仓：{pos['name']} {dir_cn} @ {pos['entry']:.2f}（{pos['strategy']}，开仓 {pos.get('open_time', '-')}）")
    return "\n".join(lines)


if __name__ == "__main__":
    wh = load_webhook()
    print("webhook:", (wh[:40] + "..." if wh else "未配置"))
    if wh:
        send_test(wh)
        print("测试消息已发送")
