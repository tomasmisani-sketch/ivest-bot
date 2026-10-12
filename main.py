import os
import json
import re
import time
import threading
import requests
import gspread
import pandas as pd
import yfinance as yf
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from flask import Flask, request, jsonify
from google import genai
from google.genai import types
from google.oauth2.service_account import Credentials

app = Flask(__name__)

# ------------------------------------------------------------------
# 1. PREMENNÉ PROSTREDIA
# ------------------------------------------------------------------
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
GOOGLE_CREDENTIALS_RAW = os.getenv("GOOGLE_CREDENTIALS")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
CRON_SECRET = os.getenv("CRON_SECRET")  # voliteľné, ochrana endpointu /cena
MIN_RRR = float(os.getenv("MIN_RRR", "1.5"))        # minimálne RRR obchodu (varovanie)
BE_RRR = float(os.getenv("BE_RRR", "1.0"))          # pri akom RRR upozorniť na posun SL na BE
MIN_SL_ATR = float(os.getenv("MIN_SL_ATR", "1.0"))  # minimálna vzdialenosť SL v násobkoch ATR

# Hlavný model sa dá zmeniť cez premennú GEMINI_MODEL bez zásahu do kódu
PRIMARY_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite")
# Voliteľné záložné modely oddelené čiarkou (over ich cez /modely)
FALLBACK_MODELS = [m.strip() for m in os.getenv("GEMINI_FALLBACK_MODELS", "gemini-3.1-flash-lite").split(",") if m.strip()]
MODELS = [PRIMARY_MODEL] + [m for m in FALLBACK_MODELS if m != PRIMARY_MODEL]

TELEGRAM_API_URL = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"

SYMBOL_MAP = {
    "GOLD": "GC=F",
    "SILVER": "SI=F",
    "OIL": "CL=F",
    "EURUSD": "EURUSD=X",
    "GBPUSD": "GBPUSD=X",
    "USDJPY": "JPY=X",
    "BTC": "BTC-USD",
    "ETH": "ETH-USD",
    "US500": "^GSPC",
    "US100": "^IXIC",
}

# Inštrumenty z TradingView (cena + EMA200 + ATR14 na D1).
# spread  = typický spread v XTB v cenových jednotkách (UPRAV podľa XTB!)
# offset  = posun TV ceny voči XTB (zatiaľ 0, rozdiel je zanedbateľný)
# contract = veľkosť 1 lotu, quote = kótovacia mena (na prepočet rizika do EUR)
INSTRUMENTS = {
    "GOLD":   {"tv": "TVC:GOLD",  "spread": 0.75,    "offset": 0.0, "contract": 100,    "quote": "USD", "digits": 3},
}
_fail_streak = {}
TV_CACHE_SECONDS = 60
_tv_cache = {}

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

sheet = None
gc = None
try:
    creds_dict = json.loads(GOOGLE_CREDENTIALS_RAW)
    credentials = Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
    gc = gspread.authorize(credentials)
    sheet = gc.open("Investicny Bot").sheet1
    print("✅ Úspešne pripojené ku Google Sheets")
except Exception as e:
    print(f"❌ Chyba pripojenia ku Google Sheets: {e}")

ai_client = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else None


# ------------------------------------------------------------------
# 2. POMOCNÉ FUNKCIE
# ------------------------------------------------------------------
def send_telegram_msg(chat_id, text, reply_markup=None, markdown=True):
    """Pošle správu. Dlhé texty rozdelí a pri chybe Markdownu pošle čistý text."""
    chunks = [text[i:i + 4000] for i in range(0, len(text), 4000)] or [""]
    for n, chunk in enumerate(chunks):
        payload = {"chat_id": chat_id, "text": chunk}
        if markdown:
            payload["parse_mode"] = "Markdown"
        if reply_markup and n == len(chunks) - 1:
            payload["reply_markup"] = reply_markup
        try:
            r = requests.post(f"{TELEGRAM_API_URL}/sendMessage", json=payload, timeout=15)
            if not r.ok and markdown:
                payload.pop("parse_mode", None)
                requests.post(f"{TELEGRAM_API_URL}/sendMessage", json=payload, timeout=15)
        except Exception as e:
            print(f"Chyba odoslania Telegram správy: {e}")


def resolve_ticker(symbol):
    sym = symbol.upper()
    return SYMBOL_MAP.get(sym, sym)


def get_market_data(symbol):
    # Inštrumenty z INSTRUMENTS idú výhradne cez TradingView (bez fallbacku na yfinance,
    # lebo napr. GC=F je futures a líši sa od TVC:GOLD). Pri chybe vráti None.
    cfg = INSTRUMENTS.get(symbol.upper())
    if cfg:
        try:
            d = get_tv_data(cfg["tv"])
            dg = cfg["digits"]
            return {
                "symbol": symbol.upper(),
                "price": round(d["price"], dg),
                "ema200": round(d["ema200"], dg),
                "atr14": round(d["atr14"], dg),
            }
        except Exception as e:
            print(f"Chyba pri sťahovaní TradingView dát pre {symbol}: {e}")
            return None

    ticker_str = resolve_ticker(symbol)
    try:
        df = yf.Ticker(ticker_str).history(period="1y", interval="1d")
        if df.empty:
            return None

        df["EMA_200"] = df["Close"].ewm(span=200, adjust=False).mean()
        high_low = df["High"] - df["Low"]
        high_close = (df["High"] - df["Close"].shift()).abs()
        low_close = (df["Low"] - df["Close"].shift()).abs()
        tr = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
        df["ATR_14"] = tr.rolling(14).mean()

        latest = df.iloc[-1]
        digits = 4 if ("USD" in symbol.upper() or "=" in ticker_str) else 2
        return {
            "symbol": symbol.upper(),
            "price": round(float(latest["Close"]), digits),
            "ema200": round(float(latest["EMA_200"]), digits),
            "atr14": round(float(latest["ATR_14"]), digits),
        }
    except Exception as e:
        print(f"Chyba pri stiahnutí yfinance dát pre {symbol}: {e}")
        return None


