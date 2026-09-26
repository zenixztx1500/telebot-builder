# -*- coding: utf-8 -*-
"""TeleBot Builder - painel Flask multi-cliente (contas + Postgres) para criar e gerenciar bots do Telegram."""
import asyncio, json, os, re, threading, time, uuid, urllib.request, urllib.error
import httpx  # importar aqui (thread principal) evita erro de módulo parcialmente inicializado quando vários bots sobem ao mesmo tempo
from functools import wraps
from pathlib import Path
import psycopg2
import psycopg2.extras
from werkzeug.security import generate_password_hash, check_password_hash
from flask import Flask, render_template, request, jsonify, send_from_directory, session, redirect, url_for
from telegram import (Update, BotCommand, InlineKeyboardButton,
                      InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton)
from telegram.ext import (Application, CommandHandler, MessageHandler,
                          CallbackQueryHandler, filters)

BASE = Path(__file__).parent
LOG_DIR = BASE / "data" / "logs"
IMG_DIR = BASE / "data" / "images"
LOG_DIR.mkdir(parents=True, exist_ok=True)
IMG_DIR.mkdir(parents=True, exist_ok=True)
LOCK = threading.RLock()

DATABASE_URL = os.environ.get("DATABASE_URL", "")
EDITABLE = ("name", "commands", "auto_replies", "inline_keyboards", "reply_keyboard",
            "banned_words", "admin_only", "whitelist", "enabled", "products", "payment")
IMG_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}

# ---------------- Banco de dados (uma conta por cliente; bots ficam num JSON dentro da conta) ----------------
def db():
    return psycopg2.connect(DATABASE_URL, sslmode="require")

