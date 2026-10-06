# -*- coding: utf-8 -*-
"""TeleBot Builder - painel Flask multi-cliente (contas + Postgres) para criar e gerenciar bots do Telegram."""
import asyncio, base64, collections, datetime, hashlib, hmac, io, json, os, re, secrets, smtplib, ssl, struct, threading, time, unicodedata, uuid, urllib.request, urllib.error
from email.message import EmailMessage
from urllib.parse import urlparse, quote
import segno
import httpx  # importar aqui (thread principal) evita erro de módulo parcialmente inicializado quando vários bots sobem ao mesmo tempo
import anyio._backends._asyncio  # idem: o anyio carrega esse backend sob demanda e várias threads ao mesmo tempo quebram o import
from functools import wraps
from pathlib import Path
import psycopg2
import psycopg2.extras
from werkzeug.security import generate_password_hash, check_password_hash
from flask import Flask, render_template, request, jsonify, session, redirect, url_for, Response, abort, make_response
from telegram import (Update, BotCommand, InlineKeyboardButton,
                      InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton)
from telegram.helpers import escape_markdown
from telegram.ext import (Application, CommandHandler, MessageHandler,
                          CallbackQueryHandler, filters)

BASE = Path(__file__).parent
DATA_DIR = Path(os.environ.get("DATA_DIR", BASE / "data"))
LOG_DIR = DATA_DIR / "logs"
IMG_DIR = DATA_DIR / "images"
LOG_DIR.mkdir(parents=True, exist_ok=True)
IMG_DIR.mkdir(parents=True, exist_ok=True)
LOCK = threading.RLock()

DATABASE_URL = os.environ.get("DATABASE_URL", "")
BUTTON_ACTIONS = ("catalog", "cart", "checkout", "clearcart", "text", "link", "support", "contact", "location", "command")
EDITABLE = ("name", "commands", "auto_replies", "inline_keyboards", "reply_keyboard", "auto_clean", "buttons", "buttons_per_row",
            "banned_words", "admin_only", "whitelist", "enabled", "products", "payment")
IMG_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
IMG_MIME = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp", ".gif": "image/gif"}

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
        cur.execute("""
            CREATE TABLE IF NOT EXISTS images (
                filename TEXT PRIMARY KEY,
                content_type TEXT NOT NULL,
                data BYTEA NOT NULL,
                created_at TIMESTAMPTZ DEFAULT now()
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS account_security (
                account_id INT PRIMARY KEY,
                totp_secret TEXT,
                backup_codes TEXT NOT NULL DEFAULT '[]'
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS signup_pending (
                email TEXT PRIMARY KEY,
                password_hash TEXT NOT NULL,
                code_hash TEXT NOT NULL,
                attempts INT NOT NULL DEFAULT 0,
                expires DOUBLE PRECISION NOT NULL
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS pix_orders (
                order_id TEXT PRIMARY KEY,
                bid TEXT NOT NULL,
                chat_id BIGINT NOT NULL,
                summary TEXT NOT NULL,
                total_cents INT NOT NULL,
                paid BOOLEAN NOT NULL DEFAULT false,
                created_at TIMESTAMPTZ DEFAULT now()
            )
        """)
        conn.commit()

def save_image(fname, content):
    """Salva a imagem DENTRO do banco (o disco do Render é apagado a cada deploy/restart)."""
    ctype = IMG_MIME.get(os.path.splitext(fname)[1].lower(), "application/octet-stream")
    with db() as conn, conn.cursor() as cur:
        cur.execute("""INSERT INTO images (filename, content_type, data) VALUES (%s,%s,%s)
                       ON CONFLICT (filename) DO UPDATE SET data=EXCLUDED.data, content_type=EXCLUDED.content_type""",
                    (fname, ctype, psycopg2.Binary(content)))
        conn.commit()

def get_image(fname):
    """Devolve (content_type, bytes) ou None."""
    if not fname:
        return None
    with db() as conn, conn.cursor() as cur:
        cur.execute("SELECT content_type, data FROM images WHERE filename=%s", (fname,))
        row = cur.fetchone()
        return (row[0], bytes(row[1])) if row else None

# ---------------- Criptografia em repouso (opcional: ligada pela variável de ambiente DATA_KEY) ----------------
# Com DATA_KEY definida, tokens (Telegram e intermediador), segredo do 2FA e o resumo dos pedidos (nome/e-mail
# do cliente) são gravados cifrados no banco. Quem conseguir uma cópia do banco não consegue ler esses campos.
# ATENÇÃO: se a DATA_KEY for trocada ou perdida, o que já foi cifrado não pode mais ser lido.
try:
    from cryptography.fernet import Fernet, InvalidToken
except ImportError:  # sem a biblioteca, segue sem cifrar
    Fernet, InvalidToken = None, Exception
_DATA_KEY = os.environ.get("DATA_KEY", "").strip()
_fernet = Fernet(base64.urlsafe_b64encode(hashlib.sha256(_DATA_KEY.encode()).digest())) if (_DATA_KEY and Fernet) else None

def enc(s):
    if not s or not isinstance(s, str) or not _fernet or s.startswith("enc:"):
        return s
    return "enc:" + _fernet.encrypt(s.encode()).decode()

def dec(s):
    if not isinstance(s, str) or not s.startswith("enc:"):
        return s
    if not _fernet:
        return ""  # cifrado, mas sem a chave neste servidor
    try:
        return _fernet.decrypt(s[4:].encode()).decode()
    except InvalidToken:
        return ""

def _bots_secrets(bots, fn):
    """Aplica fn (enc ou dec) nos campos sensíveis de cada bot, sem alterar a lista original."""
    out = []
    for b in bots or []:
        b = dict(b)
        if b.get("token"):
            b["token"] = fn(b["token"])
        if isinstance(b.get("payment"), dict):
            p = dict(b["payment"])
            for k in ("api_token", "pagbank_token"):
                if p.get(k):
                    p[k] = fn(p[k])
            b["payment"] = p
        out.append(b)
    return out

def load_bots(uid):
    with db() as conn, conn.cursor() as cur:
        cur.execute("SELECT bots FROM accounts WHERE id=%s", (uid,))
        row = cur.fetchone()
        return _bots_secrets(row[0], dec) if row else []

def save_bots(uid, bots):
    with LOCK, db() as conn, conn.cursor() as cur:
        cur.execute("UPDATE accounts SET bots=%s WHERE id=%s", (json.dumps(_bots_secrets(bots, enc)), uid))
        conn.commit()

def all_accounts():
    with db() as conn, conn.cursor() as cur:
        cur.execute("SELECT id, bots FROM accounts")
        return cur.fetchall()  # lista de (id, bots)

def save_order(order_id, bid, chat_id, summary, total_cents):
    with db() as conn, conn.cursor() as cur:
        cur.execute("INSERT INTO pix_orders (order_id, bid, chat_id, summary, total_cents) VALUES (%s,%s,%s,%s,%s)",
                    (order_id, bid, chat_id, enc(summary), total_cents))  # o resumo tem nome/e-mail do cliente
        conn.commit()

def order_row(order_id, bid):
    """(paid, chat_id) do pedido deste bot, ou None se não existe."""
    with db() as conn, conn.cursor() as cur:
        cur.execute("SELECT paid, chat_id FROM pix_orders WHERE order_id=%s AND bid=%s", (order_id, bid))
        return cur.fetchone()

def purge_old_data():
    """Minimização de dados: pedidos com mais de 90 dias e cadastros pendentes vencidos são apagados."""
    try:
        with db() as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM pix_orders WHERE created_at < now() - interval '90 days'")
            cur.execute("DELETE FROM signup_pending WHERE expires < %s", (time.time(),))
            conn.commit()
    except Exception:
        pass

def pending_orders(bid):
    """Pedidos Pix do bot ainda não pagos, criados nas últimas 24h."""
    with db() as conn, conn.cursor() as cur:
        cur.execute("""SELECT order_id FROM pix_orders
                       WHERE bid=%s AND NOT paid AND created_at > now() - interval '24 hours'""", (bid,))
        return [r[0] for r in cur.fetchall()]

def order_paid(order_id, bid):
    """True/False conforme o pedido; None se não existe."""
    with db() as conn, conn.cursor() as cur:
        cur.execute("SELECT paid FROM pix_orders WHERE order_id=%s AND bid=%s", (order_id, bid))
        row = cur.fetchone()
        return row[0] if row else None

def mark_paid(order_id):
    """Marca como pago só uma vez. Devolve (chat_id, summary) na primeira vez, senão None."""
    with db() as conn, conn.cursor() as cur:
        cur.execute("UPDATE pix_orders SET paid=true WHERE order_id=%s AND NOT paid RETURNING chat_id, summary",
                    (order_id,))
        row = cur.fetchone()
        conn.commit()
        return (row[0], dec(row[1])) if row else None

LOG_MAX = 512 * 1024  # cada log guarda no máximo ~512 KB; ao passar disso, fica só a metade mais recente

def log(bid, kind, text):
    path = LOG_DIR / f"{re.sub(r'[^\w-]', '', str(bid))}.log"
    linha = re.sub(r"[\r\n]+", " ", str(text))[:2000]  # uma linha por evento: impede forjar linhas falsas no log
    with LOCK:
        try:
            if path.exists() and path.stat().st_size > LOG_MAX:
                dados = path.read_bytes()[-LOG_MAX // 2:]
                path.write_bytes(dados[dados.find(b"\n") + 1:])
        except OSError:
            pass
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"[{time.strftime('%H:%M:%S')}] {kind}: {linha}\n")

def public(b):
    """Nunca devolve o token completo (nem o do intermediador de pagamento) para o navegador."""
    out = {k: v for k, v in b.items() if k != "token"}
    out["token_hint"] = b["token"].split(":")[0] + ":..." + b["token"][-4:]
    raw = b.get("payment") or {}
    pay = {k: v for k, v in raw.items() if k not in ("api_token", "pagbank_token", "pagbank_env")}
    tok = gw_token(raw)
    pay["api_token_hint"] = ("..." + tok[-4:]) if tok else ""
    pay["gateway"] = gw_name(raw)
    pay["api_token_gateway"] = pay["gateway"] if tok else ""
    pay["api_env"] = mp_env_from_token(tok) if pay["gateway"] == "mercadopago" and tok else gw_env(raw)
    out["payment"] = pay
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

def cpf_ok(cpf):
    if len(cpf) != 11 or cpf == cpf[0] * 11:
        return False
    for i in (9, 10):
        soma = sum(int(cpf[j]) * (i + 1 - j) for j in range(i))
        if (soma * 10) % 11 % 10 != int(cpf[i]):
            return False
    return True

def email_ok(email):
    return re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email) is not None

# ---------------- Pix com valor exato (BR Code estático do Banco Central, sem intermediador) ----------------
def _emv(tag, value):
    return f"{tag}{len(value):02d}{value}"

def crc16_ccitt(data):
    crc = 0xFFFF
    for byte in data.encode("utf-8"):
        crc ^= byte << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) if crc & 0x8000 else (crc << 1)
            crc &= 0xFFFF
    return f"{crc:04X}"

def pix_ascii(s, limit):
    """Nome/cidade no BR Code: sem acento, maiúsculas, só letras/números/espaço."""
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode()
    return re.sub(r"[^A-Za-z0-9 ]", "", s).strip().upper()[:limit]

