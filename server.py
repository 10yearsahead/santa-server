import os
import secrets
from datetime import datetime, timedelta
from flask import Flask, request, jsonify, render_template, redirect, url_for, session
from flask_sqlalchemy import SQLAlchemy
from functools import wraps
import json
import mercadopago
from github import Github, GithubException, Auth as GithubAuth

# ----------------- Config -----------------
app = Flask(__name__)

app.secret_key = os.environ.get("SECRET_KEY", secrets.token_hex(16))

db_uri = os.environ.get("DATABASE_URL", "sqlite:///licenses.db")
app.config["SQLALCHEMY_DATABASE_URI"] = db_uri.replace("postgres://", "postgresql://")
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
db = SQLAlchemy(app)

ADMIN_USER = os.environ.get("ADMIN_USER", "rochazrx")
ADMIN_PASS = os.environ.get("ADMIN_PASS", "Maria$5")
BOT_API_KEY = os.environ.get("BOT_API_KEY", "superseguro123")

# GitHub
GITHUB_TOKEN     = os.environ.get("GITHUB_TOKEN")
GITHUB_REPO      = os.environ.get("GITHUB_REPO")
GITHUB_FILE_PATH = os.environ.get("GITHUB_FILE_PATH")

# ----------------- GitHub helpers -----------------
def _get_gh():
    if not GITHUB_TOKEN:
        raise Exception("GITHUB_TOKEN não configurado")
    return Github(auth=GithubAuth.Token(GITHUB_TOKEN))

def _read_lines():
    gh   = _get_gh()
    repo = gh.get_repo(GITHUB_REPO)
    f    = repo.get_contents(GITHUB_FILE_PATH)
    lines = [l for l in f.decoded_content.decode("utf-8").splitlines() if l.strip()]
    return lines, f.sha

def _write_lines(lines, sha, msg):
    gh   = _get_gh()
    repo = gh.get_repo(GITHUB_REPO)
    repo.update_file(GITHUB_FILE_PATH, msg, "\n".join(lines) + "\n", sha)

def _find_user(lines, discord_id):
    for i, line in enumerate(lines):
        fields = line.split(":")
        if fields and fields[0].strip() == str(discord_id):
            return i, fields
    return None, None

# ----------------- Modelo -----------------
class License(db.Model):
    id      = db.Column(db.Integer, primary_key=True)
    key     = db.Column(db.String(128), unique=True, nullable=False)
    hwid    = db.Column(db.String(256), nullable=True)
    expires = db.Column(db.DateTime, nullable=True)
    created = db.Column(db.DateTime, default=datetime.utcnow)
    note    = db.Column(db.String(256), nullable=True)

# ----------------- Login -----------------
def login_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not session.get("logged_in"):
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return wrapper

# ----------------- Rotas -----------------
@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        user = request.form.get("username", "").strip()
        pw   = request.form.get("password", "").strip()
        if user == ADMIN_USER and pw == ADMIN_PASS:
            session["logged_in"] = True
            return redirect(url_for("admin"))
        return render_template("login.html", error="Usuário ou senha incorretos.")
    return render_template("login.html")

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))

@app.route("/")
def home():
    return redirect(url_for("admin"))

@app.route("/admin")
@login_required
def admin():
    licenses = License.query.order_by(License.created.desc()).all()
    return render_template("admin.html", licenses=licenses)

@app.route("/create_license", methods=["POST"])
@login_required
def create_license():
    key  = request.form.get("key", "").strip()
    if not key:
        import random, string
        partes = ["".join(random.choices(string.ascii_uppercase + string.digits, k=4)) for _ in range(4)]
        key = "SANTA-" + "-".join(partes)
    days    = int(request.form.get("days", 0))
    note    = request.form.get("note", "").strip()
    expires = datetime.utcnow() + timedelta(days=days) if days > 0 else None
    lic     = License(key=key, expires=expires, note=note)
    db.session.add(lic)
    db.session.commit()
    return redirect(url_for("admin"))

@app.route("/delete_license/<int:license_id>", methods=["POST"])
@login_required
def delete_license(license_id):
    lic = License.query.get_or_404(license_id)
    db.session.delete(lic)
    db.session.commit()
    return redirect(url_for("admin"))

# ----------------- API validate (launcher key) -----------------
@app.route("/validate", methods=["POST"])
def validate():
    data = request.get_json(silent=True)
    if not data:
        return jsonify({"ok": False, "error": "requisição inválida"}), 400
    key  = data.get("key",  "").strip()
    hwid = data.get("hwid", "").strip()
    if not key or not hwid:
        return jsonify({"ok": False, "error": "faltando key ou hwid"}), 400
    lic = License.query.filter_by(key=key).first()
    if not lic:
        return jsonify({"ok": False, "error": "licença inválida"}), 404
    if lic.hwid and lic.hwid != hwid:
        return jsonify({"ok": False, "error": "licença já usada em outro PC"}), 403
    if lic.expires and datetime.utcnow() > lic.expires:
        return jsonify({"ok": False, "error": "licença expirada"}), 403
    if not lic.hwid:
        lic.hwid = hwid
        db.session.commit()
    return jsonify({"ok": True, "message": "Licença válida"})

