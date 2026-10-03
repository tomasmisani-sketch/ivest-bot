
import os
import requests
from flask import Flask, request

app = Flask(__name__)

# Načítame Telegram token z premenných prostredia na Renderi
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_API_URL = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"

@app.route("/")
def home():
    return "Investicny bot 24/7 je online!"

# Endpoint, cez ktorý bude Telegram posielať správy nášmu botovi (tzv. Webhook)
@app.route(f"/{TELEGRAM_TOKEN}", methods=["POST"])
def telegram_webhook():
    update = request.get_json()
    
    if "message" in update:
        chat_id = update["message"]["chat"]["id"]
        text = update["message"].get("text", "")
        
        # Jednoduchá odpoveď na skúšku
        reply_text = f"Ahoj! Dostal som tvoju správu: '{text}'"
        
        # Odoslanie odpovede späť na Telegram
        send_message(chat_id, reply_text)
        
    return "OK", 200

def send_message(chat_id, text):
    url = f"{TELEGRAM_API_URL}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text
    }
    requests.post(url, json=payload)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