def pix_key_normalize(key):
    """E-mail em minúsculas; CPF/CNPJ só números; telefone +55...; chave aleatória como está."""
    k = (key or "").strip()
    if "@" in k:
        return k.lower()
    digits = re.sub(r"\D", "", k)
    if k.startswith("+"):
        return "+" + digits
    if re.fullmatch(r"[\d.\-/ ()]+", k):
        if "(" in k or len(digits) == 10:  # telefone com DDD
            return "+55" + digits
        if len(digits) in (12, 13) and digits.startswith("55"):
            return "+" + digits
        if len(digits) == 11 and not cpf_ok(digits):  # 11 dígitos que não fecham como CPF = celular com DDD
            return "+55" + digits
        return digits  # CPF (11) ou CNPJ (14)
    return k

def pix_brcode(key, name, city, amount_cents, txid):
    """Pix copia e cola (EMV/BR Code) com valor fixo. txid: até 25 letras/números, aparece no extrato de vários bancos."""
    mai = _emv("00", "br.gov.bcb.pix") + _emv("01", pix_key_normalize(key))
    payload = (_emv("00", "01") + _emv("26", mai) + _emv("52", "0000") + _emv("53", "986")
               + _emv("54", f"{amount_cents / 100:.2f}") + _emv("58", "BR")
               + _emv("59", pix_ascii(name, 25) or "RECEBEDOR") + _emv("60", pix_ascii(city, 15) or "BRASIL")
               + _emv("62", _emv("05", re.sub(r"[^A-Za-z0-9]", "", txid)[:25] or "***")) + "6304")
    return payload + crc16_ccitt(payload)

def qr_png(text):
    buf = io.BytesIO()
    segno.make(text, error="m").save(buf, kind="png", scale=8, border=2)
    return buf.getvalue()

# ---------------- Pix automático por token de API (PagBank, Mercado Pago, Asaas) ----------------
# Cada intermediador implementa: token_ok (sync), create (async) e is_paid (async).
# create devolve (id_da_cobrança, copia_e_cola, qr) onde qr é bytes do PNG ou URL da imagem.
GATEWAYS = {
    "pagbank": {"label": "PagBank", "has_env": True,
                "urls": {"producao": "https://api.pagseguro.com", "sandbox": "https://sandbox.api.pagseguro.com"}},
    "mercadopago": {"label": "Mercado Pago", "has_env": False,  # o próprio token diz o modo (TEST-... = testes)
                    "urls": {"producao": "https://api.mercadopago.com", "sandbox": "https://api.mercadopago.com"}},
    "asaas": {"label": "Asaas", "has_env": True,
              "urls": {"producao": "https://api.asaas.com/v3", "sandbox": "https://api-sandbox.asaas.com/v3"}},
}
UA = "TeleBotBuilder/1.0"

def gw_name(pay):
    """Intermediador escolhido. Configuração antiga (pagbank_token) = PagBank; nova sem escolha = Mercado Pago (recomendado)."""
    pay = pay or {}
    g = pay.get("gateway")
    if g in GATEWAYS:
        return g
    return "pagbank" if pay.get("pagbank_token") else "mercadopago"

def gw_token(pay):
    pay = pay or {}
    return pay.get("api_token") or pay.get("pagbank_token", "")  # pagbank_token = formato antigo

def gw_env(pay):
    pay = pay or {}
    return "producao" if (pay.get("api_env") or pay.get("pagbank_env")) == "producao" else "sandbox"

def skip_customer_ready(pay):
    """(pode_pular, motivo). Só pula as perguntas ao cliente se o vendedor desligou a opção E cadastrou
    os dados padrão que o intermediador exige: e-mail sempre; CPF no PagBank e no Asaas."""
    pay = pay or {}
    if pay.get("ask_customer", True) is not False:
        return False, ""
    if not email_ok((pay.get("default_email") or "").strip()):
        return False, "Falta o e-mail padrão da cobrança"
    cpf = re.sub(r"\D", "", pay.get("default_cpf") or "")
    if cpf and not cpf_ok(cpf):
        return False, "O CPF padrão é inválido"
    if gw_name(pay) != "mercadopago" and not cpf:
        return False, f"O {GATEWAYS[gw_name(pay)]['label']} exige CPF: preencha o CPF padrão"
    return True, ""

def gw_url(gw, env):
    return GATEWAYS[gw]["urls"]["producao" if env == "producao" else "sandbox"]

def gw_headers(gw, token):
    if gw == "asaas":
        return {"access_token": token, "Content-Type": "application/json", "User-Agent": UA}
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json", "Accept": "application/json"}

def mp_env_from_token(token):
    return "sandbox" if token.startswith("TEST-") else "producao"

def order_key(gw, charge_id):
    """Guarda o intermediador junto com o id (pedidos antigos, sem prefixo, são do PagBank)."""
    return str(charge_id) if gw == "pagbank" else f"{gw}:{charge_id}"

def split_order_key(key):
    gw, _, cid = key.partition(":")
    return (gw, cid) if cid and gw in GATEWAYS else ("pagbank", key)

def _fail(gw, r):
    raise RuntimeError(f"{GATEWAYS[gw]['label']} {r.status_code}: {r.text[:300]}")

def gateway_token_ok(gw, token, env):
    """True = aceito, False = recusado (401/403), None = sem conexão."""
    base, h = gw_url(gw, env), gw_headers(gw, token)
    probe = {"pagbank": f"{base}/orders/ORDE_00000000-0000-0000-0000-000000000000",
             "mercadopago": f"{base}/users/me",
             "asaas": f"{base}/customers?limit=1"}[gw]
    try:
        r = httpx.get(probe, headers=h, timeout=15)
    except Exception:
        return None
    return r.status_code not in (401, 403)

def gateway_diagnose(gw, token, env):
    """(True/False/None, mensagem para o painel). Detecta token de testes usado em produção e vice-versa."""
    label = GATEWAYS[gw]["label"]
    res = gateway_token_ok(gw, token, env)
    if res is None:
        return None, f"Não consegui falar com o {label} agora. Tente de novo em instantes."
    if not GATEWAYS[gw]["has_env"]:
        modo = "testes" if mp_env_from_token(token) == "sandbox" else "produção (vendas reais)"
        return (True, f"Token aceito pelo {label} (modo {modo}).") if res else \
               (False, f"O {label} recusou esse token. Use o Access Token (começa com APP_USR- ou TEST-).")
    modo = "produção" if env == "producao" else "sandbox (testes)"
    if res:
        return True, f"Token aceito pelo {label} no modo {modo}."
    outro = "sandbox" if env == "producao" else "producao"
    if gateway_token_ok(gw, token, outro):
        certo = "Sandbox (testes)" if outro == "sandbox" else "Produção"
        return False, f"Esse token é do modo {certo}. Mude o Ambiente para {certo} e salve de novo."
    return False, f"O {label} recusou esse token no modo {modo}. Copie o token de novo, inteiro e sem espaços."

async def _pagbank_create(base, h, reference, customer, lines, total):
    body = {
        "reference_id": reference,
        "customer": {"name": customer["nome"], "email": customer["email"], "tax_id": customer["cpf"]},
        "items": [{"name": n, "quantity": q, "unit_amount": c} for n, q, c in lines],
        "qr_codes": [{"amount": {"value": total}}],
    }
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(f"{base}/orders", headers=h, json=body)
    if r.status_code not in (200, 201):
        _fail("pagbank", r)
    d = r.json()
    qr = d["qr_codes"][0]
    png = next((l.get("href") for l in qr.get("links", []) if l.get("rel") == "QRCODE.PNG"), None)
    return d["id"], qr["text"], png

async def _mercadopago_create(base, h, reference, customer, lines, total):
    nome = customer["nome"].split()
    body = {
        "transaction_amount": round(total / 100, 2),
        "description": ", ".join(f"{q}x {n}" for n, q, _ in lines)[:250],
        "payment_method_id": "pix",
        "external_reference": reference,
        "payer": {"email": customer["email"], "first_name": nome[0], "last_name": " ".join(nome[1:]) or nome[0]},
    }
    if customer.get("cpf"):  # sem CPF (modo "não pedir dados"), o Mercado Pago recebe só o e-mail
        body["payer"]["identification"] = {"type": "CPF", "number": customer["cpf"]}
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(f"{base}/v1/payments", headers={**h, "X-Idempotency-Key": reference}, json=body)
    if r.status_code not in (200, 201):
        _fail("mercadopago", r)
    d = r.json()
    td = d["point_of_interaction"]["transaction_data"]
    png = base64.b64decode(td["qr_code_base64"]) if td.get("qr_code_base64") else td.get("ticket_url")
    return d["id"], td["qr_code"], png

async def _asaas_create(base, h, reference, customer, lines, total):
    async with httpx.AsyncClient(timeout=30) as client:
        # reaproveita o cliente pelo CPF para não duplicar cadastro a cada compra
        r = await client.get(f"{base}/customers", headers=h, params={"cpfCnpj": customer["cpf"]})
        found = r.json().get("data") if r.status_code == 200 else None
        if found:
            cust_id = found[0]["id"]
        else:
            r = await client.post(f"{base}/customers", headers=h, json={
                "name": customer["nome"], "cpfCnpj": customer["cpf"], "email": customer["email"]})
            if r.status_code not in (200, 201):
                _fail("asaas", r)
            cust_id = r.json()["id"]
        r = await client.post(f"{base}/payments", headers=h, json={
            "customer": cust_id, "billingType": "PIX", "value": round(total / 100, 2),
            "dueDate": datetime.date.today().isoformat(), "externalReference": reference,
            "description": ", ".join(f"{q}x {n}" for n, q, _ in lines)[:500]})
        if r.status_code not in (200, 201):
            _fail("asaas", r)
        pay_id = r.json()["id"]
        r = await client.get(f"{base}/payments/{pay_id}/pixQrCode", headers=h)
        if r.status_code != 200:
            _fail("asaas", r)
        d = r.json()
    return pay_id, d["payload"], base64.b64decode(d["encodedImage"]) if d.get("encodedImage") else None

async def gateway_create_pix(pay, reference, customer, lines):
    """lines = [(nome, qtd, centavos)]. Devolve (chave_do_pedido, copia_e_cola, qr_bytes_ou_url)."""
    gw, tok = gw_name(pay), gw_token(pay)
    env = mp_env_from_token(tok) if gw == "mercadopago" else gw_env(pay)
    create = {"pagbank": _pagbank_create, "mercadopago": _mercadopago_create, "asaas": _asaas_create}[gw]
    cid, copia, qr = await create(gw_url(gw, env), gw_headers(gw, tok), reference, customer, lines,
                                  sum(q * c for _, q, c in lines))
    return order_key(gw, cid), copia, qr

