"""
MULTI-COIN PAPER TRADING BOT (fake money, Telegram alerts only - koi real
order nahi lagta). Har coin ko ALAG track karta hai: agar us coin ki koi
open paper-trade nahi hai, signal check karta hai; mile to "trade" khol
deta hai (state.json me save, per-symbol). Agar pehle se khuli hai, check
karta hai stop/target lagi ya nahi; lagi to band karke result
trades_log.csv me likhta hai. Telegram par sirf tab message aata hai jab
kisi coin ki trade khulti ya band hoti hai - har run par spam nahi.

Install:  pip install ccxt pandas numpy
Run:      python btc_telegram_bot.py
"""
import csv
import json
import os
import smtplib
import urllib.request
import urllib.parse
from email.mime.text import MIMEText
import numpy as np
import pandas as pd

# ---------------- Apna Telegram info yahan ----------------
# GitHub Actions me ye secrets se aayega; Colab me yahi fallback values use hongi.
TOKEN = os.environ.get("TELEGRAM_TOKEN", "9080354745:AAG7KlDd_WH36W6OQGhOP3L7jtRRkZ4bLhQ")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "8655880953")

# ---------------- Email (Gmail) - Telegram na chale to bhi ye kaam karega ----------------
GMAIL_ADDRESS = os.environ.get("GMAIL_ADDRESS", "razamhaseeb7@gmail.com")
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD", "ionbluupremfpxeb")

SYMBOLS = ["BTC/USDT", "ETH/USDT", "SOL/USDT"]   # yahan coins add/hatao kar sakte ho
ENTRY_TF = "15m"
LOOKBACK_DAYS = 10

BB_N, BB_K = 20, 2.0
RSI_RANGE_MAX = 35
ADX_RANGE_MAX = 20
ATR_MULT = 1.5
MAX_HOLD_BARS = 48          # 15m * 48 = 12 ghante, itne me target/stop na lage to time-exit

STATE_FILE = "state.json"   # abhi koi open paper-trade hai ya nahi, yahan save hota hai
LOG_FILE = "trades_log.csv"  # band hui saari trades ka record
HEARTBEAT_MINUTES = 60      # itni der me ek "sab theek hai" status message (trade alert ke alawa)


def send_telegram(text):
    # ---- Telegram try karo (fail ho to bhi ruko mat, email try karenge) ----
    try:
        url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
        data = urllib.parse.urlencode({"chat_id": CHAT_ID, "text": text}).encode()
        urllib.request.urlopen(url, data=data, timeout=10)
        print("Telegram par bhej diya.")
    except Exception as e:
        print(f"Telegram message fail ho gaya (ignore, email try karenge): {e}")

    # ---- Email (Gmail) bhi bhejo - Telegram na chale to bhi ye pahunch jayega ----
    if GMAIL_ADDRESS and GMAIL_APP_PASSWORD:
        try:
            msg = MIMEText(text)
            msg["Subject"] = "BTC Bot Alert"
            msg["From"] = GMAIL_ADDRESS
            msg["To"] = GMAIL_ADDRESS
            with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
                server.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
                server.send_message(msg)
            print("Email bhej diya.")
        except Exception as e:
            print(f"Email bhi fail ho gaya: {e}")


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


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {}   # { "BTC/USDT": {"position": None or {...}}, "ETH/USDT": {...}, ... }


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def log_trade(symbol, entry_time, exit_time, entry, exit_price, reason, pnl_pct):
    is_new = not os.path.exists(LOG_FILE)
    with open(LOG_FILE, "a", newline="") as f:
        w = csv.writer(f)
        if is_new:
            w.writerow(["symbol", "entry_time", "exit_time", "entry", "exit", "reason", "pnl_pct"])
        w.writerow([symbol, entry_time, exit_time, f"{entry:.2f}", f"{exit_price:.2f}", reason, f"{pnl_pct:+.3f}"])


