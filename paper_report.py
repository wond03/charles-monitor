# -*- coding: utf-8 -*-
"""
查尔斯信号监控系统 · 模拟盘周报/月报生成器
从 paper_state.json 读取模拟账户数据，按周/月/全部生成统计报告（Markdown）。

用法：
  python paper_report.py --period weekly   --state paper_state.json --output 周报.md
  python paper_report.py --period monthly  --state paper_state.json --output 月报.md
  python paper_report.py --period all      --state paper_state.json --output 总览.md
  python paper_report.py --period weekly --image  # 生成图表图片并推送到企业微信（webhook 取 --webhook > 环境变量 REPORT_WECOM_WEBHOOK > config.yaml report_wecom_webhook）

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

import requests
import yaml

# 统一北京时间
os.environ.setdefault("TZ", "Asia/Shanghai")
time.tzset()

PERIOD_HOURS = {"weekly": 7 * 24, "monthly": 30 * 24, "all": None}
MAX_PUSH_BYTES = 4000  # 企微 markdown 上限 4096，留余量

# ---------------- Pillow 图表 ----------------
try:
    from PIL import Image, ImageDraw, ImageFont
    _HAS_PIL = True
except Exception:  # noqa: BLE001
    _HAS_PIL = False

_FONT_CANDIDATES = [
    "fonts/NotoSansCJKsc-Regular.otf",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
]


def _find_font(size: int):
    """查找可用的中文字体，找不到退回 DejaVu"""
    for p in _FONT_CANDIDATES:
        if os.path.exists(p):
            try:
                return ImageFont.truetype(p, size)
            except Exception:  # noqa: BLE001
                continue
    return ImageFont.load_default()


def build_chart_image(state: dict, trades: list, period: str, now_str: str, out_path: str):
    """用 Pillow 生成一页综合图表 PNG（资金曲线/盈亏分布/策略分布/持仓）"""
    if not _HAS_PIL:
        raise RuntimeError("缺少 Pillow，请先 pip install pillow")

    W, H = 1200, 1560
    img = Image.new("RGB", (W, H), "white")
    d = ImageDraw.Draw(img)
    font_title = _find_font(44)
    font_h1 = _find_font(30)
    font_h2 = _find_font(24)
    font_n = _find_font(26)
    font_s = _find_font(22)

    initial = state.get("initial_balance", 100.0)
    balance = state.get("balance", initial)
    total_pnl = state.get("stats", {}).get("pnl", balance - initial)
    n = len(trades)
    wins = sum(1 for t in trades if t.get("pnl", 0) >= 0)
    win_rate = wins / n * 100 if n else 0.0
    profits = [t["pnl"] for t in trades if t.get("pnl", 0) > 0]
    losses_amt = [t["pnl"] for t in trades if t.get("pnl", 0) < 0]
    avg_win = sum(profits) / len(profits) if profits else 0.0
    avg_loss = abs(sum(losses_amt) / len(losses_amt)) if losses_amt else 0.0
    rr = avg_win / avg_loss if avg_loss > 0 else 0.0
    mdd = max_drawdown_from_trades(trades, initial)
    ret_pct = (balance - initial) / initial * 100 if initial else 0.0
    period_cn = {"weekly": "周报", "monthly": "月报", "all": "总览"}[period]

    y = 30
    d.text((40, y), f"模拟盘{period_cn}  {now_str}", font=font_title, fill="#1f2937")
    y += 70

    # 指标卡片（5列2行）
    cards = [
        ("当前余额", f"{balance:,.2f} USDT", "#2563eb"),
        ("累计盈亏", f"{total_pnl:+.2f} ({ret_pct:+.2f}%)", "#16a34a" if total_pnl >= 0 else "#dc2626"),
        ("最大回撤", f"{mdd:.2f}%", "#dc2626"),
        ("胜率", f"{win_rate:.1f}%", "#7c3aed"),
        ("盈亏比", f"{rr:.2f}", "#ea580c"),
    ]
    cw, ch = (W - 40 - 4 * 20) // 5, 92
    for i, (k, v, color) in enumerate(cards):
        x = 40 + i * (cw + 20)
        d.rounded_rectangle((x, y, x + cw, y + ch), radius=12, fill="#f8fafc", outline="#e2e8f0", width=2)
        d.text((x + 16, y + 16), k, font=font_s, fill="#64748b")
        d.text((x + 16, y + 44), v, font=font_h2, fill=color)
    y += ch + 30

    # 资金曲线
    d.text((40, y), "权益曲线（按平仓顺序）", font=font_h1, fill="#1f2937")
    y += 52
    chart_x, chart_y, chart_w, chart_h = 70, y, W - 140, 240
    d.rectangle((chart_x, chart_y, chart_x + chart_w, chart_y + chart_h), outline="#cbd5e1", width=2)
    # 网格线
    for i in range(1, 5):
        gy = chart_y + chart_h * i // 5
        d.line((chart_x, gy, chart_x + chart_w, gy), fill="#f1f5f9", width=1)
    pts = [(chart_x, chart_y + chart_h)]
    if trades:
        bal = initial
        cum = [initial]
        for t in trades:
            bal += t.get("pnl", 0.0)
            cum.append(bal)
        lo, hi = min(cum + [initial]), max(cum + [initial])
        if hi == lo:
            hi, lo = lo + 1, lo - 1
        def px(i_):
            return chart_x + chart_w * i_ / (len(cum) - 1) if len(cum) > 1 else chart_x
        def py(v_):
            return chart_y + chart_h * (1 - (v_ - lo) / (hi - lo))
        pts = [(px(i), py(v)) for i, v in enumerate(cum)]
        for i in range(1, len(pts)):
            color = "#16a34a" if cum[i] >= cum[i - 1] else "#dc2626"
            d.line((pts[i - 1], pts[i]), fill=color, width=4)
        # 起点终点标注
        d.ellipse((pts[0][0] - 6, pts[0][1] - 6, pts[0][0] + 6, pts[0][1] + 6), fill="#2563eb")
        d.ellipse((pts[-1][0] - 6, pts[-1][1] - 6, pts[-1][0] + 6, pts[-1][1] + 6), fill="#2563eb")
        d.text((pts[-1][0] - 60, pts[-1][1] - 34), f"{cum[-1]:.2f}", font=font_s, fill="#2563eb")
    else:
        d.line((pts[0], (chart_x + chart_w, pts[0][1])), fill="#94a3b8", width=3)
        d.text((chart_x + chart_w // 2 - 70, chart_y + chart_h // 2 - 12), "暂无平仓记录", font=font_n, fill="#94a3b8")
    y += chart_h + 36

    # 盈亏分布柱状图
    if trades:
        d.text((40, y), "每笔盈亏（USDT）", font=font_h1, fill="#1f2937")
        y += 52
        bx, by, bw, bh = 70, y, W - 140, 220
        pnls = [t.get("pnl", 0.0) for t in trades]
        lo, hi = min(pnls + [0]), max(pnls + [0])
        if hi == lo:
            hi, lo = lo + 1, lo - 1
        zero_y = by + bh * (hi / (hi - lo)) if hi != lo else by + bh // 2
        d.line((bx, zero_y, bx + bw, zero_y), fill="#94a3b8", width=2)
        slot = bw / max(len(pnls), 1)
        for i, v in enumerate(pnls[-20:]):
            h_ = max(2, bh * abs(v) / (hi - lo))
            x0 = bx + i * slot + slot * 0.25
            x1 = x0 + slot * 0.5
            if v >= 0:
                d.rectangle((x0, zero_y - h_, x1, zero_y), fill="#16a34a")
            else:
                d.rectangle((x0, zero_y, x1, zero_y + h_), fill="#dc2626")
        d.text((bx, by - 26), f"最高 +{hi:.2f} / 最低 {lo:.2f}", font=font_s, fill="#64748b")
        y += bh + 36

    # 策略分布
    by_strategy = stat_by(trades, "strategy")
    if by_strategy:
        d.text((40, y), "按策略表现", font=font_h1, fill="#1f2937")
        y += 50
        for k, g in list(by_strategy.items())[:6]:
            label = f"{k}  {g['n']}笔  胜率{g['win_rate']:.0f}%  {g['pnl']:+.2f} USDT"
            d.text((60, y), label, font=font_n, fill="#334155")
            y += 40

    # 平仓原因
    by_reason = stat_by(trades, "reason")
    if by_reason:
        reason_cn = {"TP": "止盈", "SL": "止损", "TIMEOUT": "超时强平", "REVERSE": "反向平仓", "LIQ": "爆仓"}
        d.text((40, y), "平仓原因", font=font_h1, fill="#1f2937")
        y += 50
        for k, g in by_reason.items():
            d.text((60, y), f"{reason_cn.get(k, k)}  {g['n']}笔", font=font_n, fill="#334155")
            y += 40

    d.text((40, H - 56), "⚠️ 模拟单仅作练习记录，不涉及真实资金", font=font_s, fill="#94a3b8")
    img.save(out_path, "PNG")
    return out_path


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

    return "\n".join(L)


def load_webhook(cli_webhook: str = "", config_path: str = "config.yaml") -> str:
    """报告推送 webhook：CLI 参数 > 环境变量 REPORT_WECOM_WEBHOOK > config.yaml report_wecom_webhook"""
    if cli_webhook.strip():
        return cli_webhook.strip()
    env = os.environ.get("REPORT_WECOM_WEBHOOK", "").strip()
    if env:
        return env
    if os.path.exists(config_path):
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                cfg = yaml.safe_load(f)
            return (cfg.get("report_wecom_webhook") or "").strip()
        except Exception:  # noqa: BLE001
            pass
    return ""


def build_push_text(state: dict, trades: list, period: str, now_str: str) -> str:
    """生成企业微信 markdown 推送文本（企微不支持表格，用简洁行；超长截断）"""
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
        f"## 📊 模拟盘{period_cn}（{now_str}）",
        f"> 余额：**{balance:,.2f} USDT**",
        f"> 累计盈亏：**{total_pnl:+.2f} USDT**（{ret_pct:+.2f}%）",
        f"> 最大回撤：{mdd:.2f}%",
        "",
        f"**交易统计**：{n} 笔 | 胜 {wins} / 负 {losses}",
        f"> 胜率：**{win_rate:.1f}%**",
        f"> 平均盈利：{avg_win:+.2f} | 平均亏损：{avg_loss:.2f}",
        f"> 盈亏比：**{rr:.2f}**",
        f"> 单笔期望：{exp:+.2f} USDT",
        f"> 区间盈亏：{sum(t.get('pnl', 0) for t in trades):+.2f} USDT",
        "",
    ]
    if n == 0:
        L.append("📭 该区间暂无已平仓交易。")
    else:
        by_strategy = stat_by(trades, "strategy")
        if by_strategy:
            top = list(by_strategy.items())[:5]
            L.append("**按策略**：")
            for k, g in top:
                L.append(f"> {k}：{g['n']}笔 胜率{g['win_rate']:.0f}% 盈亏{g['pnl']:+.2f}")
            L.append("")
        by_reason = stat_by(trades, "reason")
        if by_reason:
            reason_cn = {"TP": "止盈", "SL": "止损", "TIMEOUT": "超时强平", "REVERSE": "反向平仓", "LIQ": "爆仓"}
            parts = " | ".join(f"{reason_cn.get(k, k)} {g['n']}笔" for k, g in list(by_reason.items())[:5])
            L.append(f"**平仓原因**：{parts}")
            L.append("")

    L.append("> ⚠️ 模拟单仅作练习记录，不涉及真实资金")

    text = "\n".join(L)
    # 企微 markdown 上限 4096 字节，超出截断
    while len(text.encode("utf-8")) > MAX_PUSH_BYTES:
        cut = int(len(text) * MAX_PUSH_BYTES / len(text.encode("utf-8")))
        text = text[:cut].rsplit("\n", 1)[0]
    return text


def send_wecom(webhook: str, content: str) -> bool:
    """发送企业微信 markdown 消息"""
    if not webhook:
        raise ValueError("未配置报告推送 webhook，请用 --webhook 传参、设置 REPORT_WECOM_WEBHOOK 或在 config.yaml 配置 report_wecom_webhook")
    payload = {"msgtype": "markdown", "markdown": {"content": content}}
    r = requests.post(webhook, json=payload, timeout=10)
    r.raise_for_status()
    data = r.json()
    if data.get("errcode") != 0:
        raise RuntimeError(f"企业微信推送失败: {data}")
    return True


def send_wecom_image(webhook: str, image_path: str) -> bool:
    """上传图片文件并发送企业微信 file 消息（企微 webhook 上传仅 file 类型稳定可用）"""
    if not webhook:
        raise ValueError("未配置报告推送 webhook")
    key = webhook.split("key=", 1)[-1]
    up_url = f"https://qyapi.weixin.qq.com/cgi-bin/webhook/upload_media?key={key}&type=file"
    with open(image_path, "rb") as f:
        files = {"media": (os.path.basename(image_path), f, "image/png")}
        r = requests.post(up_url, files=files, timeout=20)
    r.raise_for_status()
    up = r.json()
    if up.get("errcode") != 0:
        raise RuntimeError(f"企业微信图片上传失败: {up}")
    media_id = up["media_id"]
    payload = {"msgtype": "file", "file": {"media_id": media_id}}
    r = requests.post(webhook, json=payload, timeout=10)
    r.raise_for_status()
    data = r.json()
    if data.get("errcode") != 0:
        raise RuntimeError(f"企业微信图片推送失败: {data}")
    return True


def main():
    ap = argparse.ArgumentParser(description="模拟盘周报/月报生成器")
    ap.add_argument("--period", choices=["weekly", "monthly", "all"], default="weekly")
    ap.add_argument("--state", default="paper_state.json")
    ap.add_argument("--output", default="")
    ap.add_argument("--image", action="store_true", help="生成图表图片并推送图片消息（需 Pillow）")
    ap.add_argument("--webhook", default="", help="报告推送 webhook URL（优先级最高）")
    args = ap.parse_args()

    state = load_state(args.state)
    trades = filter_trades(state.get("closed_trades", []), args.period)
    now_str = time.strftime("%Y-%m-%d %H:%M")
    report = build_report(state, trades, args.period, now_str)
    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(report)
        print(f"已生成: {args.output}")
    if args.image:
        webhook = load_webhook(args.webhook)
        img_path = args.output + ".png" if args.output else f"report_{args.period}_{time.strftime('%Y%m%d')}.png"
        build_chart_image(state, trades, args.period, now_str, img_path)
        send_wecom_image(webhook, img_path)
        print(f"已生成图表并推送: {img_path}")
    if not args.output and not args.image:
        print(report)


if __name__ == "__main__":
    main()
