"""
Album Credits Tool - Backend
Requirements:
    pip install flask flask-cors requests instaloader beautifulsoup4
"""

import os
import re
import time
import json
import sqlite3
import logging
from contextlib import contextmanager
from flask import Flask, request, jsonify
from flask_cors import CORS
import requests

try:
    import instaloader
    INSTALOADER_AVAILABLE = True
except ImportError:
    INSTALOADER_AVAILABLE = False

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)
CORS(app)

# ── CONFIG ──────────────────────────────────────────────────────────────────
GENIUS_ACCESS_TOKEN = "DN7-IZJBGU5-I0OkZDPI63hhApYFgi34XwBr5C4L8mkhZD7e4718lUxWs8oJNLdm"
GENIUS_BASE = "https://api.genius.com"
HEADERS = {"Authorization": f"Bearer {GENIUS_ACCESS_TOKEN}"}

BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
    "Connection": "keep-alive",
}

DB_PATH = os.path.join(os.path.dirname(__file__), "creditcrate.db")
CACHE_TTL_SECONDS = 180 * 24 * 60 * 60  # 6 months


# ── DATABASE ─────────────────────────────────────────────────────────────────

def init_db():
    with get_db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS artist_cache (
                genius_id       INTEGER PRIMARY KEY,
                name            TEXT,
                instagram_handle TEXT,
                instagram_email  TEXT,
                instagram_bio    TEXT,
                instagram_full_name TEXT,
                instagram_followers INTEGER,
                instagram_error  TEXT,
                last_updated     REAL
            )
        """)
        conn.commit()
    logger.info(f"Database ready at {DB_PATH}")


@contextmanager
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def cache_get(genius_id: int):
    """Return cached artist data if fresh (< 6 months old), else None."""
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM artist_cache WHERE genius_id = ?", (genius_id,)
        ).fetchone()
    if row is None:
        return None
    age = time.time() - row["last_updated"]
    if age > CACHE_TTL_SECONDS:
        logger.info(f"Cache expired for genius_id={genius_id} (age={age/86400:.0f}d)")
        return None
    logger.info(f"Cache hit for genius_id={genius_id} (age={age/86400:.0f}d)")
    return dict(row)


def cache_set(genius_id: int, name: str, instagram_handle: str, ig_data: dict):
    """Upsert artist data into cache."""
    email      = ig_data.get("email") if ig_data else None
    bio        = ig_data.get("bio") if ig_data else None
    full_name  = ig_data.get("full_name") if ig_data else None
    followers  = ig_data.get("followers") if ig_data else None
    error      = ig_data.get("error") if ig_data else None

    with get_db() as conn:
        conn.execute("""
            INSERT INTO artist_cache
                (genius_id, name, instagram_handle, instagram_email,
                 instagram_bio, instagram_full_name, instagram_followers,
                 instagram_error, last_updated)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(genius_id) DO UPDATE SET
                name                = excluded.name,
                instagram_handle    = excluded.instagram_handle,
                instagram_email     = excluded.instagram_email,
                instagram_bio       = excluded.instagram_bio,
                instagram_full_name = excluded.instagram_full_name,
                instagram_followers = excluded.instagram_followers,
                instagram_error     = excluded.instagram_error,
                last_updated        = excluded.last_updated
        """, (genius_id, name, instagram_handle, email, bio, full_name, followers, error, time.time()))
        conn.commit()
    logger.info(f"Cached artist genius_id={genius_id} name={name!r} ig=@{instagram_handle}")


# ── GENIUS API ───────────────────────────────────────────────────────────────

def genius_search_album(album_name, artist_name=""):
    query = f"{artist_name} {album_name}".strip()
    resp = requests.get(
        f"{GENIUS_BASE}/search", headers=HEADERS,
        params={"q": query, "per_page": 20}, timeout=10
    )
    resp.raise_for_status()
    hits = resp.json()["response"]["hits"]

    albums_found = {}
    for hit in hits[:8]:
        song_id = hit["result"]["id"]
        song_resp = requests.get(f"{GENIUS_BASE}/songs/{song_id}", headers=HEADERS, timeout=10)
        if song_resp.status_code != 200:
            continue
        song_data = song_resp.json()["response"]["song"]
        album_info = song_data.get("album")
        if album_info:
            alb_id = album_info["id"]
            if alb_id not in albums_found:
                albums_found[alb_id] = {
                    "id":        alb_id,
                    "name":      album_info["name"],
                    "artist":    song_data["primary_artist"]["name"],
                    "cover_art": album_info.get("cover_art_url"),
                    "songs":     [],
                }
        time.sleep(0.3)

    return list(albums_found.values())


def get_album_tracks(album_id):
    tracks = []
    page = 1
    while True:
        resp = requests.get(
            f"{GENIUS_BASE}/albums/{album_id}/tracks",
            headers=HEADERS, params={"page": page, "per_page": 50}, timeout=10
        )
        resp.raise_for_status()
        data = resp.json()["response"]
        tracks.extend(data["tracks"])
        if data["next_page"] is None:
            break
        page += 1
    return tracks


def get_song_credits(song_id):
    resp = requests.get(f"{GENIUS_BASE}/songs/{song_id}", headers=HEADERS, timeout=10)
    resp.raise_for_status()
    song_data = resp.json()["response"]["song"]
    credits = {}

    for perf in song_data.get("custom_performances", []):
        role = perf.get("label", "")
        for artist in perf.get("artists", []):
            name = artist.get("name", "")
            if name:
                if name not in credits:
                    credits[name] = {
                        "name":       name,
                        "roles":      [],
                        "genius_url": artist.get("url", ""),
                        "genius_id":  artist.get("id"),
                    }
                if role and role not in credits[name]["roles"]:
                    credits[name]["roles"].append(role)

    for artist in song_data.get("producer_artists", []):
        name = artist.get("name", "")
        if name:
            if name not in credits:
                credits[name] = {
                    "name":       name,
                    "roles":      [],
                    "genius_url": artist.get("url", ""),
                    "genius_id":  artist.get("id"),
                }
            if "Producer" not in credits[name]["roles"]:
                credits[name]["roles"].append("Producer")

    return credits


def get_artist_instagram(genius_artist_id):
    if not genius_artist_id:
        return None
    try:
        resp = requests.get(
            f"{GENIUS_BASE}/artists/{genius_artist_id}",
            headers=HEADERS, timeout=10
        )
        if resp.status_code != 200:
            return None
        artist_data = resp.json().get("response", {}).get("artist", {})
        return artist_data.get("instagram_name")
    except Exception as e:
        logger.warning(f"Could not get Instagram for artist {genius_artist_id}: {e}")
    return None


# ── INSTAGRAM ────────────────────────────────────────────────────────────────

def extract_email_from_bio(bio):
    if not bio:
        return None
    match = re.search(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}", bio)
    return match.group(0) if match else None


def get_instagram_email(ig_handle):
    if not ig_handle:
        return None
    try:
        url = "https://instagram-api-fast-reliable-data-scraper.p.rapidapi.com/profile"
        headers = {
            "x-rapidapi-key":  "a931159602mshc052a773bf091ecp14c985jsn4eebab3ee251",
            "x-rapidapi-host": "instagram-api-fast-reliable-data-scraper.p.rapidapi.com",
        }
        resp = requests.get(url, headers=headers, params={"username": ig_handle}, timeout=10)
        if resp.status_code != 200:
            return {"email": None, "error": f"HTTP {resp.status_code}"}

        data = resp.json()
        bio     = data.get("biography") or data.get("bio") or ""
        email   = extract_email_from_bio(bio)
        ext_url = data.get("external_url") or data.get("website") or ""
        if not email and ext_url:
            email = extract_email_from_bio(ext_url)

        return {
            "email":      email,
            "full_name":  data.get("full_name") or data.get("fullName"),
            "bio":        bio,
            "followers":  data.get("followers") or data.get("follower_count"),
        }
    except Exception as e:
        logger.warning(f"Error fetching Instagram for {ig_handle}: {e}")
        return None


def get_instagram_data_cached(genius_id: int, name: str, ig_handle: str) -> dict:
    """
    Return Instagram data for an artist, using the DB cache.
    Re-fetches only if the entry is older than 6 months or missing.
    Attaches a `from_cache` bool for the frontend to display.
    """
    cached = cache_get(genius_id)

    if cached is not None:
        # Build the same shape as a live fetch result
        ig_data = {
            "email":      cached["instagram_email"],
            "bio":        cached["instagram_bio"],
            "full_name":  cached["instagram_full_name"],
            "followers":  cached["instagram_followers"],
            "from_cache": True,
        }
        if cached["instagram_error"]:
            ig_data["error"] = cached["instagram_error"]
        return ig_data

    # Not cached (or expired) → fetch live
    ig_data = get_instagram_email(ig_handle) or {}
    ig_data["from_cache"] = False
    cache_set(genius_id, name, ig_handle, ig_data)
    return ig_data


# ── ROUTES ───────────────────────────────────────────────────────────────────

@app.route("/api/search", methods=["GET"])
def search_album():
    album  = request.args.get("album", "").strip()
    artist = request.args.get("artist", "").strip()
    if not album:
        return jsonify({"error": "album parameter required"}), 400
    try:
        results = genius_search_album(album, artist)
        return jsonify({"results": results})
    except Exception as e:
        logger.error(f"Search error: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/album/<int:album_id>/credits", methods=["GET"])
def get_album_credits(album_id):
    try:
        tracks      = get_album_tracks(album_id)
        all_credits = {}

        for track_obj in tracks:
            song       = track_obj["song"]
            song_id    = song["id"]
            song_title = song["title"]
            logger.info(f"Fetching credits for: {song_title} (id={song_id})")
            song_credits = get_song_credits(song_id)

            for name, info in song_credits.items():
                if name not in all_credits:
                    all_credits[name] = {
                        **info,
                        "songs":            [],
                        "instagram_handle": None,
                        "instagram_data":   None,
                    }
                all_credits[name]["songs"].append({"title": song_title, "roles": info["roles"]})
                for role in info["roles"]:
                    if role not in all_credits[name]["roles"]:
                        all_credits[name]["roles"].append(role)
            time.sleep(0.5)

        for name, credit in all_credits.items():
            genius_id = credit.get("genius_id")
            if genius_id:
                # Always get IG handle from Genius (fast, no cache needed)
                ig_handle = get_artist_instagram(genius_id)
                credit["instagram_handle"] = ig_handle

                if ig_handle:
                    logger.info(f"Found IG: @{ig_handle} — checking cache…")
                    credit["instagram_data"] = get_instagram_data_cached(genius_id, name, ig_handle)
                else:
                    # Still cache the "no IG" result so we don't re-hit Genius next time
                    cache_set(genius_id, name, None, {})

            time.sleep(0.3)

        return jsonify({"credits": list(all_credits.values())})

    except Exception as e:
        logger.error(f"Credits error: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500


@app.route("/api/health", methods=["GET"])
def health():
    # Count cached artists
    with get_db() as conn:
        total_cached = conn.execute("SELECT COUNT(*) FROM artist_cache").fetchone()[0]
        fresh_cached = conn.execute(
            "SELECT COUNT(*) FROM artist_cache WHERE last_updated > ?",
            (time.time() - CACHE_TTL_SECONDS,)
        ).fetchone()[0]

    return jsonify({
        "status":            "ok",
        "instaloader":       INSTALOADER_AVAILABLE,
        "genius_token_set":  GENIUS_ACCESS_TOKEN != "YOUR_GENIUS_TOKEN_HERE",
        "cache": {
            "total_artists": total_cached,
            "fresh_artists": fresh_cached,
            "db_path":       DB_PATH,
            "ttl_days":      180,
        },
    })


@app.route("/api/cache/stats", methods=["GET"])
def cache_stats():
    with get_db() as conn:
        rows = conn.execute("""
            SELECT
                COUNT(*) as total,
                SUM(CASE WHEN last_updated > ? THEN 1 ELSE 0 END) as fresh,
                SUM(CASE WHEN instagram_email IS NOT NULL THEN 1 ELSE 0 END) as with_email,
                SUM(CASE WHEN instagram_handle IS NOT NULL THEN 1 ELSE 0 END) as with_ig
            FROM artist_cache
        """, (time.time() - CACHE_TTL_SECONDS,)).fetchone()
    return jsonify(dict(rows))


if __name__ == "__main__":
    init_db()
    app.run(debug=True, port=5000)
