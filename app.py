import os, io, time, uuid, secrets, hmac, zipfile
from flask import Flask, request, jsonify, send_from_directory, make_response, Response
import qrcode
import store, runner, bot_key_seller

app = Flask(__name__, static_folder=None)
app.config["MAX_CONTENT_LENGTH"] = 50 * 1024 * 1024  # 50 MB max upload
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "admin123").strip()
PUBLIC_DIR = os.path.join(os.path.dirname(__file__), "public")

def get_session_key():
    code = request.cookies.get("adi_key") or request.headers.get("X-Access-Key")
    return store.verify_key(code)

def is_admin():
    tok = request.cookies.get("adi_admin")
    return bool(tok) and hmac.compare_digest(tok, ADMIN_PASSWORD)

@app.get("/")
def index():
    return send_from_directory(PUBLIC_DIR, "index.html")

@app.get("/admin")
def admin_ui():
    return send_from_directory(PUBLIC_DIR, "admin.html")

@app.get("/health")
def health():
    return jsonify({"ok": True, "brand": store.get_settings()["brandName"]})

# --- QR Code Generator Endpoint (Dynamic UPI & Site/File Share QR) ---
@app.get("/api/qr")
def api_qr():
    data = request.args.get("data", "").strip()
    if not data:
        s = store.get_settings()
        amt = request.args.get("amount", "35")
        data = f"upi://pay?pa={s['upiId']}&pn={s['upiName']}&am={amt}&cu=INR"
    qr = qrcode.QRCode(box_size=8, border=2)
    qr.add_data(data)
    qr.make(fit=True)
    img = qr.make_image(fill_color="#00ffcc", back_color="#0a0f18")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return Response(buf.getvalue(), mimetype="image/png")

# --- Public Config & Terminal Access Key Auth ---
@app.get("/api/public-config")
def public_config():
    s = store.get_settings()
    key_rec = get_session_key()
    return jsonify({
        "brandName": s["brandName"],
        "tagline": s["tagline"],
        "supportUrl": s["supportUrl"],
        "upiId": s["upiId"],
        "upiName": s["upiName"],
        "announcement": s["announcement"],
        "plans": s["plans"],
        "authenticated": bool(key_rec),
        "keyInfo": key_rec
    })

@app.post("/api/verify-key")
def api_verify_key():
    body = request.get_json(silent=True) or {}
    code = str(body.get("key", "")).strip().upper()
    rec = store.verify_key(code)
    if not rec:
        return jsonify({"ok": False, "error": "INVALID OR EXPIRED ACCESS KEY"}), 401
    resp = make_response(jsonify({"ok": True, "keyInfo": rec}))
    resp.set_cookie("adi_key", code, max_age=86400 * 30, httponly=True, samesite="Lax")
    store.log_event(f"Terminal login with key: {code}")
    return resp

@app.post("/api/logout")
def api_logout():
    resp = make_response(jsonify({"ok": True}))
    resp.set_cookie("adi_key", "", max_age=0)
    return resp

@app.post("/api/submit-payment")
def api_submit_payment():
    body = request.get_json(silent=True) or {}
    utr = str(body.get("utr", "")).strip()
    plan_id = str(body.get("planId", "p30"))
    contact = str(body.get("contact", "Web User")).strip()
    if len(utr) < 6:
        return jsonify({"error": "Valid UTR / Transaction ID daalein"}), 400
    s = store.get_settings()
    plan = next((p for p in s["plans"] if p["id"] == plan_id), s["plans"][0])
    pay = {
        "id": f"WEB-{secrets.token_hex(3).upper()}",
        "tgUser": contact,
        "tgName": contact,
        "planId": plan["id"],
        "planName": plan["name"],
        "amount": plan["price"],
        "utr": utr,
        "status": "pending",
        "time": time.strftime("%Y-%m-%d %H:%M:%S")
    }
    def _add(d):
        d.setdefault("payments", []).insert(0, pay)
    store.db.write_all(_add)
    store.log_event(f"New Web UTR submitted: {utr} for {plan['name']}")
    return jsonify({"ok": True, "paymentId": pay["id"]})

# --- Dashboard Data ---
@app.get("/api/dashboard")
def api_dashboard():
    rec = get_session_key()
    if not rec:
        return jsonify({"error": "Unauthorized"}), 401
    code = rec["code"]
    data = store.db.read_all()
    bots = []
    for bid, b in data.get("bots", {}).items():
        if b.get("keyCode") == code:
            item = dict(b)
            item["metrics"] = runner.get_bot_metrics(bid)
            bots.append(item)
    sites = [s for s in data.get("sites", {}).values() if s.get("keyCode") == code]
    files = [f for f in data.get("files", {}).values() if f.get("keyCode") == code]
    return jsonify({"keyInfo": rec, "bots": bots, "sites": sites, "files": files})

