from flask import Flask, render_template, jsonify, send_from_directory, request, Response
from flask_caching import Cache
from waitress import serve
import os
import time
import requests
import sqlite3
from html import escape
from datetime import datetime, timezone

# Try to import psycopg (PostgreSQL driver); optional for local SQLite
try:
    import psycopg  # psycopg3
except Exception:  # pragma: no cover
    psycopg = None

app = Flask(__name__)

# Version string to bust browser cache for static assets
ASSET_VERSION = os.getenv("ASSET_VERSION") or str(int(time.time()))

@app.context_processor
def inject_asset_version():
    return {"ASSET_VERSION": ASSET_VERSION}

# Simple in-memory cache; good enough for small deployments
cache = Cache(app, config={"CACHE_TYPE": "SimpleCache", "CACHE_DEFAULT_TIMEOUT": 300})

DB_PATH = os.getenv("DB_PATH") or os.path.join(app.root_path, "app.db")


def _get_pg_dsn():
    """Build a PostgreSQL DSN from env vars, preferring DATABASE_URL."""
    url = os.getenv("DATABASE_URL")
    if url:
        return url
    host = os.getenv("PGHOST")
    if not host:
        return None
    parts = [
        f"host={host}",
        f"port={os.getenv('PGPORT', '5432')}",
        f"dbname={os.getenv('PGDATABASE')}",
        f"user={os.getenv('PGUSER')}",
        f"password={os.getenv('PGPASSWORD')}",
    ]
    sslmode = os.getenv("PGSSLMODE") or os.getenv("DATABASE_SSLMODE")
    if sslmode:
        parts.append(f"sslmode={sslmode}")
    # Filter missing values like dbname=None
    return " ".join(p for p in parts if p and not p.endswith("=None"))


def _is_postgres() -> bool:
    return psycopg is not None and _get_pg_dsn() is not None


def _get_db():
    # Use Postgres on Railway (or wherever DATABASE_URL/PG* provided), else SQLite locally
    if _is_postgres():
        return psycopg.connect(_get_pg_dsn())
    # Use check_same_thread=False to be safe under threaded servers; each usage is short-lived
    return sqlite3.connect(DB_PATH, check_same_thread=False)


def _init_db():
    with _get_db() as conn:
        # Use a cursor for compatibility across drivers
        cur = conn.cursor()
        # Load SQL from files depending on the driver
        try:
            if _is_postgres():
                sql_path = os.path.join(app.root_path, "sql", "create_contact_messages_postgres.sql")
            else:
                sql_path = os.path.join(app.root_path, "sql", "create_contact_messages_sqlite.sql")
            with open(sql_path, "r") as f:
                create_sql = f.read()
            cur.execute(create_sql)
            try:
                conn.commit()
            except Exception:
                pass
        except Exception:
            # If loading from file fails, do not crash app startup
            try:
                conn.rollback()
            except Exception:
                pass
            pass

_init_db()

# Log which DB is being used (avoid printing secrets)
try:
    if _is_postgres():
        print("[startup] Using PostgreSQL (DATABASE_URL detected)")
    else:
        print(f"[startup] Using SQLite at {DB_PATH}")
except Exception:
    pass


