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

# 查尔斯体系 6 类独立信号（固定展示口径；0.5回踩仅为位置筛选，不独立成类）
SIGNAL_STRATEGIES = ["BOS", "MSS", "CHoCH", "归汤", "保底", "模板"]


def bj_time_str(ts: float) -> str:
    """Unix 时间戳 -> 北京时间（%Y-%m-%d %H:%M），不依赖状态文件里可能时区错乱的字符串"""
    return time.strftime("%Y-%m-%d %H:%M", time.gmtime(ts + 8 * 3600))

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


# ---------------- fpdf2 矢量 PDF ----------------
try:
    from fpdf import FPDF as _FPDF
    _HAS_FPDF = True
except Exception:  # noqa: BLE001
    _HAS_FPDF = False

_PDF_PAGE_W, _PDF_PAGE_H = 210, 297   # A4 竖版 mm
_PDF_MARGIN = 12
_PDF_FOOTER_SAFE = 285                # y 超过则换页，避免与页脚遮挡


def _find_font_path() -> str:
    """查找可嵌入 PDF 的中文 TTF/OTF 字体文件路径"""
    for p in _FONT_CANDIDATES:
        if os.path.exists(p):
            return p
    return ""


class _ReportPDF(_FPDF):
    """A4 报表画布，自动页脚"""

    def footer(self):
        self.set_y(-8)
        self.set_font("NotoCJK", "", 8)
        self.set_text_color(148, 163, 184)
        self.cell(0, 5, "模拟单仅作练习记录，不涉及真实资金", align="C")


