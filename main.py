import os
import json
import requests
import gspread
import pandas as pd
import yfinance as yf
from datetime import datetime
from flask import Flask, request, jsonify
from apscheduler.schedulers.background import BackgroundScheduler
from google import genai
from google.genai import types
from google.oauth2.service_account import Credentials

app = Flask(__name__)

# ------------------------------------------------------------------
# 1. NAČÍTANIE PREMENNÝCH PROSTREDIA
# ------------------------------------------------------------------
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
GOOGLE_CREDENTIALS_RAW = os.getenv("GOOGLE_CREDENTIALS")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

TELEGRAM_API_URL = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"

# Mapovanie bežných názvov na yfinance tickery
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
    "US100": "^IXIC"
}

# Auth pre Google Sheets
SCOPES = ['https://www.googleapis.com/auth/spreadsheets', 'https://www.googleapis.com/auth/drive']

try:
    creds_dict = json.loads(GOOGLE_CREDENTIALS_RAW)
    credentials = Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
    gc = gspread.authorize(credentials)
    sheet = gc.open("Investicny Bot").sheet1
    print("✅ Úspešne pripojené ku Google Sheets")
except Exception as e:
    print(f"❌ Chyba pripojenia ku Google Sheets: {e}")

# Gemini AI Client Setup
ai_client = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else None

def send_telegram_msg(chat_id, text, reply_markup=None):
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    requests.post(f"{TELEGRAM_API_URL}/sendMessage", json=payload)

def resolve_ticker(symbol):
    sym = symbol.upper()
    return SYMBOL_MAP.get(sym, sym)

def get_market_data(symbol):
    ticker_str = resolve_ticker(symbol)
    try:
        ticker = yf.Ticker(ticker_str)
        df = ticker.history(period="60d", interval="1d")
        if df.empty:
            return None

        df['EMA_200'] = df['Close'].ewm(span=200, adjust=False).mean()
        high_low = df['High'] - df['Low']
        high_close = (df['High'] - df['Close'].shift()).abs()
        low_close = (df['Low'] - df['Close'].shift()).abs()
        tr = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
        df['ATR_14'] = tr.rolling(14).mean()

        latest = df.iloc[-1]
        return {
            "symbol": symbol.upper(),
            "price": round(latest['Close'], 4 if "USD" in symbol or "=" in ticker_str else 2),
            "ema200": round(latest['EMA_200'], 4 if "USD" in symbol or "=" in ticker_str else 2),
            "atr14": round(latest['ATR_14'], 4 if "USD" in symbol or "=" in ticker_str else 2)
        }
    except Exception as e:
        print(f"Chyba pri stiahnutí yfinance dát pre {symbol}: {e}")
        return None

