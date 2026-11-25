import os, sys, time, shutil, signal, zipfile, subprocess, threading
from pathlib import Path
import psutil, store

BASE_DATA = Path(__file__).parent / "data"
BOTS_DIR = BASE_DATA / "bots"
SITES_DIR = BASE_DATA / "sites"
UPLOADS_DIR = BASE_DATA / "uploads"

for d in (BOTS_DIR, SITES_DIR, UPLOADS_DIR):
    d.mkdir(parents=True, exist_ok=True)

_procs = {}
_logs_meta = {}
_lock = threading.RLock()

def bot_path(bot_id):
    p = BOTS_DIR / bot_id
    p.mkdir(parents=True, exist_ok=True)
    return p

def log_file(bot_id):
    return bot_path(bot_id) / "console.log"

def append_log(bot_id, text):
    with open(log_file(bot_id), "a", encoding="utf-8") as f:
        f.write(f"[{time.strftime('%H:%M:%S')}] {text}\n")

def read_logs(bot_id, lines=200):
    lf = log_file(bot_id)
    if not lf.exists():
        return "No logs yet. Click START BOT to launch."
    return "\n".join(lf.read_text(encoding="utf-8", errors="replace").splitlines()[-lines:])

def clear_logs(bot_id):
    log_file(bot_id).write_text("", encoding="utf-8")

def extract_bot_archive(bot_id, raw_bytes, filename):
    app_dir = bot_path(bot_id) / "app"
    if app_dir.exists():
        shutil.rmtree(app_dir, ignore_errors=True)
    app_dir.mkdir(parents=True, exist_ok=True)

    fn = filename.lower()
    if fn.endswith(".zip"):
        import io
        with zipfile.ZipFile(io.BytesIO(raw_bytes)) as zf:
            zf.extractall(app_dir)
        # Flatten single top-level folder if present
        items = list(app_dir.iterdir())
        if len(items) == 1 and items[0].is_dir():
            sub = items[0]
            for child in sub.iterdir():
                shutil.move(str(child), str(app_dir / child.name))
            sub.rmdir()
    elif fn.endswith(".py"):
        (app_dir / "bot.py").write_bytes(raw_bytes)
    elif fn.endswith(".js"):
        (app_dir / "index.js").write_bytes(raw_bytes)
    else:
        raise ValueError("Upload .zip, .py, or .js file only")

    # Detect runtime and entrypoint
    for py_entry in ["bot.py", "main.py", "app.py", "run.py"]:
        if (app_dir / py_entry).exists():
            return "python", py_entry
    for js_entry in ["index.js", "bot.js", "main.js", "server.js"]:
        if (app_dir / js_entry).exists():
            return "node", js_entry
    # Fallback first .py or .js
    py_files = list(app_dir.glob("*.py"))
    if py_files:
        return "python", py_files[0].name
    js_files = list(app_dir.glob("*.js"))
    if js_files:
        return "node", js_files[0].name
    raise ValueError("No entry file (bot.py / index.js) found in archive")

def start_bot(bot_id):
    with _lock:
        if bot_id in _procs and _procs[bot_id].poll() is None:
            return True, "Already running"

    def _worker():
        data = store.db.read_all()
        bot = data.get("bots", {}).get(bot_id)
        if not bot:
            return
        app_dir = bot_path(bot_id) / "app"
        runtime = bot.get("runtime", "python")
        entry = bot.get("entry", "bot.py")
        env = os.environ.copy()
        env["PYTHONUNBUFFERED"] = "1"
        for k, v in (bot.get("env") or {}).items():
            env[k] = str(v)

        lf = open(log_file(bot_id), "a", encoding="utf-8")
        lf.write(f"\n=== DEPLOYING {bot['name']} ({runtime}: {entry}) ===\n")
        lf.flush()

        try:
            if runtime == "python" and (app_dir / "requirements.txt").exists():
                lf.write("[HOST] Installing Python dependencies...\n")
                lf.flush()
                subprocess.run([sys.executable, "-m", "pip", "install", "-r", "requirements.txt"],
                               cwd=str(app_dir), stdout=lf, stderr=lf, timeout=300)
            elif runtime == "node" and (app_dir / "package.json").exists():
                lf.write("[HOST] Installing Node.js dependencies...\n")
                lf.flush()
                subprocess.run(["npm", "install", "--omit=dev"],
                               cwd=str(app_dir), stdout=lf, stderr=lf, timeout=300)

            cmd = [sys.executable, "-u", entry] if runtime == "python" else ["node", entry]
            proc = subprocess.Popen(cmd, cwd=str(app_dir), env=env, stdout=lf, stderr=lf, start_new_session=True)
            with _lock:
                _procs[bot_id] = proc
                _logs_meta[bot_id] = time.time()

            def _set_run(d):
                if bot_id in d.get("bots", {}):
                    d["bots"][bot_id]["status"] = "running"
            store.db.write_all(_set_run)
            store.log_event(f"Bot started: {bot['name']} ({bot_id})")
        except Exception as e:
            lf.write(f"[HOST ERROR] {e}\n")
            lf.close()

    threading.Thread(target=_worker, daemon=True).start()
    return True, "Deployment started"

def stop_bot(bot_id):
    with _lock:
        proc = _procs.pop(bot_id, None)
    if proc and proc.poll() is None:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except Exception:
            proc.kill()
    append_log(bot_id, "[HOST] Bot stopped.")
    def _set_stop(d):
        if bot_id in d.get("bots", {}):
            d["bots"][bot_id]["status"] = "stopped"
    store.db.write_all(_set_stop)

def get_bot_metrics(bot_id):
    with _lock:
        proc = _procs.get(bot_id)
    if not proc or proc.poll() is not None:
        return {"status": "stopped", "cpu": 0, "ramMB": 0, "uptime": 0}
    try:
        p = psutil.Process(proc.pid)
        mem = p.memory_info().rss / (1024 * 1024)
        cpu = p.cpu_percent(interval=None)
        up = int(time.time() - _logs_meta.get(bot_id, time.time()))
        return {"status": "running", "cpu": round(cpu, 1), "ramMB": round(mem, 1), "uptime": up}
    except Exception:
        return {"status": "running", "cpu": 0, "ramMB": 0, "uptime": 0}
