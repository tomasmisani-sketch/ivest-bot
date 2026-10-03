import os
import json
from flask import Flask, request
import requests
import gspread
from google.oauth2.service_account import Credentials
from datetime import datetime

app = Flask(__name__)

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_API_URL = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"

def get_sheet():
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive"
    ]
    creds_json = os.environ.get("GOOGLE_CREDENTIALS")
    creds_dict = json.loads(creds_json)
    
    credentials = Credentials.from_service_account_info(creds_dict, scopes=scopes)
    gc = gspread.authorize(credentials)
    
    sheet_title = "Investicny Bot"
    spreadsheet = gc.open(sheet_title)
    return spreadsheet.sheet1

@app.route("/", methods=["GET"])
def index():
    return "Bot is running!", 200

@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.get_json()
    
    if data and "message" in data:
        chat_id = data["message"]["chat"]["id"]
        text = data["message"].get("text", "")
        
        reply_text = f"Prijaté: {text}"
        
        try:
            sheet = get_sheet()
            current_time = datetime.now().strftime("%d.%m.%Y %H:%M")
            sheet.append_row([current_time, "Správa", text, "-"])
            reply_text += " ✅ (Zapísané do tabuľky!)"
        except Exception as e:
            reply_text += f" ❌ (Chyba tabuľky: {str(e)})"

        payload = {
            "chat_id": chat_id,
            "text": reply_text
        }
        requests.post(TELEGRAM_API_URL, json=payload)
        
    return "OK", 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))