def get_tv_data(tv_symbol="TVC:GOLD"):
    """Cena, EMA200 a ATR14 (D1) z TradingView scanneru. 3 pokusy, 60 s cache."""
    now = time.time()
    cached = _tv_cache.get(tv_symbol)
    if cached and now - cached[0] < TV_CACHE_SECONDS:
        return cached[1]

    last_err = None
    for attempt in range(3):
        try:
            r = requests.post(
                "https://scanner.tradingview.com/global/scan",
                json={"symbols": {"tickers": [tv_symbol]}, "columns": ["close", "EMA200", "ATR"]},
                timeout=15,
            )
            r.raise_for_status()
            rows = r.json().get("data") or []
            if not rows:
                raise ValueError(f"TradingView nevrátil dáta pre {tv_symbol}")
            close, ema200, atr = rows[0]["d"]
            if None in (close, ema200, atr):
                raise ValueError(f"Neúplné dáta pre {tv_symbol}: {rows[0]['d']}")
            result = {"price": float(close), "ema200": float(ema200), "atr14": float(atr)}
            _tv_cache[tv_symbol] = (time.time(), result)
            return result
        except Exception as e:
            last_err = e
            print(f"TradingView pokus {attempt + 1}/3 zlyhal [{tv_symbol}]: {e}")
            time.sleep(1.5)
    raise last_err


def get_tv_price(tv_symbol="TVC:GOLD"):
    return get_tv_data(tv_symbol)["price"]


def get_price_sheet():
    """Samostatný hárok 'Ceny', aby sa nepomiešal s pozíciami na sheet1."""
    ss = gc.open("Investicny Bot")
    try:
        return ss.worksheet("Ceny")
    except gspread.WorksheetNotFound:
        ws = ss.add_worksheet(title="Ceny", rows=1000, cols=3)
        ws.append_row(["DATETIME", "SYMBOL", "PRICE"])
        return ws


def market_open():
    """Víkendové zatvorenie (Pi 22:00 -> Ne 23:00 bratislavského času)."""
    now = datetime.now(ZoneInfo("Europe/Bratislava"))
    wd, h = now.weekday(), now.hour
    if wd == 5 or (wd == 6 and h < 23) or (wd == 4 and h >= 22):
        return False
    return True


def effective_exit_price(price, direction, cfg):
    """Cena, za ktorú by sa pozícia reálne zavrela v XTB:
    LONG sa zatvára na bid (cena - pol spreadu), SHORT na ask (cena + pol spreadu)."""
    if not cfg:
        return price
    price = price + cfg["offset"]
    half = cfg["spread"] / 2
    return price - half if direction == "LONG" else price + half


def note_data_failure(symbol, chat_id):
    n = _fail_streak.get(symbol, 0) + 1
    _fail_streak[symbol] = n
    if n == 3:  # raz za sériu výpadkov
        send_telegram_msg(
            chat_id,
            f"⚠️ Cenu {symbol} sa nepodarilo stiahnuť 3x po sebe. "
            f"Kontrola SL/TP/BE momentálne nefunguje!",
            markdown=False,
        )


def note_data_ok(symbol):
    _fail_streak[symbol] = 0


def risk_in_eur(symbol, sl_dist, volume):
    cfg = INSTRUMENTS[symbol]
    risk_quote = sl_dist * volume * cfg["contract"]
    if cfg["quote"] == "EUR":
        return round(risk_quote, 2)
    if cfg["quote"] == "USD":
        # EURUSD sa používa len ako kurz na prepočet USD -> EUR
        return round(risk_quote / get_tv_price("FX:EURUSD"), 2)
    raise ValueError("neznáma kótovacia mena")


def to_float(v):
    """Číslo z bunky aj pri slovenskom formáte (4194,6 alebo 4 194,6)."""
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return float(v)
    return float(str(v).strip().replace("\u00a0", "").replace(" ", "").replace(",", "."))


def get_positions():
    """Riadky pozícií s presnými číslami. Bez UNFORMATTED_VALUE gspread pri slovenskom
    formáte bunky číta '4194,6' ako 41946 (čiarku berie ako oddeľovač tisícov)."""
    try:
        return sheet.get_all_records(value_render_option="UNFORMATTED_VALUE")
    except TypeError:
        print("⚠️ Stará verzia gspread, čísla sa čítajú vo formáte bunky. Aktualizuj gspread.")
        return sheet.get_all_records()


def find_row_by_id(pos_id):
    for i, v in enumerate(sheet.col_values(1), start=1):
        if i > 1 and str(v) == str(pos_id):
            return i
    raise ValueError(f"Pozícia ID {pos_id} sa v tabuľke nenašla")


def next_position_id():
    nums = [int(x) for x in sheet.col_values(1)[1:] if str(x).isdigit()]
    return max(nums, default=0) + 1


def generate_analysis(prompt):
    """Skúsi modely postupne. Najprv s Google Search, potom bez neho."""
    if not ai_client:
        raise RuntimeError("Chýba GEMINI_API_KEY.")

    first_error = None
    for model in MODELS:
        for grounding in (True, False):
            try:
                config = None
                if grounding:
                    config = types.GenerateContentConfig(
                        tools=[types.Tool(google_search=types.GoogleSearch())]
                    )
                response = ai_client.models.generate_content(
                    model=model, contents=prompt, config=config
                )
                text = response.text
                if text:
                    note = "" if grounding else "\n\n(ℹ️ Analýza bez vyhľadávania na webe.)"
                    return text + note
            except Exception as e:
                if first_error is None:
                    first_error = e  # hlásime prvú (skutočnú) chybu
                print(f"Gemini chyba [{model}, grounding={grounding}]: {e}")
    raise RuntimeError(first_error)


# ------------------------------------------------------------------
# ROZŠÍRENÁ ANALÝZA: TradingView dáta + kalendár + validácia + log
# ------------------------------------------------------------------
TV_D1 = ["close", "open", "high", "low", "change", "EMA20", "EMA50", "EMA200", "RSI",
         "MACD.macd", "MACD.signal", "ADX", "ATR", "BB.upper", "BB.lower", "Perf.W", "Perf.1M"]
