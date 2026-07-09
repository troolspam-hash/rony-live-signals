import os
import sqlite3
import secrets
from datetime import datetime, timezone, timedelta
from functools import wraps
from pathlib import Path
from zoneinfo import ZoneInfo

from flask import (
    Flask, abort, flash, jsonify, redirect, render_template,
    request, send_from_directory, session, url_for,
)
from werkzeug.security import check_password_hash, generate_password_hash


BASE_DIR = Path(__file__).resolve().parent
ALERT_DIR = BASE_DIR / "alerta"
DB_PATH = Path(os.getenv("DATABASE_PATH", BASE_DIR / "data" / "live_signals.db"))
INITIAL_CAPITAL = float(os.getenv("SITE_INITIAL_CAPITAL", "1000"))
DISPLAY_TZ = ZoneInfo(os.getenv("DISPLAY_TIMEZONE", "America/Sao_Paulo"))
COIN_LOGOS = {
    "BTC": "https://s2.coinmarketcap.com/static/img/coins/64x64/1.png",
    "ETH": "https://s2.coinmarketcap.com/static/img/coins/64x64/1027.png",
    "SOL": "https://s2.coinmarketcap.com/static/img/coins/64x64/5426.png",
    "BNB": "https://s2.coinmarketcap.com/static/img/coins/64x64/1839.png",
}

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


@app.template_filter("pct_fmt")
def pct_fmt(value):
    try:
        pct = float(value)
    except (TypeError, ValueError):
        return "--"
    if abs(pct) < 0.005:
        pct = 0.0
    return f"{pct:+.2f}%"


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
                status TEXT DEFAULT 'open',
                received_at TEXT NOT NULL
            )
        """)
        cols = {row[1] for row in conn.execute("PRAGMA table_info(open_signals)")}
        if "status" not in cols:
            conn.execute("ALTER TABLE open_signals ADD COLUMN status TEXT DEFAULT 'open'")
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
        conn.execute("""
            CREATE TABLE IF NOT EXISTS user_sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT UNIQUE NOT NULL,
                user_id INTEGER NOT NULL,
                login_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                logout_at TEXT,
                last_path TEXT,
                ip TEXT,
                user_agent TEXT,
                FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_user_sessions_user ON user_sessions(user_id, last_seen_at DESC)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_user_sessions_active ON user_sessions(logout_at, last_seen_at DESC)")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS user_ip_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                session_id TEXT,
                ip TEXT NOT NULL,
                event TEXT NOT NULL,
                path TEXT,
                user_agent TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_user_ip_logs_user ON user_ip_logs(user_id, created_at DESC)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_user_ip_logs_ip ON user_ip_logs(ip, created_at DESC)")

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


def parse_utc(value):
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except ValueError:
        return None


