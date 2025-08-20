from flask import Flask, render_template, jsonify, send_from_directory, request, Response, redirect
from flask_caching import Cache
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_talisman import Talisman
from werkzeug.middleware.proxy_fix import ProxyFix
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

# Default Google reCAPTCHA v3 keys (will be used if environment variables are not set)
os.environ.setdefault("RECAPTCHA_SITE_KEY", "6Lf3qqwrAAAAAM5Hg2lqbmKxeRdXyAegwPwGbdgs")
os.environ.setdefault("RECAPTCHA_SECRET_KEY", "6Lf3qqwrAAAAALBP2QNzhQl4nbK70W0byrIATN7C")

app = Flask(__name__)

# Version string to bust browser cache for static assets
ASSET_VERSION = os.getenv("ASSET_VERSION") or str(int(time.time()))

# Flask-Limiter configuration (in-memory by default; configurable via env)
_default_rate = (os.getenv("DEFAULT_RATE_LIMIT", "") or "").strip()
_default_limits = [_default_rate] if _default_rate else None
limiter = Limiter(
    get_remote_address,
    app=app,
    storage_uri=os.getenv("RATELIMIT_STORAGE_URI", "memory://"),
    default_limits=_default_limits,
)

@app.context_processor
def inject_asset_version():
    return {"ASSET_VERSION": ASSET_VERSION}

# Simple in-memory cache; good enough for small deployments
cache = Cache(app, config={"CACHE_TYPE": "SimpleCache", "CACHE_DEFAULT_TIMEOUT": 300})

# ---------- Security: Flask-Talisman ----------
# Enforce HTTPS by default, but automatically disable in common local dev environments.
# You can always override with TALISMAN_FORCE_HTTPS=0/1.
_dev_flags = {
    "FLASK_ENV": os.getenv("FLASK_ENV", "").lower(),
    "ENV": os.getenv("ENV", "").lower(),
    "DEBUG": os.getenv("DEBUG", "").lower(),
}
_host_env = os.getenv("HOST", "0.0.0.0").lower()
_is_dev_like = (
    _dev_flags["FLASK_ENV"] == "development"
    or _dev_flags["ENV"] == "development"
    or _dev_flags["DEBUG"] in ("1", "true", "yes")
    or _host_env in ("127.0.0.1", "localhost")
)
_default_force_https = "0" if _is_dev_like else "1"
# Final decision honors explicit env if provided, else uses smarter default
force_https = (os.getenv("TALISMAN_FORCE_HTTPS", _default_force_https) not in ("0", "false", "False"))

# Base CSP; expanded below if CAPTCHA is enabled
csp = {
    'default-src': ["'self'"],
    'base-uri': ["'self'"],
    'object-src': ["'none'"],
    'img-src': ["'self'", "data:"],
    'style-src': ["'self'", "'unsafe-inline'"],  # inline styles used in templates
    'script-src': ["'self'", "'unsafe-inline'"],  # small inline scripts used in templates
    'connect-src': ["'self'"],  # XHR/Fetch to same-origin APIs
    'form-action': ["'self'"],
    'frame-ancestors': ["'self'"],
}

# Allow CAPTCHA providers if configured in templates
if os.getenv("TURNSTILE_SITE_KEY"):
    csp['script-src'] += ["https://challenges.cloudflare.com"]
    csp['frame-src'] = csp.get('frame-src', []) + ["https://challenges.cloudflare.com"]
if os.getenv("HCAPTCHA_SITE_KEY"):
    csp['script-src'] += ["https://js.hcaptcha.com"]
    csp['frame-src'] = csp.get('frame-src', []) + ["https://newassets.hcaptcha.com", "https://hcaptcha.com", "https://*.hcaptcha.com"]
if os.getenv("RECAPTCHA_SITE_KEY"):
    # Google reCAPTCHA v3 loads scripts from google.com and gstatic.com and uses iframes on google.com
    csp['script-src'] += ["https://www.google.com", "https://www.gstatic.com"]
    csp['frame-src'] = csp.get('frame-src', []) + ["https://www.google.com"]

# If running behind a reverse proxy, enable ProxyFix so Flask/Talisman see the original scheme/host
if os.getenv("USE_PROXY_FIX", "1") not in ("0", "false", "False"):
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_port=1, x_prefix=1)

# Secure cookie flags (HTTPS-only when force_https is enabled)
app.config.update(
    SESSION_COOKIE_SECURE=True if force_https else False,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE=os.getenv("SESSION_COOKIE_SAMESITE", "Lax"),
)