TV_H4 = ["RSI|240", "EMA50|240", "EMA200|240", "ATR|240"]
TV_W1 = ["RSI|1W", "EMA50|1W", "EMA200|1W"]
TV_LEVELS = [
    ("Pivot S3 (týždenný)", "Pivot.M.Classic.S3|1W"),
    ("Pivot S2 (týždenný)", "Pivot.M.Classic.S2|1W"),
    ("Pivot S1 (týždenný)", "Pivot.M.Classic.S1|1W"),
    ("Pivot stred (týždenný)", "Pivot.M.Classic.Middle|1W"),
    ("Pivot R1 (týždenný)", "Pivot.M.Classic.R1|1W"),
    ("Pivot R2 (týždenný)", "Pivot.M.Classic.R2|1W"),
    ("Pivot R3 (týždenný)", "Pivot.M.Classic.R3|1W"),
    ("Maximum 1M", "High.1M"),
    ("Minimum 1M", "Low.1M"),
    ("Maximum 3M", "High.3M"),
    ("Minimum 3M", "Low.3M"),
    ("Maximum 6M", "High.6M"),
    ("Minimum 6M", "Low.6M"),
    ("52-týždňové maximum", "price_52_week_high"),
    ("52-týždňové minimum", "price_52_week_low"),
]
TV_MACRO = [
    ("DXY (sila dolára)", "TVC:DXY"),
    ("US 10Y výnos %", "TVC:US10Y"),
    ("US 2Y výnos %", "TVC:US02Y"),
    ("VIX (strach)", "TVC:VIX"),
    ("S&P 500", "SP:SPX"),
    ("Striebro", "TVC:SILVER"),
]
TV_MACRO_COLS = ["close", "change", "Perf.W", "Perf.1M", "EMA200"]
CAL_KEYWORDS = ("cpi", "pce", "payroll", "nonfarm", "fomc", "fed ", "interest rate",
                "unemployment", "gdp", "ppi", "retail sales", "powell")


def _num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _f(v, nd=2):
    return f"{v:.{nd}f}" if _num(v) else "n/a"


def tv_scan(tickers, columns):
    r = requests.post(
        "https://scanner.tradingview.com/global/scan",
        json={"symbols": {"tickers": tickers}, "columns": columns},
        timeout=15,
    )
    r.raise_for_status()
    out = {}
    for row in r.json().get("data") or []:
        out[row["s"]] = dict(zip(columns, row["d"]))
    return out


def tv_scan_safe(tickers, columns):
    """Skúsi všetky stĺpce naraz. Ak scanner niektorý stĺpec odmietne (4xx),
    vezme ich po jednom a vráti zoznam stĺpcov, ktoré neexistujú."""
    try:
        return tv_scan(tickers, columns), []
    except requests.HTTPError as e:
        code = e.response.status_code if e.response is not None else 0
        if not (400 <= code < 500):
            raise
        print(f"Scanner odmietol stĺpce ({code}), skúšam po jednom...")
    result, missing = {t: {} for t in tickers}, []
    for col in columns:
        try:
            for t, vals in tv_scan(tickers, [col]).items():
                result.setdefault(t, {}).update(vals)
        except Exception:
            missing.append(col)
    return result, missing


def get_calendar_text():
    """Dôležité US udaje za posledných 48 h a najbližšie 3 dni (TradingView kalendár)."""
    try:
        now = datetime.now(timezone.utc)
        fmt = "%Y-%m-%dT%H:%M:%S.000Z"
        r = requests.get(
            "https://economic-calendar.tradingview.com/events",
            params={
                "from": (now - timedelta(hours=48)).strftime(fmt),
                "to": (now + timedelta(hours=72)).strftime(fmt),
                "countries": "US",
            },
            headers={"Origin": "https://www.tradingview.com"},
            timeout=15,
        )
        r.raise_for_status()
        events = r.json().get("result") or []
    except Exception as e:
        print(f"Chyba sťahovania kalendára: {e}")
        return None

    tz = ZoneInfo("Europe/Bratislava")
    past, future = [], []
    for ev in events:
        title = str(ev.get("title", ""))
        important = (ev.get("importance") or 0) >= 1 or any(k in title.lower() for k in CAL_KEYWORDS)
        if not important:
            continue
        try:
            dt = datetime.fromisoformat(str(ev["date"]).replace("Z", "+00:00"))
        except Exception:
            continue
        head = f"- {dt.astimezone(tz).strftime('%d.%m. %H:%M')} | {title} {ev.get('period') or ''}".rstrip()
        if dt <= now:
            past.append((dt, f"{head} | skutočnosť: {ev.get('actual')} | očakávanie: {ev.get('forecast')} | predtým: {ev.get('previous')}"))
        else:
            future.append((dt, f"{head} | očakávanie: {ev.get('forecast')} | predtým: {ev.get('previous')}"))

    past.sort(key=lambda x: x[0])
    future.sort(key=lambda x: x[0])
    out = ["Vydané (posledných 48 h):"]
    out += [x[1] for x in past[-12:]] or ["- žiadne dôležité udalosti"]
    out.append("Nadchádzajúce (3 dni):")
    out += [x[1] for x in future[:12]] or ["- žiadne dôležité udalosti"]
    return "\n".join(out)


def build_levels(price, d):
    levels = [(label, d.get(key)) for label, key in TV_LEVELS]
    levels += [("EMA50 (D1)", d.get("EMA50")), ("EMA200 (D1)", d.get("EMA200"))]
    levels = [(l, v) for l, v in levels if _num(v)]
    above = sorted([x for x in levels if x[1] > price], key=lambda x: x[1])[:4]
    below = sorted([x for x in levels if x[1] < price], key=lambda x: -x[1])[:4]
    lines = ["Rezistencie nad cenou (od najbližšej):"]
    lines += [f"- {_f(v)} ({l})" for l, v in above] or ["- žiadne dáta"]
    lines.append("Supporty pod cenou (od najbližšieho):")
    lines += [f"- {_f(v)} ({l})" for l, v in below] or ["- žiadne dáta"]
    return "\n".join(lines)