def process_symbol(symbol, state):
    sym_state = state.setdefault(symbol, {"position": None})
    pos = sym_state.get("position")
    status_line = ""

    d15 = fetch(symbol, ENTRY_TF, LOOKBACK_DAYS)
    d1h = resample_1h(d15)
    d15["rsi"], d15["atr"] = rsi(d15["close"]), atr(d15)
    d15["bb_lower"], d15["bb_mid"], d15["bb_upper"] = bollinger(d15["close"], BB_N, BB_K)
    adx1h = adx(d1h)

    last = d15.iloc[-1]
    last_adx = adx1h.iloc[-1]
    price = float(last["close"])

    if pos is not None:
        # ---- Is coin ki paper-trade pehle se khuli hai ----
        entry_time = pd.Timestamp(pos["entry_time"])
        entry, stop, target = pos["entry"], pos["stop"], pos["target"]
        since = d15[d15.index >= entry_time]

        reason = exit_price = exit_time = None
        for idx, row in since.iterrows():
            if row["low"] <= stop:
                reason, exit_price, exit_time = "SL", stop, idx
                break
            if row["high"] >= target:
                reason, exit_price, exit_time = "TARGET", target, idx
                break
        if reason is None and len(since) > MAX_HOLD_BARS:
            reason, exit_price, exit_time = "TIME", price, d15.index[-1]

        if reason:
            pnl_pct = (exit_price - entry) / entry * 100
            log_trade(symbol, entry_time, exit_time, entry, exit_price, reason, pnl_pct)
            result = "WIN" if pnl_pct > 0 else "LOSS"
            send_telegram(f"[{symbol}] PAPER TRADE CLOSED ({result})\n"
                          f"Entry: ${entry:,.2f} -> Exit: ${exit_price:,.2f}\n"
                          f"Reason: {reason} | PnL: {pnl_pct:+.2f}%")
            print(f"[{symbol}] Trade band hui: {result}, {pnl_pct:+.2f}%")
            sym_state["position"] = None
            status_line = f"{symbol}: ${price:,.2f} | trade abhi band hui ({result})"
        else:
            print(f"[{symbol}] Trade abhi open hai. Entry {entry:.2f}, abhi price {price:.2f}")
            status_line = f"{symbol}: ${price:,.2f} | OPEN trade (entry ${entry:,.2f})"

    else:
        # ---- Koi trade khuli nahi: naya signal check karo ----
        touch = last["low"] <= last["bb_lower"] and last["close"] > last["bb_lower"]
        signal = touch and last["rsi"] < RSI_RANGE_MAX and last_adx < ADX_RANGE_MAX

        if signal:
            stop = price - ATR_MULT * float(last["atr"])
            target = float(last["bb_mid"])
            sym_state["position"] = {"entry": price, "stop": stop, "target": target,
                                     "entry_time": str(d15.index[-1])}
            send_telegram(f"[{symbol}] PAPER TRADE OPENED\n"
                          f"Entry: ${price:,.2f}\nStop: ${stop:,.2f}\nTarget: ${target:,.2f}")
            print(f"[{symbol}] Nayi paper-trade khol di.")
            status_line = f"{symbol}: ${price:,.2f} | nayi trade khuli"
        else:
            print(f"[{symbol}] Abhi koi signal nahi. Price {price:.2f}, "
                  f"RSI {last['rsi']:.1f}, ADX {last_adx:.1f}")
            status_line = (f"{symbol}: ${price:,.2f} | RSI {last['rsi']:.1f} | "
                           f"ADX {last_adx:.1f} | koi signal nahi")

    return status_line


if __name__ == "__main__":
    state = load_state()
    status_lines = []
    for sym in SYMBOLS:
        try:
            status_lines.append(process_symbol(sym, state))
        except Exception as e:
            print(f"[{sym}] Error: {e}")
            status_lines.append(f"{sym}: error - {e}")

    # ---- Hourly "sab theek hai" heartbeat (trade alerts se alag) ----
    meta = state.setdefault("_meta", {})
    last_hb = meta.get("last_heartbeat")
    now = pd.Timestamp.utcnow().tz_localize(None)
    due = last_hb is None or (now - pd.Timestamp(last_hb)).total_seconds() >= HEARTBEAT_MINUTES * 60
    if due:
        send_telegram("Status update:\n" + "\n".join(status_lines))
        meta["last_heartbeat"] = str(now)
        print("Heartbeat bhej diya.")

    save_state(state)