def init_db():
    with db() as conn, conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS accounts (
                id SERIAL PRIMARY KEY,
                email TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                bots JSONB NOT NULL DEFAULT '[]',
                created_at TIMESTAMPTZ DEFAULT now()
            )
        """)
        conn.commit()

def load_bots(uid):
    with db() as conn, conn.cursor() as cur:
        cur.execute("SELECT bots FROM accounts WHERE id=%s", (uid,))
        row = cur.fetchone()
        return row[0] if row else []

def save_bots(uid, bots):
    with LOCK, db() as conn, conn.cursor() as cur:
        cur.execute("UPDATE accounts SET bots=%s WHERE id=%s", (json.dumps(bots), uid))
        conn.commit()

def all_accounts():
    with db() as conn, conn.cursor() as cur:
        cur.execute("SELECT id, bots FROM accounts")
        return cur.fetchall()  # lista de (id, bots)

def log(bid, kind, text):
    with LOCK, open(LOG_DIR / f"{bid}.log", "a", encoding="utf-8") as f:
        f.write(f"[{time.strftime('%H:%M:%S')}] {kind}: {text}\n")

def public(b):
    """Nunca devolve o token completo para o navegador."""
    out = {k: v for k, v in b.items() if k != "token"}
    out["token_hint"] = b["token"].split(":")[0] + ":..." + b["token"][-4:]
    return out

def check_token(token):
    """Valida no Telegram. Sem internet? aceita (o log mostra o erro depois)."""
    try:
        with urllib.request.urlopen(f"https://api.telegram.org/bot{token}/getMe", timeout=6) as r:
            return True, json.loads(r.read())["result"].get("username", "")
    except urllib.error.HTTPError as e:
        return (False, "Token inválido") if e.code in (401, 404) else (True, "")
    except Exception:
        return True, ""

def parse_price(s):
    """Extrai um número de um texto de preço tipo 'R$ 1.234,56' -> 1234.56."""
    s = re.sub(r"[^\d,.\-]", "", s or "")
    if not s:
        return 0.0
    if "," in s and "." in s:
        s = s.replace(".", "").replace(",", ".")
    elif "," in s:
        s = s.replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return 0.0

def fmt_price(v):
    return "R$ " + f"{v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")

# ---------------- Bot ----------------
def build_app(bot):
    bid = bot["id"]
    cmds, cmd_catalog = {}, set()
    cmd_image, cmd_link, cmd_contact, cmd_location, cmd_payment = {}, {}, set(), set(), set()
    for c in bot.get("commands", []):
        n = re.sub(r"[^a-z0-9_]", "", (c.get("cmd") or "").lower())[:32]
        if not n:
            continue
        act = c.get("action") or ""
        if act == "catalog":
            cmd_catalog.add(n)
        elif act == "image":
            cmd_image[n] = {"image": c.get("image"), "text": c.get("text") or ""}
        elif act == "link":
            cmd_link[n] = {"url": c.get("url") or "", "text": c.get("text") or "Abrir link"}
        elif act == "contact":
            cmd_contact.add(n)
        elif act == "location":
            cmd_location.add(n)
        elif act == "payment":
            cmd_payment.add(n)
        else:
            cmds[n] = c.get("text") or "..."
    auto = [r for r in bot.get("auto_replies", []) if r.get("match")]
    inline = bot.get("inline_keyboards", [])
    banned = {w.lower() for w in bot.get("banned_words", [])}
    whitelist = set(bot.get("whitelist", []))
    admin_only = bot.get("admin_only", False)
    payment = bot.get("payment") or {}
    rk = bot.get("reply_keyboard") or []
    rmarkup = ReplyKeyboardMarkup(rk, resize_keyboard=True) if rk else None
    cb_map = {b.get("callback"): b.get("reply", "")
              for ik in inline for row in ik.get("buttons", []) for b in row}

    async def allowed(update):
        if not admin_only:
            return True
        u, ch = update.effective_user, update.effective_chat
        if not u:
            return False
        if u.id in whitelist:
            return True
        if ch and ch.type in ("group", "supergroup"):
            m = await ch.get_member(u.id)
            return m.status in ("administrator", "creator")
        return False

    products = bot.get("products", [])
    carts = {}  # user_id -> lista de produtos; fica só na memória (zera ao reiniciar o bot)
    menu_row = []
    if products:
        menu_row += ["🛍 Produtos e Serviços", "🛒 Carrinho"]
    if (payment or {}).get("type"):
        menu_row.append("✅ Finalizar compra")
    menu_markup = ReplyKeyboardMarkup([menu_row], resize_keyboard=True) if menu_row else None

    def make_cmd(name, text):
        async def h(update, ctx):
            if not await allowed(update):
                await update.message.reply_text("Somente administradores.")
                return
            markup = rmarkup if rmarkup else (menu_markup if name == "start" else None)
            await update.message.reply_text(text, reply_markup=markup)
            log(bid, "CMD", f"/{name} de {update.effective_user.id}")
        return h

    def make_cmd_image(name, image, caption):
        async def h(update, ctx):
            if not await allowed(update):
                await update.message.reply_text("Somente administradores.")
                return
            path = (IMG_DIR / image) if image else None
            if path and path.exists():
                with open(path, "rb") as f:
                    await update.message.reply_photo(photo=f, caption=caption or None)
            else:
                await update.message.reply_text(caption or "(sem imagem cadastrada)")
            log(bid, "CMD", f"/{name} (imagem) de {update.effective_user.id}")
        return h

    def make_cmd_link(name, url, text):
        async def h(update, ctx):
            if not await allowed(update):
                await update.message.reply_text("Somente administradores.")
                return
            markup = InlineKeyboardMarkup([[InlineKeyboardButton(text or "Abrir", url=url)]]) if url else None
            await update.message.reply_text(text or "Link", reply_markup=markup)
            log(bid, "CMD", f"/{name} (link) de {update.effective_user.id}")
        return h

    def make_cmd_contact(name):
        async def h(update, ctx):
            if not await allowed(update):
                await update.message.reply_text("Somente administradores.")
                return
            kb = ReplyKeyboardMarkup([[KeyboardButton("Compartilhar contato", request_contact=True)]],
                                      resize_keyboard=True, one_time_keyboard=True)
            await update.message.reply_text("Toque no botão abaixo para compartilhar seu contato.", reply_markup=kb)
            log(bid, "CMD", f"/{name} (pedir contato) de {update.effective_user.id}")
        return h

    def make_cmd_location(name):
        async def h(update, ctx):
            if not await allowed(update):
                await update.message.reply_text("Somente administradores.")
                return
            kb = ReplyKeyboardMarkup([[KeyboardButton("Compartilhar localização", request_location=True)]],
                                      resize_keyboard=True, one_time_keyboard=True)
            await update.message.reply_text("Toque no botão abaixo para compartilhar sua localização.", reply_markup=kb)
            log(bid, "CMD", f"/{name} (pedir localização) de {update.effective_user.id}")
        return h

    async def send_payment(message, items_desc=None):
        ptype = (payment or {}).get("type") or ""
        if ptype == "pix":
            if items_desc:
                await message.reply_text(f"🧾 *Resumo da compra:*\n{items_desc}", parse_mode="Markdown")
            key = payment.get("pix_key") or "(chave não configurada)"
            await message.reply_text(f"💳 *Chave Pix* (toque para copiar):\n`{key}`", parse_mode="Markdown")
            pname = payment.get("pix_name") or ""
            instr = payment.get("instructions") or ""
            info = (f"👤 Recebedor: {pname}\n" if pname else "") + (instr or "")
            if info.strip():
                await message.reply_text(info.strip())
            qr = payment.get("qr_image")
            path = (IMG_DIR / qr) if qr else None
            if path and path.exists():
                with open(path, "rb") as f:
                    await message.reply_photo(photo=f, caption="📷 QR Code Pix")
        elif ptype == "api":
            await message.reply_text("Pagamento automático ainda não disponível. Em breve!")
        else:
            await message.reply_text("Pagamento não configurado — fale com o vendedor.")

    async def do_checkout(message, uid):
        items = carts.get(uid) or []
        if not items:
            await message.reply_text("Seu carrinho está vazio. Adicione produtos no Catálogo antes de finalizar a compra.")
            return
        total = sum(parse_price(p.get("price")) for p in items)
        resumo = "\n".join(f"- {p.get('name')} ({p.get('price') or ''})" for p in items)
        marca = "🟢 " if len(items) > 1 else ""
        resumo += f"\n\n{marca}*Total: {fmt_price(total)}*"
        await send_payment(message, resumo)
        carts[uid] = []

    def make_cmd_payment(name, pay):
        async def h(update, ctx):
            if not await allowed(update):
                await update.message.reply_text("Somente administradores.")
                return
            await do_checkout(update.message, update.effective_user.id)
            log(bid, "CMD", f"/{name} (finalizar compra) de {update.effective_user.id}")
        return h

    def get_categories():
        cats = {}
        for idx, p in enumerate(products):
            c = (p.get("category") or "Geral").strip() or "Geral"
            cats.setdefault(c, []).append(idx)
        return cats

    async def send_category(message, cat, idxs):
        for idx in idxs:
            p = products[idx]
            caption = f"*{p.get('name') or 'Produto'}*\n{p.get('price') or ''}\n\n{p.get('description') or ''}".strip()
            kb = InlineKeyboardMarkup([[
                InlineKeyboardButton("🛒 Adicionar ao carrinho", callback_data=f"addcart|{idx}"),
                InlineKeyboardButton("💳 Comprar", callback_data=f"buy|{idx}")
            ]])
            img = p.get("image")
            path = (IMG_DIR / img) if img else None
            if path and path.exists():
                with open(path, "rb") as f:
                    await message.reply_photo(photo=f, caption=caption, parse_mode="Markdown", reply_markup=kb)
            else:
                await message.reply_text(caption, parse_mode="Markdown", reply_markup=kb)

    async def on_catalog(update, ctx):
        if not await allowed(update):
            await update.message.reply_text("Somente administradores.")
            return
        if not products:
            await update.message.reply_text("Nenhum produto no catálogo ainda.")
            return
        cats = get_categories()
        if len(cats) <= 1:
            cat = next(iter(cats))
            await send_category(update.message, cat, cats[cat])
        else:
            kb = InlineKeyboardMarkup([[InlineKeyboardButton(f"{c} ({len(idxs)})", callback_data=f"catsel|{c}")]
                                        for c, idxs in cats.items()])
            await update.message.reply_text("📂 *Escolha uma categoria:*", parse_mode="Markdown", reply_markup=kb)
        log(bid, "CATALOGO", f"{update.effective_user.id}: {len(products)} produtos")

    async def on_cart(update, ctx):
        if not await allowed(update):
            await update.message.reply_text("Somente administradores.")
            return
        u = update.effective_user
        items = carts.get(u.id) or []
        if not items:
            await update.message.reply_text("Seu carrinho está vazio. Use /catalogo para adicionar produtos.")
            return
        resumo = "\n".join(f"- {p.get('name')} ({p.get('price') or ''})" for p in items)
        total = sum(parse_price(p.get("price")) for p in items)
        marca = "🟢 " if len(items) > 1 else ""
        resumo += f"\n\n{marca}*Total: {fmt_price(total)}*"
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Finalizar compra", callback_data="checkout"),
            InlineKeyboardButton("🗑 Esvaziar", callback_data="clearcart")
        ]])
        await update.message.reply_text(f"🛒 *Seu carrinho:*\n{resumo}", parse_mode="Markdown", reply_markup=kb)

    async def on_contact(update, ctx):
        c = update.message.contact
        log(bid, "CONTATO", f"{update.effective_user.id}: {c.phone_number}")
        await update.message.reply_text("Contato recebido, obrigado!")

    async def on_location(update, ctx):
        l = update.message.location
        log(bid, "LOCALIZACAO", f"{update.effective_user.id}: {l.latitude},{l.longitude}")
        await update.message.reply_text("Localização recebida, obrigado!")

    async def on_text(update, ctx):
        msg, u = update.message, update.effective_user
        txt = (msg.text or "").strip() if msg else ""
        if not txt or not u:
            return
        if not await allowed(update):
            await msg.reply_text("Somente administradores.")
            return
        if txt == "🛍 Produtos e Serviços":
            await on_catalog(update, ctx)
            return
        if txt == "🛒 Carrinho":
            await on_cart(update, ctx)
            return
        if txt == "✅ Finalizar compra":
            await do_checkout(msg, u.id)
            return
        low = txt.lower()
        if banned & set(re.findall(r"\w+", low)):
            await msg.reply_text("Palavra não permitida aqui.")
            log(bid, "FILTRO", f"{u.id}: {txt[:60]}")
            return
        for r in auto:
            m = r["match"].strip().lower()
            if not ((low == m) if r.get("mode") == "exact" else (m in low)):
                continue
            markup = rmarkup
            for ik in inline:
                if (ik.get("trigger") or "").strip().lower() == m:
                    markup = InlineKeyboardMarkup([[InlineKeyboardButton(b["text"], callback_data=b["callback"])
                                                    for b in row] for row in ik["buttons"]])
                    break
            await msg.reply_text(r.get("reply") or "...", reply_markup=markup)
            log(bid, "AUTO", f"{u.id}: {txt[:40]}")
            return
        log(bid, "TEXT", f"{u.id}: {txt[:80]}")

    async def on_callback(update, ctx):
        q = update.callback_query
        data = q.data or ""
        u = update.effective_user
        if data.startswith("catsel|"):
            cat = data.split("|", 1)[1]
            idxs = get_categories().get(cat, [])
            await q.answer()
            await send_category(q.message, cat, idxs)
            return
        if data.startswith("addcart|"):
            idx = int(data.split("|", 1)[1])
            if 0 <= idx < len(products):
                p = products[idx]
                carts.setdefault(u.id, []).append(p)
                items = carts[u.id]
                total = sum(parse_price(x.get("price")) for x in items)
                txt = f"✅ *{p.get('name')}* adicionado — *{p.get('price') or ''}*"
                if len(items) > 1:
                    txt += f"\n\n🟢 *Total do carrinho: {fmt_price(total)}*"
                await q.answer("Adicionado ao carrinho!")
                await q.message.reply_text(txt, parse_mode="Markdown")
                log(bid, "CARRINHO", f"{u.id} adicionou {p.get('name')}")
            else:
                await q.answer("Produto não encontrado.")
            return
        if data.startswith("buy|"):
            idx = int(data.split("|", 1)[1])
            await q.answer()
            if 0 <= idx < len(products):
                p = products[idx]
                await send_payment(q.message, f"- *{p.get('name')}* — *{p.get('price') or ''}*")
                log(bid, "COMPRA", f"{u.id} comprou direto {p.get('name')}")
            return
        if data == "checkout":
            await q.answer()
            await do_checkout(q.message, u.id)
            log(bid, "COMPRA", f"{u.id} finalizou carrinho")
            return
        if data == "clearcart":
            carts[u.id] = []
            await q.answer("Carrinho esvaziado.")
            return
        await q.answer()
        log(bid, "CALLBACK", data)
        if cb_map.get(data):
            await q.message.reply_text(cb_map[data])

    a = Application.builder().token(bot["token"]).build()
    for name, text in cmds.items():
        a.add_handler(CommandHandler(name, make_cmd(name, text)))
    for name in cmd_catalog:
        a.add_handler(CommandHandler(name, on_catalog))
    for name, meta in cmd_image.items():
        a.add_handler(CommandHandler(name, make_cmd_image(name, meta["image"], meta["text"])))
    for name, meta in cmd_link.items():
        a.add_handler(CommandHandler(name, make_cmd_link(name, meta["url"], meta["text"])))
    for name in cmd_contact:
        a.add_handler(CommandHandler(name, make_cmd_contact(name)))
    for name in cmd_location:
        a.add_handler(CommandHandler(name, make_cmd_location(name)))
    for name in cmd_payment:
        a.add_handler(CommandHandler(name, make_cmd_payment(name, payment)))
    if "catalogo" not in cmds and "catalogo" not in cmd_catalog:
        a.add_handler(CommandHandler("catalogo", on_catalog))
    if "carrinho" not in cmds:
        a.add_handler(CommandHandler("carrinho", on_cart))
    a.add_handler(CallbackQueryHandler(on_callback))
    a.add_handler(MessageHandler(filters.CONTACT, on_contact))
    a.add_handler(MessageHandler(filters.LOCATION, on_location))
    a.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    cmd_meta = [(n, f"Comando /{n}") for n in cmds]
    a.bot_data["cmd_names"] = cmd_meta
    return a

class Worker:
    """Um thread com seu próprio event loop por bot (run_polling não funciona fora da thread principal)."""
    def __init__(self, bot):
        self.bot, self.stop = bot, threading.Event()
        self.thread = threading.Thread(target=self.run, daemon=True)

    def run(self):
        bid = self.bot["id"]
        try:
            asyncio.run(self.main())
        except Exception as e:
            log(bid, "ERR", f"{type(e).__name__}: {e}")

    async def main(self):
        bid = self.bot["id"]
        a = build_app(self.bot)
        await a.initialize()
        try:
            try:
                await a.bot.set_my_commands([BotCommand(n, d) for n, d in a.bot_data["cmd_names"]])
            except Exception as e:
                log(bid, "WARN", f"set_my_commands: {e}")
            await a.start()
            await a.updater.start_polling(allowed_updates=Update.ALL_TYPES)
            log(bid, "SYS", f"online como @{a.bot.username}")
            while not self.stop.is_set():
                await asyncio.sleep(0.5)
            await a.updater.stop()
            await a.stop()
        finally:
            await a.shutdown()

workers = {}  # bid -> Worker (processo/thread único do app inteiro — ver nota do gunicorn --workers 1)

def stop_worker(bid):
    w = workers.pop(bid, None)
    if w:
        w.stop.set()
        w.thread.join(timeout=10)
        log(bid, "SYS", "parado")

def start_worker(uid, bid):
    stop_worker(bid)
    bot = next((b for b in load_bots(uid) if b["id"] == bid), None)
    if bot and bot.get("enabled"):
        w = workers[bid] = Worker(bot)
        w.thread.start()
        log(bid, "SYS", "iniciando...")

# ---------------- Auth ----------------
app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "").strip() or os.urandom(24)

def login_required(f):
    @wraps(f)
    def wrapper(*a, **kw):
        if "uid" not in session:
            if request.path.startswith("/api/"):
                return jsonify(ok=False, err="Sessão expirada, faça login de novo"), 401
            return redirect(url_for("login_page"))
        return f(*a, **kw)
    return wrapper

@app.route("/login")
def login_page():
    if "uid" in session:
        return redirect(url_for("index"))
    return render_template("login.html")

@app.route("/api/signup", methods=["POST"])
def api_signup():
    d = request.get_json(force=True)
    email = (d.get("email") or "").strip().lower()
    pw = d.get("password") or ""
    if "@" not in email or len(pw) < 6:
        return jsonify(ok=False, err="E-mail inválido ou senha muito curta (mín. 6 caracteres)"), 400
    try:
        with db() as conn, conn.cursor() as cur:
            cur.execute("INSERT INTO accounts (email, password_hash) VALUES (%s,%s) RETURNING id",
                        (email, generate_password_hash(pw)))
            uid = cur.fetchone()[0]
            conn.commit()
    except psycopg2.errors.UniqueViolation:
        return jsonify(ok=False, err="Esse e-mail já tem conta"), 400
    session["uid"] = uid
    return jsonify(ok=True)

@app.route("/api/login", methods=["POST"])
def api_login():
    d = request.get_json(force=True)
    email = (d.get("email") or "").strip().lower()
    pw = d.get("password") or ""
    with db() as conn, conn.cursor() as cur:
        cur.execute("SELECT id, password_hash FROM accounts WHERE email=%s", (email,))
        row = cur.fetchone()
    if not row or not check_password_hash(row[1], pw):
        return jsonify(ok=False, err="E-mail ou senha incorretos"), 400
    session["uid"] = row[0]
    return jsonify(ok=True)

@app.route("/api/logout", methods=["POST"])
def api_logout():
    session.clear()
    return jsonify(ok=True)

# ---------------- API ----------------
@app.route("/")
@login_required
def index():
    return render_template("index.html")

@app.route("/images/<path:filename>")
def api_image(filename):
    safe = re.sub(r"[^\w.-]", "", filename)
    return send_from_directory(IMG_DIR, safe)

@app.route("/api/upload_image", methods=["POST"])
@login_required
def api_upload_image():
    f = request.files.get("image")
    if not f or not f.filename:
        return jsonify(ok=False, err="Nenhuma imagem enviada"), 400
    ext = os.path.splitext(f.filename)[1].lower()
    if ext not in IMG_EXTS:
        return jsonify(ok=False, err="Formato de imagem inválido (use jpg, png, webp ou gif)"), 400
    fname = uuid.uuid4().hex + ext
    f.save(IMG_DIR / fname)
    return jsonify(ok=True, filename=fname)

@app.route("/api/bots")
@login_required
def api_bots():
    return jsonify([public(b) for b in load_bots(session["uid"])])

@app.route("/api/bots/new", methods=["POST"])
@login_required
def api_new():
    uid = session["uid"]
    d = request.get_json(force=True)
    token = (d.get("token") or "").strip()
    name = (d.get("name") or "MeuBot").strip()
    if not re.fullmatch(r"\d+:[\w-]{20,}", token):
        return jsonify(ok=False, err="Formato de token inválido (esperado 123456:ABC...)"), 400
    bots = load_bots(uid)
    if any(b["token"] == token for b in bots):
        return jsonify(ok=False, err="Token já cadastrado"), 400
    ok, info = check_token(token)
    if not ok:
        return jsonify(ok=False, err=info), 400
    bid = "bot_" + uuid.uuid4().hex[:8]
    bots.append({
        "id": bid, "name": name, "token": token, "enabled": True,
        "commands": [{"cmd": "start", "text": f"Olá! Eu sou o {name}. Como posso ajudar?"}],
        "auto_replies": [], "inline_keyboards": [], "reply_keyboard": [],
        "banned_words": [], "admin_only": False, "whitelist": [], "products": [],
        "payment": {"type": "", "pix_key": "", "pix_name": "", "instructions": "", "qr_image": ""},
        "created": time.strftime("%Y-%m-%d %H:%M"),
    })
    save_bots(uid, bots)
    start_worker(uid, bid)
    return jsonify(ok=True, id=bid, username=info)

@app.route("/api/bots/<bid>", methods=["PUT"])
@login_required
def api_update(bid):
    uid = session["uid"]
    body = request.get_json(force=True)
    with LOCK:
        bots = load_bots(uid)
        b = next((x for x in bots if x["id"] == bid), None)
        if not b:
            return jsonify(ok=False, err="Bot não encontrado"), 404
        for k in EDITABLE:
            if k in body:
                b[k] = body[k]
        b["whitelist"] = [int(x) for x in b.get("whitelist", []) if str(x).lstrip("-").isdigit()]
        save_bots(uid, bots)
    start_worker(uid, bid)  # reinicia se estiver ativo; se pausado, só para
    return jsonify(ok=True)

@app.route("/api/bots/<bid>", methods=["DELETE"])
@login_required
def api_delete(bid):
    uid = session["uid"]
    stop_worker(bid)
    bots = [b for b in load_bots(uid) if b["id"] != bid]
    save_bots(uid, bots)
    (LOG_DIR / f"{bid}.log").unlink(missing_ok=True)
    return jsonify(ok=True)

@app.route("/api/bots/<bid>/log")
@login_required
def api_log(bid):
    uid = session["uid"]
    if not any(b["id"] == bid for b in load_bots(uid)):
        return jsonify(ok=False, err="Bot não encontrado"), 404
    safe = re.sub(r"[^\w-]", "", bid)
    p = LOG_DIR / (safe + ".log")
    return jsonify(p.read_text(encoding="utf-8").splitlines()[-200:] if p.exists() else [])

# ---------------- Inicialização (roda tanto no "python app.py" quanto sob gunicorn) ----------------
init_db()
for _uid, _bots in all_accounts():
    for _b in _bots:
        if _b.get("enabled"):
            start_worker(_uid, _b["id"])
            time.sleep(0.3)

if __name__ == "__main__":
    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", 5000))
    print(f"TeleBot Builder em http://{host}:{port}")
    app.run(host=host, port=port, debug=False)