def build_technical_text(price, d):
    L = [f"- Cena: {_f(price)} | dnešná sviečka: open {_f(d.get('open'))}, high {_f(d.get('high'))}, "
         f"low {_f(d.get('low'))}, zmena {_f(d.get('change'))} %"]
    for name in ("EMA20", "EMA50", "EMA200"):
        v = d.get(name)
        if _num(v):
            L.append(f"- {name} (D1): {_f(v)} → cena je {'NAD' if price > v else 'POD'}")
    atr, ema200 = d.get("ATR"), d.get("EMA200")
    if _num(atr) and atr:
        L.append(f"- ATR14 (D1): {_f(atr)}")
        if _num(ema200):
            L.append(f"- Vzdialenosť od EMA200: {(price - ema200) / atr:+.1f}× ATR")
    rsi = d.get("RSI")
    if _num(rsi):
        zone = "prekúpené" if rsi > 70 else "prepredané" if rsi < 30 else "neutrálne"
        L.append(f"- RSI14 (D1): {_f(rsi, 1)} ({zone})")
    macd, sig = d.get("MACD.macd"), d.get("MACD.signal")
    if _num(macd) and _num(sig):
        L.append(f"- MACD (D1): {_f(macd)} vs signál {_f(sig)} → {'bullish' if macd > sig else 'bearish'}")
    adx = d.get("ADX")
    if _num(adx):
        strength = "silný trend" if adx > 25 else "slabý trend / range" if adx < 20 else "stredný trend"
        L.append(f"- ADX (D1): {_f(adx, 1)} ({strength})")
    if _num(d.get("BB.upper")) and _num(d.get("BB.lower")):
        L.append(f"- Bollinger (D1): horné {_f(d['BB.upper'])}, dolné {_f(d['BB.lower'])}")
    L.append(f"- Výkonnosť: týždeň {_f(d.get('Perf.W'))} %, mesiac {_f(d.get('Perf.1M'))} %")

    h4 = [f"RSI {_f(d.get('RSI|240'), 1)}"]
    for k in ("EMA50|240", "EMA200|240"):
        v = d.get(k)
        if _num(v):
            h4.append(f"{k.split('|')[0]} {_f(v)} ({'cena nad' if price > v else 'cena pod'})")
    L.append("- H4: " + ", ".join(h4))
    w1 = [f"RSI {_f(d.get('RSI|1W'), 1)}"]
    for k in ("EMA50|1W", "EMA200|1W"):
        v = d.get(k)
        if _num(v):
            w1.append(f"{k.split('|')[0]} {_f(v)} ({'cena nad' if price > v else 'cena pod'})")
    L.append("- Týždenný: " + ", ".join(w1))
    return "\n".join(L)


def build_macro_text(macro, gold_price):
    L = []
    for label, tkr in TV_MACRO:
        m = macro.get(tkr) or {}
        if not _num(m.get("close")):
            L.append(f"- {label}: n/a")
            continue
        trend = f", {'nad' if m['close'] > m['EMA200'] else 'pod'} EMA200" if _num(m.get("EMA200")) else ""
        L.append(f"- {label}: {_f(m['close'])} (deň {_f(m.get('change'))} %, týždeň {_f(m.get('Perf.W'))} %, "
                 f"mesiac {_f(m.get('Perf.1M'))} %{trend})")
    silver = (macro.get("TVC:SILVER") or {}).get("close")
    if _num(silver) and silver:
        L.append(f"- Pomer zlato/striebro: {gold_price / silver:.1f}")
    return "\n".join(L)


def split_ai_json(text):
    """Oddelí záverečný JSON blok od textu analýzy."""
    m = re.search(r"```json\s*(\{.*?\})\s*```", text, re.S)
    if not m:
        found = re.findall(r'\{[^{}]*"direction"[^{}]*\}', text)
        if not found:
            return text.strip(), None
        raw, span = found[-1], (text.rfind(found[-1]), text.rfind(found[-1]) + len(found[-1]))
    else:
        raw, span = m.group(1), m.span()
    try:
        plan = json.loads(raw)
    except Exception:
        return text.strip(), None
    return (text[:span[0]] + text[span[1]:]).strip(), plan


def validate_plan(plan, price, atr):
    """Kód (nie AI) skontroluje návrh. Vráti (údaje, zoznam varovaní)."""
    if not plan:
        return None, ["AI nevrátila strojovo čitateľný návrh obchodu."]
    direction = str(plan.get("direction", "")).upper()
    try:
        score = float(plan.get("score"))
    except Exception:
        score = None
    if direction not in ("LONG", "SHORT"):
        return {"direction": "NONE", "score": score}, []
    try:
        entry = float(plan.get("entry") or price)
        sl, tp = float(plan["sl"]), float(plan["tp"])
    except Exception:
        return {"direction": direction, "score": score}, ["AI nevrátila platné SL/TP."]

    warnings = []
    side_ok = (direction == "LONG" and sl < entry < tp) or (direction == "SHORT" and tp < entry < sl)
    if not side_ok:
        warnings.append("SL/TP sú na zlej strane vstupu.")
    sl_dist = abs(entry - sl)
    rrr = abs(tp - entry) / sl_dist if sl_dist else None
    sl_atr = sl_dist / atr if _num(atr) and atr else None
    if sl_atr is not None:
        if sl_atr < MIN_SL_ATR:
            warnings.append(f"SL je užší než {MIN_SL_ATR:g}× ATR ({sl_atr:.2f}×), hrozí vystopovanie šumom.")
        elif sl_atr > 3:
            warnings.append(f"SL je veľmi široký ({sl_atr:.2f}× ATR).")
    if rrr is not None and rrr < MIN_RRR:
        warnings.append(f"RRR 1:{rrr:.2f} je pod minimom 1:{MIN_RRR:g}.")
    if _num(atr) and atr and abs(entry - price) > 0.5 * atr:
        warnings.append("Navrhovaný vstup je ďaleko od aktuálnej ceny.")
    return {"direction": direction, "score": score, "entry": entry, "sl": sl, "tp": tp,
            "rrr": rrr, "sl_atr": sl_atr}, warnings


def get_analysis_sheet():
    ss = gc.open("Investicny Bot")
    try:
        return ss.worksheet("Analyzy")
    except gspread.WorksheetNotFound:
        ws = ss.add_worksheet(title="Analyzy", rows=1000, cols=13)
        ws.append_row(["DATETIME", "SYMBOL", "PRICE", "DIRECTION", "SCORE", "ENTRY", "SL", "TP",
                       "RRR", "SL_ATR", "VALID", "WARNINGS", "OUTCOME"])
        return ws


def log_analysis(symbol, price, info, warnings):
    try:
        info = info or {}
        now = datetime.now(ZoneInfo("Europe/Bratislava")).strftime("%Y-%m-%d %H:%M")
        rnd = lambda v, n=2: round(v, n) if _num(v) else ""
        get_analysis_sheet().append_row([
            now, symbol, price, info.get("direction", ""), info.get("score", ""),
            rnd(info.get("entry")), rnd(info.get("sl")), rnd(info.get("tp")),
            rnd(info.get("rrr")), rnd(info.get("sl_atr")),
            "OK" if not warnings else "WARN", " | ".join(warnings), "",
        ])
    except Exception as e:
        print(f"Chyba logu analýzy do Sheets: {e}")


