import os
import time
import threading
from flask import Flask
import telebot

# Environment variables
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
UPSTASH_REDIS_REST_URL = os.getenv("UPSTASH_REDIS_REST_URL", "").strip()
UPSTASH_REDIS_REST_TOKEN = os.getenv("UPSTASH_REDIS_REST_TOKEN", "").strip()

if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN set nahi hai! Render Environment Variables mein BOT_TOKEN add karein.")

bot = telebot.TeleBot(BOT_TOKEN)
app = Flask(__name__)


@app.route("/")
def home():
    return "OK - Service is running", 200


@app.route("/health")
def health():
    return "Healthy", 200


# =========================================================
# YAHAN APNE EXISTING BOT HANDLERS / COMMANDS RAKHEIN
# (Jaise @bot.message_handler, callback handlers, key seller logic)
# =========================================================

@bot.message_handler(commands=["start"])
def handle_start(message):
    bot.reply_to(message, "Bot active hai!")


def run_web_server():
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port, use_reloader=False)


def run_bot_polling():
    # Purana webhook hata dein taaki polling ke saath conflict na ho
    try:
        bot.remove_webhook()
        time.sleep(1)
    except Exception as e:
        print("Webhook remove warning:", e)

    while True:
        try:
            print("Telegram bot polling started...")
            bot.infinity_polling(
                skip_pending=True,
                timeout=20,
                long_polling_timeout=20,
            )
        except Exception as e:
            err_msg = str(e)
            if "409" in err_msg or "Conflict" in err_msg:
                print("409 Conflict: Kahin aur bhi isi token se bot chal raha hai. 15s wait kar rahe hain...")
                time.sleep(15)
            else:
                print("Polling error:", err_msg)
                time.sleep(5)


if __name__ == "__main__":
    # Polling ko background thread mein safe loop ke saath chalayein
    threading.Thread(target=run_bot_polling, daemon=True).start()
    # Main thread mein Render Web Service ka port bind karein
    run_web_server()
  