async def gateway_is_paid(pay, key):
    gw, cid = split_order_key(key)
    tok = gw_token(pay)
    env = mp_env_from_token(tok) if gw == "mercadopago" else gw_env(pay)
    base, h = gw_url(gw, env), gw_headers(gw, tok)
    path = {"pagbank": f"/orders/{cid}", "mercadopago": f"/v1/payments/{cid}", "asaas": f"/payments/{cid}"}[gw]
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get(base + path, headers=h)
    if r.status_code != 200:
        _fail(gw, r)
    d = r.json()
    if gw == "pagbank":
        return any(ch.get("status") == "PAID" for ch in d.get("charges", []))
    if gw == "mercadopago":
        return d.get("status") == "approved"
    return d.get("status") in ("RECEIVED", "CONFIRMED", "RECEIVED_IN_CASH")

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

    # ---- chat limpo: mensagens de navegação são apagadas na próxima escolha do cliente ----
    auto_clean = bot.get("auto_clean", True)
    trash = {}        # chat_id -> ids de mensagens já usadas (catálogo, carrinho, perguntas respondidas)
    last_notice = {}  # chat_id -> id do último "adicionado ao carrinho" (só o mais recente fica na tela)
    pix_msgs = {}     # chave do pedido -> ids do QR Code e do copia e cola (apagados quando o pagamento é confirmado)

    def track(m):
        if auto_clean and m is not None and getattr(m, "message_id", None):
            trash.setdefault(m.chat_id, []).append(m.message_id)
        return m

    def remember_pix(key, *msgs):
        if auto_clean:
            pix_msgs[key] = [m.message_id for m in msgs if m is not None and getattr(m, "message_id", None)]

    async def drop(bot_api, chat_id, ids):
        for mid in ids:
            try:
                await bot_api.delete_message(chat_id, mid)
            except Exception:
                pass  # mensagem já apagada, antiga demais (48h) ou sem permissão no grupo

    async def clean(bot_api, chat_id, also=None):
        """Apaga as mensagens de navegação anteriores e, se passada, a mensagem do cliente que disparou a escolha."""
        if not auto_clean:
            return
        ids = trash.pop(chat_id, [])
        if chat_id in last_notice:
            ids.append(last_notice.pop(chat_id))
        if also is not None and getattr(also, "message_id", None):
            ids.append(also.message_id)
        await drop(bot_api, chat_id, ids)

    products = bot.get("products", [])
    carts = {}  # user_id -> lista de produtos; fica só na memória (zera ao reiniciar o bot)
    menu_row = []
    if products:
        menu_row += ["🛍 Produtos e Serviços", "🛒 Carrinho"]
    if (payment or {}).get("type"):
        menu_row.append("✅ Finalizar compra")
    menu_markup = ReplyKeyboardMarkup([menu_row], resize_keyboard=True) if menu_row else None

    # ---- botões personalizados (aba Botões do painel): substituem o menu padrão acima ----
    buttons = [b for b in (bot.get("buttons") or []) if (b.get("text") or "").strip()]
    try:
        per_row = min(3, max(1, int(bot.get("buttons_per_row") or 2)))
    except (TypeError, ValueError):
        per_row = 2

    def kb_button(b):
        act, text = b.get("action") or "text", b["text"].strip()
        if act == "contact":
            return KeyboardButton(text, request_contact=True)
        if act == "location":
            return KeyboardButton(text, request_location=True)
        return KeyboardButton(text)

    custom_markup = ReplyKeyboardMarkup(
        [[kb_button(b) for b in buttons[i:i + per_row]] for i in range(0, len(buttons), per_row)],
        resize_keyboard=True, is_persistent=True) if buttons else None
    if menu_markup:
        menu_markup = ReplyKeyboardMarkup([menu_row], resize_keyboard=True, is_persistent=True)
    # contato e localização são enviados pelo próprio Telegram ao tocar; os demais chegam como texto
    btn_map = {b["text"].strip(): b for b in buttons if (b.get("action") or "text") not in ("contact", "location")}
    cmd_handlers = {}  # nome do comando -> handler (preenchido no registro, lá embaixo)

    # ---- entrega do menu de botões: não depende do tipo do comando /start ----
    main_markup = custom_markup or rmarkup or menu_markup
    kb_seen = set()  # quem já recebeu o menu desde que o bot (re)iniciou

    async def ensure_menu(message, uid, force=False):
        """Manda o menu de botões numa mensagem própria, uma vez por cliente (ou sempre, no /start)."""
        if not main_markup or (uid in kb_seen and not force):
            return
        kb_seen.add(uid)
        try:
            await message.reply_text("Use os botões abaixo para navegar.", reply_markup=main_markup)
        except Exception as e:
            log(bid, "WARN", f"menu de botões: {e}")

    def make_cmd(name, text):
        async def h(update, ctx):
            if not await allowed(update):
                await update.message.reply_text("Somente administradores.")
                return
            if main_markup:
                kb_seen.add(update.effective_user.id)
            await update.message.reply_text(text, reply_markup=main_markup)
            log(bid, "CMD", f"/{name} de {update.effective_user.id}")
        return h

    def make_cmd_image(name, image, caption):
        async def h(update, ctx):
            if not await allowed(update):
                await update.message.reply_text("Somente administradores.")
                return
            img = get_image(image)
            if img:
                await update.message.reply_photo(photo=io.BytesIO(img[1]), caption=caption or None)
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

    pay_state = {}  # user_id -> {"step", "lines", "data"} enquanto o cliente informa nome/CPF/e-mail
    customers = {}  # user_id -> {"nome", "cpf", "email"}; fica só na memória (pergunta de novo após reiniciar)

    def cart_lines(items):
        """Agrupa itens iguais -> [(nome, qtd, centavos)] para o intermediador de pagamento."""
        grouped = {}
        for p in items:
            k = ((p.get("name") or "Produto")[:64], int(round(parse_price(p.get("price")) * 100)))
            grouped[k] = grouped.get(k, 0) + 1
        return [(n, q, c) for (n, c), q in grouped.items() if c > 0]

    async def pix_flood(message, user):
        """No máximo 6 cobranças por cliente a cada 10 minutos (evita encher a conta do vendedor de cobranças)."""
        if rl_take(("pix", bid, user.id), 6, 600):
            track(await message.reply_text("Muitos pedidos em pouco tempo. Aguarde alguns minutos e tente de novo."))
            log(bid, "WARN", f"{user.id} bloqueado por excesso de pedidos de Pix")
            return True
        return False

    async def create_and_send_pix(message, user, lines, customer):
        if await pix_flood(message, user):
            return
        total = sum(q * c for _, q, c in lines)
        summary = (f"Cliente: {customer['nome']} (@{user.username or 'sem username'}, ID {user.id})\n"
                   + ("" if customer.get("padrao") else f"E-mail: {customer['email']}\n") + "\n"
                   + "\n".join(f"{q}x {n}" for n, q, _ in lines) + f"\n\nTotal: {fmt_price(total / 100)}")
        try:
            order_id, copia, qr = await gateway_create_pix(
                payment, f"{bid}-{user.id}-{uuid.uuid4().hex[:8]}", customer, lines)
        except Exception as e:
            log(bid, "ERR", f"Pix: {e}")
            label = GATEWAYS[gw_name(payment)]["label"]
            reserva = bool((payment.get("pix_key") or "").strip())
            await alert_seller(message.get_bot(), f"⚠️ O {label} recusou a cobrança de um cliente"
                               + (" — enviei o Pix com valor exato (chave reserva) no lugar." if reserva else " — a venda não foi gerada.")
                               + f"\n\nMotivo: {str(e)[:300]}\n\nConfira o token na aba Pagamento do painel (botão Testar token).")
            if reserva:
                log(bid, "WARN", f"{label} falhou: usando Pix com valor exato (chave reserva)")
                await pixqr_send(message, user, lines)
            else:
                track(await message.reply_text("⚠️ Não consegui gerar o Pix agora. O vendedor já foi avisado — tente de novo em alguns minutos."))
            return
        try:
            save_order(order_id, bid, message.chat_id, summary, total)
        except Exception as e:  # a cobrança já existe: entrega o Pix mesmo assim, só a confirmação automática fica sem registro
            log(bid, "ERR", f"salvar pedido {order_id}: {e}")
        log(bid, "PIX", f"{user.id} pedido {order_id} {fmt_price(total / 100)}")
        await message.reply_text(f"💳 Pedido criado! Total: {fmt_price(total / 100)}")
        foto_msg = None
        if qr:
            try:
                if isinstance(qr, bytes):  # Mercado Pago e Asaas já devolvem a imagem
                    foto = io.BytesIO(qr)
                else:  # PagBank devolve um link: baixa aqui (mais confiável do que o Telegram buscar sozinho)
                    async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
                        r = await client.get(qr)
                    foto = io.BytesIO(r.content) if r.status_code == 200 and r.content else qr
                foto_msg = await message.reply_photo(photo=foto, caption="📷 Escaneie o QR Code no app do seu banco.")
            except Exception as e:
                log(bid, "WARN", f"imagem do QR: {e}")
        copia_msg = await message.reply_text(
            f"Pix copia e cola (toque para copiar):\n\n`{copia}`\n\n"
            "Assim que o pagamento cair, você recebe a confirmação aqui automaticamente.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔄 Já paguei, verificar",
                                                                     callback_data=f"pixchk|{order_id}")]]))
        remember_pix(order_id, foto_msg, copia_msg)

    def default_customer(user):
        """Dados padrão cadastrados pelo vendedor, usados quando 'Pedir dados do cliente' está desligado.
        Devolve None se a opção está ligada ou se faltam os dados que o intermediador exige."""
        ok, _ = skip_customer_ready(payment)
        if not ok:
            return None
        nome = (getattr(user, "full_name", "") or "").strip() or "Cliente Telegram"
        if len(nome.split()) < 2:
            nome += " Cliente"
        return {"nome": nome[:100], "cpf": re.sub(r"\D", "", payment.get("default_cpf") or ""),
                "email": payment["default_email"].strip(), "padrao": True}

    async def api_checkout(message, user, items):
        if not gw_token(payment):
            track(await message.reply_text("Pagamento automático não configurado — fale com o vendedor."))
            return
        lines = cart_lines(items)
        if not lines:
            track(await message.reply_text("Esses produtos estão sem preço. Fale com o vendedor."))
            return
        padrao = default_customer(user)
        if padrao:  # vendedor desligou "Pedir dados do cliente": gera o Pix direto, sem perguntar nada
            await create_and_send_pix(message, user, lines, padrao)
            return
        if user.id in customers:
            await create_and_send_pix(message, user, lines, customers[user.id])
            return
        pay_state[user.id] = {"step": "nome", "lines": lines, "data": {}}
        track(await message.reply_text("Para gerar o Pix preciso de alguns dados (só na primeira compra). "
                                       "Envie *cancelar* para desistir.\n\n1/3 — Qual é o seu *nome completo*?",
                                       parse_mode="Markdown"))

    async def on_pay_step(msg, u, txt):
        """Cada resposta do cliente (nome, CPF, e-mail) já foi apagada por quem chama; as perguntas são rastreadas."""
        st = pay_state[u.id]
        d = st["data"]
        if txt.lower() == "cancelar":
            pay_state.pop(u.id, None)
            track(await msg.reply_text("Compra cancelada."))
        elif st["step"] == "nome":
            if len(txt.split()) < 2:
                track(await msg.reply_text("Envie seu nome completo (nome e sobrenome)."))
                return
            d["nome"] = txt[:100]
            st["step"] = "cpf"
            track(await msg.reply_text("2/3 — Agora envie seu CPF (só os números)."))
        elif st["step"] == "cpf":
            cpf = re.sub(r"\D", "", txt)
            if not cpf_ok(cpf):
                track(await msg.reply_text("CPF inválido. Confira e envie de novo (11 números)."))
                return
            d["cpf"] = cpf
            st["step"] = "email"
            track(await msg.reply_text("3/3 — Por último, seu e-mail."))
        elif st["step"] == "email":
            if not email_ok(txt):
                track(await msg.reply_text("E-mail inválido. Envie no formato nome@exemplo.com"))
                return
            d["email"] = txt
            customers[u.id] = d
            pay_state.pop(u.id, None)
            track(await msg.reply_text("✅ Dados recebidos! Gerando seu Pix..."))
            await create_and_send_pix(msg, u, st["lines"], d)

    async def notify_paid(bot_api, order_id):
        row = mark_paid(order_id)
        if not row:
            return
        chat_id, summary = row
        await drop(bot_api, chat_id, pix_msgs.pop(order_id, []))  # QR e copia e cola não servem mais
        await clean(bot_api, chat_id)
        await bot_api.send_message(chat_id, "✅ Pagamento confirmado! Obrigado pela compra.")
        admin = str(payment.get("admin_chat_id") or "").strip()
        if admin.lstrip("-").isdigit():
            try:
                await bot_api.send_message(int(admin), f"✅ PAGAMENTO CONFIRMADO ({order_id})\n\n{summary}")
            except Exception as e:
                log(bid, "WARN", f"aviso ao vendedor: {e}")
        log(bid, "PAGO", f"{order_id} chat {chat_id}")

    async def check_pending(bot_api):
        """Chamado pelo Worker a cada 30s: confirma pagamentos sem o cliente precisar tocar em nada."""
        if payment.get("type") != "api" or not gw_token(payment):
            return
        for order_id in pending_orders(bid):
            if order_id.startswith("manual:"):  # Pix com valor exato: quem confirma é o vendedor
                continue
            try:
                if await gateway_is_paid(payment, order_id):
                    await notify_paid(bot_api, order_id)
            except Exception as e:
                log(bid, "ERR", f"verificar {order_id}: {e}")

    admin_id = str(payment.get("admin_chat_id") or "").strip()
    admin_id = int(admin_id) if admin_id.lstrip("-").isdigit() else None
    pix_avisados = set()  # pedidos em que o cliente já tocou "Já paguei" (evita spam para o vendedor)

    def confirm_kb(key):
        return InlineKeyboardMarkup([[InlineKeyboardButton("✅ Confirmar pagamento", callback_data=f"manconf|{key}")]])

    last_alert = {"t": 0.0}

    async def alert_seller(bot_api, text):
        """Avisa o vendedor no Telegram (no máximo 1 aviso a cada 10 min, para não virar spam)."""
        if not admin_id or time.time() - last_alert["t"] < 600:
            return
        last_alert["t"] = time.time()
        try:
            await bot_api.send_message(admin_id, text)
        except Exception as e:
            log(bid, "WARN", f"aviso ao vendedor: {e}")

    async def pixqr_checkout(message, user, items):
        await pixqr_send(message, user, cart_lines(items))

    async def pixqr_send(message, user, lines):
        """Pix com valor exato: gera o BR Code aqui mesmo, sem pedir dados do cliente. Confirmação é do vendedor."""
        key_pix = (payment.get("pix_key") or "").strip()
        if not key_pix or not lines:
            await message.reply_text("Pagamento não configurado — fale com o vendedor.")
            return
        if await pix_flood(message, user):
            return
        total = sum(q * c for _, q, c in lines)
        txid ="PED" + uuid.uuid4().hex[:10].upper()
        code = pix_brcode(key_pix, payment.get("pix_name") or "", payment.get("pix_city") or "", total, txid)
        key = f"manual:{txid}"
        nome = getattr(user, "full_name", "") or "Cliente"
        summary = (f"Cliente: {nome} (@{user.username or 'sem username'}, ID {user.id})\n\n"
                   + "\n".join(f"{q}x {n}" for n, q, _ in lines)
                   + f"\n\nTotal: {fmt_price(total / 100)}\nIdentificador no Pix: {txid}")
        try:
            save_order(key, bid, message.chat_id, summary, total)
        except Exception as e:
            log(bid, "ERR", f"salvar pedido {key}: {e}")
        log(bid, "PIX", f"{user.id} pedido {txid} {fmt_price(total / 100)} (valor exato)")
        foto_msg = await message.reply_photo(photo=io.BytesIO(qr_png(code)),
                                             caption=f"📷 Escaneie no app do seu banco — o valor de {fmt_price(total / 100)} já vem preenchido.")
        instr = (payment.get("instructions") or "").strip()
        copia_msg = await message.reply_text(
            f"Pix copia e cola (toque para copiar):\n\n`{code}`\n\n"
            + (instr + "\n\n" if instr else "") + "Depois de pagar, toque no botão abaixo.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("✅ Já paguei", callback_data=f"manpaid|{key}")]]))
        remember_pix(key, foto_msg, copia_msg)
        if admin_id:
            try:
                await message.get_bot().send_message(admin_id, f"🆕 Pedido {txid} aguardando Pix\n\n{summary}",
                                                     reply_markup=confirm_kb(key))
            except Exception as e:
                log(bid, "WARN", f"aviso ao vendedor: {e}")

    async def send_payment(message, items_desc=None, items=None, user=None):
        ptype = (payment or {}).get("type") or ""
        if ptype in ("pix", "api", "pixqr") and items_desc:
            await message.reply_text(f"🧾 *Resumo da compra:*\n{items_desc}", parse_mode="Markdown")
        if ptype == "pix":
            key = payment.get("pix_key") or "(chave não configurada)"
            await message.reply_text(f"💳 *Chave Pix* (toque para copiar):\n`{key}`", parse_mode="Markdown")
            pname = payment.get("pix_name") or ""
            instr = payment.get("instructions") or ""
            info = (f"👤 Recebedor: {pname}\n" if pname else "") + (instr or "")
            if info.strip():
                await message.reply_text(info.strip())
            qr = payment.get("qr_image")
            qimg = get_image(qr)
            if qimg:
                await message.reply_photo(photo=io.BytesIO(qimg[1]), caption="📷 QR Code Pix")
        elif ptype == "api" and items and user:
            await api_checkout(message, user, items)
        elif ptype == "pixqr" and items and user:
            await pixqr_checkout(message, user, items)
        else:
            await message.reply_text("Pagamento não configurado — fale com o vendedor.")

    async def do_checkout(message, user):
        uid = user.id
        items = carts.get(uid) or []
        if not items:
            track(await message.reply_text("Seu carrinho está vazio. Adicione produtos no Catálogo antes de finalizar a compra."))
            return
        total = sum(parse_price(p.get("price")) for p in items)
        resumo = "\n".join(f"- {p.get('name')} ({p.get('price') or ''})" for p in items)
        marca = "🟢 " if len(items) > 1 else ""
        resumo += f"\n\n{marca}*Total: {fmt_price(total)}*"
        await send_payment(message, resumo, items=items, user=user)
        carts[uid] = []

    def make_cmd_payment(name, pay):
        async def h(update, ctx):
            if not await allowed(update):
                await update.message.reply_text("Somente administradores.")
                return
            await clean(ctx.bot, update.effective_chat.id, update.message)
            await do_checkout(update.message, update.effective_user)
            log(bid, "CMD", f"/{name} (finalizar compra) de {update.effective_user.id}")
        return h

    def get_categories():
        cats = {}
        for idx, p in enumerate(products):
            c = (p.get("category") or "Geral").strip() or "Geral"
            cats.setdefault(c, []).append(idx)
        return cats

    async def send_categories(message):
        """Lista de categorias para o cliente escolher (o botão guarda a posição, não o nome: nomes longos estouram o limite do Telegram)."""
        cats = get_categories()
        kb = InlineKeyboardMarkup([[InlineKeyboardButton(f"{c} ({len(idxs)})", callback_data=f"catsel|{i}")]
                                    for i, (c, idxs) in enumerate(cats.items())])
        track(await message.reply_text("📂 *Escolha uma categoria:*", parse_mode="Markdown", reply_markup=kb))

    async def send_category(message, cat, idxs):
        varias = len(get_categories()) > 1
        if varias or cat != "Geral":  # cabeçalho: deixa claro de qual categoria são os produtos abaixo
            n = len(idxs)
            track(await message.reply_text(
                f"📂 *Categoria: {escape_markdown(cat)}*\n{n} {'produto' if n == 1 else 'produtos'} nesta categoria:",
                parse_mode="Markdown"))
        await send_products(message, idxs)
        if varias:
            track(await message.reply_text(
                f"Esses são os produtos de *{escape_markdown(cat)}*.", parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Ver outras categorias", callback_data="catback")]])))

    async def send_products(message, idxs):
        for idx in idxs:
            p = products[idx]
            caption = f"*{p.get('name') or 'Produto'}*\n{p.get('price') or ''}\n\n{p.get('description') or ''}".strip()
            kb = InlineKeyboardMarkup([[
                InlineKeyboardButton("🛒 Adicionar ao carrinho", callback_data=f"addcart|{idx}"),
                InlineKeyboardButton("💳 Comprar", callback_data=f"buy|{idx}")
            ]])
            pimg = get_image(p.get("image"))
            if pimg:
                track(await message.reply_photo(photo=io.BytesIO(pimg[1]), caption=caption, parse_mode="Markdown", reply_markup=kb))
            else:
                track(await message.reply_text(caption, parse_mode="Markdown", reply_markup=kb))

    async def on_catalog(update, ctx):
        if not await allowed(update):
            await update.message.reply_text("Somente administradores.")
            return
        await clean(ctx.bot, update.effective_chat.id, update.message)  # some o catálogo anterior e o "/catalogo" do cliente
        if not products:
            track(await update.message.reply_text("Nenhum produto no catálogo ainda."))
            return
        cats = get_categories()
        if len(cats) <= 1:
            cat = next(iter(cats))
            await send_category(update.message, cat, cats[cat])
        else:
            await send_categories(update.message)
        log(bid, "CATALOGO", f"{update.effective_user.id}: {len(products)} produtos")

    async def on_cart(update, ctx):
        if not await allowed(update):
            await update.message.reply_text("Somente administradores.")
            return
        u = update.effective_user
        await clean(ctx.bot, update.effective_chat.id, update.message)
        items = carts.get(u.id) or []
        if not items:
            track(await update.message.reply_text("Seu carrinho está vazio. Use /catalogo para adicionar produtos."))
            return
        resumo = "\n".join(f"- {p.get('name')} ({p.get('price') or ''})" for p in items)
        total = sum(parse_price(p.get("price")) for p in items)
        marca = "🟢 " if len(items) > 1 else ""
        resumo += f"\n\n{marca}*Total: {fmt_price(total)}*"
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Finalizar compra", callback_data="checkout"),
            InlineKeyboardButton("🗑 Esvaziar", callback_data="clearcart")
        ]])
        track(await update.message.reply_text(f"🛒 *Seu carrinho:*\n{resumo}", parse_mode="Markdown", reply_markup=kb))

    async def on_contact(update, ctx):
        c = update.message.contact
        log(bid, "CONTATO", f"{update.effective_user.id}: {c.phone_number}")
        await update.message.reply_text("Contato recebido, obrigado!")

    async def on_location(update, ctx):
        l = update.message.location
        log(bid, "LOCALIZACAO", f"{update.effective_user.id}: {l.latitude},{l.longitude}")
        await update.message.reply_text("Localização recebida, obrigado!")

    async def run_button(update, ctx, btn):
        """Executa a função predefinida de um botão do menu (aba Botões do painel)."""
        msg, u = update.message, update.effective_user
        act, val = btn.get("action") or "text", (btn.get("value") or "").strip()
        log(bid, "BOTAO", f"{u.id}: {btn['text'].strip()}")
        if act == "catalog":
            await on_catalog(update, ctx)
        elif act == "cart":
            await on_cart(update, ctx)
        elif act == "command":
            handler = cmd_handlers.get(val.lstrip("/").lower())
            if handler:
                await handler(update, ctx)
            else:
                track(await msg.reply_text("Esse botão ainda não foi configurado."))
        else:
            await clean(ctx.bot, msg.chat_id, msg)  # some o toque do cliente e a resposta do botão anterior
            if act == "checkout":
                await do_checkout(msg, u)
            elif act == "clearcart":
                carts[u.id] = []
                track(await msg.reply_text("Carrinho esvaziado."))
            elif act in ("link", "support"):
                url = val
                if act == "support":
                    if val.startswith("@"):
                        url = "https://t.me/" + val[1:]
                    elif not val and admin_id:
                        url = f"tg://user?id={admin_id}"
                if re.match(r"^(https?://|tg://)", url):
                    texto = "Toque abaixo para falar com o vendedor." if act == "support" else "Toque abaixo para abrir."
                    rotulo = "Abrir conversa" if act == "support" else "Abrir link"
                    track(await msg.reply_text(texto, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(rotulo, url=url)]])))
                else:
                    track(await msg.reply_text("Esse botão ainda não foi configurado."))
            else:  # text
                track(await msg.reply_text(val or "..."))

    async def on_text(update, ctx):
        msg, u = update.message, update.effective_user
        txt = (msg.text or "").strip() if msg else ""
        if not txt or not u:
            return
        if not await allowed(update):
            await msg.reply_text("Somente administradores.")
            return
        if txt in btn_map:
            kb_seen.add(u.id)  # se tocou num botão, já está com o menu
            await run_button(update, ctx, btn_map[txt])
            return
        if u.id not in pay_state and txt not in ("🛍 Produtos e Serviços", "🛒 Carrinho", "✅ Finalizar compra"):
            await ensure_menu(msg, u.id)  # cliente escreveu sem ter o menu (ex.: bot reiniciou): entrega o menu
        if txt == "🛍 Produtos e Serviços":
            await on_catalog(update, ctx)
            return
        if txt == "🛒 Carrinho":
            await on_cart(update, ctx)
            return
        if txt == "✅ Finalizar compra":
            await clean(ctx.bot, msg.chat_id, msg)
            await do_checkout(msg, u)
            return
        if u.id in pay_state:
            # apaga a resposta do cliente (nome, CPF, e-mail) e a pergunta anterior: dado pessoal não fica no chat
            await clean(ctx.bot, msg.chat_id, msg)
            await on_pay_step(msg, u, txt)
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
            markup = custom_markup or rmarkup
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
            ref = data.split("|", 1)[1]
            cats = get_categories()
            nomes = list(cats)
            # posição da categoria (formato atual) ou o próprio nome (botões enviados antes desta versão)
            cat = nomes[int(ref)] if ref.isdigit() and int(ref) < len(nomes) and ref not in cats else ref
            await q.answer()
            await clean(ctx.bot, q.message.chat_id)  # some a lista de categorias e o que estava aberto antes
            if cat in cats:
                await send_category(q.message, cat, cats[cat])
                log(bid, "CATALOGO", f"{u.id} abriu a categoria {cat}")
            else:
                await send_categories(q.message)  # a categoria mudou no painel: mostra a lista atual
            return
        if data == "catback":
            await q.answer()
            await clean(ctx.bot, q.message.chat_id)
            await send_categories(q.message)
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
                chat = q.message.chat_id
                if auto_clean and chat in last_notice:  # só o aviso mais recente fica; o catálogo continua aberto
                    await drop(ctx.bot, chat, [last_notice.pop(chat)])
                aviso = await q.message.reply_text(txt, parse_mode="Markdown")
                if auto_clean and aviso is not None and getattr(aviso, "message_id", None):
                    last_notice[chat] = aviso.message_id
                log(bid, "CARRINHO", f"{u.id} adicionou {p.get('name')}")
            else:
                await q.answer("Produto não encontrado.")
            return
        if data.startswith("buy|"):
            idx = int(data.split("|", 1)[1])
            await q.answer()
            if 0 <= idx < len(products):
                p = products[idx]
                await clean(ctx.bot, q.message.chat_id)
                await send_payment(q.message, f"- *{p.get('name')}* — *{p.get('price') or ''}*", items=[p], user=u)
                log(bid, "COMPRA", f"{u.id} comprou direto {p.get('name')}")
            return
        if data == "checkout":
            await q.answer()
            await clean(ctx.bot, q.message.chat_id)
            await do_checkout(q.message, u)
            log(bid, "COMPRA", f"{u.id} finalizou carrinho")
            return
        if data.startswith("manpaid|"):  # cliente avisa que pagou o Pix com valor exato
            key = data.split("|", 1)[1]
            await q.answer()
            row = order_row(key, bid)
            if not row or row[1] != q.message.chat_id:  # pedido inexistente ou de outra conversa: não avisa ninguém
                track(await q.message.reply_text("Pedido não encontrado."))
                return
            status = row[0]
            if status:
                await q.message.reply_text("✅ Esse pagamento já foi confirmado.")
            elif key in pix_avisados:
                await q.message.reply_text("Já avisei o vendedor. Assim que ele conferir, você recebe a confirmação aqui.")
            else:
                pix_avisados.add(key)
                await q.message.reply_text("Obrigado! Avisei o vendedor — assim que ele conferir o Pix, você recebe a confirmação aqui.")
                log(bid, "COMPRA", f"{u.id} avisou que pagou {key.split(':', 1)[1]}")
                if admin_id:
                    try:
                        await ctx.bot.send_message(admin_id, f"💬 O cliente {getattr(u, 'full_name', '') or u.id} diz que pagou o pedido "
                                                             f"{key.split(':', 1)[1]}. Confira no app do banco e toque em Confirmar.",
                                                   reply_markup=confirm_kb(key))
                    except Exception as e:
                        log(bid, "WARN", f"aviso ao vendedor: {e}")
            return
        if data.startswith("manconf|"):  # vendedor confirma o Pix com valor exato
            key = data.split("|", 1)[1]
            if u.id != admin_id:
                await q.answer("Só o vendedor pode confirmar pagamentos.", show_alert=True)
                return
            row = mark_paid(key)
            await q.answer("Pagamento confirmado!" if row else "Esse pedido já estava confirmado.")
            if row:
                chat_id, _ = row
                await drop(ctx.bot, chat_id, pix_msgs.pop(key, []))  # QR e copia e cola não servem mais
                await clean(ctx.bot, chat_id)
                await ctx.bot.send_message(chat_id, "✅ Pagamento confirmado pelo vendedor! Obrigado pela compra.")
                log(bid, "PAGO", f"{key.split(':', 1)[1]} confirmado pelo vendedor")
            try:
                await q.edit_message_reply_markup(reply_markup=None)
                await q.message.reply_text(f"✅ Pedido {key.split(':', 1)[1]} confirmado. O cliente foi avisado.")
            except Exception:
                pass
            return
        if data.startswith("pixchk|"):
            order_id = data.split("|", 1)[1]
            await q.answer()
            await clean(ctx.bot, q.message.chat_id)  # some o "ainda não identificado" anterior
            row = order_row(order_id, bid)
            status = row[0] if row and row[1] == q.message.chat_id else None  # só o dono do pedido consulta
            if status is None:
                track(await q.message.reply_text("Pedido não encontrado."))
            elif status:
                track(await q.message.reply_text("✅ Esse pagamento já foi confirmado."))
            else:
                try:
                    pago = await gateway_is_paid(payment, order_id)
                except Exception as e:
                    log(bid, "ERR", f"verificar {order_id}: {e}")
                    track(await q.message.reply_text("⚠️ Não consegui verificar agora. Tente de novo."))
                    return
                if pago:
                    await notify_paid(ctx.bot, order_id)
                else:
                    track(await q.message.reply_text("⏳ Pagamento ainda não identificado. Aguarde alguns segundos e toque em verificar de novo."))
            return
        if data == "clearcart":
            carts[u.id] = []
            await q.answer("Carrinho esvaziado.")
            await clean(ctx.bot, q.message.chat_id)  # apaga a mensagem do carrinho
            return
        await q.answer()
        log(bid, "CALLBACK", data)
        if cb_map.get(data):
            await q.message.reply_text(cb_map[data])

    a = Application.builder().token(bot["token"]).build()

    def reg(name, handler, carries_menu=False):
        """Registra o comando e guarda o handler para os botões com a função "Executar comando".
        Comandos que não são de texto (catálogo, link, imagem...) não conseguem levar o menu de botões na
        própria resposta; por isso mandam o menu antes, numa mensagem separada."""
        cmd_handlers[name] = handler
        if carries_menu:
            a.add_handler(CommandHandler(name, handler))
            return

        async def with_menu(update, ctx):
            if update.message and update.effective_user and await allowed(update):
                await ensure_menu(update.message, update.effective_user.id, force=(name == "start"))
            await handler(update, ctx)
        a.add_handler(CommandHandler(name, with_menu))

    for name, text in cmds.items():
        reg(name, make_cmd(name, text), carries_menu=True)
    for name in cmd_catalog:
        reg(name, on_catalog)
    for name, meta in cmd_image.items():
        reg(name, make_cmd_image(name, meta["image"], meta["text"]))
    for name, meta in cmd_link.items():
        reg(name, make_cmd_link(name, meta["url"], meta["text"]))
    for name in cmd_contact:
        reg(name, make_cmd_contact(name))
    for name in cmd_location:
        reg(name, make_cmd_location(name))
    for name in cmd_payment:
        reg(name, make_cmd_payment(name, payment))
    if "catalogo" not in cmds and "catalogo" not in cmd_catalog:
        reg("catalogo", on_catalog)
    if "carrinho" not in cmds:
        reg("carrinho", on_cart)
    a.add_handler(CallbackQueryHandler(on_callback))
    a.add_handler(MessageHandler(filters.CONTACT, on_contact))
    a.add_handler(MessageHandler(filters.LOCATION, on_location))
    a.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    # /start sempre existe: é o que o Telegram envia no botão "Iniciar" e o que mostra o menu de botões
    if "start" not in cmd_handlers:
        reg("start", make_cmd("start", f"Olá! Eu sou o {bot.get('name') or 'bot'}. Como posso ajudar?"), carries_menu=True)
    # lista do botão "Menu" do Telegram: todos os comandos (não só os de texto), com /start primeiro
    descr = {n: ("Começar" if n == "start" else f"Comando /{n}") for n in cmds}
    descr.update({n: "Ver produtos" for n in cmd_catalog})
    descr.update({n: "Ver imagem" for n in cmd_image})
    descr.update({n: (m["text"] or "Abrir link")[:60] for n, m in cmd_link.items()})
    descr.update({n: "Enviar meu contato" for n in cmd_contact})
    descr.update({n: "Enviar minha localização" for n in cmd_location})
    descr.update({n: "Finalizar compra" for n in cmd_payment})
    descr["start"] = "Começar"  # sempre a mesma descrição, qualquer que seja a ação do /start
    # /catalogo e /carrinho continuam funcionando se digitados, mas só entram no Menu se o vendedor os criar
    cmd_meta = [(n, d) for n, d in sorted(descr.items(), key=lambda x: (x[0] != "start",)) if n in cmd_handlers]
    a.bot_data["cmd_names"] = cmd_meta
    a.bot_data["check_pending"] = check_pending
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
            ticks = 0
            while not self.stop.is_set():
                await asyncio.sleep(0.5)
                ticks += 1
                if ticks % 60 == 0:  # a cada 30s confere os Pix pendentes
                    try:
                        await a.bot_data["check_pending"](a.bot)
                    except Exception as e:
                        log(bid, "ERR", f"verificador de pagamentos: {e}")
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

