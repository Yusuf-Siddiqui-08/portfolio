from flask import Flask, render_template, jsonify, send_from_directory, request, Response, redirect
from flask_caching import Cache
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from werkzeug.middleware.proxy_fix import ProxyFix
from waitress import serve
import os
import time
import requests
import sqlite3
from html import escape
from datetime import datetime, timezone
import resend

# Try to import psycopg (PostgreSQL driver); optional for local SQLite
try:
    import psycopg  # psycopg3
except Exception:  # pragma: no cover
    psycopg = None


app = Flask(__name__)

# Version string to bust browser cache for static assets
ASSET_VERSION = os.getenv("ASSET_VERSION") or str(int(time.time()))

# Flask-Limiter configuration (in-memory by default; configurable via env)
_default_rate = (os.getenv("DEFAULT_RATE_LIMIT", "") or "").strip()
_default_limits = [_default_rate] if _default_rate else None
# Robust rate limit key to reduce collisions behind proxies/CDNs by combining client IP and UA
# Falls back to get_remote_address if headers are missing. This helps avoid "burst cache retrials
# collision" when many clients share an egress IP.
from typing import Optional

def _rate_limit_key() -> str:
    try:
        ip = get_remote_address() or "unknown"
        ua = (request.headers.get("User-Agent") or "").strip()[:80]
        # Namespace to prevent cross-endpoint collisions if storage is shared
        path = (request.path or "/")
        return f"ip:{ip}|ua:{ua}|path:{path}"
    except Exception:
        return "ip:unknown|ua:unknown|path:unknown"

# Choose storage with an optional in-memory namespace to reduce key collisions across processes
_rl_storage = os.getenv("RATELIMIT_STORAGE_URI", "memory://")
_rl_strategy = os.getenv("RATELIMIT_STRATEGY", "fixed-window-elastic-expiry")

limiter = Limiter(
    key_func=_rate_limit_key,
    app=app,
    storage_uri=_rl_storage,
    default_limits=_default_limits,
    strategy=_rl_strategy,
    headers_enabled=True,
)

@app.context_processor
def inject_asset_version():
    return {"ASSET_VERSION": ASSET_VERSION}

# Simple in-memory cache; good enough for small deployments
cache = Cache(app, config={"CACHE_TYPE": "SimpleCache", "CACHE_DEFAULT_TIMEOUT": 300})

# ---------- Security configuration ----------
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
# Default to not forcing HTTPS unless explicitly enabled via TALISMAN_FORCE_HTTPS=1.
# This avoids potential redirect loops on some hosting providers that already manage HTTPS.
_default_force_https = "0"
# Final decision honors explicit env if provided
force_https = (os.getenv("TALISMAN_FORCE_HTTPS", _default_force_https) not in ("0", "false", "False"))


# If running behind a reverse proxy/CDN (Render, Fly, Heroku, Cloudflare, Nginx, etc.),
# keep ProxyFix enabled so Flask sees the original client IP, scheme, and host from
# X-Forwarded-* headers. This is still useful even without Flask-Talisman because:
#  - request.is_secure reflects X-Forwarded-Proto, improving HTTPS detection and URL generation
#  - get_remote_address (Flask-Limiter) uses the real client IP instead of the proxy IP
#  - request.host/host_url reflect the public host for correct redirects and canonical host logic
if os.getenv("USE_PROXY_FIX", "1") not in ("0", "false", "False"):
    # Trust this many proxy hops (comma-separated X-Forwarded-* entries).
    # Many platforms can insert 2+ hops (ingress -> router -> app). Configure via TRUSTED_PROXY_COUNT.
    # Defaults to 2 for safer HTTPS and client IP detection behind multi-hop proxies.
    try:
        _trusted_hops = int(os.getenv("TRUSTED_PROXY_COUNT", "2"))
    except Exception:
        _trusted_hops = 2
    app.wsgi_app = ProxyFix(
        app.wsgi_app,
        x_for=_trusted_hops,
        x_proto=_trusted_hops,
        x_host=_trusted_hops,
        x_port=_trusted_hops,
        x_prefix=_trusted_hops,
    )

# Secure cookie flags (HTTPS-only when force_https is enabled)
app.config.update(
    SESSION_COOKIE_SECURE=True if force_https else False,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE=os.getenv("SESSION_COOKIE_SAMESITE", "Lax"),
)

# Safer HTTPS enforcement using multiple proxy headers (optional via TALISMAN_FORCE_HTTPS)
# Only applied to idempotent methods and skipped for health/static paths to avoid loops.