def build_pdf_report(state: dict, trades: list, period: str, now_str: str, out_path: str):
    """用 fpdf2 矢量排版生成综合报表 PDF：文字/表格/折线均为原生矢量，
    不经过位图，彻底解决模糊与遮挡问题。"""
    if not _HAS_FPDF:
        raise RuntimeError("缺少 fpdf2，请先 pip install fpdf2")
    font_path = _find_font_path()
    if not font_path:
        raise RuntimeError("未找到可嵌入的中文字体（NotoSansCJK / wqy 等），请将字体放入 fonts/ 目录")

    pdf = _ReportPDF(orientation="P", unit="mm", format="A4")
    pdf.set_auto_page_break(auto=False)
    pdf.add_font("NotoCJK", "", font_path)
    pdf.add_page()
    pdf.set_margin(_PDF_MARGIN)
    y = _PDF_MARGIN

    def ensure(h):
        nonlocal y
        if y + h > _PDF_FOOTER_SAFE:
            pdf.add_page()
            y = _PDF_MARGIN

    def set_fill(hexcolor):
        pdf.set_fill_color(int(hexcolor[1:3], 16), int(hexcolor[3:5], 16), int(hexcolor[5:7], 16))

    def set_text(hexcolor):
        pdf.set_text_color(int(hexcolor[1:3], 16), int(hexcolor[3:5], 16), int(hexcolor[5:7], 16))

    def section(title):
        nonlocal y
        ensure(12)
        y += 4
        pdf.set_xy(_PDF_MARGIN, y)
        pdf.set_font("NotoCJK", "", 14)
        set_text("#1f2937")
        pdf.cell(0, 8, title)
        y += 12

    period_cn = {"weekly": "周报", "monthly": "月报", "all": "总览"}[period]
    hours = PERIOD_HOURS[period]
    period_start = bj_time_str(time.time() - hours * 3600) if hours else "全部"

    # 标题
    pdf.set_xy(_PDF_MARGIN, y)
    pdf.set_font("NotoCJK", "", 19)
    set_text("#1f2937")
    pdf.cell(0, 10, f"模拟盘{period_cn}（{period_start[5:10]} ~ {now_str[5:10]}）")
    y += 13
    pdf.set_xy(_PDF_MARGIN, y)
    pdf.set_font("NotoCJK", "", 9.5)
    set_text("#64748b")
    pdf.cell(0, 6, f"统计区间（北京时间）：{period_start} ~ {now_str}")
    y += 11

    n = len(trades)
    initial = state.get("initial_balance", 100.0)
    balance = state.get("balance", initial)
    total_pnl = state.get("stats", {}).get("pnl", balance - initial)
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
    content_w = _PDF_PAGE_W - _PDF_MARGIN * 2

    # 一、账户概览
    section("一、账户概览")
    overview = [
        ("当前余额", f"{balance:,.2f} USDT", "#2563eb", None),
        ("初始余额", f"{initial:,.2f} USDT", "#64748b", None),
        ("累计盈亏", f"{total_pnl:+.2f} USDT", "#16a34a" if total_pnl >= 0 else "#dc2626", f"（{ret_pct:+.2f}%）"),
        ("峰值权益", f"{state.get('peak_equity', balance):,.2f} USDT", "#7c3aed", None),
        ("最大回撤", f"{mdd:.2f}%", "#dc2626", None),
    ]
    card_w = (content_w - 4 * 3) / 5
    card_h = 26
    ensure(card_h + 4)
    for i, (k, v, color, sub) in enumerate(overview):
        x = _PDF_MARGIN + i * (card_w + 3)
        set_fill("#f8fafc")
        pdf.rect(x, y, card_w, card_h, style="F")
        pdf.set_draw_color(226, 232, 240)
        pdf.rect(x, y, card_w, card_h, style="D")
        pdf.set_xy(x + 3, y + 2.5)
        pdf.set_font("NotoCJK", "", 8)
        set_text("#64748b")
        pdf.cell(card_w - 6, 4, k, align="C")
        pdf.set_xy(x + 3, y + 8.5)
        pdf.set_font("NotoCJK", "", 9.5)
        set_text(color)
        pdf.cell(card_w - 6, 5, v, align="C")
        if sub:
            pdf.set_xy(x + 3, y + 14.5)
            pdf.set_font("NotoCJK", "", 8)
            set_text(color)
            pdf.cell(card_w - 6, 4, sub, align="C")
    y += card_h + 4

    # 二、交易统计
    section("二、交易统计")
    stats = [
        ("已平仓笔数", f"{n}"), ("盈利/亏损", f"{wins}/{losses}"), ("胜率", f"{win_rate:.1f}%"),
        ("平均盈利", f"{avg_win:+.2f} USDT"), ("平均亏损", f"{avg_loss:.2f} USDT"),
        ("盈亏比", f"{rr:.2f}"), ("单笔期望", f"{exp:+.2f} USDT"),
        ("区间盈亏", f"{sum(t.get('pnl', 0) for t in trades):+.2f} USDT"),
    ]
    card_w2 = (content_w - 3 * 3) / 4
    card_h2 = 17
    ensure(2 * (card_h2 + 3) + 4)
    for i in range(0, 8, 4):
        for j in range(4):
            k, v = stats[i + j]
            x = _PDF_MARGIN + j * (card_w2 + 3)
            yy = y + (i // 4) * (card_h2 + 3)
            set_fill("#f8fafc")
            pdf.rect(x, yy, card_w2, card_h2, style="F")
            pdf.set_draw_color(226, 232, 240)
            pdf.rect(x, yy, card_w2, card_h2, style="D")
            pdf.set_xy(x + 3, yy + 2)
            pdf.set_font("NotoCJK", "", 7.5)
            set_text("#64748b")
            pdf.cell(card_w2 - 6, 4, k, align="C")
            pdf.set_xy(x + 3, yy + 7.5)
            pdf.set_font("NotoCJK", "", 9)
            set_text("#1f2937")
            pdf.cell(card_w2 - 6, 5, v, align="C")
    y += 2 * (card_h2 + 3) + 4

    # 三、权益曲线（矢量折线）
    section("三、权益曲线（按平仓顺序）")
    chart_w = content_w
    chart_h = 52
    ensure(chart_h + 10)
    chart_x, chart_y = _PDF_MARGIN, y
    pdf.set_draw_color(203, 213, 225)
    pdf.rect(chart_x, chart_y, chart_w, chart_h, style="D")
    for i in range(1, 5):
        gy = chart_y + chart_h * i / 5
        pdf.set_draw_color(241, 245, 249)
        pdf.line(chart_x, gy, chart_x + chart_w, gy)
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
            if cum[i] >= cum[i - 1]:
                pdf.set_draw_color(22, 163, 74)
            else:
                pdf.set_draw_color(220, 38, 38)
            pdf.line(pts[i - 1][0], pts[i - 1][1], pts[i][0], pts[i][1])
        pdf.set_fill_color(37, 99, 235)
        pdf.rect(pts[0][0] - 1.5, pts[0][1] - 1.5, 3, 3, style="F")
        pdf.rect(pts[-1][0] - 1.5, pts[-1][1] - 1.5, 3, 3, style="F")
        pdf.set_font("NotoCJK", "", 8)
        set_text("#2563eb")
        pdf.set_xy(pts[-1][0] - 20, pts[-1][1] - 6)
        pdf.cell(24, 4, f"{cum[-1]:.2f}", align="C")
        set_text("#64748b")
        pdf.set_xy(chart_x, chart_y + chart_h + 1.5)
        pdf.cell(chart_w / 2, 4, f"首笔 {bj_time_str(trades[0].get('exit_ts', 0))}")
        pdf.set_xy(chart_x + chart_w / 2, chart_y + chart_h + 1.5)
        pdf.cell(chart_w / 2, 4, f"末笔 {bj_time_str(trades[-1].get('exit_ts', 0))}", align="R")
    else:
        pdf.set_draw_color(148, 163, 184)
        pdf.line(chart_x, chart_y + chart_h / 2, chart_x + chart_w, chart_y + chart_h / 2)
        pdf.set_font("NotoCJK", "", 10)
        set_text("#94a3b8")
        pdf.set_xy(chart_x, chart_y + chart_h / 2 - 2)
        pdf.cell(chart_w, 5, "暂无平仓记录", align="C")
    y += chart_h + 8

    # 四、按策略表现（6 类信号口径）
    section("四、按策略表现（6 类信号）")
    by_strategy = stat_by_strategy(trades)
    for k, g in by_strategy.items():
        ensure(7.5)
        pdf.set_xy(_PDF_MARGIN + 4, y)
        pdf.set_font("NotoCJK", "", 10)
        set_text("#334155")
        if g["n"] == 0:
            label = f"{k}  0笔"
        else:
            label = f"{k}  {g['n']}笔  胜率{g['win_rate']:.0f}%  盈亏{g['pnl']:+.2f} USDT"
        pdf.cell(0, 6, label)
        y += 7.5

    # 五、平仓原因
    by_reason = stat_by(trades, "reason")
    if by_reason:
        section("五、平仓原因")
        reason_cn = {"TP": "止盈", "SL": "止损", "TIMEOUT": "超时强平", "REVERSE": "反向平仓", "LIQ": "爆仓"}
        for k, g in by_reason.items():
            ensure(7.5)
            pdf.set_xy(_PDF_MARGIN + 4, y)
            pdf.set_font("NotoCJK", "", 10)
            set_text("#334155")
            pdf.cell(0, 6, f"{reason_cn.get(k, k)}  {g['n']}笔")
            y += 7.5

    # 六、交易明细（矢量表格，跨页重绘表头）
    section("六、交易明细")
    if trades:
        headers = ["平仓时间(北京)", "品种", "方向", "策略", "原因", "入场", "出场", "盈亏"]
        col_w = [33, 21, 8, 15, 15, 28, 28, 14]
        remain = content_w - sum(col_w)
        col_w[0] += remain
        row_h = 7
        def draw_header():
            nonlocal y
            pdf.set_xy(_PDF_MARGIN, y)
            pdf.set_font("NotoCJK", "", 8)
            set_text("#64748b")
            set_fill("#f1f5f9")
            x = _PDF_MARGIN
            for i, h in enumerate(headers):
                pdf.rect(x, y, col_w[i], row_h, style="F")
                pdf.set_draw_color(226, 232, 240)
                pdf.rect(x, y, col_w[i], row_h, style="D")
                pdf.set_xy(x, y + 1.2)
                pdf.cell(col_w[i], 4, h, align="C")
                x += col_w[i]
            y += row_h
        draw_header()
        reason_cn = {"TP": "止盈", "SL": "止损", "TIMEOUT": "超时强平", "REVERSE": "反向平仓", "LIQ": "爆仓"}
        for t in reversed(trades):
            if y + row_h > _PDF_FOOTER_SAFE:
                pdf.add_page()
                y = _PDF_MARGIN
                draw_header()
            pnl = t.get("pnl", 0)
            arrow = "+" if pnl >= 0 else ""
            row = [
                bj_time_str(t.get("exit_ts", 0)),
                t.get("name", ""),
                "多" if t.get("direction") == "long" else "空",
                t.get("strategy", ""),
                reason_cn.get(t.get("reason", ""), t.get("reason", "")),
                f"{t.get('entry', 0):.2f}",
                f"{t.get('exit', 0):.2f}",
                f"{arrow}{pnl:.2f}",
            ]
            x = _PDF_MARGIN
            for i, v in enumerate(row):
                if i == len(row) - 1:
                    set_text("#16a34a" if pnl >= 0 else "#dc2626")
                else:
                    set_text("#334155")
                pdf.set_xy(x, y + 1.2)
                pdf.set_font("NotoCJK", "", 8)
                pdf.cell(col_w[i], 4, v, align="C")
                x += col_w[i]
            pdf.set_draw_color(241, 245, 249)
            pdf.line(_PDF_MARGIN, y + row_h, _PDF_MARGIN + content_w, y + row_h)
            y += row_h

    pdf.output(out_path)
    return out_path


def build_chart_image(state: dict, trades: list, period: str, now_str: str, out_path: str):
    """用 Pillow 生成综合报表 PNG/PDF：账户概览/交易统计/权益曲线/策略/原因/交易明细。
    PDF 模式按页流式分页（PAGE_H 上限自动换页），画布 1600 宽保证手机端清晰。"""
    if not _HAS_PIL:
        raise RuntimeError("缺少 Pillow，请先 pip install pillow")

    n = len(trades)
    W = 1600
    PAGE_H = 2213                     # PDF 单页内容高度上限（原1660放大4/3）
    MARGIN_BOTTOM = 93                # 页面底部留白，避免与页脚遮挡
    is_pdf = out_path.lower().endswith(".pdf")
    if is_pdf:
        H = PAGE_H
    else:
        H = 2427 + max(0, n - 8) * 40
    pages = []
    img = Image.new("RGB", (W, H), "white")
    d = ImageDraw.Draw(img)
    y = 40

    def new_page():
        """保存当前页并在底部画页脚，开启新一页"""
        nonlocal img, d, y
        d.text((53, PAGE_H - 59), "⚠️ 模拟单仅作练习记录，不涉及真实资金", font=font_s, fill="#94a3b8")
        pages.append(img)
        img = Image.new("RGB", (W, H), "white")
        d = ImageDraw.Draw(img)
        y = 40

    def ensure_space(h: int):
        """PDF 模式内容放不下时自动换页；PNG 模式始终单张长图"""
        nonlocal y
        if is_pdf and y + h > PAGE_H - MARGIN_BOTTOM:
            new_page()

    font_title = _find_font(59)
    font_h1 = _find_font(40)
    font_h2 = _find_font(32)
    font_n = _find_font(35)
    font_s = _find_font(29)

    initial = state.get("initial_balance", 100.0)
    balance = state.get("balance", initial)
    total_pnl = state.get("stats", {}).get("pnl", balance - initial)
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
    hours = PERIOD_HOURS[period]
    period_start = bj_time_str(time.time() - hours * 3600) if hours else "全部"

    short_start = period_start[5:10] if period_start != "全部" else "全部"
    d.text((53, y), f"模拟盘{period_cn}（{short_start} ~ {now_str[5:10]}）", font=font_title, fill="#1f2937")
    y += 75
    d.text((53, y), f"统计区间（北京时间）：{period_start} ~ {now_str}", font=font_s, fill="#64748b")
    y += 80

    def section(title):
        nonlocal y
        ensure_space(80)
        y += 11
        d.text((53, y), title, font=font_h1, fill="#1f2937")
        y += 59

    # 一、账户概览
    section("一、账户概览")
    overview = [
        ("当前余额", f"{balance:,.2f} USDT", "#2563eb"),
        ("初始余额", f"{initial:,.2f} USDT", "#64748b"),
        ("累计盈亏", f"{total_pnl:+.2f} USDT（{ret_pct:+.2f}%）", "#16a34a" if total_pnl >= 0 else "#dc2626"),
        ("峰值权益", f"{state.get('peak_equity', balance):,.2f} USDT", "#7c3aed"),
        ("最大回撤", f"{mdd:.2f}%", "#dc2626"),
    ]
    cw, ch = (W - 53 * 2 - 4 * 27) // 5, 112
    ensure_space(ch + 32)
    for i, (k, v, color) in enumerate(overview):
        x = 53 + i * (cw + 27)
        d.rounded_rectangle((x, y, x + cw, y + ch), radius=16, fill="#f8fafc", outline="#e2e8f0", width=2)
        d.text((x + 21, y + 13), k, font=font_s, fill="#64748b")
        d.text((x + 21, y + 59), v, font=font_s, fill=color)
    y += ch + 32

    # 二、交易统计
    section("二、交易统计")
    stats = [
        ("已平仓笔数", f"{n}"),
        ("盈利/亏损", f"{wins}/{losses}"),
        ("胜率", f"{win_rate:.1f}%"),
        ("平均盈利", f"{avg_win:+.2f} USDT"),
        ("平均亏损", f"{avg_loss:.2f} USDT"),
        ("盈亏比", f"{rr:.2f}"),
        ("单笔期望", f"{exp:+.2f} USDT"),
        ("区间盈亏", f"{sum(t.get('pnl', 0) for t in trades):+.2f} USDT"),
    ]
    cw2, ch2 = (W - 53 * 2 - 3 * 27) // 4, 101
    ensure_space(2 * (ch2 + 16) + 24)
    for i in range(0, 8, 4):
        for j in range(4):
            k, v = stats[i + j]
            x = 53 + j * (cw2 + 27)
            yy = y + (i // 4) * (ch2 + 16)
            d.rounded_rectangle((x, yy, x + cw2, yy + ch2), radius=13, fill="#f8fafc", outline="#e2e8f0", width=2)
            d.text((x + 19, yy + 13), k, font=font_s, fill="#64748b")
            d.text((x + 19, yy + 51), v, font=font_n, fill="#1f2937")
    y += 2 * (ch2 + 16) + 24

    # 三、权益曲线
    section("三、权益曲线（按平仓顺序）")
    chart_w, chart_h = W - 187, 293
    ensure_space(chart_h + 53)
    chart_x, chart_y = 93, y
    d.rectangle((chart_x, chart_y, chart_x + chart_w, chart_y + chart_h), outline="#cbd5e1", width=2)
    for i in range(1, 5):
        gy = chart_y + chart_h * i // 5
        d.line((chart_x, gy, chart_x + chart_w, gy), fill="#f1f5f9", width=1)
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
            d.line((pts[i - 1], pts[i]), fill=color, width=5)
        d.ellipse((pts[0][0] - 8, pts[0][1] - 8, pts[0][0] + 8, pts[0][1] + 8), fill="#2563eb")
        d.ellipse((pts[-1][0] - 8, pts[-1][1] - 8, pts[-1][0] + 8, pts[-1][1] + 8), fill="#2563eb")
        d.text((pts[-1][0] - 80, pts[-1][1] - 45), f"{cum[-1]:.2f}", font=font_s, fill="#2563eb")
        d.text((chart_x, chart_y + chart_h + 8), f"首笔 {bj_time_str(trades[0].get('exit_ts', 0))}", font=font_s, fill="#64748b")
        d.text((chart_x + chart_w - 213, chart_y + chart_h + 8), f"末笔 {bj_time_str(trades[-1].get('exit_ts', 0))}", font=font_s, fill="#64748b")
    else:
        d.line((chart_x, chart_y + chart_h // 2, chart_x + chart_w, chart_y + chart_h // 2), fill="#94a3b8", width=3)
        d.text((chart_x + chart_w // 2 - 93, chart_y + chart_h // 2 - 16), "暂无平仓记录", font=font_n, fill="#94a3b8")
    y += chart_h + 45

    # 四、按策略表现（6 类信号口径）
    section("四、按策略表现（6 类信号）")
    by_strategy = stat_by_strategy(trades)
    for k, g in by_strategy.items():
        ensure_space(59)
        if g["n"] == 0:
            label = f"{k}  0笔"
        else:
            label = f"{k}  {g['n']}笔  胜率{g['win_rate']:.0f}%  盈亏{g['pnl']:+.2f} USDT"
        d.text((80, y), label, font=font_n, fill="#334155")
        y += 59

    # 五、平仓原因
    by_reason = stat_by(trades, "reason")
    if by_reason:
        section("五、平仓原因")
        reason_cn = {"TP": "止盈", "SL": "止损", "TIMEOUT": "超时强平", "REVERSE": "反向平仓", "LIQ": "爆仓"}
        for k, g in by_reason.items():
            ensure_space(59)
            d.text((80, y), f"{reason_cn.get(k, k)}  {g['n']}笔", font=font_n, fill="#334155")
            y += 59

    # 六、交易明细
    section("六、交易明细")
    if trades:
        headers = ["平仓时间(北京)", "品种", "方向", "策略", "原因", "入场→出场", "盈亏"]
        widths = [267, 227, 93, 120, 160, 480, 120]
        xs = []
        cx = 53
        for w in widths:
            xs.append(cx)
            cx += w
        def draw_table_header():
            nonlocal y
            for i, h in enumerate(headers):
                d.text((xs[i] + 5, y), h, font=font_s, fill="#64748b")
            y += 45
            d.line((53, y - 11, 53 + sum(widths), y - 11), fill="#e2e8f0", width=2)

        draw_table_header()
        for t in reversed(trades):
            if is_pdf and y + 51 > PAGE_H - MARGIN_BOTTOM:
                new_page()
                draw_table_header()
            d_ = "多" if t.get("direction") == "long" else "空"
            rc = {"TP": "止盈", "SL": "止损", "TIMEOUT": "超时强平", "REVERSE": "反向平仓", "LIQ": "爆仓"}.get(t.get("reason", ""), t.get("reason", ""))
            pnl = t.get("pnl", 0)
            arrow = "+" if pnl >= 0 else ""
            row = [
                bj_time_str(t.get("exit_ts", 0)),
                t.get("name", ""),
                d_,
                t.get("strategy", ""),
                rc,
                f"{t.get('entry', 0):.2f} → {t.get('exit', 0):.2f}",
                f"{arrow}{pnl:.2f}",
            ]
            for i, v in enumerate(row):
                if i == len(row) - 1:
                    fill = "#16a34a" if pnl >= 0 else "#dc2626"
                else:
                    fill = "#334155"
                d.text((xs[i] + 5, y), v, font=font_s, fill=fill)
            y += 45

    d.text((53, y + 19), "⚠️ 模拟单仅作练习记录，不涉及真实资金", font=font_s, fill="#94a3b8")
    if out_path.lower().endswith(".pdf"):
        pages.append(img)
        if len(pages) > 1:
            pages[0].save(out_path, "PDF", resolution=160.0, save_all=True, append_images=pages[1:])
        else:
            pages[0].save(out_path, "PDF", resolution=160.0)
    else:
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


def stat_by_strategy(trades: list) -> dict:
    """按 6 类信号固定口径统计策略分布：无交易的信号补齐 0 笔；非独立信号（如 0.5回踩）不展示"""
    groups = stat_by(trades, "strategy")
    out = {}
    for k in SIGNAL_STRATEGIES:
        g = groups.pop(k, None)
        if g is None:
            g = {"n": 0, "wins": 0, "losses": 0, "pnl": 0.0, "avg": 0.0, "win_rate": 0.0}
        out[k] = g
    return out


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
    hours = PERIOD_HOURS[period]
    period_start = bj_time_str(time.time() - hours * 3600) if hours else "全部"

    L = [
        f"# 模拟盘{period_cn}（北京时间 {now_str}）",
        "",
        f"> 统计区间：{period_start} ~ {now_str}（北京时间）",
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

    by_strategy = stat_by_strategy(trades)
    if by_strategy:
        L += ["## 三、按策略分布（6 类信号口径）", "", "| 策略 | 笔数 | 胜/负 | 胜率 | 盈亏合计 | 平均盈亏 |", "|------|-----|-------|------|---------|---------|"]
        for k, g in by_strategy.items():
            if g["n"] == 0:
                L.append(f"| {k} | 0 | - | - | - | - |")
            else:
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
        L += ["## 六、最近交易明细", "", "| 平仓时间(北京) | 品种 | 方向 | 策略 | 原因 | 入场→出场 | 盈亏 |", "|------|------|------|------|------|-----------|------|"]
        for t in reversed(trades[-10:]):
            d = "多" if t.get("direction") == "long" else "空"
            rc = {"TP": "止盈", "SL": "止损", "TIMEOUT": "超时强平", "REVERSE": "反向平仓", "LIQ": "爆仓"}.get(t.get("reason", ""), t.get("reason", ""))
            pnl = t.get("pnl", 0)
            arrow = "+" if pnl >= 0 else ""
            L.append(f"| {bj_time_str(t.get('exit_ts', 0))} | {t.get('name', '')} | {d} | {t.get('strategy', '')} | {rc} | {t.get('entry', 0):.2f} → {t.get('exit', 0):.2f} | **{arrow}{pnl:.2f}** |")
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
        f"## 📊 模拟盘{period_cn}（北京时间 {now_str}）",
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
        by_strategy = stat_by_strategy(trades)
        if by_strategy:
            L.append("**按策略（6 类信号口径）**：")
            for k, g in by_strategy.items():
                if g["n"] == 0:
                    L.append(f"> {k}：0笔")
                else:
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
    ap.add_argument("--pdf", action="store_true", help="生成图表 PDF 并推送文件消息（企微 file 类型）")
    ap.add_argument("--webhook", default="", help="报告推送 webhook URL（优先级最高）")
    args = ap.parse_args()

    state = load_state(args.state)
    trades = filter_trades(state.get("closed_trades", []), args.period)
    # 标题时间使用北京时间（UTC+8）
    bj_time = time.gmtime(time.time() + 8 * 3600)
    now_str = time.strftime("%Y-%m-%d %H:%M", bj_time)
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
    if args.pdf:
        webhook = load_webhook(args.webhook)
        if args.output:
            pdf_path = args.output + ".pdf"
        else:
            # 默认文件名：周报9月13日-9月19日（北京时间统计区间）
            hours = PERIOD_HOURS[args.period]
            end_t = time.gmtime(time.time() + 8 * 3600)
            if hours:
                start_t = time.gmtime(time.time() - hours * 3600 + 8 * 3600)
                label = f"{start_t.tm_mon}月{start_t.tm_mday}日-{end_t.tm_mon}月{end_t.tm_mday}日"
            else:
                label = "总览"
            period_cn = {"weekly": "周报", "monthly": "月报", "all": "总览"}[args.period]
            pdf_path = f"{period_cn}{label}.pdf"
        # PDF 由 Pillow 直接绘制输出，不经过中间 PNG
        build_chart_image(state, trades, args.period, now_str, pdf_path)
        send_wecom_image(webhook, pdf_path)
        print(f"已生成 PDF 并推送: {pdf_path}")
    if not args.output and not args.image and not args.pdf:
        print(report)


if __name__ == "__main__":
    main()