# ------------------------------------------------------------------
# 2. POSITION MANAGER & DENNÝ REPORT (APScheduler)
# ------------------------------------------------------------------
def check_positions_job():
    try:
        records = sheet.get_all_records()
        for index, row in enumerate(records, start=2): # Start=2 kvôli hlavičke v tabuľke
            status = str(row.get("STATUS", "")).upper()
            if status != "OPEN":
                continue

            symbol = str(row.get("SYMBOL", ""))
            direction = str(row.get("DIRECTION", "")).upper()
            entry_price = float(row.get("ENTRY", 0))
            sl = float(row.get("SL", 0))
            tp = float(row.get("TP", 0))
            be_price = float(row.get("BE_PRICE", 0))
            be_done = str(row.get("BE_DONE", "")).upper() == "TRUE"
            chat_id = row.get("CHAT_ID")

            market_info = get_market_data(symbol)
            if not market_info:
                continue

            current_price = market_info["price"]

            # 1. Kontrola SL
            sl_hit = (direction == "LONG" and current_price <= sl) or (direction == "SHORT" and current_price >= sl)
            if sl_hit:
                sheet.update_cell(index, 12, "CLOSED_SL") # 12. stĺpec = STATUS
                msg = f"🔴 *STOP LOSS DOSIAHNUTÝ: {symbol} {direction}*\n\nAktuálna cena (`{current_price}`) zasiahla SL (`{sl}`). Pozícia bola uzavretá v strate."
                send_telegram_msg(chat_id, msg)
                continue

            # 2. Kontrola TP
            tp_hit = (direction == "LONG" and current_price >= tp) or (direction == "SHORT" and current_price <= tp)
            if tp_hit:
                sheet.update_cell(index, 12, "CLOSED_TP") # 12. stĺpec = STATUS
                msg = f"🟢 *TAKE PROFIT DOSIAHNUTÝ: {symbol} {direction}*\n\nAktuálna cena (`{current_price}`) zasiahla TP (`{tp}`). Pozícia bola uzavretá v zisku! 🎉"
                send_telegram_msg(chat_id, msg)
                continue

            # 3. Kontrola BE Triggeru (ak ešte nebolo vykonané)
            if not be_done:
                be_hit = (direction == "SHORT" and current_price <= be_price) or (direction == "LONG" and current_price >= be_price)
                if be_hit:
                    msg = (
                        f"🛡️ *AKCIA POTREBNÁ: {symbol} {direction}*\n\n"
                        f"Aktuálna cena (`{current_price}`) dosiahla **RRR 1:1** (`{be_price}`).\n"
                        f"👉 **Návrh:** Posuň Stop Loss na hodnotu vstupu (`{entry_price}`) v XTB."
                    )
                    keyboard = {
                        "inline_keyboard": [
                            [{"text": "🛡️ POSUNUL SOM SL NA BE", "callback_data": f"be_done_{index}"}],
                            [{"text": "⏳ IGNOROVAŤ", "callback_data": f"be_ignore_{index}"}]
                        ]
                    }
                    send_telegram_msg(chat_id, msg, reply_markup=keyboard)

    except Exception as e:
        print(f"Chyba v kontrole pozícií: {e}")

def daily_report_job():
    try:
        records = sheet.get_all_records()
        open_positions = [r for r in records if str(r.get("STATUS", "")).upper() == "OPEN"]

        if not records:
            return

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
                be_done = "🛡️ SL na BE" if str(pos.get("BE_DONE", "")).upper() == "TRUE" else "⚠️ Risk aktívny"

                m_info = get_market_data(sym)
                curr = m_info["price"] if m_info else "N/A"

                report += f"• *{sym} {dir_}* | Vstup: `{entry}` | Aktuálne: `{curr}` | {be_done}\n"

        send_telegram_msg(chat_id, report)
    except Exception as e:
        print(f"Chyba pri generovaní denného reportu: {e}")

# Plánovač
scheduler = BackgroundScheduler()
scheduler.add_job(func=check_positions_job, trigger="interval", minutes=10)
scheduler.add_job(func=daily_report_job, trigger="cron", hour=20, minute=0)
scheduler.start()

# ------------------------------------------------------------------
# 3. WEBHOOK & HANDLERY
# ------------------------------------------------------------------
@app.route('/', methods=['GET', 'HEAD'])
def index():
    return "Bot beží úspešne!", 200

@app.route('/health', methods=['GET'])
def health():
    return jsonify({"status": "alive"}), 200