# ---------------- Segurança da aplicação web ----------------
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.exceptions import HTTPException

app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)  # o Render fica na frente: IP e https reais vêm nos cabeçalhos
app.secret_key = os.environ.get("SECRET_KEY", "").strip() or os.urandom(32)
IS_PROD = bool(os.environ.get("RENDER") or os.environ.get("FORCE_HTTPS"))
app.config.update(
    SESSION_COOKIE_NAME="tb_session",
    SESSION_COOKIE_HTTPONLY=True,     # JavaScript não lê o cookie (roubo de sessão por XSS)
    SESSION_COOKIE_SAMESITE="Lax",    # outros sites não mandam o cookie em POST (CSRF)
    SESSION_COOKIE_SECURE=IS_PROD,    # só trafega em https
    PERMANENT_SESSION_LIFETIME=datetime.timedelta(days=7),
    MAX_CONTENT_LENGTH=6 * 1024 * 1024,  # nenhuma requisição passa de 6 MB
)
SESSION_MAX_AGE = 7 * 24 * 3600
JSON_MAX = 1024 * 1024

# ---- limite de tentativas (em memória; o app roda em um processo só) ----
_hits, _hits_lock = {}, threading.Lock()

def _rl_prune(key, window, now):
    q = _hits.get(key)
    if q is None:
        return None
    while q and q[0] <= now - window:
        q.popleft()
    return q