def _is_request_https() -> bool:
    try:
        if request.is_secure:
            return True
    except Exception:
        pass
    xf_proto = (request.headers.get("X-Forwarded-Proto") or "").lower()
    if "https" in xf_proto:
        return True
    cf_visitor = (request.headers.get("CF-Visitor") or request.headers.get("Cf-Visitor") or "")
    if "https" in cf_visitor.lower():
        return True
    if (request.headers.get("X-Forwarded-SSL") or "").lower() == "on":
        return True
    return False

@app.before_request
def _enforce_https_safely():
    # Respect the environment's HTTPS setting
    if not force_https:
        return
    # Limit to idempotent methods to avoid breaking POST/PUT flows
    if request.method not in ("GET", "HEAD"):
        return
    # Skip well-known/static/debug paths
    path = request.path or "/"
    if (
        path.startswith("/api/health") or
        path.startswith("/__diag") or
        path.startswith("/project_images/") or
        path.startswith("/static/") or
        path == "/favicon.ico" or
        path == "/robots.txt" or
        path.startswith("/.well-known/")
    ):
        return
    if _is_request_https():
        return
    # Build an https URL preserving host/path/query
    try:
        from urllib.parse import urlsplit, urlunsplit
    except Exception:
        target = request.url.replace("http://", "https://", 1)
        return redirect(target, code=301)
    parts = urlsplit(request.url)
    # Honor public host from X-Forwarded-Host if present
    fwd_host = request.headers.get("X-Forwarded-Host")
    netloc = (fwd_host or parts.netloc)
    target = urlunsplit(("https", netloc, parts.path, parts.query, parts.fragment))
    return redirect(target, code=301)

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


def _send_contact_email_notification(name: str, email: str, message: str):
    if not resend.api_key:
        resend.api_key = os.getenv("RESEND_API_KEY")
    to_addr = os.getenv("EMAIL_TO_ADDRESS")
    from_addr = os.getenv("EMAIL_FROM_ADDRESS", "Yusuf's Portfolio <onboarding@resend.dev>")
    subject = f"New Contact Form Message from {name}"
    html_body = f"""<h1>Name: {name}</h1>
<h1>Email: {email}</h1>
<h3>Message:</h3>
<p>{message}</p>"""
    params: resend.Emails.SendParams = {
        "from": from_addr,
        "to": [to_addr],
        "subject": subject,
        "html": html_body,
    }
    return resend.Emails.send(params)

