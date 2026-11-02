"""
BTC/USDT LIVE SIGNAL + TELEGRAM ALERT.
Market check karta hai, aur agar mean-reversion signal mile to seedha
Telegram par message bhej deta hai. Pehli baar chalane par ek "connected"
test message bhi aayega, taaki pata chale sab sahi juda hai.

Install:  pip install ccxt pandas numpy
Run:      python btc_telegram_bot.py
"""
import urllib.request
import urllib.parse
import numpy as np
import pandas as pd

# ---------------- Apna Telegram info yahan ----------------
# GitHub Actions me ye secrets se aayega; Colab me yahi fallback values use hongi.
import os
TOKEN = os.environ.get("TELEGRAM_TOKEN", "9080354745:AAG7KlDd_WH36W6OQGhOP3L7jtRRkZ4bLhQ")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "8655880953")

SYMBOL = "BTC/USDT"
ENTRY_TF = "15m"
LOOKBACK_DAYS = 10

BB_N, BB_K = 20, 2.0
RSI_RANGE_MAX = 35
ADX_RANGE_MAX = 20
ATR_MULT = 1.5


def send_telegram(text):
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    data = urllib.parse.urlencode({"chat_id": CHAT_ID, "text": text}).encode()
    try:
        urllib.request.urlopen(url, data=data, timeout=10)
        print("Telegram par bhej diya.")
    except Exception as e:
        print(f"Telegram message fail ho gaya: {e}")


def _tf_ms(tf):
    n, unit = int(tf[:-1]), tf[-1]
    return n * {"m": 60_000, "h": 3_600_000, "d": 86_400_000}[unit]


def fetch(symbol, tf, days):
    import ccxt
    candidates = ["binance", "okx", "bybit", "kucoin", "kraken"]
    need = int(days * 86400_000 / _tf_ms(tf) * 0.5)
    for ex_id in candidates:
        try:
            ex = getattr(ccxt, ex_id)({"enableRateLimit": True})
            ex.load_markets()
            if symbol not in ex.markets:
                continue
            now = ex.milliseconds()
            since = now - days * 86400_000
            rows = []
            while since < now:
                batch = ex.fetch_ohlcv(symbol, tf, since=since, limit=1000)
                if not batch:
                    break
                rows += batch
                new_since = batch[-1][0] + 1
                if new_since <= since:
                    break
                since = new_since
            if len(rows) < need:
                continue
            df = pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close", "volume"])
            df["time"] = pd.to_datetime(df["ts"], unit="ms")
            df = df.drop_duplicates("ts").set_index("time").drop(columns="ts")
            print(f"Data source: {ex_id}")
            return df
        except Exception:
            continue
    raise RuntimeError("Kisi exchange se data nahi mila.")


def resample_1h(df):
    return df.resample("1h").agg({"open": "first", "high": "max", "low": "min",
                                  "close": "last", "volume": "sum"}).dropna()


def rsi(s, n=14):
    d = s.diff()
    up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    return 100 - 100 / (1 + up / dn.replace(0, np.nan))


def atr(df, n=14):
    pc = df["close"].shift()
    tr = pd.concat([df["high"] - df["low"], (df["high"] - pc).abs(),
                    (df["low"] - pc).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / n, adjust=False).mean()


def adx(df, n=14):
    up_move = df["high"].diff()
    down_move = -df["low"].diff()
    plus_dm = pd.Series(np.where((up_move > down_move) & (up_move > 0), up_move, 0.0), index=df.index)
    minus_dm = pd.Series(np.where((down_move > up_move) & (down_move > 0), down_move, 0.0), index=df.index)
    pc = df["close"].shift()
    tr = pd.concat([df["high"] - df["low"], (df["high"] - pc).abs(),
                    (df["low"] - pc).abs()], axis=1).max(axis=1)
    atr_s = tr.ewm(alpha=1 / n, adjust=False).mean()
    plus_di = 100 * plus_dm.ewm(alpha=1 / n, adjust=False).mean() / atr_s
    minus_di = 100 * minus_dm.ewm(alpha=1 / n, adjust=False).mean() / atr_s
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    return dx.ewm(alpha=1 / n, adjust=False).mean()


def bollinger(close, n, k):
    mid = close.rolling(n).mean()
    sd = close.rolling(n).std()
    return mid - k * sd, mid, mid + k * sd


if __name__ == "__main__":
    send_telegram("Bot connected! BTC/USDT check kar raha hoon...")

    d15 = fetch(SYMBOL, ENTRY_TF, LOOKBACK_DAYS)
    d1h = resample_1h(d15)

    d15["rsi"], d15["atr"] = rsi(d15["close"]), atr(d15)
    d15["bb_lower"], d15["bb_mid"], d15["bb_upper"] = bollinger(d15["close"], BB_N, BB_K)
    adx1h = adx(d1h)

    last = d15.iloc[-1]
    last_adx = adx1h.iloc[-1]
    price = last["close"]

    status = (f"BTC/USDT: ${price:,.2f}\n"
              f"RSI: {last['rsi']:.1f} | 1H ADX: {last_adx:.1f}\n"
              f"Lower Band: ${last['bb_lower']:,.2f} | Mid Band: ${last['bb_mid']:,.2f}")
    print(status)

    touch = last["low"] <= last["bb_lower"] and last["close"] > last["bb_lower"]
    signal = touch and last["rsi"] < RSI_RANGE_MAX and last_adx < ADX_RANGE_MAX

    if signal:
        stop = price - ATR_MULT * last["atr"]
        target = last["bb_mid"]
        msg = (f"SIGNAL: BUY\n{status}\n\n"
               f"Entry: ${price:,.2f}\nStop: ${stop:,.2f}\nTarget: ${target:,.2f}")
        print("\n>>> SIGNAL MILA <<<")
        send_telegram(msg)
    else:
        print("\n>>> Abhi koi signal nahi <<<")
        send_telegram(f"Abhi koi signal nahi.\n{status}")
