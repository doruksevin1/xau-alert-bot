#!/usr/bin/env python3
"""
XAU/USD Telegram sinyal botu.

Her 15 dakikalik mum kapanisinda stratejiyi kontrol eder; sinyal varsa Telegram'a
"LONG/SHORT, Entry, SL, TP1, TP2" mesaji gonderir. Bot islem ACMAZ, sadece bildirim yollar.

Strateji (baslangic sablonu, canliya almadan once --backtest ile test et):
  - 1S trend: kapanis > EMA50 > EMA200 (yukari) veya tersi (asagi)
  - 15dk tetik: trend yonunde EMA20'yi geri alma (pullback reclaim) + RSI filtresi
  - SL: son 8 mumun dip/tepesi +- 0.25 ATR (mesafe 1.0-2.5 ATR arasina sikistirilir)
  - TP1 = 1.5R, TP2 = 3R

Kullanim:
  python xau_alert_bot.py --get-chat-id   # chat_id ogren
  python xau_alert_bot.py --test          # Telegram'a deneme mesaji
  python xau_alert_bot.py --backtest      # gecmis veride stratejiyi dene
  python xau_alert_bot.py --once          # bir kez kontrol et (GitHub Actions icin)
  python xau_alert_bot.py --loop          # her 15 dk'da bir kontrol et (PC/sunucu icin)

Gerekli ortam degiskenleri: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, TWELVEDATA_API_KEY
"""
import argparse
import json
import math
import os
import sys
import time
from datetime import datetime, timedelta, timezone

import requests

SYMBOL = os.getenv("SYMBOL", "XAU/USD")
STATE_FILE = os.getenv("STATE_FILE", "state.json")
SPREAD = float(os.getenv("SPREAD", "0.40"))          # USD, backtest maliyeti icin
SL_ATR_MIN, SL_ATR_MAX = 1.0, 2.5
TP1_R, TP2_R = 1.5, 3.0
COOLDOWN_MIN = 60                                    # ayni yonde tekrar sinyal beklemesi
BAR_MIN = 15
TR_TZ = timezone(timedelta(hours=3))


