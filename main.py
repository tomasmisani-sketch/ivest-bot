import os
import time
from flask import Flask

# Vytvoríme jednoduchý webový server, aby Render videl otvorený port
app = Flask(__name__)

@app.route("/")
def home():
    return "Investicny bot 24/7 je online!"

print("Investicny bot sa spusta s web serverom...")

if __name__ == "__main__":
    # Render automaticky priradí port cez premennú prostredia 'PORT'
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
