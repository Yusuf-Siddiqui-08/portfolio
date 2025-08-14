from flask import Flask, render_template, jsonify, request, send_from_directory
from flask_caching import Cache
import os
import time
import requests

app = Flask(__name__)

# Simple in-memory cache; good enough for small deployments
cache = Cache(app, config={"CACHE_TYPE": "SimpleCache", "CACHE_DEFAULT_TIMEOUT": 300})


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
        "User-Agent": "PortfolioApp/1.0 (+https://example.com)",
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
def home():  # put application's code here
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


@app.route("/project_images/<path:filename>")
def project_images(filename):
    directory = os.path.join(app.root_path, "project_images")
    return send_from_directory(directory, filename)


if __name__ == "__main__":
    app.run()