def rl_blocked(key, limit, window):
    """Segundos que faltam para liberar, ou 0 se ainda não bateu o limite."""
    now = time.time()
    with _hits_lock:
        q = _rl_prune(key, window, now)
        return int(q[0] + window - now) + 1 if q and len(q) >= limit else 0

def rl_hit(key):
    with _hits_lock:
        if len(_hits) > 20000:  # não deixa um ataque distribuído encher a memória
            for k in [k for k, q in _hits.items() if not q or q[-1] < time.time() - 3600]:
                _hits.pop(k, None)
        _hits.setdefault(key, collections.deque(maxlen=1000)).append(time.time())

def rl_take(key, limit, window):
    wait = rl_blocked(key, limit, window)
    if not wait:
        rl_hit(key)
    return wait

def too_many(wait):
    quando = f"{max(1, round(wait / 60))} min" if wait >= 60 else f"{wait} s"
    return jsonify(ok=False, err=f"Muitas tentativas. Tente de novo em {quando}."), 429

def client_ip():
    return request.remote_addr or "?"

def jbody():
    """Corpo JSON da requisição, sempre um objeto. Qualquer outra coisa é recusada."""
    d = request.get_json(silent=True)
    if not isinstance(d, dict):
        abort(make_response(jsonify(ok=False, err="Requisição inválida"), 400))
    return d