# --- Bot Hosting Endpoints ---
@app.post("/api/bots/upload")
def api_bot_upload():
    rec = get_session_key()
    if not rec:
        return jsonify({"error": "Unauthorized"}), 401
    f = request.files.get("file")
    name = (request.form.get("name") or "my-telegram-bot").strip()
    token = (request.form.get("botToken") or "").strip()
    if not f:
        return jsonify({"error": "Select a .zip, .py, or .js file"}), 400
    data = store.db.read_all()
    my_bots = [b for b in data.get("bots", {}).values() if b.get("keyCode") == rec["code"]]
    if len(my_bots) >= rec["maxBots"]:
        return jsonify({"error": f"Plan limit reached ({rec['maxBots']} bots max)"}), 400

    bot_id = f"bot_{secrets.token_hex(4)}"
    try:
        runtime, entry = runner.extract_bot_archive(bot_id, f.read(), f.filename)
    except Exception as e:
        return jsonify({"error": str(e)}), 400

    env_vars = {}
    if token:
        env_vars["BOT_TOKEN"] = token
        env_vars["TOKEN"] = token

    bot_obj = {
        "id": bot_id,
        "keyCode": rec["code"],
        "name": name,
        "runtime": runtime,
        "entry": entry,
        "status": "stopped",
        "env": env_vars,
        "createdAt": time.strftime("%Y-%m-%d %H:%M")
    }
    def _save(d):
        d.setdefault("bots", {})[bot_id] = bot_obj
    store.db.write_all(_save)
    store.log_event(f"Bot uploaded: {name} ({runtime}) by {rec['code']}")
    return jsonify({"ok": True, "bot": bot_obj})

@app.post("/api/bots/<bot_id>/<action>")
def api_bot_action(bot_id, action):
    rec = get_session_key()
    if not rec:
        return jsonify({"error": "Unauthorized"}), 401
    if action == "start":
        runner.start_bot(bot_id)
    elif action == "stop":
        runner.stop_bot(bot_id)
    elif action == "restart":
        runner.stop_bot(bot_id)
        time.sleep(0.5)
        runner.start_bot(bot_id)
    elif action == "delete":
        runner.stop_bot(bot_id)
        def _del(d):
            d.get("bots", {}).pop(bot_id, None)
        store.db.write_all(_del)
    return jsonify({"ok": True})

@app.get("/api/bots/<bot_id>/logs")
def api_bot_logs(bot_id):
    if not get_session_key() and not is_admin():
        return jsonify({"error": "Unauthorized"}), 401
    return jsonify({"logs": runner.read_logs(bot_id, 250)})

@app.post("/api/bots/<bot_id>/logs/clear")
def api_bot_logs_clear(bot_id):
    if not get_session_key():
        return jsonify({"error": "Unauthorized"}), 401
    runner.clear_logs(bot_id)
    return jsonify({"ok": True})

@app.post("/api/bots/<bot_id>/env")
def api_bot_env(bot_id):
    if not get_session_key():
        return jsonify({"error": "Unauthorized"}), 401
    body = request.get_json(silent=True) or {}
    raw_env = str(body.get("dotenv", ""))
    parsed = {}
    for line in raw_env.splitlines():
        if "=" in line and not line.strip().startswith("#"):
            k, v = line.split("=", 1)
            parsed[k.strip()] = v.strip().strip('"').strip("'")
    def _upd(d):
        if bot_id in d.get("bots", {}):
            d["bots"][bot_id]["env"] = parsed
    store.db.write_all(_upd)
    return jsonify({"ok": True, "count": len(parsed)})

# --- Static Website Hosting (from FileSharePro & Hosting Web Src) ---
@app.post("/api/sites/deploy")
def api_site_deploy():
    rec = get_session_key()
    if not rec:
        return jsonify({"error": "Unauthorized"}), 401
    f = request.files.get("file")
    site_name = (request.form.get("name") or "my-site").strip().lower().replace(" ", "-")
    if not f:
        return jsonify({"error": "Upload HTML file or Website .zip"}), 400
    slug = f"{site_name}-{secrets.token_hex(2)}"
    target = runner.SITES_DIR / slug
    target.mkdir(parents=True, exist_ok=True)
    raw = f.read()
    if f.filename.lower().endswith(".zip"):
        with zipfile.ZipFile(io.BytesIO(raw)) as zf:
            zf.extractall(target)
        items = list(target.iterdir())
        if len(items) == 1 and items[0].is_dir():
            sub = items[0]
            for c in sub.iterdir():
                os.rename(str(c), str(target / c.name))
            sub.rmdir()
    else:
        (target / "index.html").write_bytes(raw)

    site_obj = {
        "slug": slug,
        "name": site_name,
        "keyCode": rec["code"],
        "url": f"/site/{slug}/",
        "createdAt": time.strftime("%Y-%m-%d %H:%M")
    }
    def _save(d):
        d.setdefault("sites", {})[slug] = site_obj
    store.db.write_all(_save)
    return jsonify({"ok": True, "site": site_obj})

