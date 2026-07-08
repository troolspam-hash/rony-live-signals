import os
import sqlite3
import secrets
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path
from zoneinfo import ZoneInfo

from flask import (
    Flask, abort, flash, jsonify, redirect, render_template,
    request, session, url_for,
)
from werkzeug.security import check_password_hash, generate_password_hash


BASE_DIR = Path(__file__).resolve().parent
DB_PATH = Path(os.getenv("DATABASE_PATH", BASE_DIR / "data" / "live_signals.db"))
INITIAL_CAPITAL = float(os.getenv("SITE_INITIAL_CAPITAL", "1000"))
DISPLAY_TZ = ZoneInfo(os.getenv("DISPLAY_TIMEZONE", "America/Sao_Paulo"))

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY") or secrets.token_hex(32)
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.getenv("SESSION_COOKIE_SECURE", "1") == "1",
)


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


@app.template_filter("br_time")
def br_time(value):
    if not value:
        return "-"
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        dt = dt.astimezone(DISPLAY_TZ)
        return dt.strftime("%d/%m %H:%M")
    except ValueError:
        return str(value)


@app.template_filter("price_fmt")
def price_fmt(value):
    try:
        price = float(value)
    except (TypeError, ValueError):
        return "-"
    if abs(price) >= 1000:
        return f"{price:,.2f}"
    if abs(price) >= 1:
        return f"{price:.2f}"
    return f"{price:.6f}"


