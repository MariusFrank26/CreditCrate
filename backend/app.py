"""
CreditCrate - Backend
Requirements:
    pip install flask flask-cors requests instaloader gunicorn

Einmaliges Setup für Instagram (Instaloader-Session statt RapidAPI):
    python login.py
    -> legt ein Session-File für IG_SESSION_USERNAME an, das app.py danach
       bei jedem Start wiederverwendet (kein erneuter Login pro Request).
"""

import os
import time
import random
import sqlite3
import logging
from flask import Flask, request, jsonify
from flask_cors import CORS
import requests
import instaloader
from instaloader.exceptions import (
    ProfileNotExistsException,
    LoginRequiredException,
    ConnectionException,
    TooManyRequestsException,
    InstaloaderException,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ── CORS ANPASSEN ──
app = Flask(__name__)
# Erlaubt CORS für Ihre lokalen Tests & Ihre Cloudflare-Domains:
CORS(app, origins=["*"])  # Oder spezifisch: ["https://creditcrate.deinedomain.de", "http://localhost:8085"]

# ── TOKENS AUS ENVIRONMENT-VARIABLEN ZIEHEN ──
GENIUS_ACCESS_TOKEN = os.getenv("GENIUS_ACCESS_TOKEN", "fallback_token_falls_leer")
GENIUS_BASE = "https://api.genius.com"
HEADERS = {"Authorization": f"Bearer {GENIUS_ACCESS_TOKEN}"}

# ── INSTAGRAM (Instaloader statt RapidAPI) ──
# Login läuft einmalig über login.py und legt ein Session-File an, das hier wiederverwendet wird.
IG_SESSION_USERNAME = os.getenv("IG_SESSION_USERNAME", "creditcrate.app")
# Wenn ein Fetch fehlschlägt (z.B. rate limit), erst nach X Stunden erneut versuchen.
# Erfolgreiche Treffer (auch email=None, aber Profil existiert) bleiben unbegrenzt im Cache -
# public business emails ändern sich praktisch nie, neu scrapen bringt nichts.
RETRY_FAILED_AFTER_HOURS = 12

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "creditcrate.db")

_ig_loader = None


def genius_search_album(album_name, artist_name=""):
    query = f"{artist_name} {album_name}".strip()
    resp = requests.get(f"{GENIUS_BASE}/search", headers=HEADERS, params={"q": query, "per_page": 20}, timeout=10)
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
                    "id": alb_id,
                    "name": album_info["name"],
                    "artist": song_data["primary_artist"]["name"],
                    "cover_art": album_info.get("cover_art_url"),
                    "songs": [],
                }
        time.sleep(0.3)

    return list(albums_found.values())


def get_album_tracks(album_id):
    tracks = []
    page = 1
    while True:
        resp = requests.get(f"{GENIUS_BASE}/albums/{album_id}/tracks", headers=HEADERS, params={"page": page, "per_page": 50}, timeout=10)
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
                    credits[name] = {"name": name, "roles": [], "genius_url": artist.get("url", ""), "genius_id": artist.get("id")}
                if role and role not in credits[name]["roles"]:
                    credits[name]["roles"].append(role)

    for artist in song_data.get("producer_artists", []):
        name = artist.get("name", "")
        if name:
            if name not in credits:
                credits[name] = {"name": name, "roles": [], "genius_url": artist.get("url", ""), "genius_id": artist.get("id")}
            if "Producer" not in credits[name]["roles"]:
                credits[name]["roles"].append("Producer")

    return credits


def get_artist_instagram(genius_artist_id):
    if not genius_artist_id:
        return None
    try:
        resp = requests.get(f"{GENIUS_BASE}/artists/{genius_artist_id}", headers=HEADERS, timeout=10)
        if resp.status_code != 200:
            return None
        artist_data = resp.json().get("response", {}).get("artist", {})
        return artist_data.get("instagram_name")
    except Exception as e:
        logger.warning(f"Could not get Instagram for artist {genius_artist_id}: {e}")
    return None


def init_cache_db():
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS artist_cache (
                genius_id INTEGER PRIMARY KEY,
                name TEXT,
                instagram_handle TEXT,
                instagram_email TEXT,
                instagram_bio TEXT,
                instagram_full_name TEXT,
                instagram_followers INTEGER,
                instagram_error TEXT,
                last_updated REAL
            )
        """)


init_cache_db()


def get_cached_ig(genius_id):
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM artist_cache WHERE genius_id = ?", (genius_id,)
        ).fetchone()
        return dict(row) if row else None


def store_cached_ig(genius_id, name, handle, email=None, full_name=None, followers=None, error=None):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            INSERT INTO artist_cache
                (genius_id, name, instagram_handle, instagram_email,
                 instagram_full_name, instagram_followers, instagram_error, last_updated)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(genius_id) DO UPDATE SET
                name=excluded.name,
                instagram_handle=excluded.instagram_handle,
                instagram_email=excluded.instagram_email,
                instagram_full_name=excluded.instagram_full_name,
                instagram_followers=excluded.instagram_followers,
                instagram_error=excluded.instagram_error,
                last_updated=excluded.last_updated
        """, (genius_id, name, handle, email, full_name, followers, error, time.time()))