def client_ip():
    forwarded = request.headers.get("X-Forwarded-For", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.remote_addr or ""


def log_user_ip(conn, user_id, session_id, event, path=None):
    conn.execute(
        """
        INSERT INTO user_ip_logs (user_id, session_id, ip, event, path, user_agent, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            user_id,
            session_id,
            client_ip(),
            event,
            path or request.path,
            request.headers.get("User-Agent", "")[:300],
            utc_now(),
        ),
    )


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


@app.before_request
def track_logged_user():
    uid = session.get("user_id")
    sid = session.get("session_id")
    if not uid or not sid:
        return
    if request.endpoint in ("static", "alert_file"):
        return
    now = utc_now()
    try:
        with db() as conn:
            active = conn.execute(
                """
                SELECT id, ip FROM user_sessions
                WHERE session_id=? AND user_id=? AND logout_at IS NULL
                """,
                (sid, uid),
            ).fetchone()
            if not active:
                session.clear()
                if request.endpoint not in ("login", "logout"):
                    flash("Sua sessao foi encerrada por novo login em outro navegador.", "error")
                    return redirect(url_for("login"))
                return

            ip = client_ip()
            if ip and ip != (active["ip"] or ""):
                log_user_ip(conn, uid, sid, "ip_change", request.path)

            conn.execute(
                """
                UPDATE user_sessions
                SET last_seen_at=?, last_path=?, ip=?, user_agent=?
                WHERE session_id=? AND user_id=? AND logout_at IS NULL
                """,
                (
                    now,
                    request.path,
                    ip,
                    request.headers.get("User-Agent", "")[:300],
                    sid,
                    uid,
                ),
            )
    except sqlite3.Error:
        return


def compute_trade_stats():
    def empty_curve():
        return {
            "spark_path": "M0 18 L120 18",
            "spark_area": "M0 36 L0 18 L120 18 L120 36 Z",
            "spark_min": 0.0,
            "spark_max": 0.0,
        }

    def build_curve(values):
        if not values:
            return empty_curve()
        series = [0.0] + [float(v) for v in values]
        if len(series) == 1:
            series.append(series[0])
        width = 120.0
        height = 36.0
        pad = 4.0
        low = min(series)
        high = max(series)
        span = high - low
        if span <= 0:
            span = 1.0
        points = []
        for idx, value in enumerate(series):
            x = (idx / max(len(series) - 1, 1)) * width
            y = pad + (high - value) / span * (height - pad * 2)
            points.append((x, y))
        path = " ".join(("M" if idx == 0 else "L") + f"{x:.2f} {y:.2f}" for idx, (x, y) in enumerate(points))
        area = f"M0 {height:.2f} " + " ".join(("L" if idx == 0 else "L") + f"{x:.2f} {y:.2f}" for idx, (x, y) in enumerate(points)) + f" L{width:.2f} {height:.2f} Z"
        return {
            "spark_path": path,
            "spark_area": area,
            "spark_min": low,
            "spark_max": high,
        }

    with db() as conn:
        rows = conn.execute("""
            SELECT asset, result, direction, pnl_usd, fees_usd, pnl_pct, exit_time
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
    pnl_pct_total = sum(float(r["pnl_pct"] or 0) for r in rows)
    by_direction = {
        "long": {"pnl_pct": 0.0, "count": 0, "_curve": []},
        "short": {"pnl_pct": 0.0, "count": 0, "_curve": []},
    }
    by_asset_map = {}
    for row in rows:
        direction = str(row["direction"]).lower()
        asset = str(row["asset"]).upper().replace("USDT", "")
        pnl_pct = float(row["pnl_pct"] or 0)
        if direction in by_direction:
            by_direction[direction]["pnl_pct"] += pnl_pct
            by_direction[direction]["count"] += 1
            by_direction[direction]["_curve"].append(by_direction[direction]["pnl_pct"])
        item = by_asset_map.setdefault(asset, {"asset": asset, "pnl_pct": 0.0, "count": 0, "_curve": []})
        item["pnl_pct"] += pnl_pct
        item["count"] += 1
        item["_curve"].append(item["pnl_pct"])

    for item in by_direction.values():
        item.update(build_curve(item.pop("_curve", [])))
    for item in by_asset_map.values():
        item.update(build_curve(item.pop("_curve", [])))

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
        "pnl_pct": pnl_pct_total,
        "fees": fees,
        "by_direction": by_direction,
        "by_asset": [by_asset_map[k] for k in sorted(by_asset_map)],
    }


def load_dashboard_data(asset="all", direction="all"):
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
            f"""
            SELECT * FROM open_signals {sql_where}
            ORDER BY COALESCE(entry_time, created_at, received_at) DESC, received_at DESC
            LIMIT 300
            """,
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
    return {
        "open_signals": open_signals,
        "history_signals": history_signals,
        "stats": stats,
        "trade_stats": compute_trade_stats(),
        "assets": assets,
        "asset": asset,
        "direction": direction,
        "coin_logos": COIN_LOGOS,
    }


def load_history_data(asset="all", direction="all", result="all", page=1, per_page=14):
    if per_page not in (14, 25, 50, 100):
        per_page = 14
    page = max(1, int(page or 1))
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
        total = conn.execute(f"SELECT COUNT(*) AS n FROM closed_trades {sql_where}", params).fetchone()["n"]
        pages = max(1, (total + per_page - 1) // per_page)
        page = min(page, pages)
        offset = (page - 1) * per_page
        trades = conn.execute(
            f"""
            SELECT * FROM closed_trades {sql_where}
            ORDER BY
              CASE
                WHEN instr(id, '|') > 0 THEN CAST(substr(id, 1, instr(id, '|') - 1) AS INTEGER)
                ELSE 0
              END DESC,
              exit_time DESC
            LIMIT ? OFFSET ?
            """,
            params + [per_page, offset],
        ).fetchall()
        assets = conn.execute("SELECT DISTINCT asset FROM closed_trades ORDER BY asset").fetchall()
    return {
        "trades": trades,
        "trade_stats": compute_trade_stats(),
        "assets": assets,
        "asset": asset,
        "direction": direction,
        "result": result,
        "page": page,
        "per_page": per_page,
        "pages": pages,
        "total": total,
        "start_item": (offset + 1 if total else 0),
        "end_item": min(offset + per_page, total),
        "coin_logos": COIN_LOGOS,
    }


def store_signal(conn, data: dict, received_at: str, table: str = "signals", replace: bool = False):
    if table not in ("signals", "open_signals"):
        raise ValueError("invalid signal table")
    mode = "INSERT OR REPLACE" if replace else "INSERT OR IGNORE"
    columns = "id, asset, tf, direction, entry_price, tp_price, sl_price, tp_pct, sl_pct, created_at, entry_time"
    values = [
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
    ]
    if table == "open_signals":
        columns += ", status"
        values.append(str(data.get("status") or "open"))
    columns += ", received_at"
    values.append(received_at)
    placeholders = ", ".join("?" for _ in values)
    conn.execute(f"{mode} INTO {table} ({columns}) VALUES ({placeholders})", values)


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
            session["session_id"] = secrets.token_urlsafe(24)
            now = utc_now()
            with db() as conn:
                conn.execute(
                    """
                    UPDATE user_sessions
                    SET logout_at=?, last_seen_at=?
                    WHERE user_id=? AND logout_at IS NULL
                    """,
                    (now, now, user["id"]),
                )
                conn.execute(
                    """
                    INSERT INTO user_sessions (
                      session_id, user_id, login_at, last_seen_at, last_path, ip, user_agent
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        session["session_id"],
                        user["id"],
                        now,
                        now,
                        request.path,
                        client_ip(),
                        request.headers.get("User-Agent", "")[:300],
                    ),
                )
                log_user_ip(conn, user["id"], session["session_id"], "login", request.path)
            return redirect(request.args.get("next") or url_for("dashboard"))
        flash("Usuario ou senha invalidos", "error")
    return render_template("login.html")


@app.post("/logout")
def logout():
    sid = session.get("session_id")
    uid = session.get("user_id")
    if sid and uid:
        with db() as conn:
            conn.execute(
                "UPDATE user_sessions SET logout_at=?, last_seen_at=? WHERE session_id=? AND user_id=?",
                (utc_now(), utc_now(), sid, uid),
            )
            log_user_ip(conn, uid, sid, "logout", request.path)
    session.clear()
    return redirect(url_for("login"))


@app.get("/dashboard")
@login_required
def dashboard():
    asset = request.args.get("asset", "all")
    direction = request.args.get("direction", "all")
    return render_template("dashboard.html", **load_dashboard_data(asset, direction))


@app.get("/api/dashboard-fragments")
@login_required
def dashboard_fragments():
    asset = request.args.get("asset", "all")
    direction = request.args.get("direction", "all")
    data = load_dashboard_data(asset, direction)
    version = "|".join(
        [
            str(data["trade_stats"]["total"]),
            f"{data['trade_stats']['pnl_pct']:.8f}",
            ",".join(str(row["id"]) for row in data["open_signals"]),
        ]
    )
    return jsonify({
        "version": version,
        "stats_html": render_template("_dashboard_stats.html", **data),
        "summary_html": render_template("_dashboard_summary.html", **data),
        "signals_html": render_template("_signal_list.html", **data),
    })


@app.get("/history")
@login_required
def history():
    asset = request.args.get("asset", "all")
    direction = request.args.get("direction", "all")
    result = request.args.get("result", "all")
    try:
        page = max(1, int(request.args.get("page", "1")))
    except ValueError:
        page = 1
    try:
        per_page = int(request.args.get("per_page", "14"))
    except ValueError:
        per_page = 14
    return render_template("history.html", **load_history_data(asset, direction, result, page, per_page))


@app.get("/api/history-fragments")
@login_required
def history_fragments():
    asset = request.args.get("asset", "all")
    direction = request.args.get("direction", "all")
    result = request.args.get("result", "all")
    try:
        page = max(1, int(request.args.get("page", "1")))
    except ValueError:
        page = 1
    try:
        per_page = int(request.args.get("per_page", "14"))
    except ValueError:
        per_page = 14
    data = load_history_data(asset, direction, result, page, per_page)
    version = "|".join(
        [
            str(data["total"]),
            str(data["page"]),
            ",".join(str(row["id"]) for row in data["trades"]),
        ]
    )
    return jsonify({
        "version": version,
        "history_html": render_template("_history_table.html", **data),
    })


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
        users = conn.execute("""
            SELECT
              u.id, u.username, u.role, u.active, u.created_at,
              s.session_id, s.login_at, s.last_seen_at, s.logout_at, s.last_path, s.ip
            FROM users u
            LEFT JOIN user_sessions s ON s.id = (
              SELECT id
              FROM user_sessions
              WHERE user_id = u.id
              ORDER BY last_seen_at DESC, id DESC
              LIMIT 1
            )
            ORDER BY u.id
        """).fetchall()
        ip_stats = {
            row["user_id"]: dict(row)
            for row in conn.execute("""
                SELECT
                  user_id,
                  COUNT(DISTINCT ip) AS ip_count,
                  MAX(created_at) AS last_ip_at
                FROM user_ip_logs
                GROUP BY user_id
            """).fetchall()
        }
        ip_logs = conn.execute("""
            SELECT l.created_at, l.ip, l.event, l.path, u.username
            FROM user_ip_logs l
            JOIN users u ON u.id = l.user_id
            ORDER BY l.created_at DESC, l.id DESC
            LIMIT 80
        """).fetchall()
    now = datetime.now(timezone.utc)
    user_rows = []
    for row in users:
        item = dict(row)
        last_seen = parse_utc(item.get("last_seen_at"))
        logout_at = parse_utc(item.get("logout_at"))
        item["is_online"] = bool(last_seen and not logout_at and now - last_seen <= timedelta(minutes=5))
        item["session_state"] = "Online" if item["is_online"] else ("Saiu" if logout_at else "Inativo")
        item["ip_count"] = (ip_stats.get(item["id"]) or {}).get("ip_count", 0)
        item["last_ip_at"] = (ip_stats.get(item["id"]) or {}).get("last_ip_at")
        user_rows.append(item)
    return render_template("admin_users.html", users=user_rows, ip_logs=ip_logs)


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


@app.get("/api/open-signals")
@login_required
def api_open_signals():
    with db() as conn:
        rows = conn.execute("""
            SELECT id, received_at
            FROM open_signals
            ORDER BY COALESCE(entry_time, created_at, received_at) DESC, received_at DESC
            LIMIT 500
        """).fetchall()
    return jsonify([dict(r) for r in rows])


@app.get("/api/session-check")
@login_required
def session_check():
    return jsonify({"ok": True, "active": True})


@app.get("/alerta/<path:filename>")
@login_required
def alert_file(filename):
    return send_from_directory(ALERT_DIR, filename)


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


@app.post("/api/users/sync")
def sync_users():
    token = request.headers.get("X-Ingest-Token", "")
    expected = os.getenv("INGEST_TOKEN", "")
    if not expected or not secrets.compare_digest(token, expected):
        abort(401)

    data = request.get_json(force=True, silent=False)
    rows = data.get("users") if isinstance(data, dict) else data
    if not isinstance(rows, list):
        return jsonify({"ok": False, "error": "expected list or {'users': [...]}"}), 400

    synced = 0
    with db() as conn:
        for user in rows:
            username = str(user.get("username", "")).strip()
            password = str(user.get("password", ""))
            role = str(user.get("role", "user")).strip().lower()
            active = 1 if user.get("active", True) else 0
            if not username or not password:
                continue
            if role not in ("user", "admin"):
                role = "user"
            row = conn.execute("SELECT id FROM users WHERE username=?", (username,)).fetchone()
            password_hash = generate_password_hash(password)
            if row:
                conn.execute(
                    "UPDATE users SET password_hash=?, role=?, active=? WHERE username=?",
                    (password_hash, role, active, username),
                )
            else:
                conn.execute(
                    "INSERT INTO users (username, password_hash, role, active, created_at) VALUES (?, ?, ?, ?, ?)",
                    (username, password_hash, role, active, utc_now()),
                )
            synced += 1
    return jsonify({"ok": True, "synced": synced})


@app.get("/api/signals")
@login_required
def api_signals():
    with db() as conn:
        rows = conn.execute("SELECT * FROM signals ORDER BY received_at DESC LIMIT 100").fetchall()
    return jsonify([dict(r) for r in rows])


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")), debug=True)