talisman = Talisman(
    app,
    content_security_policy=csp,
    force_https=force_https,
    referrer_policy=os.getenv("REFERRER_POLICY", "strict-origin-when-cross-origin"),
    frame_options=os.getenv("FRAME_OPTIONS", "SAMEORIGIN"),
    strict_transport_security=True,
    strict_transport_security_preload=False,
    strict_transport_security_max_age=31536000,
)

# Canonical host enforcement (production)
# Disable by default to avoid redirect loops on new deployments behind proxies/CDNs.
# To enable, set ENFORCE_CANONICAL_HOST=1 and provide CANONICAL_HOST (e.g., example.com).
CANONICAL_HOST = os.getenv("CANONICAL_HOST", "").strip()
ENFORCE_CANONICAL_HOST = os.getenv("ENFORCE_CANONICAL_HOST", "0").lower() in ("1", "true", "yes")

@app.before_request
def _enforce_canonical_host():
    # Only enforce for idempotent requests, and only when enabled
    if not ENFORCE_CANONICAL_HOST or not CANONICAL_HOST:
        return
    if request.method not in ("GET", "HEAD"):
        return

    # Collect possible hosts from proxy/CDN headers and the request to avoid redirect loops.
    candidates = []

    # Many providers (e.g., Vercel, some CDNs) set X-Forwarded-Host to the public host.
    fwd_host = request.headers.get("X-Forwarded-Host")
    if fwd_host:
        candidates.append(fwd_host)

    # Browser-provided Host header as seen by the app (may be internal on some platforms).
    hdr_host = request.headers.get("Host")
    if hdr_host:
        candidates.append(hdr_host)

    # Flask's computed host fallback.
    if request.host:
        candidates.append(request.host)

    def _norm_host(h: str) -> str:
        # Take first value if comma-separated, strip port, lowercase
        h = (h or "").split(",", 1)[0].strip().lower()
        if ":" in h:
            h = h.split(":", 1)[0]
        return h

    norm_candidates = [_norm_host(h) for h in candidates if h]
    canonical = _norm_host(CANONICAL_HOST)

    if not norm_candidates:
        return

    # Skip localhost/dev-like hosts
    if any(h == "localhost" or h.startswith("127.") for h in norm_candidates):
        return

    # If any observed host already matches the canonical host, do nothing.
    if canonical in norm_candidates:
        return

    # Otherwise, build redirect URL to canonical host with correct scheme, preserving path and query.
    try:
        from urllib.parse import urlsplit, urlunsplit
    except Exception:
        # Fallback: simple replace (should rarely happen)
        raw_host = hdr_host or request.host or ""
        target = request.url.replace(f"//{raw_host}", f"//{CANONICAL_HOST}", 1)
        if force_https and request.scheme != "https":
            target = target.replace("http://", "https://", 1)
        return redirect(target, code=301)

    parts = urlsplit(request.url)
    scheme = "https" if force_https else parts.scheme or "http"
    netloc = CANONICAL_HOST  # honor any port in env if explicitly set
    target = urlunsplit((scheme, netloc, parts.path, parts.query, parts.fragment))
    return redirect(target, code=301)

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

# Log canonical host enforcement
try:
    if ENFORCE_CANONICAL_HOST:
        print(f"[startup] Canonical host enforced: https://{CANONICAL_HOST}")
    else:
        print("[startup] Canonical host enforcement disabled")
except Exception:
    pass