# ----------------- API bind (SantaLauncher — vincula HWID no GitHub) -----------------
@app.route("/api/bind", methods=["POST"])
def handle_bind():
    data = request.get_json(silent=True)
    if not data:
        return jsonify({"error": "Invalid JSON"}), 400

    discord_id = str(data.get("discord_id", "")).strip()
    sid        = str(data.get("sid", "")).strip()

    if not discord_id or not sid:
        return jsonify({"error": "discord_id and sid are required"}), 400

    try:
        lines, sha = _read_lines()
        idx, fields = _find_user(lines, discord_id)

        if idx is None:
            return jsonify({"error": "Usuário não encontrado no GitHub"}), 404

        # Atualiza HWID (campo 1) e LicenseStartedAt (campo 5) se ainda não vinculado
        if len(fields) < 2:
            return jsonify({"error": "Formato de linha inválido"}), 500

        fields[1] = sid

        # Se não tiver data de início (campo 5), define agora
        from datetime import timezone
        now_str = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")
        while len(fields) < 6:
            fields.append("null")
        if fields[5].strip() in ("", "null"):
            fields[5] = now_str

        lines[idx] = ":".join(fields)
        _write_lines(lines, sha, f"[bind] HWID vinculado para {discord_id}")

        return jsonify({"success": True, "message": "HWID vinculado com sucesso"})

    except GithubException as e:
        return jsonify({"error": f"Erro GitHub: {str(e)}"}), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ----------------- API create_key (bot Discord) -----------------
@app.route("/api/create_key", methods=["POST"])
def create_key_api():
    data    = request.get_json(silent=True)
    if not data:
        return jsonify({"error": "JSON inválido"}), 400
    api_key = data.get("api_key", "")
    dias    = int(data.get("dias", 0))
    anotacao = data.get("anotacao", "")
    if api_key != BOT_API_KEY:
        return jsonify({"error": "API key inválida"}), 403
    import random, string
    partes = ["".join(random.choices(string.ascii_uppercase + string.digits, k=4)) for _ in range(7)]
    key     = "SANTA-" + "-".join(partes)
    expires = datetime.utcnow() + timedelta(days=dias) if dias > 0 else None
    lic     = License(key=key, expires=expires, note=anotacao)
    db.session.add(lic)
    db.session.commit()
    return jsonify({"ok": True, "key": key, "expira": expires.isoformat() if expires else "permanente", "anotacao": anotacao})

# ----------------- MP Webhook -----------------
@app.route("/mp-webhook", methods=["POST"])
def mp_webhook():
    try:
        secret_config    = os.environ.get("MP_WEBHOOK_SECRET", "")
        provided_secret  = request.headers.get("X-Webhook-Secret") or request.args.get("secret")
        if secret_config and provided_secret != secret_config:
            return jsonify({"ok": False, "error": "forbidden"}), 403
        data = request.get_json(silent=True) or {}
        if data.get("type") != "payment":
            return jsonify({"ok": True, "ignored": True})
        payment_id = (data.get("data") or {}).get("id")
        if not payment_id:
            return jsonify({"ok": False, "error": "payment_id ausente"}), 400
        token = os.getenv("MP_ACCESS_TOKEN")
        if not token:
            return jsonify({"ok": False, "error": "token Mercado Pago ausente"}), 500
        sdk     = mercadopago.SDK(str(token).strip())
        payment = sdk.payment().get(payment_id)["response"]
        status  = payment.get("status")
        thread_id = payment.get("external_reference")
        if not thread_id:
            return jsonify({"ok": False, "error": "external_reference ausente"}), 400
        if status == "approved":
            cart_path = os.path.join(os.getcwd(), "data", "cart.json")
            try:
                with open(cart_path, "r") as f:
                    cart = json.load(f)
            except FileNotFoundError:
                cart = {}
            entry = cart.get(str(thread_id)) or {}
            entry["status"]     = "approved"
            entry["payment_id"] = payment_id
            cart[str(thread_id)] = entry
            with open(cart_path, "w") as f:
                json.dump(cart, f, ensure_ascii=False, indent=2)
            return jsonify({"ok": True, "updated": True})
        return jsonify({"ok": True, "status": status})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

# ----------------- Init DB -----------------
@app.route("/init-db")
def init_db_route():
    db.create_all()
    return "✅ Banco de dados inicializado!"

if __name__ == "__main__":
    with app.app_context():
        db.create_all()
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