# ----------------------------------------------------------------- veri
def fetch_candles(n=1500):
    key = os.environ["TWELVEDATA_API_KEY"]
    r = requests.get(
        "https://api.twelvedata.com/time_series",
        params={"symbol": SYMBOL, "interval": "15min", "outputsize": n,
                "order": "ASC", "timezone": "UTC", "apikey": key},
        timeout=30,
    )
    data = r.json()
    if data.get("status") == "error" or "values" not in data:
        raise RuntimeError(f"Veri hatasi: {data.get('message', data)}")
    out = []
    for v in data["values"]:
        t = datetime.strptime(v["datetime"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        out.append({"t": t, "o": float(v["open"]), "h": float(v["high"]),
                    "l": float(v["low"]), "c": float(v["close"])})
    return out


def drop_forming(candles, now):
    """Henuz kapanmamis son mumu at."""
    if candles and candles[-1]["t"] + timedelta(minutes=BAR_MIN) > now:
        candles = candles[:-1]
    return candles


def floor_hour(t):
    return t.replace(minute=0, second=0, microsecond=0)


def resample_1h(candles):
    """15dk mumlardan 1S mum uret; sadece 4 mumu da olan saatleri al."""
    groups = {}
    for c in candles:
        groups.setdefault(floor_hour(c["t"]), []).append(c)
    out = []
    for h in sorted(groups):
        g = groups[h]
        if len(g) < 4:
            continue
        out.append({"t": h, "o": g[0]["o"], "h": max(x["h"] for x in g),
                    "l": min(x["l"] for x in g), "c": g[-1]["c"]})
    return out


# ------------------------------------------------------------ gostergeler
def ema(vals, n):
    k = 2.0 / (n + 1)
    out, e = [], None
    for v in vals:
        e = v if e is None else v * k + e * (1 - k)
        out.append(e)
    return out


def rsi(closes, n=14):
    out = [None] * len(closes)
    if len(closes) <= n:
        return out
    gains = losses = 0.0
    for i in range(1, n + 1):
        d = closes[i] - closes[i - 1]
        gains += max(d, 0)
        losses += max(-d, 0)
    ag, al = gains / n, losses / n
    out[n] = 100.0 if al == 0 else 100 - 100 / (1 + ag / al)
    for i in range(n + 1, len(closes)):
        d = closes[i] - closes[i - 1]
        ag = (ag * (n - 1) + max(d, 0)) / n
        al = (al * (n - 1) + max(-d, 0)) / n
        out[i] = 100.0 if al == 0 else 100 - 100 / (1 + ag / al)
    return out


def atr(candles, n=14):
    out = [None] * len(candles)
    if len(candles) < n + 1:
        return out
    tr = [candles[0]["h"] - candles[0]["l"]]
    for i in range(1, len(candles)):
        pc = candles[i - 1]["c"]
        tr.append(max(candles[i]["h"] - candles[i]["l"],
                      abs(candles[i]["h"] - pc), abs(candles[i]["l"] - pc)))
    a = sum(tr[1:n + 1]) / n
    out[n] = a
    for i in range(n + 1, len(candles)):
        a = (a * (n - 1) + tr[i]) / n
        out[i] = a
    return out


def build_indicators(candles):
    closes = [c["c"] for c in candles]
    ind = {"e20": ema(closes, 20), "e50": ema(closes, 50),
           "rsi": rsi(closes, 14), "atr": atr(candles, 14)}
    # 1S trend (EMA50/EMA200), her 15dk mum icin son KAPANMIS 1S mumun trendi
    hourly = resample_1h(candles)
    hc = [h["c"] for h in hourly]
    e50h, e200h = ema(hc, 50), ema(hc, 200)
    trend_by_hour = {}
    for idx, h in enumerate(hourly):
        if idx < 200:
            trend_by_hour[h["t"]] = 0
        elif hc[idx] > e50h[idx] > e200h[idx]:
            trend_by_hour[h["t"]] = 1
        elif hc[idx] < e50h[idx] < e200h[idx]:
            trend_by_hour[h["t"]] = -1
        else:
            trend_by_hour[h["t"]] = 0
    trend = []
    for c in candles:
        bar_end = c["t"] + timedelta(minutes=BAR_MIN)
        key = floor_hour(bar_end) - timedelta(hours=1)
        trend.append(trend_by_hour.get(key, 0))
    ind["trend"] = trend
    return ind


# ---------------------------------------------------------------- strateji
def signal_at(i, candles, ind):
    if i < 60:
        return None
    r, a, tr = ind["rsi"][i], ind["atr"][i], ind["trend"][i]
    if r is None or a is None or tr == 0:
        return None
    close, prev = candles[i]["c"], candles[i - 1]["c"]
    e20, e50 = ind["e20"], ind["e50"]
    side = None
    if tr == 1 and e20[i] > e50[i] and prev <= e20[i - 1] and close > e20[i] and 45 < r < 70:
        side = "LONG"
    elif tr == -1 and e20[i] < e50[i] and prev >= e20[i - 1] and close < e20[i] and 30 < r < 55:
        side = "SHORT"
    if side is None:
        return None
    look = candles[i - 7:i + 1]
    if side == "LONG":
        dist = close - (min(x["l"] for x in look) - 0.25 * a)
    else:
        dist = (max(x["h"] for x in look) + 0.25 * a) - close
    dist = min(max(dist, SL_ATR_MIN * a), SL_ATR_MAX * a)
    d = 1 if side == "LONG" else -1
    return {"side": side, "t": candles[i]["t"], "entry": close,
            "sl": close - d * dist, "tp1": close + d * TP1_R * dist,
            "tp2": close + d * TP2_R * dist, "risk": dist, "atr": a, "rsi": r, "trend": tr}


# ---------------------------------------------------------------- backtest
def simulate(s, candles, i, max_bars=96):
    """TP1'de yari kapat + SL'yi girise cek, kalan yari TP2'ye. Ayni mumda SL ve TP
    birlikte gorulurse SL once vurulmus sayilir (muhafazakar)."""
    d = 1 if s["side"] == "LONG" else -1
    entry, risk = s["entry"], s["risk"]
    sl, tp1, tp2 = s["sl"], s["tp1"], s["tp2"]
    tp1_hit, realized = False, 0.0
    last = min(i + max_bars, len(candles) - 1)
    for j in range(i + 1, last + 1):
        b = candles[j]
        hit_sl = b["l"] <= sl if d == 1 else b["h"] >= sl
        hit_tp1 = b["h"] >= tp1 if d == 1 else b["l"] <= tp1
        hit_tp2 = b["h"] >= tp2 if d == 1 else b["l"] <= tp2
        if not tp1_hit:
            if hit_sl:
                return {"R": -1.0 - SPREAD / risk, "exit_i": j, "side": s["side"]}
            if hit_tp1:
                tp1_hit, realized, sl = True, 0.5 * TP1_R, entry
        else:
            if hit_sl:
                return {"R": realized - SPREAD / risk, "exit_i": j, "side": s["side"]}
            if hit_tp2:
                return {"R": realized + 0.5 * TP2_R - SPREAD / risk, "exit_i": j, "side": s["side"]}
    frac = 0.5 if tp1_hit else 1.0
    open_r = d * (candles[last]["c"] - entry) / risk
    return {"R": realized + frac * open_r - SPREAD / risk, "exit_i": last, "side": s["side"]}


def backtest(candles):
    ind = build_indicators(candles)
    trades, i = [], 60
    while i < len(candles) - 1:
        s = signal_at(i, candles, ind)
        if not s:
            i += 1
            continue
        res = simulate(s, candles, i)
        trades.append(res)
        i = res["exit_i"] + 1
    return trades


def print_backtest(trades, candles):
    if not trades:
        print("Hic islem uretilmedi.")
        return
    rs = [t["R"] for t in trades]
    wins = [r for r in rs if r > 0]
    losses = [-r for r in rs if r <= 0]
    eq = peak = dd = 0.0
    for r in rs:
        eq += r
        peak = max(peak, eq)
        dd = max(dd, peak - eq)
    pf = (sum(wins) / sum(losses)) if losses and sum(losses) > 0 else float("inf")
    days = (candles[-1]["t"] - candles[0]["t"]).days or 1
    print(f"Veri araligi : {candles[0]['t']:%Y-%m-%d} -> {candles[-1]['t']:%Y-%m-%d} ({days} gun)")
    print(f"Islem sayisi : {len(rs)}  (LONG {sum(t['side']=='LONG' for t in trades)}, "
          f"SHORT {sum(t['side']=='SHORT' for t in trades)})")
    print(f"Kazanc orani : %{100 * len(wins) / len(rs):.1f}")
    print(f"Ortalama R   : {sum(rs) / len(rs):+.2f}")
    print(f"Toplam R     : {sum(rs):+.1f}")
    print(f"Profit factor: {pf:.2f}")
    print(f"Maks. dusus  : {dd:.1f} R")
    print("Not: gecmis performans gelecegi garanti etmez; spread ve kayma maliyeti SPREAD ile modellenir.")


# ---------------------------------------------------------------- mesaj
def fmt_message(s):
    d = 1 if s["side"] == "LONG" else -1
    emoji = "🟢" if s["side"] == "LONG" else "🔴"
    f = lambda x: f"{x:,.2f}"
    risk = s["risk"]
    lines = [
        f"{emoji} XAU/USD {s['side']} (15dk)",
        f"Entry: {f(s['entry'])}",
        f"SL: {f(s['sl'])}  (-{f(risk)} $)",
        f"TP1: {f(s['tp1'])}  (+{f(TP1_R * risk)} $, {TP1_R:g}R)",
        f"TP2: {f(s['tp2'])}  (+{f(TP2_R * risk)} $, {TP2_R:g}R)",
        f"ATR(14): {s['atr']:.2f} | RSI: {s['rsi']:.0f} | 1S trend: {'yukari' if s['trend'] == 1 else 'asagi'}",
        f"Mum kapanisi: {(s['t'] + timedelta(minutes=BAR_MIN)).astimezone(TR_TZ):%H:%M} (TR)",
        "Plan: TP1'de yarisini kapat, SL'yi girise cek.",
    ]
    acct, risk_pct = os.getenv("ACCOUNT_USD"), os.getenv("RISK_PCT")
    if acct and risk_pct:
        contract = float(os.getenv("CONTRACT_SIZE", "100"))   # 1 lot = 100 oz (brokerini kontrol et)
        lot = float(acct) * float(risk_pct) / 100 / (risk * contract)
        lot = math.floor(lot * 100) / 100
        if lot < 0.01:
            min_risk = risk * contract * 0.01
            lines.append(f"Lot: en kucuk lot (0.01) bile riski %{100 * min_risk / float(acct):.1f}'e "
                         f"({min_risk:.1f} $) cikarir. Hedefin %{risk_pct} ise atla.")
        else:
            lines.append(f"Onerilen lot (%{risk_pct} risk): {lot:.2f}")
    lines.append("Yatirim tavsiyesi degildir.")
    return "\n".join(lines)


def send_telegram(text):
    token, chat = os.environ["TELEGRAM_BOT_TOKEN"], os.environ["TELEGRAM_CHAT_ID"]
    r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                      json={"chat_id": chat, "text": text}, timeout=30)
    if not r.ok:
        raise RuntimeError(f"Telegram hatasi: {r.status_code} {r.text}")


def get_chat_id():
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    r = requests.get(f"https://api.telegram.org/bot{token}/getUpdates", timeout=30).json()
    seen = {}
    for u in r.get("result", []):
        m = u.get("message") or u.get("channel_post") or {}
        ch = m.get("chat")
        if ch:
            seen[ch["id"]] = ch.get("first_name") or ch.get("title") or ""
    if not seen:
        print("Mesaj yok. Once Telegram'da botuna bir mesaj (ornegin /start) gonder, sonra tekrar calistir.")
    for cid, name in seen.items():
        print(f"chat_id: {cid}  ({name})")


# ---------------------------------------------------------------- calistirma
def market_closed(now):
    wd, h = now.weekday(), now.hour       # Pzt=0 ... Paz=6 (UTC)
    return wd == 5 or (wd == 4 and h >= 21) or (wd == 6 and h < 22)


def in_blackout(now):
    """BLACKOUT_UTC="2026-11-25 13:30,2026-12-03 13:30" -> bu saatlerin +-BLACKOUT_MIN dk'si sinyal yok."""
    mins = int(os.getenv("BLACKOUT_MIN", "60"))
    for part in os.getenv("BLACKOUT_UTC", "").split(","):
        part = part.strip()
        if not part:
            continue
        t = datetime.strptime(part, "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
        if t - timedelta(minutes=mins) <= now <= t + timedelta(minutes=mins):
            return True
    return False


def load_state():
    try:
        with open(STATE_FILE) as fh:
            return json.load(fh)
    except Exception:
        return {}


def save_state(s):
    with open(STATE_FILE, "w") as fh:
        json.dump({"side": s["side"], "t": s["t"].isoformat()}, fh)


def run_once(now=None, candles=None, send=True):
    now = now or datetime.now(timezone.utc)
    if market_closed(now):
        print(f"{now:%H:%M} UTC piyasa kapali, atlandi.")
        return None
    candles = drop_forming(candles or fetch_candles(), now)
    if len(candles) < 300:
        print("Yeterli veri yok.")
        return None
    last_end = candles[-1]["t"] + timedelta(minutes=BAR_MIN)
    if now - last_end > timedelta(minutes=2 * BAR_MIN + 5):
        print("Son mum cok eski (piyasa kapali / veri gecikmesi), atlandi.")
        return None
    ind = build_indicators(candles)
    s = signal_at(len(candles) - 1, candles, ind)
    if not s:
        print(f"{now:%H:%M} UTC sinyal yok.")
        return None
    if in_blackout(now):
        print("Haber penceresi, sinyal gonderilmedi.")
        return None
    st = load_state()
    if st.get("side") == s["side"]:
        prev_t = datetime.fromisoformat(st["t"])
        if s["t"] - prev_t < timedelta(minutes=COOLDOWN_MIN):
            print("Bekleme suresi icinde ayni yonde sinyal, atlandi.")
            return None
    msg = fmt_message(s)
    print(msg)
    if send:
        send_telegram(msg)
        save_state(s)
    return s


def sleep_to_next_bar():
    now = time.time()
    nxt = (int(now // (BAR_MIN * 60)) + 1) * BAR_MIN * 60 + 20
    time.sleep(max(1, nxt - now))


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--get-chat-id", action="store_true")
    p.add_argument("--test", action="store_true")
    p.add_argument("--backtest", action="store_true")
    p.add_argument("--bars", type=int, default=5000)
    p.add_argument("--once", action="store_true")
    p.add_argument("--loop", action="store_true")
    a = p.parse_args()
    if a.get_chat_id:
        get_chat_id()
    elif a.test:
        send_telegram("✅ XAU/USD sinyal botu calisiyor. Bu bir deneme mesajidir.")
        print("Deneme mesaji gonderildi.")
    elif a.backtest:
        c = fetch_candles(a.bars)
        print_backtest(backtest(c), c)
    elif a.loop:
        while True:
            try:
                run_once()
            except Exception as e:                       # tek hata botu durdurmasin
                print("Hata:", e, file=sys.stderr)
            sleep_to_next_bar()
    else:
        run_once()


if __name__ == "__main__":
    main()