def _fetch_github_repos(username: str):
    """
    Fetch repositories for the given GitHub username with retry, timeout, and caching.
    Caches only successful responses.
    """
    cache_key = f"github_repos:{username}"
    cached = cache.get(cache_key)
    if cached is not None:
        return cached, None  # (data, error)

    url = f"https://api.github.com/users/{username}/repos"
    params = {"sort": "updated", "per_page": 20}
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "PortfolioApp/1.0 (+https://yusufsiddiqui.dev)",
    }

    max_attempts = 3
    backoff = 1.0  # seconds
    last_error = None

    for attempt in range(1, max_attempts + 1):
        try:
            resp = requests.get(url, params=params, headers=headers, timeout=7)
            status = resp.status_code

            if status == 200:
                data = resp.json()
                cache.set(cache_key, data, timeout=300)
                return data, None

            # Rate-limit or explicit throttling
            if status in (403, 429):
                retry_after = resp.headers.get("Retry-After")
                reset = resp.headers.get("X-RateLimit-Reset")
                msg = "GitHub API rate limit reached"
                if retry_after:
                    msg += f"; retry after {retry_after}s"
                elif reset:
                    try:
                        reset_in = max(0, int(reset) - int(time.time()))
                        msg += f"; retry in ~{reset_in}s"
                    except Exception:
                        pass
                last_error = {"type": "rate_limited", "status": status, "message": msg}
                break  # don't keep hammering on rate limits

            # Transient server errors; retry with backoff
            if 500 <= status < 600:
                last_error = {
                    "type": "upstream_error",
                    "status": status,
                    "message": f"GitHub returned {status}",
                }
                if attempt < max_attempts:
                    time.sleep(backoff)
                    backoff *= 2
                    continue
                break

            # Other non-success statuses; don't retry
            last_error = {
                "type": "bad_status",
                "status": status,
                "message": f"GitHub returned {status}",
            }
            break

        except requests.Timeout:
            last_error = {"type": "timeout", "status": 504, "message": "Request timed out"}
            if attempt < max_attempts:
                time.sleep(backoff)
                backoff *= 2
                continue
            break

        except requests.RequestException as e:
            last_error = {
                "type": "request_exception",
                "status": 502,
                "message": str(e),
            }
            if attempt < max_attempts:
                time.sleep(backoff)
                backoff *= 2
                continue
            break

    return None, last_error


@app.route("/")
def home():
    return render_template("index.html")


@app.route("/repos")
def repos():
    return render_template("repos.html")


@app.route("/contact")
def contact():
    return render_template("contact.html")


@app.route("/api/github/repos")
def api_github_repos():
    """
    Server-side endpoint to fetch GitHub repos to avoid exposing the GitHub API directly to clients.
    Supports optional ?username= query param, falling back to GITHUB_USERNAME env or a default.
    """
    username = request.args.get("username") or os.getenv("GITHUB_USERNAME", "Yusuf-Siddiqui-08")

    data, error = _fetch_github_repos(username)
    if error:
        status = error.get("status", 502)
        return (
            jsonify(
                {
                    "ok": False,
                    "error": error.get("type", "unknown"),
                    "message": error.get("message", "Unknown error"),
                    "username": username,
                }
            ),
            status if isinstance(status, int) and status >= 400 else 502,
        )

    return jsonify({"ok": True, "username": username, "repos": data})


@app.post("/api/contact")
def api_contact():
    data = request.get_json(silent=True) or request.form

    # Honeypot field (bots often fill this); ignore silently
    if (data.get("website") or "").strip():
        return jsonify({"ok": True}), 200

    name = (data.get("name") or "").strip()
    email = (data.get("email") or "").strip()
    message = (data.get("message") or "").strip()

    if not name or "@" not in email or len(message) < 5:
        return jsonify({"ok": False, "error": "validation_error"}), 400

    # Simple IP rate limit: 5 submissions per hour
    ip = request.headers.get("X-Forwarded-For", request.remote_addr) or "unknown"
    rl_key = f"rl:contact:{ip}"
    count = cache.get(rl_key) or 0
    if count >= 5:
        return jsonify({"ok": False, "error": "rate_limited"}), 429
    cache.set(rl_key, count + 1, timeout=3600)

    ts = int(time.time())
    ua = request.headers.get("User-Agent", "")
    with _get_db() as conn:
        if _is_postgres():
            cur = conn.cursor()
            # Load INSERT SQL for Postgres from file
            with open(os.path.join(app.root_path, "sql", "insert_contact_message_postgres.sql"), "r") as f:
                insert_sql = f.read()
            cur.execute(
                insert_sql,
                (name, email, message, ts, ip, ua),
            )
            row = cur.fetchone()
            msg_id = row[0] if row else None
            try:
                conn.commit()
            except Exception:
                pass
        else:
            # Load INSERT SQL for SQLite from file
            with open(os.path.join(app.root_path, "sql", "insert_contact_message_sqlite.sql"), "r") as f:
                insert_sql = f.read()
            cur = conn.execute(
                insert_sql,
                (name, email, message, ts, ip, ua),
            )
            msg_id = cur.lastrowid

    return jsonify({"ok": True, "id": msg_id}), 201


