from flask import Flask, jsonify, request, render_template_string, send_from_directory
import sqlite3, os, re, threading, time, smtplib, requests, schedule
from datetime import datetime
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from bs4 import BeautifulSoup

app = Flask(__name__)
DB_PATH = os.path.join(os.path.dirname(__file__), "pricewatch.db")

# ── DB ────────────────────────────────────────────────────────────────────────

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    c = conn.cursor()
    c.execute("""CREATE TABLE IF NOT EXISTS products (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL, url TEXT NOT NULL, selector TEXT,
        current_price REAL, previous_price REAL,
        last_checked TEXT, created_at TEXT DEFAULT (datetime('now')), active INTEGER DEFAULT 1
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS price_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        product_id INTEGER, price REAL,
        checked_at TEXT DEFAULT (datetime('now')),
        FOREIGN KEY(product_id) REFERENCES products(id)
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY, value TEXT
    )""")
    defaults = {
        "check_interval": "60", "alert_method": "email",
        "email_from": "", "email_password": "", "email_to": "",
        "telegram_token": "", "telegram_chat_id": "", "price_drop_only": "1",
        "app_password": ""
    }
    for k, v in defaults.items():
        c.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (k, v))
    conn.commit()
    conn.close()

def get_setting(key):
    conn = get_db()
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    conn.close()
    return row["value"] if row else ""

def set_setting(key, value):
    conn = get_db()
    conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, value))
    conn.commit()
    conn.close()

# ── Scraping ──────────────────────────────────────────────────────────────────

HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36"}

def extract_price(text):
    text = text.replace(",", ".").replace("\xa0", " ").replace(" ", "")
    match = re.search(r'\d+\.?\d*', text)
    return float(match.group()) if match else None

def scrape_price(url, selector=None):
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        soup = BeautifulSoup(resp.text, "html.parser")
        if selector:
            el = soup.select_one(selector)
            if el:
                return extract_price(el.get_text())
        for pattern in ['[itemprop="price"]', '.price', '#price', '.product-price',
                         '[class*="price"]', '[id*="price"]', '.amount', '.woocommerce-Price-amount']:
            el = soup.select_one(pattern)
            if el:
                price = extract_price(el.get_text())
                if price and price > 0:
                    return price
        return None
    except:
        return None

# ── Alerts ────────────────────────────────────────────────────────────────────

def send_email_alert(name, old, new, url):
    try:
        ef = get_setting("email_from"); ep = get_setting("email_password"); et = get_setting("email_to")
        if not all([ef, ep, et]): return False
        direction = "DROPPED" if new < old else "INCREASED"
        msg = MIMEMultipart()
        msg["From"] = ef; msg["To"] = et
        msg["Subject"] = f"PriceWatch: {name} price {direction}"
        msg.attach(MIMEText(f"Product: {name}\nURL: {url}\n\nOld: €{old:.2f}\nNew: €{new:.2f}\nChange: {((new-old)/old*100):+.1f}%", "plain"))
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
            s.login(ef, ep); s.send_message(msg)
        return True
    except: return False

def send_telegram_alert(name, old, new, url):
    try:
        token = get_setting("telegram_token"); chat_id = get_setting("telegram_chat_id")
        if not all([token, chat_id]): return False
        d = "📉 DROPPED" if new < old else "📈 INCREASED"
        text = f"*PriceWatch* {d}\n\n*{name}*\nOld: €{old:.2f} → New: €{new:.2f}\nChange: {((new-old)/old*100):+.1f}%\n[View]({url})"
        requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                      json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}, timeout=10)
        return True
    except: return False

def send_alert(name, old, new, url):
    m = get_setting("alert_method")
    if m in ("email", "both"): send_email_alert(name, old, new, url)
    if m in ("telegram", "both"): send_telegram_alert(name, old, new, url)

# ── Checker ───────────────────────────────────────────────────────────────────

def check_product(p):
    new_price = scrape_price(p["url"], p["selector"])
    if new_price is None: return
    now = datetime.now().isoformat()
    conn = get_db()
    conn.execute("INSERT INTO price_history (product_id, price, checked_at) VALUES (?, ?, ?)", (p["id"], new_price, now))
    old_price = p["current_price"]
    conn.execute("UPDATE products SET previous_price=?, current_price=?, last_checked=? WHERE id=?",
                 (old_price, new_price, now, p["id"]))
    conn.commit(); conn.close()
    if old_price and abs(new_price - old_price) > 0.001:
        drop_only = get_setting("price_drop_only") == "1"
        if not drop_only or new_price < old_price:
            send_alert(p["name"], old_price, new_price, p["url"])