def run_enriched_analysis(chat_id, symbol):
    cfg = INSTRUMENTS[symbol]
    try:
        cols = TV_D1 + TV_H4 + TV_W1 + [k for _, k in TV_LEVELS]
        data, missing = tv_scan_safe([cfg["tv"]], cols)
        d = data.get(cfg["tv"]) or {}
        if not _num(d.get("close")):
            raise ValueError("TradingView nevrátil cenu")
        macro, miss2 = tv_scan_safe([t for _, t in TV_MACRO], TV_MACRO_COLS)
        missing += miss2
    except Exception as e:
        send_telegram_msg(chat_id, f"❌ Nepodarilo sa stiahnuť dáta z TradingView: {e}", markdown=False)
        return

    price, atr = d["close"], d.get("ATR")
    calendar = get_calendar_text()
    if calendar is None:
        calendar = ("(Kalendár sa nepodarilo stiahnuť. Vyhľadaj ho sám na webe: US CPI, PCE, NFP, FOMC "
                    "za posledných 48 h a nasledujúce 3 dni.)")

    prompt = (
        f"Si špičkový finančný a makroekonomický analytik pre {symbol} "
        f"(zlato, CFD v XTB, swing obchodovanie na 7-10 dní).\n\n"
        f"Číselné dáta sú z TradingView ({cfg['tv']}). Cenové úrovne si NEVYMÝŠĽAJ, použi len tie z blokov nižšie.\n\n"
        f"TECHNICKÁ ANALÝZA (D1, H4, týždenný):\n{build_technical_text(price, d)}\n\n"
        f"KĽÚČOVÉ ÚROVNE (vypočítané z dát):\n{build_levels(price, d)}\n\n"
        f"MAKRO KONTEXT (sila dolára, výnosy, riziko):\n{build_macro_text(macro, price)}\n\n"
        f"EKONOMICKÝ KALENDÁR USA (čas SK):\n{calendar}\n\n"
        "INŠTRUKCIE:\n"
        "1. Vyhodnoť technický obraz (trend, momentum, kľúčové zóny) z dát vyššie.\n"
        "2. Vyhodnoť vplyv sily dolára (DXY) a výnosov na zlato. Zlato sa zvyčajne hýbe opačne ako dolár "
        "a reálne výnosy, ale vzťah nie je stabilný.\n"
        "3. Porovnaj vydané údaje v kalendári (skutočnosť vs. očakávanie) a upozorni na udalosti v najbližších "
        "dňoch (inflácia, Fed, trh práce), ktoré môžu hýbať cenou.\n"
        "4. Vyhľadaj na webe správy za posledných 24-48 hodín: očakávania ohľadom sadzieb Fedu, dátum "
        "najbližšieho zasadnutia FOMC, geopolitika, nákupy centrálnych bánk, toky do zlatých ETF.\n"
        "5. Daj skóre obchodu 0-10 a navrhni jeden smer (LONG alebo SHORT), prípadne NONE, ak nie je dobrý setup.\n"
        f"6. Stop Loss a Take Profit zdôvodni úrovňami z blokov vyššie a ATR. SL aspoň {MIN_SL_ATR:g}× ATR od vstupu, "
        f"RRR aspoň 1:{MIN_RRR:g}.\n"
        "Odpovedaj prehľadne v slovenčine s odrážkami, bez tabuliek.\n\n"
        "Na ÚPLNÝ KONIEC pridaj presne jeden blok (nuly nahraď skutočnými číslami):\n"
        '```json\n{"direction": "LONG", "score": 7, "entry": 0, "sl": 0, "tp": 0}\n```'
    )

    try:
        raw = generate_analysis(prompt)
    except Exception as e:
        send_telegram_msg(chat_id, f"❌ Chyba pri generovaní AI analýzy: {e}", markdown=False)
        return

    text, plan = split_ai_json(raw)
    info, warnings = validate_plan(plan, price, atr)

    check = ["🔎 KONTROLA NÁVRHU (počíta kód, nie AI)"]
    if info and info.get("direction") in ("LONG", "SHORT") and "sl" in info:
        check.append(f"Smer: {info['direction']} | Skóre: {_f(info.get('score'), 0)}/10")
        check.append(f"Vstup {_f(info['entry'])} | SL {_f(info['sl'])} | TP {_f(info['tp'])}")
        if info.get("rrr") is not None:
            sl_part = f" | SL = {info['sl_atr']:.2f}× ATR" if info.get("sl_atr") is not None else ""
            check.append(f"RRR 1:{info['rrr']:.2f}{sl_part}")
    elif info and info.get("direction") == "NONE":
        check.append(f"AI nenavrhuje obchod (skóre {_f(info.get('score'), 0)}/10).")
    check += [f"⚠️ {w}" for w in warnings] or ["✅ Parametre prešli kontrolou."]
    if missing:
        check.append("ℹ️ Chýbajúce dáta z TradingView: " + ", ".join(missing[:8]))

    send_telegram_msg(chat_id, f"📊 KOMPLEXNÁ ANALÝZA: {symbol}\n\n{text}\n\n" + "\n".join(check), markdown=False)
    log_analysis(symbol, price, info, warnings)


def run_analysis(chat_id, symbol):
    if symbol.upper() in INSTRUMENTS:
        return run_enriched_analysis(chat_id, symbol.upper())
    market_data = get_market_data(symbol)
    if not market_data:
        send_telegram_msg(chat_id, f"❌ Nepodarilo sa stiahnuť dáta pre inštrument `{symbol}`.")
        return

    prompt = (
        f"Si špičkový finančný a makroekonomický analytik pre inštrument {symbol} "
        f"(swing obchodovanie na 7-10 dní).\n\n"
        f"DÁTA Z TRHU:\n"
        f"- Aktuálna cena: {market_data['price']}\n"
        f"- EMA 200 (D1): {market_data['ema200']}\n"
        f"- ATR 14: {market_data['atr14']}\n\n"
        f"INŠTRUKCIE:\n"
        f"1. Vyhľadaj najnovšie makroekonomické správy a udalosti za posledných 24-48 hodín pre {symbol}.\n"
        f"2. Zohľadni technické dáta aj fundamentálny sentiment.\n"
        f"3. Vyhodnoť kľúčové zóny (support/rezistencia).\n"
        f"4. Daj celkové skóre obchodu (0 až 10).\n"
        f"5. Navrhni konkrétny Stop Loss a Take Profit.\n"
        f"Odpovedaj prehľadne v slovenčine s použitím odrážok."
    )

    try:
        output = generate_analysis(prompt)
        # AI text posielame bez Markdownu, aby nepadol na špeciálnych znakoch
        send_telegram_msg(chat_id, f"📊 KOMPLEXNÁ ANALÝZA: {symbol}\n\n{output}", markdown=False)
    except Exception as e:
        send_telegram_msg(chat_id, f"❌ Chyba pri generovaní AI analýzy: {e}", markdown=False)