@app.before_request
def security_gate():
    if "uid" in session and time.time() - session.get("at", 0) > SESSION_MAX_AGE:
        session.clear()  # sessão velha demais: exige login de novo
    if request.method in ("POST", "PUT", "DELETE", "PATCH"):
        # CSRF: a requisição tem que vir do próprio painel. Outro site não consegue mandar este cabeçalho
        # (o navegador barra) nem ter a mesma origem.
        origem = request.headers.get("Origin") or request.headers.get("Referer") or ""
        if origem and urlparse(origem).netloc != request.host:
            return jsonify(ok=False, err="Origem não permitida"), 403
        if request.headers.get("X-Requested-With") != "fetch":
            return jsonify(ok=False, err="Requisição não permitida"), 403
        if request.mimetype == "application/json" and (request.content_length or 0) > JSON_MAX:
            return jsonify(ok=False, err="Dados grandes demais"), 413
    if request.path.startswith("/api/"):
        wait = rl_take(("api", session.get("uid") or client_ip()), 300, 60)
        if wait:
            return too_many(wait)

CSP = ("default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
       "font-src 'self' https://fonts.gstatic.com; img-src 'self' data: blob:; connect-src 'self'; "
       "frame-ancestors 'none'; base-uri 'none'; form-action 'self'; object-src 'none'")

@app.after_request
def security_headers(resp):
    h = resp.headers
    h.setdefault("X-Content-Type-Options", "nosniff")        # o navegador não "adivinha" tipos (imagem que vira script)
    h.setdefault("X-Frame-Options", "DENY")                   # o painel não abre dentro de outro site (clickjacking)
    h.setdefault("Referrer-Policy", "same-origin")
    h.setdefault("Permissions-Policy", "geolocation=(), camera=(), microphone=(), payment=()")
    h.setdefault("Content-Security-Policy", CSP)
    h.setdefault("Cross-Origin-Opener-Policy", "same-origin")
    if IS_PROD:
        h.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
    if not request.path.startswith("/images/"):
        h["Cache-Control"] = "no-store"                       # dados do painel não ficam em cache
    h.pop("Server", None)
    return resp

@app.errorhandler(Exception)
def on_error(e):
    """Nunca devolve detalhes internos (rastros de erro, SQL) para quem está de fora."""
    if isinstance(e, HTTPException):
        if e.response is not None:
            return e.response
        if e.code and e.code < 400:
            return e  # redirecionamentos seguem normalmente
        msg = {404: "Não encontrado", 405: "Método não permitido", 413: "Arquivo grande demais (máximo de 5 MB)"}.get(e.code, "Requisição inválida")
        return (jsonify(ok=False, err=msg), e.code) if request.path.startswith("/api/") or e.code == 413 else (msg, e.code)
    app.logger.exception("erro interno")
    return (jsonify(ok=False, err="Erro interno. Tente de novo."), 500) if request.path.startswith("/api/") else ("Erro interno", 500)

# ---- senhas ----
SENHAS_COMUNS = {"12345678", "123456789", "1234567890", "password", "senha123", "senha1234", "qwerty123", "abc12345",
                 "11111111", "00000000", "87654321", "123123123", "password1", "iloveyou1", "brasil123", "mudar123"}
DUMMY_HASH = generate_password_hash(secrets.token_hex(16))  # usado quando o e-mail não existe: tempo de resposta igual

def password_problem(pw, email=""):
    if not isinstance(pw, str) or len(pw) < 8:
        return "A senha precisa ter pelo menos 8 caracteres."
    if len(pw) > 200:
        return "Senha longa demais."
    if not re.search(r"[A-Za-zÀ-ÿ]", pw) or not re.search(r"\d", pw):
        return "A senha precisa ter letras e números."
    if pw.lower() in SENHAS_COMUNS or (email and pw.lower() == email.split("@")[0].lower()):
        return "Essa senha é fácil demais de adivinhar. Escolha outra."
    return ""

def start_session(uid):
    session.clear()  # sessão nova a cada login (impede "fixação de sessão")
    session.permanent = True
    session["uid"], session["at"] = uid, time.time()