def get_instaloader_context():
    """Ein einziger, wiederverwendeter eingeloggter Instaloader-Context für den ganzen Prozess -
    kein Re-Login pro Request (das würde Instagram sofort auffallen)."""
    global _ig_loader
    if _ig_loader is None:
        loader = instaloader.Instaloader(
            download_pictures=False,
            download_videos=False,
            download_video_thumbnails=False,
            download_geotags=False,
            download_comments=False,
            save_metadata=False,
            compress_json=False,
            quiet=True,
        )
        try:
            loader.load_session_from_file(IG_SESSION_USERNAME)
            logger.info(f"Instagram-Session für @{IG_SESSION_USERNAME} geladen")
        except FileNotFoundError:
            logger.warning(
                "Kein Instagram-Session-File gefunden. Einmalig 'python login.py' ausführen, "
                "um dich mit dem Scraper-Account einzuloggen und die Session zu speichern."
            )
        _ig_loader = loader
    return _ig_loader


def fetch_instagram_business_email(ig_handle):
    """Holt AUSSCHLIESSLICH die public business email (Contact-Button-Feld) eines Profils.
    Kein Bio-Parsing, kein Download von Posts/Followern - ein einzelner, leichtgewichtiger
    Profil-Request pro Aufruf."""
    loader = get_instaloader_context()
    time.sleep(random.uniform(2.0, 5.0))  # kein Burst-Pattern, sieht "menschlicher" aus
    try:
        profile = instaloader.Profile.from_username(loader.context, ig_handle)
        return {
            "email": getattr(profile, "business_email", None),
            "full_name": profile.full_name,
            "followers": profile.followers,
            "error": None,
        }
    except ProfileNotExistsException:
        return {"email": None, "full_name": None, "followers": None, "error": "profile_not_found"}
    except LoginRequiredException:
        return {"email": None, "full_name": None, "followers": None, "error": "login_required"}
    except TooManyRequestsException:
        return {"email": None, "full_name": None, "followers": None, "error": "rate_limited"}
    except ConnectionException as e:
        return {"email": None, "full_name": None, "followers": None, "error": f"connection_error: {e}"}
    except InstaloaderException as e:
        return {"email": None, "full_name": None, "followers": None, "error": f"instaloader_error: {e}"}


def get_instagram_email(genius_id, name, ig_handle):
    """Aggressiv gecacht: Instagram wird nur angefragt, wenn für diesen Artist noch
    nichts Brauchbares im Cache liegt. Treffer (auch 'kein Business-Email hinterlegt')
    bleiben dauerhaft gecacht; nur gescheiterte Versuche (rate limit etc.) werden nach
    RETRY_FAILED_AFTER_HOURS erneut versucht."""
    if not ig_handle:
        return None

    cached = get_cached_ig(genius_id)
    if cached:
        error_is_stale = (
            cached["instagram_error"]
            and (time.time() - cached["last_updated"]) > RETRY_FAILED_AFTER_HOURS * 3600
        )
        if not error_is_stale:
            logger.info(f"Cache-Hit für @{ig_handle} (genius_id={genius_id})")
            return {
                "email": cached["instagram_email"],
                "full_name": cached["instagram_full_name"],
                "followers": cached["instagram_followers"],
            }

    logger.info(f"Cache-Miss - hole Instagram-Profil @{ig_handle}")
    result = fetch_instagram_business_email(ig_handle)
    store_cached_ig(
        genius_id, name, ig_handle,
        email=result["email"], full_name=result["full_name"],
        followers=result["followers"], error=result["error"],
    )
    return {"email": result["email"], "full_name": result["full_name"], "followers": result["followers"]}


@app.route("/api/search", methods=["GET"])
def search_album():
    album = request.args.get("album", "").strip()
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
        tracks = get_album_tracks(album_id)
        all_credits = {}

        for track_obj in tracks:
            song = track_obj["song"]
            song_id = song["id"]
            song_title = song["title"]
            logger.info(f"Fetching credits for: {song_title} (id={song_id})")
            song_credits = get_song_credits(song_id)

            for name, info in song_credits.items():
                if name not in all_credits:
                    all_credits[name] = {**info, "songs": [], "instagram_handle": None, "instagram_data": None}
                all_credits[name]["songs"].append({"title": song_title, "roles": info["roles"]})
                for role in info["roles"]:
                    if role not in all_credits[name]["roles"]:
                        all_credits[name]["roles"].append(role)
            time.sleep(0.5)

        for name, credit in all_credits.items():
            genius_id = credit.get("genius_id")
            if genius_id:
                ig_handle = get_artist_instagram(genius_id)
                credit["instagram_handle"] = ig_handle
                if ig_handle:
                    credit["instagram_data"] = get_instagram_email(genius_id, name, ig_handle)

        return jsonify({"credits": list(all_credits.values())})

    except Exception as e:
        logger.error(f"Credits error: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500


@app.route("/api/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"})

if __name__ == "__main__":
    # WICHTIG: host='0.0.0.0' ist nötig, damit Docker Anfragen annimmt
    app.run(host="0.0.0.0", port=5000)