# ------------------------------------------------------------------
# 3. POSITION MANAGER & DENNÝ REPORT
# ------------------------------------------------------------------
def check_positions_job():
    if sheet is None:
        return
    try:
        records = get_positions()
        for index, row in enumerate(records, start=2):
            if str(row.get("STATUS", "")).upper() != "OPEN":
                continue

            symbol = str(row.get("SYMBOL", ""))
            direction = str(row.get("DIRECTION", "")).upper()
            try:
                entry_price = to_float(row.get("ENTRY"))
                sl = to_float(row.get("SL"))
                tp = to_float(row.get("TP"))
                be_price = to_float(row.get("BE_PRICE"))
            except (ValueError, TypeError):
                print(f"Pozícia v riadku {index}: nečitateľné číslo (ENTRY/SL/TP/BE_PRICE), preskakujem.")
                continue
            be_done = str(row.get("BE_DONE", "")).upper() == "TRUE"
            be_ignored = str(row.get("BE_DONE", "")).upper() == "IGNORED"
            chat_id = row.get("CHAT_ID")
            pos_id = list(row.values())[0]  # prvý stĺpec = ID obchodu

            market_info = get_market_data(symbol)
            if not market_info:
                note_data_failure(symbol, chat_id)
                continue
            note_data_ok(symbol)

            current_price = market_info["price"]
            cfg = INSTRUMENTS.get(symbol.upper())
            px = effective_exit_price(current_price, direction, cfg)

            eff_sl = entry_price if be_done else sl  # po posune SL na BE platí vstup
            sl_hit = (direction == "LONG" and px <= eff_sl) or (direction == "SHORT" and px >= eff_sl)
            if sl_hit:
                if be_done:
                    sheet.update_cell(index, 12, "CLOSED_BE")
                    send_telegram_msg(
                        chat_id,
                        f"⚪ *POZÍCIA VRÁTENÁ NA VSTUP (BE): {symbol} {direction}*\n\n"
                        f"Aktuálna cena (`{current_price}`) je na vstupe (`{entry_price}`). "
                        f"Pozícia sa zavrela bez straty (okrem spreadu).",
                    )
                else:
                    sheet.update_cell(index, 12, "CLOSED_SL")
                    send_telegram_msg(
                        chat_id,
                        f"🔴 *STOP LOSS DOSIAHNUTÝ: {symbol} {direction}*\n\n"
                        f"Aktuálna cena (`{current_price}`) zasiahla SL (`{sl}`). Pozícia bola uzavretá v strate.\n"
                        f"Vzdialenosť od SL: `{round(abs(px - sl), 5)}`",
                    )
                continue

            tp_hit = (direction == "LONG" and px >= tp) or (direction == "SHORT" and px <= tp)
            if tp_hit:
                sheet.update_cell(index, 12, "CLOSED_TP")
                send_telegram_msg(
                    chat_id,
                    f"🟢 *TAKE PROFIT DOSIAHNUTÝ: {symbol} {direction}*\n\n"
                    f"Aktuálna cena (`{current_price}`) zasiahla TP (`{tp}`). Pozícia bola uzavretá v zisku! 🎉",
                )
                continue

            if not be_done and not be_ignored:
                be_hit = (direction == "SHORT" and px <= be_price) or (
                    direction == "LONG" and px >= be_price
                )
                if be_hit:
                    msg = (
                        f"🛡️ *AKCIA POTREBNÁ: {symbol} {direction}*\n\n"
                        f"Aktuálna cena (`{current_price}`) dosiahla *RRR 1:{BE_RRR:g}* (`{be_price}`).\n"
                        f"👉 *Návrh:* Posuň Stop Loss na hodnotu vstupu (`{entry_price}`) v XTB."
                    )
                    keyboard = {
                        "inline_keyboard": [
                            [{"text": "🛡️ POSUNUL SOM SL NA BE", "callback_data": f"be_done_{pos_id}"}],
                            [{"text": "⏳ IGNOROVAŤ", "callback_data": f"be_ignore_{pos_id}"}],
                        ]
                    }
                    send_telegram_msg(chat_id, msg, reply_markup=keyboard)
    except Exception as e:
        print(f"Chyba v kontrole pozícií: {e}")


def daily_report_job():
    if sheet is None:
        return
    try:
        records = get_positions()
        if not records:
            return
        open_positions = [r for r in records if str(r.get("STATUS", "")).upper() == "OPEN"]
        chat_id = records[0].get("CHAT_ID")

        report = "📋 *DENNÝ REPORT POZÍCIÍ*\n\n"
        if not open_positions:
            report += "Aktuálne nemáš otvorené žiadne pozície."
        else:
            report += f"Počet otvorených pozícií: *{len(open_positions)}*\n\n"
            for pos in open_positions:
                sym = pos.get("SYMBOL")
                dir_ = pos.get("DIRECTION")
                entry = pos.get("ENTRY")
                be_state = "🛡️ SL na BE" if str(pos.get("BE_DONE", "")).upper() == "TRUE" else "⚠️ Risk aktívny"
                m_info = get_market_data(str(sym))
                curr = m_info["price"] if m_info else "N/A"
                pid = list(pos.values())[0]
                report += f"• *{sym} {dir_}* (ID {pid}) | Vstup: `{entry}` | Aktuálne: `{curr}` | {be_state}\n"

        send_telegram_msg(chat_id, report)
    except Exception as e:
        print(f"Chyba pri generovaní denného reportu: {e}")


def run_scheduled_jobs(send_report=False):
    """Spúšťa sa z /cena (cron-job.org): kontrola pozícií + voliteľne denný report."""
    if market_open():
        check_positions_job()
    else:
        print("Trh je zatvorený, kontrola pozícií preskočená.")
    if send_report:
        daily_report_job()


