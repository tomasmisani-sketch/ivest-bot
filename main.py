import os
import json
from flask import Flask, request
import requests
import gspread
from google.oauth2.service_account import Credentials
from datetime import datetime

app = Flask(__name__)

# Načítanie Telegram tokenu z premenných prostredia
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_API_URL = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"

# Funkcia na pripojenie do Google Sheets
def get_sheet():
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive"
    ]
    
    # Načítame JSON kľúč priamo z premennej prostredia na Renderi
    creds_json = os.environ.get("GOOGLE_CREDENTIALS")
    creds_dict = json.loads(creds_json)
    
    credentials = Credentials.from_service_account_info(creds_dict, scopes=scopes)
    gc = gspread.authorize(credentials)
    
    # Názov tvojej Google tabuľky (musí sa presne zhodovať!)
    sheet_title = "Investicny Bot"
    spreadsheet = gc.open(sheet_title)
    return spreadsheet.sheet1  # Vráti prvý list tabuľky

@app.route("/", methods=["POST"])
def webhook():
    data = request.get_json()
    
    if "message" in data:
        chat_id = data["message"]["chat"]["id"]
        text = data["message"].get("text", "")
        
        # Jednoduchá logika: Ak napíšeš napríklad "Nákup BTC 100", bot to uloží
        # Alebo ti pre začiatok odpovie na akúkoľvek správu a skúsime zápis
        reply_text = f"Dostal som správu: {text}"
        
        try:
            # Pokus o zápis do Google Sheets
            sheet = get_sheet()
            current_time = datetime.now().strftime("%d.%m.%Y %H:%M")
            
            # Pridá riadok: [Dátum, Typ, Aktiva, Suma]
            # Na začiatok tam uložíme text správy ako test
            sheet.append_row([current_time, "Správa", text, "-"])
            reply_text += " ✅ (Úspešne zapísané do tabuľky!)"
        except Exception as e:
            reply_text += f" ❌ (Chyba pri zápise: {str(e)})"

        # Odoslanie odpovedi späť do Telegramu
        payload = {
            "chat_id": chat_id,
            "text": reply_text
        }
        requests.post(TELEGRAM_API_URL, json=payload)
        
    return "OK", 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))

