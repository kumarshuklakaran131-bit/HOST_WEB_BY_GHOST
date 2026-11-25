import os, json, time, threading, secrets
import requests

UPSTASH_URL = os.environ.get("UPSTASH_REDIS_REST_URL", "").strip().rstrip("/")
UPSTASH_TOKEN = os.environ.get("UPSTASH_REDIS_REST_TOKEN", "").strip()
LOCAL_DB = os.path.join(os.path.dirname(__file__), "data", "db.json")

DEFAULT_SETTINGS = {
    "brandName": "ADI HOSTING",
    "tagline": "SECURE TERMINAL v3.0",
    "supportUrl": "https://t.me/kumar_raj_qn_bot",
    "upiId": os.environ.get("UPI_ID", "9679838510@fam"),
    "upiName": os.environ.get("UPI_NAME", "ROHIT SHARMA"),
    "announcement": "Welcome to ADI HOSTING v3.0 — High-Speed Telegram Bot, Website & Cloud File Hosting!",
    "maxFileMB": 25,
    "autoRestart": True,
    "plans": [
        {"id": "p7", "name": "7 Days Starter", "days": 7, "price": 35, "maxBots": 2, "maxSites": 2, "ramMB": 256},
        {"id": "p15", "name": "15 Days Pro", "days": 15, "price": 75, "maxBots": 5, "maxSites": 5, "ramMB": 512},
        {"id": "p30", "name": "30 Days Ultra", "days": 30, "price": 150, "maxBots": 12, "maxSites": 10, "ramMB": 1024},
        {"id": "p9999", "name": "Unlimited VIP", "days": 3650, "price": 400, "maxBots": 50, "maxSites": 50, "ramMB": 2048},
    ]
}

class LocalStore:
    def __init__(self):
        self._lock = threading.Lock()
        os.makedirs(os.path.dirname(LOCAL_DB), exist_ok=True)
        if not os.path.exists(LOCAL_DB):
            self._save({"keys": {}, "bots": {}, "sites": {}, "files": {}, "payments": [], "logs": [], "settings": DEFAULT_SETTINGS})

    def _load(self):
        try:
            with open(LOCAL_DB, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {"keys": {}, "bots": {}, "sites": {}, "files": {}, "payments": [], "logs": [], "settings": DEFAULT_SETTINGS}

    def _save(self, data):
        with open(LOCAL_DB, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)

    def read_all(self):
        with self._lock:
            return self._load()

    def write_all(self, fn):
        with self._lock:
            data = self._load()
            res = fn(data)
            self._save(data)
            return res

class UpstashStore(LocalStore):
    def __init__(self, url, token):
        super().__init__()
        self.url = url
        self.headers = {"Authorization": f"Bearer {token}"}

    def _cmd(self, *parts):
        r = requests.post(self.url, headers=self.headers, json=list(parts), timeout=10)
        return r.json().get("result")

    def read_all(self):
        try:
            raw = self._cmd("GET", "adi_hosting_v3_db")
            if raw:
                return json.loads(raw)
        except Exception:
            pass
        return super().read_all()

    def write_all(self, fn):
        data = self.read_all()
        res = fn(data)
        try:
            self._cmd("SET", "adi_hosting_v3_db", json.dumps(data))
        except Exception:
            pass
        super()._save(data)
        return res

db = UpstashStore(UPSTASH_URL, UPSTASH_TOKEN) if (UPSTASH_URL and UPSTASH_TOKEN) else LocalStore()

def get_settings():
    s = db.read_all().get("settings") or {}
    merged = dict(DEFAULT_SETTINGS)
    merged.update(s)
    return merged

def log_event(msg):
    def _up(d):
        d.setdefault("logs", []).insert(0, {"t": time.strftime("%Y-%m-%d %H:%M:%S"), "msg": msg})
        d["logs"] = d["logs"][:250]
    db.write_all(_up)

def create_access_key(plan_id="p30", custom_code=None, owner_tg=None):
    settings = get_settings()
    plan = next((p for p in settings["plans"] if p["id"] == plan_id), settings["plans"][0])
    code = (custom_code or f"ADI-{secrets.token_hex(3).upper()}-{secrets.token_hex(2).upper()}").strip().upper()
    now = time.time()
    rec = {
        "code": code,
        "planId": plan["id"],
        "planName": plan["name"],
        "maxBots": plan["maxBots"],
        "maxSites": plan["maxSites"],
        "ramMB": plan["ramMB"],
        "createdAt": now,
        "expiresAt": now + plan["days"] * 86400,
        "banned": False,
        "ownerTg": owner_tg
    }
    def _up(d):
        d.setdefault("keys", {})[code] = rec
    db.write_all(_up)
    log_event(f"Key generated: {code} ({plan['name']})")
    return rec

def verify_key(code):
    if not code:
        return None
    code = code.strip().upper()
    rec = db.read_all().get("keys", {}).get(code)
    if not rec or rec.get("banned"):
        return None
    if rec.get("expiresAt", 0) < time.time():
        return None
    return rec