# ---- verificação em duas etapas (TOTP, RFC 6238: Google Authenticator, Authy, etc.) ----
def totp_at(secret_b32, t):
    key = base64.b32decode(secret_b32.upper() + "=" * (-len(secret_b32) % 8))
    mac = hmac.new(key, struct.pack(">Q", int(t // 30)), "sha1").digest()
    o = mac[-1] & 15
    return f"{(struct.unpack('>I', mac[o:o + 4])[0] & 0x7fffffff) % 1000000:06d}"

_totp_used = {}  # uid -> último intervalo aceito (o mesmo código não vale duas vezes)

def totp_ok(secret, code, uid=None):
    code = re.sub(r"\D", "", str(code or ""))
    if len(code) != 6 or not secret:
        return False
    now = time.time()
    for w in (-1, 0, 1):  # tolera 30 s de diferença de relógio
        step = int((now + w * 30) // 30)
        if hmac.compare_digest(totp_at(secret, now + w * 30), code):
            if uid is not None:
                if _totp_used.get(uid, 0) >= step:
                    return False
                _totp_used[uid] = step
            return True
    return False

def sec_get(uid):
    with db() as conn, conn.cursor() as cur:
        cur.execute("SELECT totp_secret, backup_codes FROM account_security WHERE account_id=%s", (uid,))
        row = cur.fetchone()
    if not row:
        return "", []
    codes = row[1]
    if isinstance(codes, str):
        try:
            codes = json.loads(codes or "[]")
        except ValueError:
            codes = []
    return dec(row[0] or ""), list(codes or [])

def sec_set(uid, secret, codes):
    with db() as conn, conn.cursor() as cur:
        cur.execute("""INSERT INTO account_security (account_id, totp_secret, backup_codes) VALUES (%s,%s,%s)
                       ON CONFLICT (account_id) DO UPDATE SET totp_secret=EXCLUDED.totp_secret, backup_codes=EXCLUDED.backup_codes""",
                    (uid, enc(secret) if secret else None, json.dumps(codes)))
        conn.commit()

def _code_hash(code):
    return hashlib.sha256(re.sub(r"[^a-z0-9]", "", str(code or "").lower()).encode()).hexdigest()

def second_factor_ok(uid, code):
    """Aceita o código do aplicativo ou um código reserva (cada reserva vale uma vez)."""
    secret, codes = sec_get(uid)
    if totp_ok(secret, code, uid):
        return True
    h = _code_hash(code)
    if len(re.sub(r"[^a-z0-9]", "", str(code or "").lower())) == 8 and h in codes:
        codes.remove(h)
        sec_set(uid, secret, codes)
        return True
    return False

# ---- e-mail (confirmação de cadastro; só funciona com SMTP_HOST e SMTP_FROM configurados) ----
def smtp_ready():
    return bool(os.environ.get("SMTP_HOST") and os.environ.get("SMTP_FROM"))

def send_mail(to, subject, text):
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = os.environ["SMTP_FROM"], to, subject
    msg.set_content(text)
    host, port = os.environ["SMTP_HOST"], int(os.environ.get("SMTP_PORT") or 587)
    ctx = ssl.create_default_context()
    smtp = smtplib.SMTP_SSL(host, port, timeout=15, context=ctx) if port == 465 else smtplib.SMTP(host, port, timeout=15)
    with smtp:
        if port != 465:
            smtp.starttls(context=ctx)
        if os.environ.get("SMTP_USER"):
            smtp.login(os.environ["SMTP_USER"], os.environ.get("SMTP_PASS", ""))
        smtp.send_message(msg)

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

def _str(d, key, limit=320):
    v = d.get(key)
    return v.strip()[:limit] if isinstance(v, str) else ""

def create_account(email, password_hash):
    """Cria a conta e devolve o id, ou None se o e-mail já existe."""
    try:
        with db() as conn, conn.cursor() as cur:
            cur.execute("INSERT INTO accounts (email, password_hash) VALUES (%s,%s) RETURNING id", (email, password_hash))
            uid = cur.fetchone()[0]
            conn.commit()
            return uid
    except psycopg2.errors.UniqueViolation:
        return None

@app.route("/api/signup", methods=["POST"])
def api_signup():
    d = jbody()
    email, pw = _str(d, "email").lower(), d.get("password")
    wait = rl_take(("signup", client_ip()), 6, 3600)  # no máximo 6 cadastros por hora por endereço
    if wait:
        return too_many(wait)
    if len(email) > 254 or not email_ok(email):
        return jsonify(ok=False, err="E-mail inválido"), 400
    problema = password_problem(pw, email)
    if problema:
        return jsonify(ok=False, err=problema), 400
    with db() as conn, conn.cursor() as cur:
        cur.execute("SELECT 1 FROM accounts WHERE email=%s", (email,))
        existe = cur.fetchone()
    if not smtp_ready():  # sem servidor de e-mail configurado: cria direto (protegido pelo limite acima)
        if existe:
            return jsonify(ok=False, err="Esse e-mail já tem conta"), 400
        uid = create_account(email, generate_password_hash(pw))
        if uid is None:
            return jsonify(ok=False, err="Esse e-mail já tem conta"), 400
        start_session(uid)
        return jsonify(ok=True)
    # com e-mail configurado: a conta só nasce depois do código. A resposta é a mesma existindo ou não a conta,
    # para ninguém descobrir quais e-mails estão cadastrados.
    if not existe:
        code = f"{secrets.randbelow(1000000):06d}"
        with db() as conn, conn.cursor() as cur:
            cur.execute("""INSERT INTO signup_pending (email, password_hash, code_hash, attempts, expires) VALUES (%s,%s,%s,0,%s)
                           ON CONFLICT (email) DO UPDATE SET password_hash=EXCLUDED.password_hash,
                           code_hash=EXCLUDED.code_hash, attempts=0, expires=EXCLUDED.expires""",
                        (email, generate_password_hash(pw), _code_hash(code + email), time.time() + 600))
            conn.commit()
        try:
            send_mail(email, "Seu código do TeleBot Builder",
                      f"Seu código de confirmação é {code}\n\nEle vale por 10 minutos. Se não foi você, ignore este e-mail.")
        except Exception:
            app.logger.exception("falha ao enviar e-mail")
            return jsonify(ok=False, err="Não consegui enviar o e-mail de confirmação. Tente de novo em instantes."), 502
    return jsonify(ok=True, need_code=True)

@app.route("/api/signup/verify", methods=["POST"])
def api_signup_verify():
    d = jbody()
    email, code = _str(d, "email").lower(), re.sub(r"\D", "", _str(d, "code", 12))
    wait = rl_take(("verify", client_ip()), 20, 600)
    if wait:
        return too_many(wait)
    erro = (jsonify(ok=False, err="Código incorreto ou vencido. Peça um novo código."), 400)
    with db() as conn, conn.cursor() as cur:
        cur.execute("SELECT password_hash, code_hash, attempts, expires FROM signup_pending WHERE email=%s", (email,))
        row = cur.fetchone()
        if not row or row[3] < time.time() or row[2] >= 5:
            return erro
        if not hmac.compare_digest(row[1], _code_hash(code + email)):
            cur.execute("UPDATE signup_pending SET attempts=attempts+1 WHERE email=%s", (email,))  # 5 erros queimam o código
            conn.commit()
            return erro
        cur.execute("DELETE FROM signup_pending WHERE email=%s", (email,))
        conn.commit()
    uid = create_account(email, row[0])
    if uid is None:
        return erro
    start_session(uid)
    return jsonify(ok=True)

@app.route("/api/login", methods=["POST"])
def api_login():
    d = jbody()
    email, pw, ip = _str(d, "email").lower(), d.get("password"), client_ip()
    # força bruta: 8 erros por conta ou 40 por endereço em 15 minutos bloqueiam novas tentativas
    wait = rl_blocked(("fail", email), 8, 900) or rl_blocked(("failip", ip), 40, 900)
    if wait:
        return too_many(wait)
    with db() as conn, conn.cursor() as cur:
        cur.execute("SELECT id, password_hash FROM accounts WHERE email=%s", (email,))
        row = cur.fetchone()
    ok = check_password_hash(row[1] if row else DUMMY_HASH, pw if isinstance(pw, str) else "") and bool(row)
    if not ok:
        rl_hit(("fail", email)); rl_hit(("failip", ip))
        return jsonify(ok=False, err="E-mail ou senha incorretos"), 400
    if sec_get(row[0])[0]:  # conta com verificação em duas etapas: a senha sozinha não entra
        session.clear()
        session["p2"], session["p2_at"] = row[0], time.time()
        return jsonify(ok=True, need_2fa=True)
    start_session(row[0])
    return jsonify(ok=True)

@app.route("/api/login/2fa", methods=["POST"])
def api_login_2fa():
    d = jbody()
    uid = session.get("p2")
    if not uid or time.time() - session.get("p2_at", 0) > 300:
        session.clear()
        return jsonify(ok=False, err="O tempo acabou. Entre com e-mail e senha de novo."), 400
    wait = rl_blocked(("2fa", uid), 6, 900)
    if wait:
        return too_many(wait)
    if not second_factor_ok(uid, _str(d, "code", 20)):
        rl_hit(("2fa", uid))
        return jsonify(ok=False, err="Código incorreto"), 400
    start_session(uid)
    return jsonify(ok=True)

@app.route("/api/logout", methods=["POST"])
def api_logout():
    session.clear()
    return jsonify(ok=True)

# ---- conta: senha e verificação em duas etapas ----
def _account_row(uid):
    with db() as conn, conn.cursor() as cur:
        cur.execute("SELECT email, password_hash FROM accounts WHERE id=%s", (uid,))
        return cur.fetchone()

@app.route("/api/account")
@login_required
def api_account():
    row = _account_row(session["uid"])
    if not row:
        session.clear()
        return jsonify(ok=False, err="Sessão expirada, faça login de novo"), 401
    secret, codes = sec_get(session["uid"])
    return jsonify(ok=True, email=row[0], twofa=bool(secret), backup_left=len(codes),
                   encrypted=bool(_fernet), email_check=smtp_ready())

@app.route("/api/account/password", methods=["POST"])
@login_required
def api_account_password():
    d, uid = jbody(), session["uid"]
    wait = rl_take(("sens", uid), 10, 600)
    if wait:
        return too_many(wait)
    row = _account_row(uid)
    atual, nova = d.get("current"), d.get("new")
    if not row or not check_password_hash(row[1], atual if isinstance(atual, str) else ""):
        return jsonify(ok=False, err="Senha atual incorreta"), 400
    problema = password_problem(nova, row[0])
    if problema:
        return jsonify(ok=False, err=problema), 400
    with db() as conn, conn.cursor() as cur:
        cur.execute("UPDATE accounts SET password_hash=%s WHERE id=%s", (generate_password_hash(nova), uid))
        conn.commit()
    start_session(uid)
    return jsonify(ok=True)

@app.route("/api/2fa/setup", methods=["POST"])
@login_required
def api_2fa_setup():
    uid = session["uid"]
    row = _account_row(uid)
    if sec_get(uid)[0]:
        return jsonify(ok=False, err="A verificação em duas etapas já está ligada"), 400
    secret = base64.b32encode(secrets.token_bytes(20)).decode()
    session["totp_setup"] = secret
    uri = f"otpauth://totp/{quote('TeleBot Builder')}:{quote(row[0])}?secret={secret}&issuer={quote('TeleBot Builder')}"
    return jsonify(ok=True, secret=secret, qr="data:image/png;base64," + base64.b64encode(qr_png(uri)).decode())

@app.route("/api/2fa/enable", methods=["POST"])
@login_required
def api_2fa_enable():
    d, uid = jbody(), session["uid"]
    wait = rl_take(("sens", uid), 10, 600)
    if wait:
        return too_many(wait)
    secret = session.get("totp_setup")
    if not secret or not totp_ok(secret, _str(d, "code", 12)):
        return jsonify(ok=False, err="Código incorreto. Confira o relógio do celular e tente de novo."), 400
    reservas = [secrets.token_hex(4) for _ in range(8)]
    sec_set(uid, secret, [_code_hash(c) for c in reservas])
    session.pop("totp_setup", None)
    return jsonify(ok=True, backup_codes=[c[:4] + "-" + c[4:] for c in reservas])

@app.route("/api/2fa/disable", methods=["POST"])
@login_required
def api_2fa_disable():
    d, uid = jbody(), session["uid"]
    wait = rl_take(("sens", uid), 10, 600)
    if wait:
        return too_many(wait)
    row = _account_row(uid)
    senha = d.get("password")
    if not row or not check_password_hash(row[1], senha if isinstance(senha, str) else "") or not second_factor_ok(uid, _str(d, "code", 20)):
        return jsonify(ok=False, err="Senha ou código incorreto"), 400
    sec_set(uid, None, [])
    return jsonify(ok=True)

# ---------------- API ----------------
@app.route("/")
@login_required
def index():
    return render_template("index.html")

@app.route("/images/<path:filename>")
def api_image(filename):
    if not IMG_NAME.fullmatch(filename):  # só nomes gerados pelo próprio painel (nada de ../ ou caminhos)
        return jsonify(ok=False, err="Imagem não encontrada"), 404
    img = get_image(filename)
    if not img or img[0] not in IMG_MIME.values():
        return jsonify(ok=False, err="Imagem não encontrada"), 404
    ctype, data = img
    resp = Response(data, mimetype=ctype)
    resp.headers["Cache-Control"] = "public, max-age=86400"
    resp.headers["Content-Disposition"] = "inline"
    return resp

IMG_NAME = re.compile(r"[0-9a-f]{32}\.(jpg|jpeg|png|webp|gif)")
IMG_MAX = 5 * 1024 * 1024

def image_kind(data):
    """Extensão real do arquivo, pelos primeiros bytes (a extensão do nome pode mentir)."""
    if data[:3] == b"\xff\xd8\xff":
        return ".jpg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return ".png"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return ".gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    return ""

@app.route("/api/upload_image", methods=["POST"])
@login_required
def api_upload_image():
    wait = rl_take(("upload", session["uid"]), 40, 600)  # 40 imagens a cada 10 minutos por conta
    if wait:
        return too_many(wait)
    f = request.files.get("image")
    if not f or not f.filename:
        return jsonify(ok=False, err="Nenhuma imagem enviada"), 400
    data = f.read(IMG_MAX + 1)
    if len(data) > IMG_MAX:
        return jsonify(ok=False, err="Imagem grande demais (máximo de 5 MB)"), 413
    ext = image_kind(data)
    if not ext:
        return jsonify(ok=False, err="Esse arquivo não é uma imagem válida (use jpg, png, webp ou gif)"), 400
    fname = uuid.uuid4().hex + ext
    try:
        save_image(fname, data)
    except Exception:
        app.logger.exception("falha ao salvar imagem")
        return jsonify(ok=False, err="Não consegui salvar a imagem. Tente de novo."), 500
    return jsonify(ok=True, filename=fname)

# ---- limpeza do que o navegador envia (tipos, tamanhos e nomes de arquivo) ----
def _s(v, n):
    if isinstance(v, str):
        return v[:n]
    return str(v)[:n] if isinstance(v, (int, float)) and not isinstance(v, bool) else ""

def _img(v):
    return v if isinstance(v, str) and IMG_NAME.fullmatch(v) else ""

def _rows(v, spec, max_items):
    out = []
    for x in (v if isinstance(v, list) else [])[:max_items]:
        if isinstance(x, dict):
            out.append({k: (_img(x.get(k)) if n == "img" else _s(x.get(k), n)) for k, n in spec.items()})
    return out

PAY_FIELDS = {"type": 10, "pix_key": 140, "pix_name": 60, "pix_city": 40, "instructions": 1000, "gateway": 20,
              "api_token": 400, "api_env": 10, "admin_chat_id": 20, "default_email": 254, "default_cpf": 20,
              "pagbank_token": 400, "pagbank_env": 10}

def sanitize_bot(b):
    b["name"] = _s(b.get("name"), 60).strip() or "Meu bot"
    b["enabled"], b["admin_only"] = bool(b.get("enabled")), bool(b.get("admin_only"))
    b["auto_clean"] = b.get("auto_clean") is not False
    b["commands"] = _rows(b.get("commands"), {"cmd": 32, "text": 4000, "action": 20, "image": "img", "url": 1000}, 60)
    b["products"] = _rows(b.get("products"), {"name": 64, "price": 30, "description": 900, "image": "img", "category": 60}, 300)
    b["auto_replies"] = _rows(b.get("auto_replies"), {"match": 200, "mode": 10, "reply": 4000}, 100)
    bw = b.get("banned_words")
    b["banned_words"] = [w.strip().lower()[:60] for w in (bw if isinstance(bw, list) else []) if isinstance(w, str) and w.strip()][:300]
    wl = b.get("whitelist")
    b["whitelist"] = [int(x) for x in (wl if isinstance(wl, list) else []) if str(x).lstrip("-").isdigit() and len(str(x)) < 20][:200]
    rk = b.get("reply_keyboard")
    b["reply_keyboard"] = [[c[:40] for c in row if isinstance(c, str)][:4] for row in (rk if isinstance(rk, list) else []) if isinstance(row, list)][:8]
    ik = b.get("inline_keyboards")
    b["inline_keyboards"] = [k for k in (ik if isinstance(ik, list) else []) if isinstance(k, dict) and isinstance(k.get("buttons"), list)
                             and all(isinstance(r, list) and all(isinstance(x, dict) and isinstance(x.get("text"), str)
                                     and isinstance(x.get("callback"), str) for x in r) for r in k["buttons"])][:20]
    p = b.get("payment") if isinstance(b.get("payment"), dict) else {}
    pay = {k: _s(p.get(k), n) for k, n in PAY_FIELDS.items() if k in p}
    pay["type"] = pay.get("type") if pay.get("type") in ("api", "pix", "pixqr") else ""
    pay["qr_image"] = _img(p.get("qr_image"))
    pay["ask_customer"] = p.get("ask_customer") is not False
    b["payment"] = pay
    return b

@app.route("/api/bots")
@login_required
def api_bots():
    return jsonify([public(b) for b in load_bots(session["uid"])])

@app.route("/api/bots/new", methods=["POST"])
@login_required
def api_new():
    uid = session["uid"]
    d = jbody()
    token = _str(d, "token", 120)
    name = _str(d, "name", 60) or "Meu bot"
    if not re.fullmatch(r"\d+:[\w-]{20,}", token):
        return jsonify(ok=False, err="Formato de token inválido (esperado 123456:ABC...)"), 400
    wait = rl_take(("newbot", uid), 10, 3600)
    if wait:
        return too_many(wait)
    bots = load_bots(uid)
    if len(bots) >= 30:
        return jsonify(ok=False, err="Limite de 30 bots por conta"), 400
    if any(b["token"] == token for b in bots):
        return jsonify(ok=False, err="Token já cadastrado"), 400
    ok, info = check_token(token)
    if not ok:
        return jsonify(ok=False, err=info), 400
    bid = "bot_" + secrets.token_hex(8)  # identificador longo e aleatório: não dá para adivinhar o de outra conta
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
    body = jbody()
    if "payment" in body and not isinstance(body["payment"], dict):
        body = {**body, "payment": {}}
    with LOCK:
        bots = load_bots(uid)
        b = next((x for x in bots if x["id"] == bid), None)
        if not b:
            return jsonify(ok=False, err="Bot não encontrado"), 404
        warn = None
        if "payment" in body:
            # O navegador nunca recebe o token: campo vazio = manter o token salvo (se o intermediador não mudou).
            newp = {k: v for k, v in (body.get("payment") or {}).items()
                    if k not in ("api_token_hint", "api_token_gateway", "pagbank_token_hint", "pagbank_token", "pagbank_env")}
            oldp = b.get("payment") or {}
            gw = gw_name(newp)
            old_gw = gw_name(oldp)
            typed = (newp.get("api_token") or "").strip()
            tok = typed or (gw_token(oldp) if gw == old_gw else "")
            env = "producao" if newp.get("api_env") == "producao" else "sandbox"
            if gw == "mercadopago" and tok:
                env = mp_env_from_token(tok)
            newp.update(gateway=gw, api_token=tok, api_env=env)
            changed = tok != gw_token(oldp) or env != gw_env(oldp) or gw != old_gw
            label = GATEWAYS[gw]["label"]
            if newp.get("type") == "api" and tok and changed:
                res, msg = gateway_diagnose(gw, tok, env)
                if res is False:
                    # Salva todo o resto; só o intermediador/token/ambiente novos ficam de fora.
                    newp.update(gateway=old_gw, api_token=gw_token(oldp), api_env=gw_env(oldp))
                    warn = "Tudo foi salvo, menos o token. " + msg
                elif res is None:
                    warn = f"Salvo, mas não deu para conferir o token no {label} agora."
            elif newp.get("type") == "api" and not tok:
                warn = f"Salvo. Falta colar o token do {label} para o Pix automático funcionar."
            if newp.get("type") == "api" and newp.get("ask_customer", True) is False and not warn:
                pode, motivo = skip_customer_ready(newp)
                if not pode:
                    warn = f"Salvo. {motivo}; até lá o bot continua pedindo os dados do cliente."
            if newp.get("type") == "pixqr":
                if not (newp.get("pix_key") or "").strip():
                    warn = "Salvo. Falta a sua chave Pix para o bot montar o QR Code."
                elif not str(newp.get("admin_chat_id") or "").strip().lstrip("-").isdigit():
                    warn = "Salvo. Coloque o seu ID do Telegram, senão você não recebe os pedidos para confirmar."
            body = {**body, "payment": newp}
        for k in EDITABLE:
            if k in body:
                b[k] = body[k]
        sanitize_bot(b)  # tipos e tamanhos conferidos: nada do que vem do navegador é gravado sem passar por aqui
        # botões do menu: só texto não vazio, função conhecida, no máximo 24
        b["buttons"] = [{"text": str(x.get("text") or "").strip()[:40],
                         "action": x.get("action") if x.get("action") in BUTTON_ACTIONS else "text",
                         "value": str(x.get("value") or "").strip()[:1000]}
                        for x in (b["buttons"] if isinstance(b.get("buttons"), list) else [])
                        if isinstance(x, dict) and str(x.get("text") or "").strip()][:24]
        try:
            b["buttons_per_row"] = min(3, max(1, int(b.get("buttons_per_row") or 2)))
        except (TypeError, ValueError):
            b["buttons_per_row"] = 2
        save_bots(uid, bots)
    start_worker(uid, bid)  # reinicia se estiver ativo; se pausado, só para
    return jsonify(ok=True, warn=warn)

@app.route("/api/bots/<bid>/payment_test", methods=["POST"])
@login_required
def api_payment_test(bid):
    """Botão 'Testar token' do painel: confere o token digitado (ou o salvo) sem salvar nada."""
    d = jbody()
    b = next((x for x in load_bots(session["uid"]) if x["id"] == bid), None)
    if not b:
        return jsonify(ok=False, err="Bot não encontrado"), 404
    oldp = b.get("payment") or {}
    gw = d.get("gateway") if d.get("gateway") in GATEWAYS else gw_name(oldp)
    env = "producao" if (d.get("env") or gw_env(oldp)) == "producao" else "sandbox"
    tok = (d.get("token") or "").strip() or (gw_token(oldp) if gw == gw_name(oldp) else "")
    if not tok:
        return jsonify(ok=False, err=f"Cole o token do {GATEWAYS[gw]['label']} primeiro.")
    res, msg = gateway_diagnose(gw, tok, env)
    return jsonify(ok=bool(res), msg=msg, err=None if res else msg)

@app.route("/api/bots/<bid>/payment_trial", methods=["POST"])
@login_required
def api_payment_trial(bid):
    """Aba 'Testar pagamento': gera um Pix de R$ 1,00 com a configuração SALVA e consulta se foi pago.
    Não cria pedido no bot (nada é enviado para clientes)."""
    d = jbody()
    b = next((x for x in load_bots(session["uid"]) if x["id"] == bid), None)
    if not b:
        return jsonify(ok=False, err="Bot não encontrado"), 404
    pay = b.get("payment") or {}
    ptype = pay.get("type") or ""
    if d.get("action") == "status":
        key = d.get("key") or ""
        if ptype != "api" or key.startswith("manual:"):
            return jsonify(ok=False, err="No Pix com valor exato a confirmação é pelo app do seu banco.")
        try:
            return jsonify(ok=True, paid=asyncio.run(gateway_is_paid(pay, key)))
        except Exception as e:
            return jsonify(ok=False, err=f"Não consegui consultar: {str(e)[:300]}")
    lines = [("Teste do painel", 1, 100)]
    ref = f"teste-{bid}-{uuid.uuid4().hex[:8]}"
    if ptype == "pixqr" or (ptype == "api" and d.get("fallback")):
        if not (pay.get("pix_key") or "").strip():
            return jsonify(ok=False, err="Salve a sua chave Pix primeiro.")
        txid = "TESTE" + uuid.uuid4().hex[:8].upper()
        code = pix_brcode(pay["pix_key"], pay.get("pix_name") or "", pay.get("pix_city") or "", 100, txid)
        return jsonify(ok=True, key=f"manual:{txid}", copia=code, manual=True,
                       key_used=pix_key_normalize(pay["pix_key"]),
                       qr="data:image/png;base64," + base64.b64encode(qr_png(code)).decode())
    if ptype != "api":
        return jsonify(ok=False, err='Escolha e salve o Tipo "Atendimento automático" ou "Pix com valor exato" na aba Pagamento.')
    if not gw_token(pay):
        return jsonify(ok=False, err="Salve o token do intermediador na aba Pagamento primeiro.")
    nome, cpf, email = (d.get("nome") or "").strip(), re.sub(r"\D", "", d.get("cpf") or ""), (d.get("email") or "").strip()
    if len(nome.split()) < 2 or not cpf_ok(cpf) or not email_ok(email):
        return jsonify(ok=False, err="Preencha nome completo, CPF válido e e-mail do pagador de teste.")
    try:
        key, copia, qr = asyncio.run(gateway_create_pix(pay, ref, {"nome": nome, "cpf": cpf, "email": email}, lines))
    except Exception as e:
        return jsonify(ok=False, err=f"O {GATEWAYS[gw_name(pay)]['label']} recusou: {str(e)[:400]}")
    if isinstance(qr, str) and qr:
        try:
            r = httpx.get(qr, timeout=20, follow_redirects=True)
            qr = r.content if r.status_code == 200 else None
        except Exception:
            qr = None
    return jsonify(ok=True, key=key, copia=copia, manual=False,
                   qr=("data:image/png;base64," + base64.b64encode(qr).decode()) if qr else "")

@app.route("/api/bots/<bid>", methods=["DELETE"])
@login_required
def api_delete(bid):
    uid = session["uid"]
    bots = load_bots(uid)
    if not any(b["id"] == bid for b in bots):  # só o dono mexe: antes disso nada é parado nem apagado
        return jsonify(ok=False, err="Bot não encontrado"), 404
    stop_worker(bid)
    save_bots(uid, [b for b in bots if b["id"] != bid])
    (LOG_DIR / (re.sub(r"[^\w-]", "", bid) + ".log")).unlink(missing_ok=True)
    return jsonify(ok=True)

@app.route("/api/bots/<bid>/log")
@login_required
def api_log(bid):
    uid = session["uid"]
    if not any(b["id"] == bid for b in load_bots(uid)):
        return jsonify(ok=False, err="Bot não encontrado"), 404
    safe = re.sub(r"[^\w-]", "", bid)
    p = LOG_DIR / (safe + ".log")
    return jsonify(p.read_text(encoding="utf-8").splitlines()[-500:] if p.exists() else [])

@app.route("/api/bots/<bid>/log", methods=["DELETE"])
@login_required
def api_log_clear(bid):
    uid = session["uid"]
    if not any(b["id"] == bid for b in load_bots(uid)):
        return jsonify(ok=False, err="Bot não encontrado"), 404
    safe = re.sub(r"[^\w-]", "", bid)
    with LOCK:
        (LOG_DIR / (safe + ".log")).unlink(missing_ok=True)
    return jsonify(ok=True)

# ---------------- Inicialização (roda tanto no "python app.py" quanto sob gunicorn) ----------------
init_db()
purge_old_data()
if IS_PROD and not os.environ.get("SECRET_KEY", "").strip():
    app.logger.warning("SECRET_KEY não definida: as sessões caem a cada reinício. Defina SECRET_KEY no ambiente.")
if IS_PROD and not _fernet:
    app.logger.warning("DATA_KEY não definida: tokens e dados de pedidos ficam sem criptografia no banco.")
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
