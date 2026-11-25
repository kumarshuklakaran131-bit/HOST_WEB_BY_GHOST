import os, io, time, threading
import qrcode
import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
import store

BOT_TOKEN = os.environ.get("TELEGRAM_KEY_BOT_TOKEN", "9033637317:AAEa96Vl-GE8CaFjy03xAMPyp5ma0U8SpGI").strip()
ADMIN_TG_ID = os.environ.get("ADMIN_TELEGRAM_ID", "8680144387").strip()

def generate_upi_qr(upi_id, payee_name, amount, note="ADI Hosting Key"):
    upi_uri = f"upi://pay?pa={upi_id}&pn={payee_name}&am={amount}&cu=INR&tn={note}"
    qr = qrcode.QRCode(box_size=8, border=2)
    qr.add_data(upi_uri)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return buf

def start_telegram_bot():
    if not BOT_TOKEN:
        return
    bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML")
    pending_utr = {}

    def main_menu():
        kb = InlineKeyboardMarkup(row_width=2)
        kb.add(InlineKeyboardButton("🛒 Buy Key of Web", callback_data="buy_menu"))
        kb.add(
            InlineKeyboardButton("🔑 My Keys", callback_data="my_keys"),
            InlineKeyboardButton("🆘 Support", url=store.get_settings()["supportUrl"])
        )
        return kb

    @bot.message_handler(commands=["start"])
    def cmd_start(m):
        s = store.get_settings()
        text = (
            f"✨ <b>{s['brandName']} — ACCESS KEY CENTER</b> ✨\n\n"
            f"Welcome, <b>{m.from_user.first_name}</b>!\n"
            "Buy an access key for the ADI HOSTING website (Bot Hosting + Website Hosting + File Vault). "
            "Payment is checked by admin before a key is issued."
        )
        bot.send_message(m.chat.id, text, reply_markup=main_menu())

    @bot.callback_query_handler(func=lambda c: True)
    def callbacks(c):
        s = store.get_settings()
        if c.data == "home":
            bot.edit_message_text(
                f"✨ <b>{s['brandName']} KEY CENTER</b>\nChoose an option:",
                c.message.chat.id, c.message.message_id, reply_markup=main_menu()
            )
        elif c.data == "buy_menu":
            kb = InlineKeyboardMarkup(row_width=1)
            for p in s["plans"]:
                kb.add(InlineKeyboardButton(f"{p['name']} — ₹{p['price']}", callback_data=f"plan_{p['id']}"))
            kb.add(InlineKeyboardButton("⬅️ Back", callback_data="home"))
            bot.edit_message_text("💎 <b>SELECT YOUR ACCESS PLAN</b>\nChoose a plan:",
                                  c.message.chat.id, c.message.message_id, reply_markup=kb)
        elif c.data.startswith("plan_"):
            pid = c.data.split("_", 1)[1]
            plan = next((p for p in s["plans"] if p["id"] == pid), None)
            if not plan:
                return
            pending_utr[c.from_user.id] = plan
            qr_buf = generate_upi_qr(s["upiId"], s["upiName"], plan["price"], f"ADI-{plan['id']}")
            caption = (
                f"💳 <b>PAYMENT DETAILS</b>\n\n"
                f"<b>Plan:</b> {plan['name']}\n"
                f"<b>Amount:</b> ₹{plan['price']}\n\n"
                f"<b>UPI ID:</b> <code>{s['upiId']}</code>\n"
                f"<b>Payee:</b> {s['upiName']}\n\n"
                "QR scan karke payment karein aur uske baad <b>12-digit UTR number</b> ya screenshot yahan bhejein. "
                "Admin approval ke baad aapko REAL Access Key mil jayegi!"
            )
            kb = InlineKeyboardMarkup()
            kb.add(InlineKeyboardButton("⬅️ Back to Plans", callback_data="buy_menu"))
            bot.send_photo(c.message.chat.id, qr_buf, caption=caption, reply_markup=kb)
        elif c.data == "my_keys":
            uid = str(c.from_user.id)
            keys = [k for k in store.db.read_all().get("keys", {}).values() if str(k.get("ownerTg")) == uid]
            if not keys:
                bot.answer_callback_query(c.id, "Abhi aapke paas koi key nahi hai!", show_alert=True)
                return
            lines = ["🔑 <b>YOUR ACTIVE ACCESS KEYS:</b>\n"]
            for k in keys:
                exp = time.strftime("%d %b %Y", time.localtime(k["expiresAt"]))
                lines.append(f"• <code>{k['code']}</code> ({k['planName']}) — Exp: {exp}")
            bot.send_message(c.message.chat.id, "\n".join(lines))
        elif c.data.startswith("approve_") and str(c.from_user.id) == ADMIN_TG_ID:
            _, target_uid, pid = c.data.split("_", 2)
            key_rec = store.create_access_key(plan_id=pid, owner_tg=target_uid)
            bot.send_message(
                int(target_uid),
                f"✅ <b>PAYMENT APPROVED!</b>\n\n"
                f"Your Access Key: <code>{key_rec['code']}</code>\n"
                f"Plan: <b>{key_rec['planName']}</b>\n"
                f"Website par jaakar ye key daalein aur hosting start karein!"
            )
            bot.edit_message_caption(f"✅ Approved & Sent Key: {key_rec['code']}", c.message.chat.id, c.message.message_id)

    @bot.message_handler(content_types=["text", "photo"])
    def catch_utr(m):
        plan = pending_utr.get(m.from_user.id)
        if not plan:
            return
        utr_info = m.text or m.caption or "Screenshot Uploaded"
        pay_rec = {
            "id": f"PAY-{int(time.time())}",
            "tgUser": m.from_user.id,
            "tgName": m.from_user.first_name,
            "planId": plan["id"],
            "planName": plan["name"],
            "amount": plan["price"],
            "utr": utr_info,
            "status": "pending",
            "time": time.strftime("%Y-%m-%d %H:%M:%S")
        }
        def _add_pay(d):
            d.setdefault("payments", []).insert(0, pay_rec)
        store.db.write_all(_add_pay)
        bot.reply_to(m, "⏳ <b>UTR / Proof Received!</b> Admin verify karke aapko key bhejega.")
        if ADMIN_TG_ID:
            kb = InlineKeyboardMarkup()
            kb.add(InlineKeyboardButton("✅ Approve & Issue Key", callback_data=f"approve_{m.from_user.id}_{plan['id']}"))
            bot.send_message(
                int(ADMIN_TG_ID),
                f"🔔 <b>NEW PAYMENT REQUEST</b>\nUser: {m.from_user.first_name} (<code>{m.from_user.id}</code>)\n"
                f"Plan: {plan['name']} (₹{plan['price']})\nUTR/Note: <code>{utr_info}</code>",
                reply_markup=kb
            )

    threading.Thread(target=lambda: bot.infinity_polling(skip_pending=True), daemon=True).start()