# ------------------------------------------------------------------
# 4. WEBHOOK & ENDPOINTY
# ------------------------------------------------------------------
# ------------------------------------------------------------------
# ZOZNAM PRÍKAZOV (/settings) + menu príkazov v Telegrame
# ------------------------------------------------------------------
BOT_COMMANDS = [
    {"command": "settings", "description": "Zoznam všetkých príkazov a nastavení"},
    {"command": "cena", "description": "Aktuálna cena zlata (TVC:GOLD)"},
    {"command": "analytik", "description": "AI analýza zlata so skóre, SL a TP"},
    {"command": "pozicia", "description": "Uloží nový obchod"},
    {"command": "pozicie", "description": "Otvorené pozície s ID"},
    {"command": "zatvor", "description": "Ručne zatvorí pozíciu podľa ID"},
    {"command": "modely", "description": "Dostupné Gemini modely"},
]


def build_settings_text():
    gold = INSTRUMENTS["GOLD"]
    return (
        "⚙️ PRÍKAZY BOTA\n\n"
        "/settings - zobrazí tento zoznam príkazov a aktuálne nastavenia.\n\n"
        "/cena - pošle aktuálnu cenu zlata (TVC:GOLD) z TradingView.\n\n"
        "/analytik GOLD - spraví AI analýzu (technika, makro, kalendár USA) so skóre 0-10, "
        "návrhom SL/TP a kontrolou RRR.\n\n"
        "/pozicia GOLD SHORT 0.003 4197 4255 4081 - uloží obchod "
        "(symbol, smer, objem, vstup, SL, TP) a vypočíta RRR, riziko v EUR a BE cenu.\n\n"
        "/pozicie - vypíše otvorené pozície s ID, aktuálnou cenou a stavom BE.\n\n"
        "/zatvor ID - označí pozíciu ako ručne zatvorenú, napr. /zatvor 3.\n\n"
        "/modely - vypíše dostupné Gemini modely.\n\n"
        "🤖 AUTOMATICKY (bez príkazu):\n"
        "- kontrola SL / TP / BE podľa cron-job.org (odporúčané každých 10 min),\n"
        "- tlačidlá pri BE upozornení: POSUNUL SOM SL NA BE / IGNOROVAŤ,\n"
        "- denný report otvorených pozícií o 20:00.\n\n"
        "🔧 AKTUÁLNE NASTAVENIA:\n"
        f"- minimálne RRR: 1:{MIN_RRR:g}\n"
        f"- BE upozornenie pri RRR: 1:{BE_RRR:g}\n"
        f"- minimálny SL: {MIN_SL_ATR:g}x ATR\n"
        f"- spread zlata: {gold['spread']}\n"
        "Zmena hodnôt: Render -> Environment (MIN_RRR, BE_RRR, MIN_SL_ATR)."
    )


def register_bot_commands():
    """Nastaví menu príkazov v Telegrame (tlačidlo / pri písaní správy)."""
    if not TELEGRAM_TOKEN:
        return
    try:
        requests.post(f"{TELEGRAM_API_URL}/setMyCommands", json={"commands": BOT_COMMANDS}, timeout=10)
    except Exception as e:
        print(f"Chyba setMyCommands: {e}")


threading.Thread(target=register_bot_commands, daemon=True).start()


@app.route("/", methods=["GET", "HEAD"])
def index():
    return "Bot beží úspešne!", 200


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "alive"}), 200


@app.route("/cena", methods=["GET"])
def cena():
    """Volá ho cron-job.org (napr. 3x denne). Robí:
    1) stiahne TVC:GOLD a zapíše do hárku 'Ceny',
    2) spustí kontrolu pozícií (SL / TP / BE),
    3) ak je ?report=1, pošle aj denný report.
    Test bez zápisu a bez kontroly:  /cena?test=1
    Ak je nastavený CRON_SECRET, pridaj &key=TVOJ_SECRET
    """
    if CRON_SECRET and request.args.get("key") != CRON_SECRET:
        return jsonify({"status": "forbidden"}), 403
    try:
        price = get_tv_price("TVC:GOLD")
    except Exception as e:
        print(f"Chyba /cena: {e}")
        return jsonify({"status": "error", "detail": str(e)}), 500

    if request.args.get("test"):
        return jsonify({"status": "ok", "mode": "test", "price": price}), 200

    now = datetime.now(ZoneInfo("Europe/Bratislava")).strftime("%Y-%m-%d %H:%M")
    if not request.args.get("check"):  # ?check=1 = len kontrola, bez zápisu do Sheets
        try:
            get_price_sheet().append_row([now, "TVC:GOLD", price])
        except Exception as e:
            print(f"Chyba zápisu ceny do Sheets: {e}")

    # Kontrola beží na pozadí, aby cron dostal odpoveď hneď
    threading.Thread(
        target=run_scheduled_jobs,
        args=(bool(request.args.get("report")),),
        daemon=True,
    ).start()
    return jsonify({"status": "ok", "time": now, "price": price, "checking": True}), 200