@app.get("/api/search")
def api_search():
    q = (request.args.get("q") or "").strip()
    if not q:
        return jsonify({"ok": True, "results": []})

    username = request.args.get("username") or os.getenv("GITHUB_USERNAME", "Yusuf-Siddiqui-08")
    data, error = _fetch_github_repos(username)
    if error:
        return jsonify({"ok": False, "error": error.get("type"), "message": error.get("message")}), 502

    ql = q.lower()
    results = []
    for r in data:
        text = " ".join([
            r.get("name") or "",
            r.get("description") or "",
            r.get("language") or "",
            " ".join(r.get("topics") or []),
        ]).lower()
        if ql in text:
            results.append({
                "name": r.get("name"),
                "html_url": r.get("html_url"),
                "description": r.get("description"),
                "language": r.get("language"),
                "stars": r.get("stargazers_count", 0),
                "updated_at": r.get("pushed_at") or r.get("updated_at"),
            })

    results.sort(key=lambda x: (x["stars"], x["updated_at"] or ""), reverse=True)
    return jsonify({"ok": True, "count": len(results), "results": results[:10]})


def _to_rfc2822(iso_ts: str) -> str:
    try:
        dt = datetime.fromisoformat((iso_ts or "").replace("Z", "+00:00"))
    except Exception:
        dt = datetime.now(timezone.utc)
    return dt.strftime("%a, %d %b %Y %H:%M:%S %z")


@app.get("/feed.xml")
def feed_xml():
    username = os.getenv("GITHUB_USERNAME", "Yusuf-Siddiqui-08")
    data, error = _fetch_github_repos(username)
    if error:
        return Response("Service Unavailable", status=503, mimetype="text/plain")

    # Top 10 by last update
    repos = sorted(
        data,
        key=lambda r: r.get("pushed_at") or r.get("updated_at") or "",
        reverse=True
    )[:10]

    site_title = f"{username}'s updates"
    site_link = request.url_root.rstrip("/")
    items = []
    for r in repos:
        title = escape(r.get("name") or "Repository")
        link = escape(r.get("html_url") or site_link)
        desc = escape(r.get("description") or "")
        pub_date = _to_rfc2822(r.get("pushed_at") or r.get("updated_at") or "")
        items.append(f"""
            <item>
                <title>{title}</title>
                <link>{link}</link>
                <guid isPermaLink=\"true\">{link}</guid>
                <pubDate>{pub_date}</pubDate>
                <description>{desc}</description>
            </item>
        """.strip())

    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
<channel>
<title>{escape(site_title)}</title>
<link>{site_link}</link>
<description>Latest updated repositories</description>
{''.join(items)}
</channel>
</rss>"""
    resp = Response(xml, mimetype="application/rss+xml")
    # Cache feed in clients/CDN for 5 minutes
    resp.headers["Cache-Control"] = "public, max-age=300"
    return resp


@app.route("/project_images/<path:filename>")
def project_images(filename):
    directory = os.path.join(app.root_path, "project_images")
    return send_from_directory(directory, filename)


@app.get("/api/health")
def api_health():
    try:
        with _get_db() as conn:
            cur = conn.cursor()
            # Load health check SQL from file
            with open(os.path.join(app.root_path, "sql", "health_check.sql"), "r") as f:
                health_sql = f.read()
            cur.execute(health_sql)
            one = cur.fetchone()
        return jsonify({
            "ok": True,
            "db": "postgres" if _is_postgres() else "sqlite",
            "select1": one[0] if one else None,
        }), 200
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


if __name__ == "__main__":
    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("PORT", "8080"))
    serve(app, host=host, port=port)
