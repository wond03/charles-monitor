# -*- coding: utf-8 -*-
"""
查尔斯信号监控系统 · 数据源层
支持：
  - Gate.io 永续合约  (BTC_USDT / PAXG_USDT 永续K线)  [主，贴近实际交易标的]
  - Gate.io 现货      (同交易对现货K线)               [备]
  - OKX      (BTC-USDT 等K线)                        [备]
  - Binance  (BTCUSDT 等K线)                         [备]
  - 新浪财经  (hf_XAU 伦敦金实时快照，仅用于推送校准)
统一输出 Kline: [ {ts, open, high, low, close}, ... ] 时间升序
"""
import time
import requests

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
        })
    return sorted(out, key=lambda x: x["ts"])


def _gate_klines(inst: str, interval: str, limit: int) -> list:
    """Gate.io 现货K线。interval: 1m/5m/15m/1h/4h/1d"""
    url = "https://api.gateio.ws/api/v4/spot/candlesticks"
    params = {"currency_pair": inst, "interval": interval, "limit": limit}
    r = requests.get(url, params=params, timeout=TIMEOUT)
    r.raise_for_status()
    rows = r.json()
    # Gate 返回: [ts, quote_volume, close, high, low, open, base_volume, closed]
    out = []
    for row in rows:
        out.append({
            "ts": int(row[0]),
            "open": float(row[6]),
            "high": float(row[3]),
            "low": float(row[4]),
            "close": float(row[2]),
        })
    return sorted(out, key=lambda x: x["ts"])


def _okx_klines(inst: str, interval: str, limit: int) -> list:
    """OKX K线。inst 形如 BTC-USDT，interval: 1m/5m/15m/1H/4H/1D"""
    url = "https://www.okx.com/api/v5/market/candles"
    params = {"instId": inst, "bar": interval, "limit": min(limit, 300)}
    r = requests.get(url, params=params, timeout=TIMEOUT)
    r.raise_for_status()
    rows = r.json()["data"]
    # OKX 返回: [ts, open, high, low, close, vol, volCcy, ...] 时间倒序
    out = []
    for row in rows:
        out.append({
            "ts": int(row[0]),
            "open": float(row[1]),
            "high": float(row[2]),
            "low": float(row[3]),
            "close": float(row[4]),
        })
    return sorted(out, key=lambda x: x["ts"])


def _binance_klines(inst: str, interval: str, limit: int) -> list:
    """Binance K线。inst 形如 BTCUSDT，interval: 1m/5m/15m/1h/4h/1d"""
    url = "https://api.binance.com/api/v3/klines"
    params = {"symbol": inst, "interval": interval, "limit": min(limit, 500)}
    r = requests.get(url, params=params, timeout=TIMEOUT)
    r.raise_for_status()
    rows = r.json()
    out = []
    for row in rows:
        out.append({
            "ts": int(row[0]),
            "open": float(row[1]),
            "high": float(row[2]),
            "low": float(row[3]),
            "close": float(row[4]),
        })
    return sorted(out, key=lambda x: x["ts"])


# 各交易所的 interval 映射
_INTERVAL_MAP = {
    "gate-futures": {"1m": "1m", "5m": "5m", "15m": "15m", "1h": "1h", "4h": "4h", "1d": "1d"},
    "gate": {"1m": "1m", "5m": "5m", "15m": "15m", "1h": "1h", "4h": "4h", "1d": "1d"},
    "okx":  {"1m": "1m", "5m": "5m", "15m": "15m", "1h": "1H", "4h": "4H", "1d": "1D"},
    "binance": {"1m": "1m", "5m": "5m", "15m": "15m", "1h": "1h", "4h": "4h", "1d": "1d"},
}

_SOURCE_FUNCS = {
    "gate-futures": lambda inst, iv, lim: _gate_futures_klines(inst, iv, lim),
    "gate": lambda inst, iv, lim: _gate_klines(inst, iv, lim),
    "okx": lambda inst, iv, lim: _okx_klines(inst, iv, lim),
    "binance": lambda inst, iv, lim: _binance_klines(inst, iv, lim),
}


def fetch_klines(inst: str, interval: str = "1h", limit: int = 120,
                 sources: tuple = ("gate", "okx", "binance")) -> list:
    """按优先级依次尝试多个数据源，返回统一格式K线列表。"""
    last_err = None
    for src in sources:
        try:
            iv = _INTERVAL_MAP[src][interval]
            return _SOURCE_FUNCS[src](inst, iv, limit)
        except Exception as e:  # noqa: BLE001
            last_err = e
    raise RuntimeError(f"所有数据源均失败: {last_err}")


def fetch_gold_snapshot() -> dict:
    """新浪伦敦金实时快照（仅推送展示用）。hf_XAU 字段:
    0最新价 1昨收? 2? 3最高 4? 5最低 6时间 7昨收? 8买/卖价 9-11 0 12日期 13名称
    """
    try:
        url = "https://hq.sinajs.cn/list=hf_XAU"
        r = requests.get(url, headers={
            "User-Agent": "Mozilla/5.0",
            "Referer": "https://finance.sina.com.cn/",
        }, timeout=TIMEOUT)
        r.encoding = "gbk"
        txt = r.text
        if "hf_XAU" not in txt:
            return {}
        payload = txt.split('"')[1].split(",")
        return {
            "name": payload[13].strip(),
            "price": float(payload[0]),
            "high": float(payload[3]),
            "low": float(payload[5]),
            "time": payload[6],
            "date": payload[12],
        }
    except Exception:  # noqa: BLE001
        return {}


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
    btc = fetch_klines("BTC_USDT", "1h", 10)
    gold = fetch_klines("PAXG_USDT", "1h", 10)
    print("BTC 1H x", len(btc), "最新", last_price(btc))
    print("PAXG 1H x", len(gold), "最新", last_price(gold))
    print("新浪黄金快照", fetch_gold_snapshot())
    print("耗时 %.2fs" % (time.time() - t0))
