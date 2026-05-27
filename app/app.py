import os
import string
import random
import re
from datetime import datetime

from flask import Flask, request, redirect, jsonify, render_template, abort
import psycopg2
import psycopg2.extras
import redis

app = Flask(__name__)

# ── Config ──────────────────────────────────────────────────────────────────
DB_HOST     = os.environ.get("DB_HOST", "db-postgresql")
DB_NAME     = os.environ.get("DB_NAME", "flaskdb")
DB_USER     = os.environ.get("DB_USER")
DB_PASSWORD = os.environ.get("DB_PASSWORD")
REDIS_HOST  = os.environ.get("REDIS_HOST", "redis-redis")
BASE_URL     = os.environ.get("BASE_URL", "http://localhost")

ALPHABET   = string.ascii_letters + string.digits   # base62
CODE_LEN   = 6
CACHE_TTL  = 3600  # seconds


# ── Connections ──────────────────────────────────────────────────────────────
def get_db():
    return psycopg2.connect(
        host=DB_HOST, dbname=DB_NAME,
        user=DB_USER, password=DB_PASSWORD,
        cursor_factory=psycopg2.extras.RealDictCursor,
    )

def get_redis():
    return redis.Redis(host=REDIS_HOST, port=6379, decode_responses=True)


# ── DB Init ──────────────────────────────────────────────────────────────────
def init_db():
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS urls (
                    id          SERIAL PRIMARY KEY,
                    code        VARCHAR(32) UNIQUE NOT NULL,
                    original    TEXT NOT NULL,
                    clicks      INTEGER DEFAULT 0,
                    created_at  TIMESTAMP DEFAULT NOW()
                );
                CREATE INDEX IF NOT EXISTS idx_urls_code ON urls(code);
            """)
        conn.commit()

with app.app_context():
    init_db()


# ── Helpers ──────────────────────────────────────────────────────────────────
def generate_code():
    return "".join(random.choices(ALPHABET, k=CODE_LEN))

def is_valid_url(url: str) -> bool:
    pattern = re.compile(
        r"^https?://"
        r"(?:(?:[A-Z0-9](?:[A-Z0-9-]{0,61}[A-Z0-9])?\.)+[A-Z]{2,6}\.?|"
        r"localhost|"
        r"\d{1,3}(?:\.\d{1,3}){3})"
        r"(?::\d+)?"
        r"(?:/?|[/?]\S+)$",
        re.IGNORECASE,
    )
    return bool(pattern.match(url))

def is_valid_slug(slug: str) -> bool:
    return bool(re.match(r"^[a-zA-Z0-9_-]{3,32}$", slug))

def create_short(original: str, custom_slug: str = None) -> dict:
    r = get_redis()

    with get_db() as conn:
        with conn.cursor() as cur:
            # Custom slug validation
            if custom_slug:
                if not is_valid_slug(custom_slug):
                    raise ValueError("Slug inválido. Usa letras, números, - o _ (3–32 chars).")
                cur.execute("SELECT code FROM urls WHERE code = %s", (custom_slug,))
                if cur.fetchone():
                    raise ValueError("Ese slug ya está en uso.")
                code = custom_slug
            else:
                # Generate unique random code
                for _ in range(10):
                    code = generate_code()
                    cur.execute("SELECT code FROM urls WHERE code = %s", (code,))
                    if not cur.fetchone():
                        break
                else:
                    raise RuntimeError("No se pudo generar un código único.")

            cur.execute(
                "INSERT INTO urls (code, original) VALUES (%s, %s) RETURNING *",
                (code, original),
            )
            row = dict(cur.fetchone())
        conn.commit()

    # Cache it immediately
    r.setex(f"url:{code}", CACHE_TTL, original)

    row["short_url"] = f"{BASE_URL}/{code}"
    return row


# ── Routes ───────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html", base_url=BASE_URL)


@app.route("/<code>")
def redirect_short(code):
    r = get_redis()

    # 1. Try cache first
    original = r.get(f"url:{code}")

    if not original:
        # 2. Fallback to DB
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT original FROM urls WHERE code = %s", (code,))
                row = cur.fetchone()
        if not row:
            abort(404)
        original = row["original"]
        r.setex(f"url:{code}", CACHE_TTL, original)

    # 3. Increment click counter asynchronously-ish (fire and forget in DB)
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("UPDATE urls SET clicks = clicks + 1 WHERE code = %s", (code,))
            conn.commit()
    except Exception:
        pass  # Don't break redirect if counter fails

    return redirect(original, code=302)


# ── API ───────────────────────────────────────────────────────────────────────

@app.route("/api/shorten", methods=["POST"])
def api_shorten():
    data = request.get_json(silent=True) or {}
    original = (data.get("url") or "").strip()
    slug     = (data.get("slug") or "").strip() or None

    if not original:
        return jsonify({"error": "El campo 'url' es requerido."}), 400
    if not is_valid_url(original):
        return jsonify({"error": "URL inválida."}), 400

    try:
        row = create_short(original, slug)
        return jsonify({
            "code":      row["code"],
            "short_url": row["short_url"],
            "original":  row["original"],
            "created_at": row["created_at"].isoformat(),
        }), 201
    except ValueError as e:
        return jsonify({"error": str(e)}), 409
    except Exception as e:
        return jsonify({"error": "Error interno."}), 500


@app.route("/api/stats/<code>")
def api_stats(code):
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM urls WHERE code = %s", (code,))
            row = cur.fetchone()
    if not row:
        return jsonify({"error": "No encontrado."}), 404
    return jsonify({
        "code":       row["code"],
        "original":   row["original"],
        "clicks":     row["clicks"],
        "short_url":  f"{BASE_URL}/{row['code']}",
        "created_at": row["created_at"].isoformat(),
    })


@app.route("/api/links")
def api_links():
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM urls ORDER BY created_at DESC LIMIT 50")
            rows = cur.fetchall()
    return jsonify([
        {
            "code":       r["code"],
            "original":   r["original"],
            "clicks":     r["clicks"],
            "short_url":  f"{BASE_URL}/{r['code']}",
            "created_at": r["created_at"].isoformat(),
        }
        for r in rows
    ])


@app.errorhandler(404)
def not_found(e):
    return render_template("404.html"), 404


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