@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.get_json(silent=True)
    if not data:
        return jsonify({"status": "error"}), 400

    # Tlačidlá
    if "callback_query" in data:
        cb = data["callback_query"]
        chat_id = cb["message"]["chat"]["id"]
        cb_data = cb["data"]

        try:
            if cb_data.startswith("be_done_"):
                row_index = find_row_by_id(cb_data.split("_")[2])
                sheet.update_cell(row_index, 11, "TRUE")
                answer = "Uložené!"
                send_telegram_msg(chat_id, "✅ *Stav aktualizovaný:* Pozícia bola označená ako BE = TRUE.")
            elif cb_data.startswith("be_ignore_"):
                row_index = find_row_by_id(cb_data.split("_")[2])
                sheet.update_cell(row_index, 11, "IGNORED")
                answer = "Ignorované"
                send_telegram_msg(chat_id, "⏳ Upozornenie na BE pre túto pozíciu bolo vypnuté.")
            else:
                answer = "Neznáma akcia"
        except Exception as e:
            print(f"Chyba callbacku: {e}")
            answer = "Chyba pri ukladaní"

        try:
            requests.post(
                f"{TELEGRAM_API_URL}/answerCallbackQuery",
                json={"callback_query_id": cb["id"], "text": answer},
                timeout=10,
            )
        except Exception:
            pass
        return jsonify({"status": "ok"}), 200

    # Správy
    if "message" in data and "text" in data["message"]:
        chat_id = data["message"]["chat"]["id"]
        text = data["message"]["text"]

        if text.startswith(("/settings", "/help", "/start")):
            send_telegram_msg(chat_id, build_settings_text(), markdown=False)

        elif text.startswith("/modely"):
            try:
                names = [
                    m.name for m in ai_client.models.list()
                    if "generateContent" in (m.supported_actions or [])
                ]
                send_telegram_msg(chat_id, "Dostupné modely:\n" + "\n".join(names), markdown=False)
            except Exception as e:
                send_telegram_msg(chat_id, f"❌ Chyba: {e}", markdown=False)

        elif text.startswith("/cena"):
            try:
                price = get_tv_price("TVC:GOLD")
                send_telegram_msg(chat_id, f"🥇 TVC:GOLD: {price} USD", markdown=False)
            except Exception as e:
                send_telegram_msg(chat_id, f"❌ Chyba pri sťahovaní ceny: {e}", markdown=False)

        elif text.startswith("/pozicie"):
            try:
                lines = []
                for row in get_positions():
                    if str(row.get("STATUS", "")).upper() != "OPEN":
                        continue
                    pid = list(row.values())[0]
                    sym = str(row.get("SYMBOL", ""))
                    m = get_market_data(sym)
                    curr = m["price"] if m else "N/A"
                    be = {"TRUE": "SL na BE", "IGNORED": "BE ignorované"}.get(
                        str(row.get("BE_DONE", "")).upper(), "riziko aktívne")
                    lines.append(
                        f"ID {pid} | {sym} {row.get('DIRECTION')} | vstup {row.get('ENTRY')} | "
                        f"SL {row.get('SL')} | TP {row.get('TP')} | teraz {curr} | {be}"
                    )
                msg = ("📋 OTVORENÉ POZÍCIE\n\n" + "\n".join(lines)) if lines else "Žiadne otvorené pozície."
                send_telegram_msg(chat_id, msg, markdown=False)
            except Exception as e:
                print(f"Chyba /pozicie: {e}")
                send_telegram_msg(chat_id, f"❌ Chyba: {e}", markdown=False)

        elif text.startswith("/zatvor"):
            try:
                pos_id = text.split()[1]
                row_index = find_row_by_id(pos_id)
                sheet.update_cell(row_index, 12, "CLOSED_MANUAL")
                send_telegram_msg(chat_id, f"✅ Pozícia ID {pos_id} označená ako zatvorená (CLOSED_MANUAL).", markdown=False)
            except Exception as e:
                print(f"Chyba /zatvor: {e}")
                send_telegram_msg(chat_id, "❌ Použi: /zatvor ID (napr. /zatvor 3). ID nájdeš v potvrdení /pozicia alebo v dennom reporte.", markdown=False)

        elif text.startswith("/analytik"):
            parts = text.split()
            symbol = parts[1].upper() if len(parts) > 1 else "GOLD"
            send_telegram_msg(chat_id, f"⏳ Analyzujem `{symbol}`, chvíľu strpenia...")
            # Vlákno: webhook hneď vráti 200, Telegram neopakuje správu
            threading.Thread(target=run_analysis, args=(chat_id, symbol), daemon=True).start()

        elif text.startswith("/pozicia"):
            try:
                parts = text.split()
                symbol, direction = parts[1].upper(), parts[2].upper()
                if direction not in ("LONG", "SHORT"):
                    raise ValueError("direction")
                volume, entry, sl, tp = float(parts[3]), float(parts[4]), float(parts[5]), float(parts[6])

                if symbol not in INSTRUMENTS:
                    send_telegram_msg(
                        chat_id,
                        "❌ Nepodporovaný inštrument. Dostupné: " + ", ".join(INSTRUMENTS),
                        markdown=False,
                    )
                    return jsonify({"status": "ok"}), 200

                cfg = INSTRUMENTS[symbol]
                side_ok = (direction == "LONG" and sl < entry < tp) or (direction == "SHORT" and tp < entry < sl)
                if not side_ok:
                    send_telegram_msg(
                        chat_id,
                        "❌ SL a TP sú na zlej strane vstupu.\n"
                        "LONG: SL < vstup < TP\nSHORT: TP < vstup < SL",
                        markdown=False,
                    )
                    return jsonify({"status": "ok"}), 200
                sl_dist = abs(entry - sl)
                rrr = round(abs(tp - entry) / sl_dist, 2)
                rrr_warn = f" ⚠️ pod minimom 1:{MIN_RRR:g}" if rrr < MIN_RRR else ""
                try:
                    risk_eur = risk_in_eur(symbol, sl_dist, volume)
                except Exception as e:
                    print(f"Chyba výpočtu rizika: {e}")
                    risk_eur = "N/A"
                be_dist = sl_dist * BE_RRR
                be_price = round(entry - be_dist if direction == "SHORT" else entry + be_dist, cfg["digits"])

                row_id = next_position_id()
                new_row = [row_id, chat_id, symbol, direction, volume, entry, sl, tp, risk_eur, be_price, "FALSE", "OPEN"]
                sheet.append_row(new_row)

                send_telegram_msg(
                    chat_id,
                    f"🟢 *POZÍCIA ULOŽENÁ DO GOOGLE SHEETS*\n\n"
                    f"*{symbol} {direction}* ({volume} lota)\n"
                    f"Vstup: `{entry}` | SL: `{sl}` | TP: `{tp}`\n"
                    f"🆔 ID: `{row_id}`\n"
                    f"📐 RRR: `1:{rrr}`{rrr_warn}\n"
                    f"⚠️ Riziko: `{risk_eur} EUR`\n"
                    f"🛡️ BE Trigger cena (RRR 1:{BE_RRR:g}): `{be_price}`",
                )
            except Exception as e:
                print(f"Chyba /pozicia: {e}")
                send_telegram_msg(
                    chat_id,
                    "❌ Chybný formát. Použi napr.:\n"
                    "`/pozicia GOLD SHORT 0.01 2680 2728 2580`\nalebo\n"
                    "`/pozicia EURUSD LONG 0.1 1.0850 1.0800 1.0950`",
                )

    return jsonify({"status": "ok"}), 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)))