def db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                role TEXT NOT NULL DEFAULT 'user',
                active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS signals (
                id TEXT PRIMARY KEY,
                asset TEXT NOT NULL,
                tf TEXT NOT NULL,
                direction TEXT NOT NULL,
                entry_price REAL NOT NULL,
                tp_price REAL NOT NULL,
                sl_price REAL NOT NULL,
                tp_pct REAL NOT NULL,
                sl_pct REAL NOT NULL,
                created_at TEXT NOT NULL,
                entry_time TEXT,
                received_at TEXT NOT NULL
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_signals_received ON signals(received_at DESC)")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS open_signals (
                id TEXT PRIMARY KEY,
                asset TEXT NOT NULL,
                tf TEXT NOT NULL,
                direction TEXT NOT NULL,
                entry_price REAL NOT NULL,
                tp_price REAL NOT NULL,
                sl_price REAL NOT NULL,
                tp_pct REAL NOT NULL,
                sl_pct REAL NOT NULL,
                created_at TEXT NOT NULL,
                entry_time TEXT,
                received_at TEXT NOT NULL
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_open_signals_received ON open_signals(received_at DESC)")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS closed_trades (
                id TEXT PRIMARY KEY,
                setup_id TEXT,
                asset TEXT NOT NULL,
                tf TEXT NOT NULL,
                direction TEXT NOT NULL,
                entry_price REAL NOT NULL,
                exit_price REAL NOT NULL,
                entry_time TEXT NOT NULL,
                exit_time TEXT NOT NULL,
                size_usd REAL DEFAULT 0,
                pnl_usd REAL DEFAULT 0,
                pnl_pct REAL DEFAULT 0,
                fees_usd REAL DEFAULT 0,
                result TEXT,
                received_at TEXT NOT NULL
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_closed_trades_exit ON closed_trades(exit_time DESC)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_closed_trades_asset ON closed_trades(asset, tf, direction)")

    admin_user = os.getenv("ADMIN_USERNAME", "admin")
    admin_password = os.getenv("ADMIN_PASSWORD")
    with db() as conn:
        exists = conn.execute("SELECT id FROM users WHERE role='admin' LIMIT 1").fetchone()
        if not exists and admin_password:
            conn.execute(
                "INSERT INTO users (username, password_hash, role, active, created_at) VALUES (?, ?, 'admin', 1, ?)",
                (admin_user, generate_password_hash(admin_password), utc_now()),
            )


@app.before_request
def ensure_db():
    init_db()


def current_user():
    uid = session.get("user_id")
    if not uid:
        return None
    with db() as conn:
        return conn.execute(
            "SELECT id, username, role, active FROM users WHERE id=? AND active=1",
            (uid,),
        ).fetchone()


def login_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not current_user():
            return redirect(url_for("login", next=request.path))
        return fn(*args, **kwargs)
    return wrapper


def admin_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        user = current_user()
        if not user:
            return redirect(url_for("login", next=request.path))
        if user["role"] != "admin":
            abort(403)
        return fn(*args, **kwargs)
    return wrapper


def compute_trade_stats():
    with db() as conn:
        rows = conn.execute("""
            SELECT result, direction, pnl_usd, fees_usd, exit_time
            FROM closed_trades
            ORDER BY exit_time ASC, id ASC
        """).fetchall()

    total = len(rows)
    wins = sum(1 for r in rows if str(r["result"]).lower() == "tp" or float(r["pnl_usd"] or 0) > 0)
    losses = sum(1 for r in rows if str(r["result"]).lower() == "sl" or float(r["pnl_usd"] or 0) < 0)
    longs = sum(1 for r in rows if str(r["direction"]).lower() == "long")
    shorts = sum(1 for r in rows if str(r["direction"]).lower() == "short")
    gross_profit = sum(max(float(r["pnl_usd"] or 0), 0.0) for r in rows)
    gross_loss = sum(abs(min(float(r["pnl_usd"] or 0), 0.0)) for r in rows)
    fees = sum(float(r["fees_usd"] or 0) for r in rows)
    pnl = sum(float(r["pnl_usd"] or 0) for r in rows)

    equity = INITIAL_CAPITAL
    peak = INITIAL_CAPITAL
    max_dd = 0.0
    for row in rows:
        equity += float(row["pnl_usd"] or 0)
        peak = max(peak, equity)
        if peak > 0:
            max_dd = min(max_dd, ((equity - peak) / peak) * 100)

    return {
        "total": total,
        "wins": wins,
        "losses": losses,
        "longs": longs,
        "shorts": shorts,
        "wr": (wins / total * 100) if total else None,
        "pf": (gross_profit / gross_loss) if gross_loss else None,
        "dd": max_dd,
        "pnl": pnl,
        "fees": fees,
    }


def store_signal(conn, data: dict, received_at: str, table: str = "signals", replace: bool = False):
    if table not in ("signals", "open_signals"):
        raise ValueError("invalid signal table")
    mode = "INSERT OR REPLACE" if replace else "INSERT OR IGNORE"
    conn.execute(f"""
        {mode} INTO {table} (
          id, asset, tf, direction, entry_price, tp_price, sl_price,
          tp_pct, sl_pct, created_at, entry_time, received_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        str(data["id"]),
        str(data["asset"]).upper(),
        str(data["tf"]),
        str(data["direction"]).lower(),
        float(data["entry_price"]),
        float(data["tp_price"]),
        float(data["sl_price"]),
        float(data["tp_pct"]),
        float(data["sl_pct"]),
        str(data["created_at"]),
        str(data.get("entry_time")) if data.get("entry_time") else None,
        received_at,
    ))


@app.context_processor
def inject_user():
    return {"me": current_user()}


@app.get("/")
def home():
    if current_user():
        return redirect(url_for("dashboard"))
    return redirect(url_for("login"))


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        with db() as conn:
            user = conn.execute(
                "SELECT * FROM users WHERE username=? AND active=1",
                (username,),
            ).fetchone()
        if user and check_password_hash(user["password_hash"], password):
            session.clear()
            session["user_id"] = user["id"]
            return redirect(request.args.get("next") or url_for("dashboard"))
        flash("Usuario ou senha invalidos", "error")
    return render_template("login.html")


@app.post("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.get("/dashboard")
@login_required
def dashboard():
    asset = request.args.get("asset", "all")
    direction = request.args.get("direction", "all")
    params = []
    where = []
    if asset != "all":
        where.append("asset=?")
        params.append(asset)
    if direction != "all":
        where.append("direction=?")
        params.append(direction)
    sql_where = "WHERE " + " AND ".join(where) if where else ""
    with db() as conn:
        open_signals = conn.execute(
            f"SELECT * FROM open_signals {sql_where} ORDER BY received_at DESC LIMIT 300",
            params,
        ).fetchall()
        history_signals = conn.execute(
            f"SELECT * FROM signals {sql_where} ORDER BY received_at DESC LIMIT 300",
            params,
        ).fetchall()
        stats = conn.execute("""
            SELECT
              COUNT(*) AS total,
              SUM(CASE WHEN direction='long' THEN 1 ELSE 0 END) AS longs,
              SUM(CASE WHEN direction='short' THEN 1 ELSE 0 END) AS shorts
            FROM open_signals
        """).fetchone()
        assets = conn.execute("""
            SELECT asset FROM (
                SELECT DISTINCT asset FROM open_signals
                UNION
                SELECT DISTINCT asset FROM signals
            ) ORDER BY asset
        """).fetchall()
    trade_stats = compute_trade_stats()
    return render_template(
        "dashboard.html",
        open_signals=open_signals,
        history_signals=history_signals,
        stats=stats,
        trade_stats=trade_stats,
        assets=assets,
        asset=asset,
        direction=direction,
    )


@app.get("/history")
@login_required
def history():
    asset = request.args.get("asset", "all")
    direction = request.args.get("direction", "all")
    result = request.args.get("result", "all")
    params = []
    where = []
    if asset != "all":
        where.append("asset=?")
        params.append(asset)
    if direction != "all":
        where.append("direction=?")
        params.append(direction)
    if result != "all":
        where.append("result=?")
        params.append(result)
    sql_where = "WHERE " + " AND ".join(where) if where else ""
    with db() as conn:
        trades = conn.execute(
            f"""
            SELECT * FROM closed_trades {sql_where}
            ORDER BY
              CASE
                WHEN instr(id, '|') > 0 THEN CAST(substr(id, 1, instr(id, '|') - 1) AS INTEGER)
                ELSE 0
              END DESC,
              exit_time DESC
            LIMIT 500
            """,
            params,
        ).fetchall()
        assets = conn.execute("SELECT DISTINCT asset FROM closed_trades ORDER BY asset").fetchall()
    trade_stats = compute_trade_stats()
    return render_template(
        "history.html",
        trades=trades,
        trade_stats=trade_stats,
        assets=assets,
        asset=asset,
        direction=direction,
        result=result,
    )


@app.route("/admin/users", methods=["GET", "POST"])
@admin_required
def admin_users():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        role = request.form.get("role", "user")
        if not username or not password:
            flash("Informe usuario e senha", "error")
        elif role not in ("user", "admin"):
            flash("Role invalida", "error")
        else:
            try:
                with db() as conn:
                    conn.execute(
                        "INSERT INTO users (username, password_hash, role, active, created_at) VALUES (?, ?, ?, 1, ?)",
                        (username, generate_password_hash(password), role, utc_now()),
                    )
                flash("Usuario criado", "success")
            except sqlite3.IntegrityError:
                flash("Usuario ja existe", "error")
    with db() as conn:
        users = conn.execute("SELECT id, username, role, active, created_at FROM users ORDER BY id").fetchall()
    return render_template("admin_users.html", users=users)


@app.post("/admin/users/<int:user_id>/toggle")
@admin_required
def toggle_user(user_id):
    user = current_user()
    if user and user["id"] == user_id:
        flash("Voce nao pode desativar seu proprio usuario", "error")
        return redirect(url_for("admin_users"))
    with db() as conn:
        row = conn.execute("SELECT active FROM users WHERE id=?", (user_id,)).fetchone()
        if row:
            conn.execute("UPDATE users SET active=? WHERE id=?", (0 if row["active"] else 1, user_id))
    return redirect(url_for("admin_users"))


@app.post("/admin/users/<int:user_id>/delete")
@admin_required
def delete_user(user_id):
    user = current_user()
    if user and user["id"] == user_id:
        flash("Voce nao pode remover seu proprio usuario", "error")
        return redirect(url_for("admin_users"))
    with db() as conn:
        conn.execute("DELETE FROM users WHERE id=?", (user_id,))
    return redirect(url_for("admin_users"))


@app.post("/api/signals")
def ingest_signal():
    token = request.headers.get("X-Ingest-Token", "")
    expected = os.getenv("INGEST_TOKEN", "")
    if not expected or not secrets.compare_digest(token, expected):
        abort(401)

    data = request.get_json(force=True, silent=False)
    required = ["id", "asset", "tf", "direction", "entry_price", "tp_price", "sl_price", "tp_pct", "sl_pct", "created_at"]
    missing = [k for k in required if k not in data]
    if missing:
        return jsonify({"ok": False, "error": "missing fields", "fields": missing}), 400

    with db() as conn:
        store_signal(conn, data, utc_now())
    return jsonify({"ok": True})


@app.post("/api/open-signals")
def sync_open_signals():
    token = request.headers.get("X-Ingest-Token", "")
    expected = os.getenv("INGEST_TOKEN", "")
    if not expected or not secrets.compare_digest(token, expected):
        abort(401)

    data = request.get_json(force=True, silent=False)
    rows = data.get("signals") if isinstance(data, dict) else data
    if not isinstance(rows, list):
        return jsonify({"ok": False, "error": "expected list or {'signals': [...]}"}), 400

    required = ["id", "asset", "tf", "direction", "entry_price", "tp_price", "sl_price", "tp_pct", "sl_pct", "created_at"]
    now = utc_now()
    synced = 0
    with db() as conn:
        conn.execute("DELETE FROM open_signals")
        for signal in rows:
            missing = [k for k in required if k not in signal or signal.get(k) in (None, "")]
            if missing:
                continue
            store_signal(conn, signal, now, table="open_signals", replace=True)
            synced += 1
    return jsonify({"ok": True, "synced": synced})


@app.post("/api/trades")
def ingest_trades():
    token = request.headers.get("X-Ingest-Token", "")
    expected = os.getenv("INGEST_TOKEN", "")
    if not expected or not secrets.compare_digest(token, expected):
        abort(401)

    data = request.get_json(force=True, silent=False)
    rows = data.get("trades") if isinstance(data, dict) else data
    if not isinstance(rows, list):
        return jsonify({"ok": False, "error": "expected list or {'trades': [...]}"}), 400

    required = ["id", "asset", "tf", "direction", "entry_price", "exit_price", "entry_time", "exit_time"]
    now = utc_now()
    inserted = 0
    with db() as conn:
        for trade in rows:
            missing = [k for k in required if k not in trade or trade.get(k) in (None, "")]
            if missing:
                continue
            conn.execute("""
                INSERT OR REPLACE INTO closed_trades (
                  id, setup_id, asset, tf, direction, entry_price, exit_price,
                  entry_time, exit_time, size_usd, pnl_usd, pnl_pct, fees_usd,
                  result, received_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                str(trade["id"]),
                str(trade.get("setup_id")) if trade.get("setup_id") else None,
                str(trade["asset"]).upper(),
                str(trade["tf"]),
                str(trade["direction"]).lower(),
                float(trade["entry_price"]),
                float(trade["exit_price"]),
                str(trade["entry_time"]),
                str(trade["exit_time"]),
                float(trade.get("size_usd") or 0),
                float(trade.get("pnl_usd") or 0),
                float(trade.get("pnl_pct") or 0),
                float(trade.get("fees_usd") or 0),
                str(trade.get("result")).lower() if trade.get("result") else None,
                now,
            ))
            inserted += 1
    return jsonify({"ok": True, "received": inserted})


@app.get("/api/signals")
@login_required
def api_signals():
    with db() as conn:
        rows = conn.execute("SELECT * FROM signals ORDER BY received_at DESC LIMIT 100").fetchall()
    return jsonify([dict(r) for r in rows])


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")), debug=True)
