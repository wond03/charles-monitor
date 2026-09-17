# -*- coding: utf-8 -*-
"""
查尔斯信号监控系统 · 企业微信机器人推送
"""
import os
import requests
import yaml

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
    lines = [
        f"**{sig.symbol} {dir_cn}**",
        f"> 策略：{sig.strategy}（{sig.level}）",
        f"> 现价：**{sig.price:.2f}**",
    ]
    if extra:
        lines.append(f"> {extra}")
    if key_lines:
        lines.append(key_lines.rstrip("\n"))
    lines.append(f"> 说明：{sig.detail}")
    lines.append("> ⚠️ 规则化信号仅作提醒，实盘请人工复核（风控→仓位→心态）")
    return "\n".join(lines)


if __name__ == "__main__":
    wh = load_webhook()
    print("webhook:", (wh[:40] + "..." if wh else "未配置"))
    if wh:
        send_test(wh)
        print("测试消息已发送")