@app.route('/webhook', methods=['POST'])
def webhook():
    data = request.get_json()

    if not data:
        return jsonify({"status": "error"}), 400

    # Tlačidlá (Callback Query)
    if "callback_query" in data:
        cb = data["callback_query"]
        chat_id = cb["message"]["chat"]["id"]
        cb_data = cb["data"]

        if cb_data.startswith("be_done_"):
            row_index = int(cb_data.split("_")[2])
            sheet.update_cell(row_index, 11, "TRUE") # 11. stĺpec = BE_DONE

            requests.post(f"{TELEGRAM_API_URL}/answerCallbackQuery", json={"callback_query_id": cb["id"], "text": "Uložené!"})
            send_telegram_msg(chat_id, "✅ *Stav aktualizovaný:* Pozícia bola v Google Sheets označená ako BE = TRUE.")

        return jsonify({"status": "ok"}), 200

    # Správy
    if "message" in data and "text" in data["message"]:
        chat_id = data["message"]["chat"]["id"]
        text = data["message"]["text"]

        # Príkaz 1: /analytik [SYMBOL]
        if text.startswith("/analytik"):
            parts = text.split()
            symbol = parts[1].upper() if len(parts) > 1 else "GOLD"

            market_data = get_market_data(symbol)
            if not market_data:
                send_telegram_msg(chat_id, f"❌ Nepodarilo sa stiahnuť dáta pre inštrument `{symbol}`.")
                return jsonify({"status": "ok"}), 200

            prompt = (
                f"Si špičkový finančný a makroekonomický analytik pre inštrument {symbol} (swing obchodovanie na 7-10 dní).\n\n"
                f"DÁTA Z TRHU:\n"
                f"- Aktuálna cena: {market_data['price']}\n"
                f"- EMA 200 (D1): {market_data['ema200']}\n"
                f"- ATR 14: {market_data['atr14']}\n\n"
                f"INŠTRUKCIE:\n"
                f"1. Vyhľadaj na webe najnovšie makroekonomické správy, fundamenty a udalosť za posledných 24-48 hodín pre {symbol}.\n"
                f"2. Zohľadni technické dáta aj fundamentálny sentiment.\n"
                f"3. Vyhodnoť kľúčové zóny (support/rezistenciu).\n"
                f"4. Daj celkové skóre obchodu (0 až 10).\n"
                f"5. Navrhni konkrétny Stop Loss a Take Profit.\n"
                f"Odpovedaj prehľadne v slovenčine s použitím odrážok."
            )

            try:
                # Použitie Google Search Grounding v novom google-genai SDK
                response = ai_client.models.generate_content(
                    model='gemini-3.8-flash',
                    contents=prompt,
                    config=types.GenerateContentConfig(
                        tools=[types.Tool(google_search=types.GoogleSearch())]
                    )
                )

                output_text = response.text if hasattr(response, 'text') else "Analýzu sa nepodarilo vygenerovať."
                send_telegram_msg(chat_id, f"📊 *KOMPLEXNÁ ANALÝZA: {symbol}*\n\n{output_text}")
            except Exception as e:
                send_telegram_msg(chat_id, f"❌ Chyba pri generovaní AI analýzy: {e}")

        # Príkaz 2: /pozicia [SYMBOL] [DIRECTION] [VOLUME] [ENTRY] [SL] [TP]
        elif text.startswith("/pozicia"):
            try:
                parts = text.split()
                symbol, direction = parts[1].upper(), parts[2].upper()
                volume, entry, sl, tp = float(parts[3]), float(parts[4]), float(parts[5]), float(parts[6])

                sl_pips = abs(entry - sl)
                tp_pips = abs(entry - tp)
                risk_eur = round(sl_pips * volume * 100, 2)
                be_price = round(entry - sl_pips if direction == "SHORT" else entry + sl_pips, 4)

                all_rows = len(sheet.get_all_values()) + 1
                new_row = [all_rows, chat_id, symbol, direction, volume, entry, sl, tp, risk_eur, be_price, "FALSE", "OPEN"]
                sheet.append_row(new_row)

                reply = (
                    f"🟢 *POZÍCIA ULOŽENÁ DO GOOGLE SHEETS*\n\n"
                    f"**{symbol} {direction}** ({volume} lota)\n"
                    f"Vstup: `{entry}` | SL: `{sl}` | TP: `{tp}`\n"
                    f"⚠️ Riziko: `{risk_eur} EUR`\n"
                    f"🛡️ BE Trigger cena: `{be_price}`"
                )
                send_telegram_msg(chat_id, reply)

            except Exception as e:
                send_telegram_msg(chat_id, "❌ Chybný formát. Použi napr.:\n`/pozicia GOLD SHORT 0.01 2680 2728 2580`\nalebo\n`/pozicia EURUSD LONG 0.1 1.0850 1.0800 1.0950`")

    return jsonify({"status": "ok"}), 200

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)