@app.post("/api/contact")
@limiter.limit(os.getenv("RL_CONTACT", "3 per minute; 10 per hour; 30 per day"))
def api_contact():
    data = request.get_json(silent=True) or request.form

    # Honeypot field (bots often fill this); ignore silently
    if (data.get("website") or "").strip():
        return jsonify({"ok": True}), 200

    name = (data.get("name") or "").strip()
    email = (data.get("email") or "").strip()
    message = (data.get("message") or "").strip()

    # Enhanced validation
    # - Name: at least 2 characters after trimming
    # - Email: basic RFC5322-like regex (not perfect, but robust enough for most cases)
    # - Message: at least 20 characters to reduce spam/noise; configurable via env CONTACT_MIN_MESSAGE_LEN
    try:
        import re
        email_regex = re.compile(r"^[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}$", re.IGNORECASE)
    except Exception:
        email_regex = None

    min_message_len = 0
    try:
        min_message_len = int(os.getenv("CONTACT_MIN_MESSAGE_LEN", "20"))
    except Exception:
        min_message_len = 20

    valid_name = len(name) >= 2
    valid_email = bool(email_regex.match(email)) if email_regex else ("@" in email and "." in email)
    valid_message = len(message) >= min_message_len

    if not (valid_name and valid_email and valid_message):
        return jsonify({
            "ok": False,
            "error": "validation_error",
            "details": {
                "name": "too_short" if not valid_name else None,
                "email": "invalid" if not valid_email else None,
                "message": f"too_short_min_{min_message_len}" if not valid_message else None,
            }
        }), 400

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
        # Extra abuse protection when CAPTCHA is bypassed (local/non-canonical):
        try:
            # Build a composite key using real client IP (from ProxyFix) and user agent
            real_ip = get_remote_address()
            ua_local = (request.headers.get("User-Agent") or "").strip()[:120]
            k_prefix = os.getenv("CONTACT_BYPASS_KEY_PREFIX", "contact_bypass")
            window_s = int(os.getenv("CONTACT_BYPASS_WINDOW_SEC", "60"))
            burst_limit = int(os.getenv("CONTACT_BYPASS_BURST", "1"))
            hourly_limit = int(os.getenv("CONTACT_BYPASS_HOURLY", "3"))
            day_limit = int(os.getenv("CONTACT_BYPASS_DAILY", "10"))

            def _inc_and_get(key: str, timeout: int):
                val = cache.get(key)
                try:
                    cur = int(val or 0) + 1
                except Exception:
                    cur = 1
                cache.set(key, cur, timeout=timeout)
                return cur

            # Short window burst limit (per minute by default)
            # Add a tiny rolling window bucket suffix to reduce thundering-herd collisions
            burst_bucket = int(time.time() // max(1, min(window_s, 10)))
            burst_key = f"{k_prefix}:burst:{real_ip}:{ua_local}:{burst_bucket}"
            burst = _inc_and_get(burst_key, window_s)
            if burst > burst_limit:
                return jsonify({"ok": False, "error": "rate_limited", "reason": "burst"}), 429

            # Hourly cap
            hourly_key = f"{k_prefix}:hour:{real_ip}:{ua_local}:{int(time.time()//3600)}"
            hourly = _inc_and_get(hourly_key, 3700)
            if hourly > hourly_limit:
                return jsonify({"ok": False, "error": "rate_limited", "reason": "hourly"}), 429

            # Daily cap
            daily_key = f"{k_prefix}:day:{real_ip}:{ua_local}:{int(time.time()//86400)}"
            daily = _inc_and_get(daily_key, 90000)
            if daily > day_limit:
                return jsonify({"ok": False, "error": "rate_limited", "reason": "daily"}), 429

            # Prevent repeated identical messages from same IP within 10 minutes
            msg_fingerprint = f"{k_prefix}:dedupe:{real_ip}:{hash((ua_local, (data.get('message') or '').strip()) )}"
            if cache.get(msg_fingerprint):
                return jsonify({"ok": False, "error": "duplicate", "reason": "recent_duplicate"}), 429
            cache.set(msg_fingerprint, 1, timeout=int(os.getenv("CONTACT_BYPASS_DEDUPE_SEC", "600")))
        except Exception as _e:
            # Fail-closed to conservative behavior? Here we fail-open to not break local dev unexpectedly,
            # but we still have the global Flask-Limiter decorator as a backstop.
            pass

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

    # Send email notification (best-effort, fails silently)
    try:
        _send_contact_email_notification(name=name, email=email, message=message)
    except Exception as e:
        # Log and ignore to ensure the client still gets a success response
        print(f"[api_contact] Failed to dispatch email notification: {e}")

    return jsonify({"ok": True, "id": msg_id, "created_at": ts}), 201


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


@app.get("/__diag")
def diag():
    # Diagnostics endpoint to visualize proxy headers and HTTPS detection in production
    info = {
        "ok": True,
        "request": {
            "url": request.url,
            "base_url": request.base_url,
            "path": request.path,
            "method": request.method,
            "scheme": request.scheme,
            "host": request.host,
            "remote_addr": request.remote_addr,
        },
        "headers": {
            "Host": request.headers.get("Host"),
            "X-Forwarded-Proto": request.headers.get("X-Forwarded-Proto"),
            "X-Forwarded-For": request.headers.get("X-Forwarded-For"),
            "X-Forwarded-Host": request.headers.get("X-Forwarded-Host"),
            "X-Forwarded-Port": request.headers.get("X-Forwarded-Port"),
            "X-Forwarded-Prefix": request.headers.get("X-Forwarded-Prefix"),
            "X-Forwarded-SSL": request.headers.get("X-Forwarded-SSL"),
            "CF-Visitor": request.headers.get("CF-Visitor") or request.headers.get("Cf-Visitor"),
            "CF-Connecting-IP": request.headers.get("CF-Connecting-IP"),
        },
        "computed": {
            "is_request_https": _is_request_https(),
            "force_https_env": bool(force_https),
            "enforce_canonical": bool(ENFORCE_CANONICAL_HOST),
            "canonical_host": CANONICAL_HOST,
            "use_proxy_fix": os.getenv("USE_PROXY_FIX", "1"),
            "trusted_proxy_count": os.getenv("TRUSTED_PROXY_COUNT", "2"),
        },
    }
    return jsonify(info), 200

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
    # Provide consistent JSON with Retry-After header to help clients back off
    retry_after = getattr(e, "retry_after", None)
    headers = {"Content-Type": "application/json; charset=utf-8"}
    if retry_after is not None:
        try:
            headers["Retry-After"] = str(int(retry_after))
        except Exception:
            pass
    if request.path.startswith("/api/"):
        resp = {
            "ok": False,
            "error": "rate_limited",
            "message": getattr(e, "description", "Too many requests"),
            "path": request.path,
            "timestamp": int(time.time()),
        }
        return jsonify(resp), 429, headers
    # For non-API paths, return plain text with Retry-After if present
    if "Retry-After" in headers:
        return ("Too Many Requests", 429, {**headers, "Content-Type": "text/plain; charset=utf-8"})
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