def _fetch_github_repos(username: str):
    """
    Fetch repositories for the given GitHub username with retry, timeout, and caching.
    Caches only successful responses.
    Additionally, enriches each repo with its topics (as 'topics': [str, ...]).
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

    def _fetch_topics(owner: str, repo: str):
        try:
            t_url = f"https://api.github.com/repos/{owner}/{repo}/topics"
            # GitHub topics are included in this endpoint; standard accept works
            t_resp = requests.get(t_url, headers=headers, timeout=5)
            if t_resp.status_code == 200:
                t_json = t_resp.json() or {}
                topics = t_json.get("names") or t_json.get("topics") or []
                if isinstance(topics, list):
                    # normalize to strings
                    return [str(x) for x in topics if isinstance(x, (str, int, float))]
        except Exception:
            pass
        return []

    for attempt in range(1, max_attempts + 1):
        try:
            resp = requests.get(url, params=params, headers=headers, timeout=7)
            status = resp.status_code

            if status == 200:
                data = resp.json()
                # Enrich with topics (best-effort; ignore errors)
                owner = username
                for r in data:
                    if isinstance(r, dict) and "topics" not in r:
                        name = r.get("name")
                        if not name:
                            r["topics"] = []
                            continue
                        r["topics"] = _fetch_topics(owner, name)
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
    # Expose CAPTCHA site keys to template; widgets render only if configured
    turnstile_site_key = os.getenv("TURNSTILE_SITE_KEY")
    hcaptcha_site_key = os.getenv("HCAPTCHA_SITE_KEY")
    recaptcha_site_key = os.getenv("RECAPTCHA_SITE_KEY")

    # Only enable Google reCAPTCHA front-end on the canonical host (or if explicitly allowed in dev)
    host_hdr = (request.headers.get("Host") or request.host or "").split(",", 1)[0].strip().lower()
    if ":" in host_hdr:
        host_hdr = host_hdr.split(":", 1)[0]
    canonical = (os.getenv("CANONICAL_HOST", "yusufsiddiqui.dev") or "").strip().lower()
    allow_dev = os.getenv("ALLOW_RECAPTCHA_ON_DEV", "0").lower() in ("1", "true", "yes")
    is_dev_like = (host_hdr == "localhost" or host_hdr.startswith("127."))
    if recaptcha_site_key and not allow_dev:
        # Disable the recaptcha widget on non-canonical or dev-like hosts to avoid Google warning overlays
        if is_dev_like or (canonical and host_hdr and host_hdr != canonical):
            recaptcha_site_key = None

    return render_template(
        "contact.html",
        turnstile_site_key=turnstile_site_key,
        hcaptcha_site_key=hcaptcha_site_key,
        recaptcha_site_key=recaptcha_site_key,
    )


@app.route("/api/github/repos")
@limiter.limit(os.getenv("RL_GITHUB_REPOS", "30 per minute"))
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
@limiter.limit(os.getenv("RL_CONTACT", "3 per minute; 10 per hour"))
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

    # CAPTCHA verification (if configured)
    ip = request.headers.get("X-Forwarded-For", request.remote_addr) or "unknown"
    turnstile_secret = os.getenv("TURNSTILE_SECRET_KEY")
    hcaptcha_secret = os.getenv("HCAPTCHA_SECRET_KEY")
    recaptcha_secret = os.getenv("RECAPTCHA_SECRET_KEY")

    # On dev-like hosts (localhost/127.*) or non-canonical hosts, optionally bypass CAPTCHA for local testing
    host_hdr = (request.headers.get("Host") or request.host or "").split(",", 1)[0].strip().lower()
    if ":" in host_hdr:
        host_hdr = host_hdr.split(":", 1)[0]
    canonical = (os.getenv("CANONICAL_HOST", "yusufsiddiqui.dev") or "").strip().lower()
    is_dev_like = (host_hdr == "localhost" or host_hdr.startswith("127."))
    require_on_dev = os.getenv("CAPTCHA_REQUIRE_ON_DEV", "0").lower() in ("1", "true", "yes")
    if (is_dev_like or (canonical and host_hdr and host_hdr != canonical)) and not require_on_dev:
        turnstile_secret = None
        hcaptcha_secret = None
        recaptcha_secret = None

    # Prefer Turnstile if multiple are configured, then hCaptcha, then reCAPTCHA v3
    if turnstile_secret:
        token = (data.get("cf_turnstile_token") or data.get("cf-turnstile-response") or "").strip()
        if not token:
            return jsonify({"ok": False, "error": "captcha_required"}), 400
        try:
            vresp = requests.post(
                "https://challenges.cloudflare.com/turnstile/v0/siteverify",
                data={"secret": turnstile_secret, "response": token, "remoteip": ip},
                timeout=5,
            )
            vjson = vresp.json() if vresp.ok else {}
            if not vjson.get("success"):
                return jsonify({"ok": False, "error": "captcha_failed"}), 400
        except Exception:
            return jsonify({"ok": False, "error": "captcha_failed"}), 400
    elif hcaptcha_secret:
        token = (data.get("hcaptcha_token") or data.get("h-captcha-response") or "").strip()
        if not token:
            return jsonify({"ok": False, "error": "captcha_required"}), 400
        try:
            vresp = requests.post(
                "https://hcaptcha.com/siteverify",
                data={"secret": hcaptcha_secret, "response": token, "remoteip": ip},
                timeout=5,
            )
            vjson = vresp.json() if vresp.ok else {}
            if not vjson.get("success"):
                return jsonify({"ok": False, "error": "captcha_failed"}), 400
        except Exception:
            return jsonify({"ok": False, "error": "captcha_failed"}), 400
    elif recaptcha_secret:
        token = (data.get("recaptcha_token") or data.get("g-recaptcha-response") or "").strip()
        if not token:
            return jsonify({"ok": False, "error": "captcha_required"}), 400
        try:
            vresp = requests.post(
                "https://www.google.com/recaptcha/api/siteverify",
                data={"secret": recaptcha_secret, "response": token, "remoteip": ip},
                timeout=5,
            )
            vjson = vresp.json() if vresp.ok else {}
            # Validate success, score threshold, and action if provided
            if not vjson.get("success"):
                return jsonify({"ok": False, "error": "captcha_failed"}), 400
            score = vjson.get("score")
            action = vjson.get("action")
            if score is not None:
                try:
                    if float(score) < float(os.getenv("RECAPTCHA_MIN_SCORE", "0.5")):
                        return jsonify({"ok": False, "error": "captcha_failed"}), 400
                except Exception:
                    pass
            # If action returned, ensure it matches our expected action
            if action and action != "contact":
                return jsonify({"ok": False, "error": "captcha_failed"}), 400
        except Exception:
            return jsonify({"ok": False, "error": "captcha_failed"}), 400

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
@limiter.limit(os.getenv("RL_SEARCH", "20 per minute"))
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
                "forks": r.get("forks_count", 0),
                "updated_at": r.get("pushed_at") or r.get("updated_at"),
                "topics": r.get("topics") or [],
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


# ---------- Error Handlers ----------
@app.errorhandler(404)
def handle_404(e):
    # JSON for API routes
    if request.path.startswith("/api/"):
        return jsonify({
            "ok": False,
            "error": "not_found",
            "message": "Resource not found",
            "path": request.path,
            "timestamp": int(time.time()),
        }), 404
    # HTML for normal routes
    return render_template("404.html", path=request.path), 404


@app.errorhandler(500)
def handle_500(e):
    # JSON for API routes
    if request.path.startswith("/api/"):
        return jsonify({
            "ok": False,
            "error": "server_error",
            "message": "An internal error occurred",
            "path": request.path,
            "timestamp": int(time.time()),
        }), 500
    # HTML for normal routes
    return render_template("500.html"), 500


# Rate limit (429) handler to return JSON for API endpoints
@app.errorhandler(429)
def handle_429(e):
    if request.path.startswith("/api/"):
        # Flask-Limiter attaches a description; include Retry-After if available
        retry_after = getattr(e, "retry_after", None)
        resp = {
            "ok": False,
            "error": "rate_limited",
            "message": getattr(e, "description", "Too many requests"),
            "path": request.path,
            "timestamp": int(time.time()),
        }
        return jsonify(resp), 429
    # For non-API paths, just show 429 text
    return ("Too Many Requests", 429, {"Content-Type": "text/plain; charset=utf-8"})


if __name__ == "__main__":
    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("PORT", "8080"))

    enable_dev_https = os.getenv("ENABLE_DEV_HTTPS", "0").lower() in ("1", "true", "yes")
    if enable_dev_https:
        # Optional local HTTPS for development using Werkzeug's server with an ad-hoc cert.
        # This is useful to test HTTPS redirects/CSP/HSTS locally without a reverse proxy.
        dev_https_port = int(os.getenv("DEV_HTTPS_PORT", "8443"))
        debug_flag = os.getenv("DEBUG", "").lower() in ("1", "true", "yes") or os.getenv("FLASK_ENV", "").lower() == "development"
        print(f"[startup] DEV HTTPS mode enabled: https://{host}:{dev_https_port}")
        print("[startup] Note: In this mode, the Flask dev server is used instead of Waitress.")
        print("[startup] To disable, unset ENABLE_DEV_HTTPS or set it to 0.")
        try:
            # Use Flask's dev server with an ad-hoc SSL context.
            app.run(host=host, port=dev_https_port, debug=debug_flag, ssl_context="adhoc")
        except Exception as e:
            print(f"[startup] Failed to start dev HTTPS server: {e}")
    else:
        # Production/normal mode: use Waitress (plain HTTP). For HTTPS, terminate TLS at a reverse proxy.
        serve(app, host=host, port=port)
