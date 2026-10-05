import os
import json
import threading
import requests
import gspread
import pandas as pd
import yfinance as yf
from flask import Flask, request, jsonify
from apscheduler.schedulers.background import BackgroundScheduler
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

# Hlavný model sa dá zmeniť cez premennú GEMINI_MODEL bez zásahu do kódu
PRIMARY_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.8-flash")
# Voliteľné záložné modely oddelené čiarkou (over ich cez /modely)
FALLBACK_MODELS = [m.strip() for m in os.getenv("GEMINI_FALLBACK_MODELS", "").split(",") if m.strip()]
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

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

sheet = None
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


def run_analysis(chat_id, symbol):
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
        records = sheet.get_all_records()
        for index, row in enumerate(records, start=2):
            if str(row.get("STATUS", "")).upper() != "OPEN":
                continue

            symbol = str(row.get("SYMBOL", ""))
            direction = str(row.get("DIRECTION", "")).upper()
            entry_price = float(row.get("ENTRY", 0))
            sl = float(row.get("SL", 0))
            tp = float(row.get("TP", 0))
            be_price = float(row.get("BE_PRICE", 0))
            be_done = str(row.get("BE_DONE", "")).upper() == "TRUE"
            be_ignored = str(row.get("BE_DONE", "")).upper() == "IGNORED"
            chat_id = row.get("CHAT_ID")

            market_info = get_market_data(symbol)
            if not market_info:
                continue
            current_price = market_info["price"]

            sl_hit = (direction == "LONG" and current_price <= sl) or (direction == "SHORT" and current_price >= sl)
            if sl_hit:
                sheet.update_cell(index, 12, "CLOSED_SL")
                send_telegram_msg(
                    chat_id,
                    f"🔴 *STOP LOSS DOSIAHNUTÝ: {symbol} {direction}*\n\n"
                    f"Aktuálna cena (`{current_price}`) zasiahla SL (`{sl}`). Pozícia bola uzavretá v strate.",
                )
                continue

            tp_hit = (direction == "LONG" and current_price >= tp) or (direction == "SHORT" and current_price <= tp)
            if tp_hit:
                sheet.update_cell(index, 12, "CLOSED_TP")
                send_telegram_msg(
                    chat_id,
                    f"🟢 *TAKE PROFIT DOSIAHNUTÝ: {symbol} {direction}*\n\n"
                    f"Aktuálna cena (`{current_price}`) zasiahla TP (`{tp}`). Pozícia bola uzavretá v zisku! 🎉",
                )
                continue

            if not be_done and not be_ignored:
                be_hit = (direction == "SHORT" and current_price <= be_price) or (
                    direction == "LONG" and current_price >= be_price
                )
                if be_hit:
                    msg = (
                        f"🛡️ *AKCIA POTREBNÁ: {symbol} {direction}*\n\n"
                        f"Aktuálna cena (`{current_price}`) dosiahla *RRR 1:1* (`{be_price}`).\n"
                        f"👉 *Návrh:* Posuň Stop Loss na hodnotu vstupu (`{entry_price}`) v XTB."
                    )
                    keyboard = {
                        "inline_keyboard": [
                            [{"text": "🛡️ POSUNUL SOM SL NA BE", "callback_data": f"be_done_{index}"}],
                            [{"text": "⏳ IGNOROVAŤ", "callback_data": f"be_ignore_{index}"}],
                        ]
                    }
                    send_telegram_msg(chat_id, msg, reply_markup=keyboard)
    except Exception as e:
        print(f"Chyba v kontrole pozícií: {e}")


def daily_report_job():
    if sheet is None:
        return
    try:
        records = sheet.get_all_records()
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
                report += f"• *{sym} {dir_}* | Vstup: `{entry}` | Aktuálne: `{curr}` | {be_state}\n"

        send_telegram_msg(chat_id, report)
    except Exception as e:
        print(f"Chyba pri generovaní denného reportu: {e}")


scheduler = BackgroundScheduler()
scheduler.add_job(func=check_positions_job, trigger="interval", minutes=10)
scheduler.add_job(func=daily_report_job, trigger="cron", hour=20, minute=0)
scheduler.start()


# ------------------------------------------------------------------
# 4. WEBHOOK
# ------------------------------------------------------------------
@app.route("/", methods=["GET", "HEAD"])
def index():
    return "Bot beží úspešne!", 200


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "alive"}), 200


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
                row_index = int(cb_data.split("_")[2])
                sheet.update_cell(row_index, 11, "TRUE")
                answer = "Uložené!"
                send_telegram_msg(chat_id, "✅ *Stav aktualizovaný:* Pozícia bola označená ako BE = TRUE.")
            elif cb_data.startswith("be_ignore_"):
                row_index = int(cb_data.split("_")[2])
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

        if text.startswith("/modely"):
            try:
                names = [
                    m.name for m in ai_client.models.list()
                    if "generateContent" in (m.supported_actions or [])
                ]
                send_telegram_msg(chat_id, "Dostupné modely:\n" + "\n".join(names), markdown=False)
            except Exception as e:
                send_telegram_msg(chat_id, f"❌ Chyba: {e}", markdown=False)

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

                sl_dist = abs(entry - sl)
                risk_eur = round(sl_dist * volume * 100, 2)
                be_price = round(entry - sl_dist if direction == "SHORT" else entry + sl_dist, 4)

                row_id = len(sheet.get_all_values())  # hlavička sa nezapočíta
                new_row = [row_id, chat_id, symbol, direction, volume, entry, sl, tp, risk_eur, be_price, "FALSE", "OPEN"]
                sheet.append_row(new_row)

                send_telegram_msg(
                    chat_id,
                    f"🟢 *POZÍCIA ULOŽENÁ DO GOOGLE SHEETS*\n\n"
                    f"*{symbol} {direction}* ({volume} lota)\n"
                    f"Vstup: `{entry}` | SL: `{sl}` | TP: `{tp}`\n"
                    f"⚠️ Riziko: `{risk_eur} EUR`\n"
                    f"🛡️ BE Trigger cena: `{be_price}`",
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
