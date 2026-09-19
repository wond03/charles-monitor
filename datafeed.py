# -*- coding: utf-8 -*-
"""
查尔斯信号监控系统 · 数据源层
支持：
  - Gate.io 永续合约  (BTC_USDT / XAU_USDT 永续K线)      [主，贴近实际交易标的]
  - Weex 合约V3       (BTCUSDT / XAUUSDT 永续K线)        [备，纯永续]
  （OKX / Binance / Gate现货 / Weex现货 已按用户要求废除，相关函数与映射已删除）
统一输出 Kline: [ {ts, open, high, low, close, volume}, ... ] 时间升序
"""
import json
import logging
import os
import time
import requests

log = logging.getLogger(__name__)

TIMEOUT = 15


def _gate_futures_klines(inst: str, interval: str, limit: int) -> list:
    """Gate.io 永续合约K线(USDT本位)。contract 形如 BTC_USDT
    interval: 1m/5m/15m/30m/1h/4h/8h/1d/7d/30d
    返回: [{t,o,h,l,c,v,sum}, ...] 时间倒序(最新在前)
    """
    url = "https://api.gateio.ws/api/v4/futures/usdt/candlesticks"
    params = {"contract": inst, "interval": interval, "limit": limit}
    r = requests.get(url, params=params, timeout=TIMEOUT)
    r.raise_for_status()
    rows = r.json()
    out = []
    for row in rows:
        out.append({
            "ts": int(row["t"]),
            "open": float(row["o"]),
            "high": float(row["h"]),
            "low": float(row["l"]),
            "close": float(row["c"]),
            "volume": float(row["v"] or 0),
        })
    return sorted(out, key=lambda x: x["ts"])


def _weex_contract_klines(inst: str, interval: str, limit: int) -> list:
    """Weex 合约V3 K线（公开免鉴权）。inst 形如 XAU_USDT -> symbol=XAUUSDT
    interval: 1m/5m/15m/30m/1h/4h/12h/1d/1w
    响应为数组，索引同现货V3: 0开盘时间 1开 2高 3低 4收 5量 6收盘时间 7成交额 ...
    用于黄金 XAUUSDT 永续（2026-07-28 上线）等 TradFi 标的；DNS 污染同样可用 WEEX_API_IP 兜底。
    """
    symbol = inst.replace("_", "").replace("-", "")
    params = {"symbol": symbol, "interval": interval, "limit": min(limit, 1000)}
    ip = os.environ.get("WEEX_API_IP", "")
    if ip:
        import subprocess
        from urllib.parse import urlencode
        qs = urlencode(params)
        cmd = ["curl", "-s", "--max-time", str(TIMEOUT),
               "--resolve", f"api-contract.weex.com:443:{ip}",
               f"https://api-contract.weex.com/capi/v3/market/klines?{qs}"]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=TIMEOUT + 5)
        if proc.returncode != 0:
            raise RuntimeError(f"Weex合约 curl 失败: {proc.stderr.strip() or 'exit ' + str(proc.returncode)}")
        data = json.loads(proc.stdout)
    else:
        r = requests.get("https://api-contract.weex.com/capi/v3/market/klines",
                         params=params, timeout=TIMEOUT)
        r.raise_for_status()
        data = r.json()
    if isinstance(data, dict):  # 错误响应 {"code":xxx,"msg":...}
        raise RuntimeError(f"Weex合约 {symbol} 不可用: {data.get('msg', data)}")
    out = []
    for row in data:
        out.append({
            "ts": int(row[0]),
            "open": float(row[1]),
            "high": float(row[2]),
            "low": float(row[3]),
            "close": float(row[4]),
            "volume": float(row[5] or 0),
        })
    return sorted(out, key=lambda x: x["ts"])


def _weex_route(inst: str, iv: str, lim: int) -> list:
    """Weex 源路由：统一走合约域永续（BTCUSDT / XAUUSDT 等），
    现货域已随 OKX/Binance 一同废除（纯永续链策略）。"""
    return _weex_contract_klines(inst, iv, lim)


# 各交易所的 interval 映射（仅保留纯永续链：gate-futures 主 + weex 合约域备）
_INTERVAL_MAP = {
    "gate-futures": {"1m": "1m", "5m": "5m", "15m": "15m", "1h": "1h", "4h": "4h", "1d": "1d"},
    "weex": {"1m": "1m", "5m": "5m", "15m": "15m", "1h": "1h", "4h": "4h", "1d": "1d"},
}

_SOURCE_FUNCS = {
    "gate-futures": lambda inst, iv, lim: _gate_futures_klines(inst, iv, lim),
    "weex": _weex_route,
}


def fetch_klines(inst: str, interval: str = "1h", limit: int = 120,
                 sources: tuple = ("gate-futures", "weex")) -> list:
    """按优先级依次尝试多个数据源，返回统一格式K线列表。
    默认源为纯永续链（gate-futures 主 + weex 合约域备），现货/OKX/Binance 已废除。"""
    last_err = None
    for src in sources:
        t0 = time.time()
        try:
            iv = _INTERVAL_MAP[src][interval]
            data = _SOURCE_FUNCS[src](inst, iv, limit)
            log.debug("数据源 %s 成功: %s %s x%d (%.1fs)", src, inst, interval, len(data), time.time() - t0)
            return data
        except Exception as e:  # noqa: BLE001
            last_err = e
            log.warning("数据源 %s 失败: %s %s %s（%.1fs，将尝试下一备援）",
                        src, inst, interval, e, time.time() - t0)
    log.error("所有数据源均失败: %s %s %s: %s", inst, interval, sources, last_err)
    raise RuntimeError(f"所有数据源均失败: {last_err}")


def pct_change(klines: list, lookback: int = 24) -> float:
    """当前价相对 N 根K线前的涨跌幅(%)"""
    if len(klines) < 2:
        return 0.0
    base = klines[max(0, len(klines) - 1 - lookback)]["close"]
    cur = klines[-1]["close"]
    if base == 0:
        return 0.0
    return (cur - base) / base * 100.0


def last_price(klines: list) -> float:
    return klines[-1]["close"] if klines else 0.0


if __name__ == "__main__":
    t0 = time.time()
    srcs = ("gate-futures", "weex")
    btc = fetch_klines("BTC_USDT", "1h", 10, sources=srcs)
    gold = fetch_klines("XAU_USDT", "1h", 10, sources=srcs)
    print("BTC 1H x", len(btc), "最新", last_price(btc))
    print("XAU 1H x", len(gold), "最新", last_price(gold))
    print("耗时 %.2fs" % (time.time() - t0))