@app.get("/site/<slug>/")
@app.get("/site/<slug>/<path:subpath>")
def serve_hosted_site(slug, subpath="index.html"):
    return send_from_directory(str(runner.SITES_DIR / slug), subpath)

# --- Cloud File / APK / Video / Audio / Image Share (from FileSharePro) ---
@app.post("/api/files/upload")
def api_file_upload():
    rec = get_session_key()
    if not rec:
        return jsonify({"error": "Unauthorized"}), 401
    f = request.files.get("file")
    if not f:
        return jsonify({"error": "No file selected"}), 400
    fid = secrets.token_hex(5)
    ext = os.path.splitext(f.filename)[1].lower()
    saved_name = f"{fid}{ext}"
    dest = runner.UPLOADS_DIR / saved_name
    f.save(str(dest))
    size_kb = round(dest.stat().st_size / 1024, 1)
    file_obj = {
        "id": fid,
        "keyCode": rec["code"],
        "origName": f.filename,
        "savedName": saved_name,
        "ext": ext,
        "sizeKB": size_kb,
        "url": f"/cdn/{saved_name}",
        "createdAt": time.strftime("%Y-%m-%d %H:%M")
    }
    def _save(d):
        d.setdefault("files", {})[fid] = file_obj
    store.db.write_all(_save)
    return jsonify({"ok": True, "file": file_obj})

@app.get("/cdn/<filename>")
def serve_cdn(filename):
    return send_from_directory(str(runner.UPLOADS_DIR), filename)

# --- Admin Panel APIs ---
@app.post("/api/admin/login")
def api_admin_login():
    body = request.get_json(silent=True) or {}
    pw = str(body.get("password", ""))
    if not hmac.compare_digest(pw, ADMIN_PASSWORD):
        return jsonify({"error": "Wrong Admin Password"}), 401
    resp = make_response(jsonify({"ok": True}))
    resp.set_cookie("adi_admin", ADMIN_PASSWORD, max_age=43200, httponly=True, samesite="Lax")
    return resp

@app.get("/api/admin/overview")
def api_admin_overview():
    if not is_admin():
        return jsonify({"error": "Unauthorized"}), 401
    data = store.db.read_all()
    bots = list(data.get("bots", {}).values())
    for b in bots:
        b["metrics"] = runner.get_bot_metrics(b["id"])
    return jsonify({
        "settings": store.get_settings(),
        "keys": list(data.get("keys", {}).values()),
        "bots": bots,
        "sites": list(data.get("sites", {}).values()),
        "payments": data.get("payments", []),
        "logs": data.get("logs", [])
    })

@app.post("/api/admin/action")
def api_admin_action():
    if not is_admin():
        return jsonify({"error": "Unauthorized"}), 401
    body = request.get_json(silent=True) or {}
    act = body.get("action")
    if act == "generate_key":
        rec = store.create_access_key(plan_id=body.get("planId", "p30"), custom_code=body.get("customCode"))
        return jsonify({"ok": True, "key": rec})
    elif act == "toggle_ban_key":
        code = body.get("code")
        def _tog(d):
            if code in d.get("keys", {}):
                d["keys"][code]["banned"] = not d["keys"][code].get("banned", False)
        store.db.write_all(_tog)
    elif act == "delete_key":
        code = body.get("code")
        def _dk(d):
            d.get("keys", {}).pop(code, None)
        store.db.write_all(_dk)
    elif act == "approve_payment":
        pid = body.get("paymentId")
        data = store.db.read_all()
        pay = next((p for p in data.get("payments", []) if p["id"] == pid), None)
        if pay:
            rec = store.create_access_key(plan_id=pay["planId"], owner_tg=pay.get("tgUser"))
            def _mark(d):
                for p in d.get("payments", []):
                    if p["id"] == pid:
                        p["status"] = "approved"
                        p["issuedKey"] = rec["code"]
            store.db.write_all(_mark)
            return jsonify({"ok": True, "issuedKey": rec["code"]})
    elif act == "save_settings":
        new_s = body.get("settings") or {}
        def _ss(d):
            cur = store.get_settings()
            cur.update(new_s)
            d["settings"] = cur
        store.db.write_all(_ss)
        store.log_event("Admin updated system settings & UPI details")
    elif act == "bot_control":
        bid = body.get("botId")
        sub = body.get("sub")
        if sub == "start":
            runner.start_bot(bid)
        elif sub == "stop":
            runner.stop_bot(bid)
        elif sub == "delete":
            runner.stop_bot(bid)
            def _dbot(d):
                d.get("bots", {}).pop(bid, None)
            store.db.write_all(_dbot)
    return jsonify({"ok": True})

bot_key_seller.start_telegram_bot()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