def check_all():
    conn = get_db()
    products = conn.execute("SELECT * FROM products WHERE active=1").fetchall()
    conn.close()
    for p in products:
        check_product(dict(p))

# ── Scheduler ─────────────────────────────────────────────────────────────────

def run_scheduler():
    while True:
        interval = int(get_setting("check_interval") or 60)
        schedule.clear()
        schedule.every(interval).minutes.do(check_all)
        while True:
            schedule.run_pending()
            time.sleep(30)

# ── Auth middleware ───────────────────────────────────────────────────────────

def check_auth(req):
    pw = get_setting("app_password")
    if not pw: return True  # no password set
    token = req.headers.get("X-Password") or req.args.get("pw") or ""
    return token == pw

# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template_string(HTML)

@app.route("/api/products", methods=["GET"])
def get_products():
    conn = get_db()
    rows = conn.execute("SELECT * FROM products ORDER BY created_at DESC").fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])

@app.route("/api/products", methods=["POST"])
def add_product():
    data = request.json
    conn = get_db()
    cur = conn.execute("INSERT INTO products (name, url, selector) VALUES (?, ?, ?)",
                       (data["name"], data["url"], data.get("selector", "")))
    conn.commit(); pid = cur.lastrowid; conn.close()
    threading.Thread(target=lambda: check_product({"id": pid, "url": data["url"],
        "selector": data.get("selector",""), "current_price": None, "name": data["name"]}), daemon=True).start()
    return jsonify({"id": pid})

@app.route("/api/products/<int:pid>", methods=["DELETE"])
def delete_product(pid):
    conn = get_db()
    conn.execute("DELETE FROM products WHERE id=?", (pid,))
    conn.execute("DELETE FROM price_history WHERE product_id=?", (pid,))
    conn.commit(); conn.close()
    return jsonify({"status": "deleted"})

@app.route("/api/history")
def get_history():
    pid = request.args.get("id")
    conn = get_db()
    rows = conn.execute("SELECT price, checked_at FROM price_history WHERE product_id=? ORDER BY checked_at DESC LIMIT 50", (pid,)).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])

@app.route("/api/settings", methods=["GET"])
def get_settings():
    conn = get_db()
    rows = conn.execute("SELECT key, value FROM settings").fetchall()
    conn.close()
    s = {r["key"]: r["value"] for r in rows}
    s.pop("email_password", None)  # never expose password
    return jsonify(s)

@app.route("/api/settings", methods=["POST"])
def save_settings():
    for k, v in request.json.items():
        set_setting(k, v)
    return jsonify({"status": "saved"})

@app.route("/api/check_now", methods=["GET"])
def check_now():
    threading.Thread(target=check_all, daemon=True).start()
    return jsonify({"status": "checking"})

@app.route("/api/test_alert", methods=["POST"])
def test_alert():
    m = get_setting("alert_method")
    ok = False
    if m in ("email", "both"): ok = send_email_alert("Test Product", 99.99, 79.99, "https://example.com")
    if m in ("telegram", "both"): ok = send_telegram_alert("Test Product", 99.99, 79.99, "https://example.com")
    return jsonify({"status": "sent" if ok else "failed"})

# ── HTML ──────────────────────────────────────────────────────────────────────

HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>PriceWatch Pro</title>
<link href="https://fonts.googleapis.com/css2?family=Space+Mono:wght@400;700&family=Syne:wght@400;600;800&display=swap" rel="stylesheet">
<style>
  :root {
    --bg: #0a0a0f; --surface: #12121a; --card: #1a1a26; --border: #2a2a3d;
    --accent: #00ff88; --accent2: #ff4d6d; --accent3: #4d88ff;
    --text: #e8e8f0; --muted: #6b6b8a;
    --mono: 'Space Mono', monospace; --sans: 'Syne', sans-serif;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    background: var(--bg); color: var(--text); font-family: var(--sans); min-height: 100vh;
    background-image: radial-gradient(ellipse 80% 50% at 50% -20%, rgba(0,255,136,0.08) 0%, transparent 60%),
                      radial-gradient(ellipse 60% 40% at 80% 80%, rgba(77,136,255,0.05) 0%, transparent 50%);
  }
  header {
    border-bottom: 1px solid var(--border); padding: 16px 24px;
    display: flex; align-items: center; justify-content: space-between;
    backdrop-filter: blur(10px); position: sticky; top: 0; z-index: 100;
    background: rgba(10,10,15,0.9);
  }
  .logo { font-family: var(--mono); font-size: 16px; font-weight: 700; color: var(--accent); }
  .logo span { color: var(--muted); }
  nav { display: flex; gap: 4px; }
  nav button {
    background: none; border: none; color: var(--muted); padding: 8px 14px;
    border-radius: 6px; cursor: pointer; font-family: var(--sans); font-size: 13px; font-weight: 600; transition: all 0.2s;
  }
  nav button:hover { color: var(--text); background: var(--surface); }
  nav button.active { color: var(--accent); background: rgba(0,255,136,0.08); }
  main { max-width: 1000px; margin: 0 auto; padding: 24px 16px; }
  .page { display: none; } .page.active { display: block; }

  .stats { display: grid; grid-template-columns: repeat(3,1fr); gap: 12px; margin-bottom: 24px; }
  .stat-card {
    background: var(--card); border: 1px solid var(--border); border-radius: 10px; padding: 16px 20px;
    position: relative; overflow: hidden;
  }
  .stat-card::before { content:''; position:absolute; top:0; left:0; right:0; height:2px; background: linear-gradient(90deg, var(--accent), transparent); }
  .stat-label { font-size: 10px; text-transform: uppercase; letter-spacing: 1px; color: var(--muted); margin-bottom: 6px; font-family: var(--mono); }
  .stat-value { font-size: 28px; font-weight: 800; }
  .stat-value.green { color: var(--accent); } .stat-value.red { color: var(--accent2); }

  .add-form { background: var(--card); border: 1px solid var(--border); border-radius: 10px; padding: 20px; margin-bottom: 20px; }
  .form-title { font-size: 12px; font-weight: 600; color: var(--muted); text-transform: uppercase; letter-spacing: 1px; margin-bottom: 14px; font-family: var(--mono); }
  .form-row { display: grid; gap: 10px; grid-template-columns: 1fr 2fr 1fr auto; align-items: end; }
  @media(max-width:700px) { .form-row { grid-template-columns: 1fr; } .stats { grid-template-columns: repeat(3,1fr); } }
  label { display: block; font-size: 10px; color: var(--muted); margin-bottom: 5px; font-family: var(--mono); text-transform: uppercase; letter-spacing: 0.5px; }
  input, select {
    width: 100%; background: var(--bg); border: 1px solid var(--border); color: var(--text);
    padding: 9px 12px; border-radius: 7px; font-family: var(--mono); font-size: 12px; outline: none; transition: border-color 0.2s;
  }
  input:focus, select:focus { border-color: var(--accent); }
  input::placeholder { color: var(--muted); }
  .btn { padding: 9px 18px; border-radius: 7px; border: none; cursor: pointer; font-family: var(--sans); font-weight: 600; font-size: 13px; transition: all 0.2s; white-space: nowrap; }
  .btn-primary { background: var(--accent); color: #000; }
  .btn-primary:hover { background: #00e87a; transform: translateY(-1px); }
  .btn-secondary { background: var(--surface); color: var(--text); border: 1px solid var(--border); }
  .btn-secondary:hover { border-color: var(--accent); color: var(--accent); }
  .btn-danger { background: rgba(255,77,109,0.15); color: var(--accent2); border: 1px solid rgba(255,77,109,0.3); }
  .btn-sm { padding: 5px 10px; font-size: 11px; }

  .products-list { display: flex; flex-direction: column; gap: 10px; }
  .product-card {
    background: var(--card); border: 1px solid var(--border); border-radius: 10px; padding: 16px 20px;
    display: grid; grid-template-columns: 1fr auto auto auto; gap: 14px; align-items: center;
    animation: fadeIn 0.3s ease;
  }
  @media(max-width:600px) { .product-card { grid-template-columns: 1fr 1fr; grid-template-rows: auto auto; } }
  @keyframes fadeIn { from { opacity:0; transform:translateY(6px); } to { opacity:1; transform:translateY(0); } }
  .product-card.price-drop { border-left: 3px solid var(--accent); }
  .product-card.price-rise { border-left: 3px solid var(--accent2); }
  .product-name { font-weight: 600; font-size: 15px; margin-bottom: 3px; }
  .product-url { font-family: var(--mono); font-size: 10px; color: var(--muted); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; max-width: 280px; }
  .product-url a { color: var(--muted); text-decoration: none; }
  .product-url a:hover { color: var(--accent3); }
  .current-price { font-family: var(--mono); font-size: 20px; font-weight: 700; }
  .price-change { font-family: var(--mono); font-size: 11px; margin-top: 2px; }
  .price-change.down { color: var(--accent); } .price-change.up { color: var(--accent2); }
  .last-checked { font-family: var(--mono); font-size: 10px; color: var(--muted); text-align: center; line-height: 1.5; }
  .card-actions { display: flex; flex-direction: column; gap: 6px; }
  .no-products { text-align: center; padding: 50px; color: var(--muted); font-family: var(--mono); font-size: 13px; line-height: 2; }

  .tag { display: inline-block; padding: 2px 7px; border-radius: 4px; font-size: 10px; font-family: var(--mono); font-weight: 700; text-transform: uppercase; margin-left: 6px; }
  .tag-new { background: rgba(77,136,255,0.15); color: var(--accent3); }
  .tag-drop { background: rgba(0,255,136,0.15); color: var(--accent); }
  .tag-rise { background: rgba(255,77,109,0.15); color: var(--accent2); }

  .modal-overlay { display:none; position:fixed; inset:0; background:rgba(0,0,0,0.75); backdrop-filter:blur(4px); z-index:200; align-items:center; justify-content:center; }
  .modal-overlay.open { display:flex; }
  .modal { background:var(--card); border:1px solid var(--border); border-radius:14px; padding:28px; width:520px; max-width:95vw; max-height:80vh; overflow-y:auto; }
  .modal-title { font-size:18px; font-weight:800; margin-bottom:20px; }
  .history-row { display:flex; justify-content:space-between; padding:9px 0; border-bottom:1px solid var(--border); font-family:var(--mono); font-size:12px; }
  .history-row:last-child { border-bottom:none; }

  .settings-grid { display:grid; gap:20px; }
  .settings-section { background:var(--card); border:1px solid var(--border); border-radius:10px; padding:20px; }
  .settings-section h3 { font-size:12px; font-weight:600; color:var(--accent); text-transform:uppercase; letter-spacing:1px; margin-bottom:16px; font-family:var(--mono); }
  .settings-row { display:grid; grid-template-columns:1fr 1fr; gap:14px; margin-bottom:14px; }
  @media(max-width:500px) { .settings-row { grid-template-columns:1fr; } }
  .field { display:flex; flex-direction:column; gap:5px; }
  .save-row { display:flex; gap:10px; flex-wrap:wrap; }
  .hint { font-size:11px; color:var(--muted); font-family:var(--mono); margin-top:6px; }

  .pulse { display:inline-block; width:7px; height:7px; border-radius:50%; background:var(--accent); margin-right:6px; animation:pulse 2s infinite; }
  @keyframes pulse { 0%,100%{opacity:1;box-shadow:0 0 0 0 rgba(0,255,136,0.4);}50%{opacity:.8;box-shadow:0 0 0 5px rgba(0,255,136,0);} }

  .toast { position:fixed; bottom:20px; right:20px; background:var(--card); border:1px solid var(--accent); color:var(--accent); padding:10px 18px; border-radius:7px; font-family:var(--mono); font-size:12px; z-index:999; animation:slideUp 0.3s ease; display:none; }
  @keyframes slideUp { from{opacity:0;transform:translateY(16px);}to{opacity:1;transform:translateY(0);} }

  .pw-gate { position:fixed; inset:0; background:var(--bg); z-index:999; display:flex; align-items:center; justify-content:center; flex-direction:column; gap:16px; }
  .pw-gate h2 { font-family:var(--mono); color:var(--accent); font-size:20px; }
  .pw-gate input { width:260px; text-align:center; }
  .pw-gate .hint { color:var(--muted); font-size:12px; font-family:var(--mono); }
</style>
</head>
<body>

<!-- Password gate (shown only if app_password is set) -->
<div class="pw-gate" id="pw-gate" style="display:none">
  <h2>PriceWatch Pro</h2>
  <p class="hint">Enter your access password</p>
  <input id="pw-input" type="password" placeholder="Password" onkeydown="if(event.key==='Enter')checkPw()" />
  <button class="btn btn-primary" onclick="checkPw()">Enter</button>
  <p class="hint" id="pw-err" style="color:var(--accent2);display:none">Wrong password</p>
</div>

<header>
  <div class="logo">Price<span>Watch</span> Pro</div>
  <nav>
    <button class="active" onclick="showPage('dashboard',this)">Dashboard</button>
    <button onclick="showPage('settings',this)">Settings</button>
  </nav>
  <button class="btn btn-secondary btn-sm" onclick="checkNow()"><span class="pulse"></span>Check Now</button>
</header>

<main>
  <div class="page active" id="page-dashboard">
    <div class="stats">
      <div class="stat-card"><div class="stat-label">Tracking</div><div class="stat-value" id="stat-total">0</div></div>
      <div class="stat-card"><div class="stat-label">Drops</div><div class="stat-value green" id="stat-drops">0</div></div>
      <div class="stat-card"><div class="stat-label">Rises</div><div class="stat-value red" id="stat-rises">0</div></div>
    </div>
    <div class="add-form">
      <div class="form-title">+ Add Product to Track</div>
      <div class="form-row">
        <div><label>Product Name</label><input id="in-name" placeholder="Nike Air Max" /></div>
        <div><label>Product URL</label><input id="in-url" placeholder="https://competitor.com/product" /></div>
        <div><label>CSS Selector (optional)</label><input id="in-selector" placeholder=".price" /></div>
        <div><label>&nbsp;</label><button class="btn btn-primary" onclick="addProduct()">Track</button></div>
      </div>
    </div>
    <div class="products-list" id="products-list">
      <div class="no-products">No products yet.<br>Add one above ↑</div>
    </div>
  </div>

  <div class="page" id="page-settings">
    <div class="settings-grid">
      <div class="settings-section">
        <h3>Access Password</h3>
        <div class="field">
          <label>App Password (optional — protects your data)</label>
          <input id="s-app-password" type="password" placeholder="Leave blank for no password" />
        </div>
        <div class="hint">If set, visitors must enter this password to access the app.</div>
      </div>
      <div class="settings-section">
        <h3>Alert Method</h3>
        <div class="field"><label>How to receive alerts</label>
          <select id="s-alert-method" onchange="toggleAlertFields()">
            <option value="email">Email</option>
            <option value="telegram">Telegram</option>
            <option value="both">Both</option>
          </select>
        </div>
      </div>
      <div class="settings-section" id="email-section">
        <h3>Email Settings (Gmail)</h3>
        <div class="settings-row">
          <div class="field"><label>Gmail Address</label><input id="s-email-from" placeholder="you@gmail.com" /></div>
          <div class="field"><label>App Password</label><input id="s-email-password" type="password" placeholder="xxxx xxxx xxxx xxxx" /></div>
        </div>
        <div class="field"><label>Send Alerts To</label><input id="s-email-to" placeholder="alerts@youremail.com" /></div>
        <div class="hint">Use Gmail App Password (not your main password). Enable 2FA first in Google account.</div>
      </div>
      <div class="settings-section" id="telegram-section" style="display:none">
        <h3>Telegram Settings</h3>
        <div class="settings-row">
          <div class="field"><label>Bot Token</label><input id="s-telegram-token" placeholder="123456:ABC-DEF..." /></div>
          <div class="field"><label>Chat ID</label><input id="s-telegram-chat-id" placeholder="-100123456789" /></div>
        </div>
        <div class="hint">Create bot via @BotFather · Get Chat ID via @userinfobot</div>
      </div>
      <div class="settings-section">
        <h3>Check Frequency</h3>
        <div class="settings-row">
          <div class="field"><label>Interval</label>
            <select id="s-check-interval">
              <option value="30">Every 30 min</option>
              <option value="60" selected>Every hour</option>
              <option value="360">Every 6 hours</option>
              <option value="720">Every 12 hours</option>
              <option value="1440">Every day</option>
            </select>
          </div>
          <div class="field"><label>Alert On</label>
            <select id="s-price-drop-only">
              <option value="1">Price drops only</option>
              <option value="0">Any price change</option>
            </select>
          </div>
        </div>
      </div>
      <div class="save-row">
        <button class="btn btn-primary" onclick="saveSettings()">Save Settings</button>
        <button class="btn btn-secondary" onclick="testAlert()">Send Test Alert</button>
      </div>
    </div>
  </div>
</main>

<div class="modal-overlay" id="modal" onclick="if(event.target===this)this.classList.remove('open')">
  <div class="modal">
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:20px">
      <div class="modal-title" id="modal-title">Price History</div>
      <button class="btn btn-secondary btn-sm" onclick="document.getElementById('modal').classList.remove('open')">✕</button>
    </div>
    <div id="history-list"></div>
  </div>
</div>

<div class="toast" id="toast"></div>

<script>
let appPassword = '';

// ── Auth ──
async function initAuth() {
  const res = await fetch('/api/settings');
  const s = await res.json();
  const pw = s.app_password || '';
  if (pw) {
    document.getElementById('pw-gate').style.display = 'flex';
  }
}

function checkPw() {
  const input = document.getElementById('pw-input').value;
  // We verify by trying an API call with the password header
  fetch('/api/products', { headers: { 'X-Password': input } })
    .then(r => {
      if (r.ok) { appPassword = input; document.getElementById('pw-gate').style.display = 'none'; loadProducts(); }
      else { document.getElementById('pw-err').style.display = 'block'; }
    });
}

function apiHeaders() {
  return appPassword ? { 'Content-Type': 'application/json', 'X-Password': appPassword } : { 'Content-Type': 'application/json' };
}

// ── UI ──
function showPage(name, btn) {
  document.querySelectorAll('.page').forEach(p => p.classList.remove('active'));
  document.querySelectorAll('nav button').forEach(b => b.classList.remove('active'));
  document.getElementById('page-' + name).classList.add('active');
  if (btn) btn.classList.add('active');
  if (name === 'settings') loadSettings();
}

function toast(msg, isError=false) {
  const t = document.getElementById('toast');
  t.textContent = msg;
  t.style.borderColor = isError ? 'var(--accent2)' : 'var(--accent)';
  t.style.color = isError ? 'var(--accent2)' : 'var(--accent)';
  t.style.display = 'block';
  setTimeout(() => t.style.display = 'none', 3000);
}

// ── Products ──
async function loadProducts() {
  const res = await fetch('/api/products');
  const products = await res.json();
  renderProducts(products);
}

function renderProducts(products) {
  const list = document.getElementById('products-list');
  if (!products.length) {
    list.innerHTML = '<div class="no-products">No products yet.<br>Add one above ↑</div>';
    document.getElementById('stat-total').textContent = 0;
    document.getElementById('stat-drops').textContent = 0;
    document.getElementById('stat-rises').textContent = 0;
    return;
  }
  let drops = 0, rises = 0;
  list.innerHTML = products.map(p => {
    const hasPrice = p.current_price !== null;
    const changed = p.previous_price !== null && hasPrice && Math.abs(p.current_price - p.previous_price) > 0.001;
    const isDrop = changed && p.current_price < p.previous_price;
    const isRise = changed && p.current_price > p.previous_price;
    if (isDrop) drops++; if (isRise) rises++;
    let changeHtml = '', cardClass = '', tag = hasPrice ? '' : '<span class="tag tag-new">NEW</span>';
    if (changed) {
      const diff = p.current_price - p.previous_price;
      const pct = (diff / p.previous_price * 100).toFixed(1);
      if (isDrop) {
        changeHtml = `<div class="price-change down">▼ €${Math.abs(diff).toFixed(2)} (${Math.abs(pct)}%)</div>`;
        cardClass = 'price-drop'; tag = '<span class="tag tag-drop">↓ DROP</span>';
      } else {
        changeHtml = `<div class="price-change up">▲ €${Math.abs(diff).toFixed(2)} (${Math.abs(pct)}%)</div>`;
        cardClass = 'price-rise'; tag = '<span class="tag tag-rise">↑ RISE</span>';
      }
    }
    const checked = p.last_checked ? new Date(p.last_checked).toLocaleString() : 'Pending...';
    return `<div class="product-card ${cardClass}">
      <div>
        <div style="display:flex;align-items:center;flex-wrap:wrap;margin-bottom:3px">
          <div class="product-name">${p.name}</div>${tag}
        </div>
        <div class="product-url"><a href="${p.url}" target="_blank">${p.url}</a></div>
      </div>
      <div>
        <div class="current-price">${hasPrice ? '€'+p.current_price.toFixed(2) : '...'}</div>
        ${changeHtml}
      </div>
      <div class="last-checked">Last check<br>${checked}</div>
      <div class="card-actions">
        <button class="btn btn-secondary btn-sm" onclick="showHistory(${p.id},'${p.name.replace(/'/g,"\\'")}')">History</button>
        <button class="btn btn-danger btn-sm" onclick="deleteProduct(${p.id})">Delete</button>
      </div>
    </div>`;
  }).join('');
  document.getElementById('stat-total').textContent = products.length;
  document.getElementById('stat-drops').textContent = drops;
  document.getElementById('stat-rises').textContent = rises;
}

async function addProduct() {
  const name = document.getElementById('in-name').value.trim();
  const url = document.getElementById('in-url').value.trim();
  const selector = document.getElementById('in-selector').value.trim();
  if (!name || !url) { toast('Enter name and URL', true); return; }
  const btn = event.target; btn.textContent = 'Adding...'; btn.disabled = true;
  await fetch('/api/products', { method:'POST', headers: apiHeaders(), body: JSON.stringify({name,url,selector}) });
  document.getElementById('in-name').value = '';
  document.getElementById('in-url').value = '';
  document.getElementById('in-selector').value = '';
  toast('Added! Fetching price...');
  setTimeout(loadProducts, 3000);
  btn.textContent = 'Track'; btn.disabled = false;
}

async function deleteProduct(id) {
  if (!confirm('Remove this product?')) return;
  await fetch('/api/products/'+id, { method:'DELETE', headers: apiHeaders() });
  toast('Removed'); loadProducts();
}

async function showHistory(id, name) {
  document.getElementById('modal-title').textContent = name + ' – History';
  document.getElementById('history-list').innerHTML = '<div style="color:var(--muted);font-family:var(--mono);font-size:12px">Loading...</div>';
  document.getElementById('modal').classList.add('open');
  const res = await fetch('/api/history?id='+id);
  const rows = await res.json();
  document.getElementById('history-list').innerHTML = rows.length
    ? rows.map(r => `<div class="history-row"><span>${new Date(r.checked_at).toLocaleString()}</span><span>€${r.price.toFixed(2)}</span></div>`).join('')
    : '<div style="color:var(--muted);font-family:var(--mono);font-size:12px">No history yet.</div>';
}

async function checkNow() {
  toast('Checking prices...'); await fetch('/api/check_now');
  setTimeout(loadProducts, 5000);
}

async function loadSettings() {
  const res = await fetch('/api/settings');
  const s = await res.json();
  document.getElementById('s-alert-method').value = s.alert_method || 'email';
  document.getElementById('s-email-from').value = s.email_from || '';
  document.getElementById('s-email-to').value = s.email_to || '';
  document.getElementById('s-telegram-token').value = s.telegram_token || '';
  document.getElementById('s-telegram-chat-id').value = s.telegram_chat_id || '';
  document.getElementById('s-check-interval').value = s.check_interval || '60';
  document.getElementById('s-price-drop-only').value = s.price_drop_only || '1';
  toggleAlertFields();
}

function toggleAlertFields() {
  const m = document.getElementById('s-alert-method').value;
  document.getElementById('email-section').style.display = m !== 'telegram' ? 'block' : 'none';
  document.getElementById('telegram-section').style.display = m !== 'email' ? 'block' : 'none';
}

async function saveSettings() {
  const data = {
    app_password: document.getElementById('s-app-password').value,
    alert_method: document.getElementById('s-alert-method').value,
    email_from: document.getElementById('s-email-from').value,
    email_password: document.getElementById('s-email-password').value,
    email_to: document.getElementById('s-email-to').value,
    telegram_token: document.getElementById('s-telegram-token').value,
    telegram_chat_id: document.getElementById('s-telegram-chat-id').value,
    check_interval: document.getElementById('s-check-interval').value,
    price_drop_only: document.getElementById('s-price-drop-only').value,
  };
  await fetch('/api/settings', { method:'POST', headers: apiHeaders(), body: JSON.stringify(data) });
  toast('Settings saved!');
}

async function testAlert() {
  toast('Sending test...');
  const res = await fetch('/api/test_alert', { method:'POST', headers: apiHeaders(), body:'{}' });
  const d = await res.json();
  toast(d.status === 'sent' ? '✓ Test alert sent!' : '✗ Failed — check settings', d.status !== 'sent');
}

// Init
initAuth();
loadProducts();
setInterval(loadProducts, 60000);
</script>
</body>
</html>"""

if __name__ == "__main__":
    init_db()
    threading.Thread(target=run_scheduler, daemon=True).start()
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
