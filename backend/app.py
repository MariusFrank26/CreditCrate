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
import re
import time
import random
import sqlite3
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
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

import contacts
import accounts

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
# Kein Treffer bei den öffentlichen Quellen: nach X Tagen erneut suchen (Leute ergänzen ihre Links)
RETRY_NOT_FOUND_AFTER_DAYS = 30
# Instaloader (eingeloggt, rate-limitiert) nur als letzte Quelle, abschaltbar mit INSTAGRAM_FALLBACK=0
INSTAGRAM_FALLBACK = os.getenv("INSTAGRAM_FALLBACK", "1") != "0"
LOOKUP_WORKERS = 4

DB_DIR = os.getenv("CACHE_DB_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), "data"))
os.makedirs(DB_DIR, exist_ok=True)
DB_PATH = os.path.join(DB_DIR, "creditcrate.db")

_ig_loader = None
_ig_lock = threading.Lock()  # Instaloader-Requests nie parallel - eine Session, ein Request zur Zeit


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


def get_artist_profile(genius_artist_id):
    """Instagram/Twitter-Handle und Bio (Text + Links) eines Genius-Artists."""
    if not genius_artist_id:
        return None
    try:
        resp = requests.get(
            f"{GENIUS_BASE}/artists/{genius_artist_id}", headers=HEADERS,
            params={"text_format": "html,plain"}, timeout=10,
        )
        if resp.status_code != 200:
            return None
        artist = resp.json().get("response", {}).get("artist", {})
        description = artist.get("description") or {}
        return {
            "instagram": artist.get("instagram_name") or None,
            "twitter": artist.get("twitter_name") or None,
            "bio": description.get("plain", ""),
            "bio_links": re.findall(r'href="(https?://[^"]+)"', description.get("html", "")),
        }
    except Exception as e:
        logger.warning(f"Could not get Genius profile for artist {genius_artist_id}: {e}")
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
        existing = {row[1] for row in conn.execute("PRAGMA table_info(artist_cache)")}
        for column in ("contact_source", "contact_url"):
            if column not in existing:
                conn.execute(f"ALTER TABLE artist_cache ADD COLUMN {column} TEXT")


init_cache_db()
accounts.init_db()


def get_cached_ig(genius_id):
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM artist_cache WHERE genius_id = ?", (genius_id,)
        ).fetchone()
        return dict(row) if row else None


def store_cached_ig(genius_id, name, handle, email=None, full_name=None, followers=None, error=None,
                    source=None, source_url=None):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            INSERT INTO artist_cache
                (genius_id, name, instagram_handle, instagram_email,
                 instagram_full_name, instagram_followers, instagram_error, last_updated,
                 contact_source, contact_url)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(genius_id) DO UPDATE SET
                name=excluded.name,
                instagram_handle=excluded.instagram_handle,
                instagram_email=excluded.instagram_email,
                instagram_full_name=excluded.instagram_full_name,
                instagram_followers=excluded.instagram_followers,
                instagram_error=excluded.instagram_error,
                last_updated=excluded.last_updated,
                contact_source=excluded.contact_source,
                contact_url=excluded.contact_url
        """, (genius_id, name, handle, email, full_name, followers, error, time.time(), source, source_url))


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
    with _ig_lock:
        return _fetch_instagram_business_email(ig_handle)


def _fetch_instagram_business_email(ig_handle):
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


def get_contact_email(genius_id, name, profile):
    """Sucht eine öffentlich hinterlegte E-Mail: erst die öffentlichen Quellen aus
    contacts.py (Genius-Bio, Link-in-Bio, SoundCloud, YouTube, Website), dann - falls
    aktiviert - Instaloader als letzte Quelle.

    Gecacht: Treffer bleiben dauerhaft. 'Nichts gefunden' wird nach
    RETRY_NOT_FOUND_AFTER_DAYS erneut gesucht, Fehler (rate limit etc.) nach
    RETRY_FAILED_AFTER_HOURS."""
    ig_handle, twitter = profile.get("instagram"), profile.get("twitter")
    if not (ig_handle or twitter or profile.get("bio")):
        return None

    cached = get_cached_ig(genius_id)
    if cached:
        age = time.time() - cached["last_updated"]
        if cached["instagram_email"]:
            fresh = True
        elif cached["instagram_error"]:
            fresh = age < RETRY_FAILED_AFTER_HOURS * 3600
        else:
            fresh = age < RETRY_NOT_FOUND_AFTER_DAYS * 86400
        if fresh:
            logger.info(f"Cache-Hit für {name} (genius_id={genius_id})")
            return {
                "email": cached["instagram_email"],
                "full_name": cached["instagram_full_name"],
                "followers": cached["instagram_followers"],
                "source": cached.get("contact_source"),
                "source_url": cached.get("contact_url"),
                "error": cached["instagram_error"],
            }

    logger.info(f"Cache-Miss - suche Kontakt für {name}")
    found = contacts.find_contact(name, ig_handle, twitter, profile.get("bio", ""), profile.get("bio_links", []))
    if found:
        store_cached_ig(genius_id, name, ig_handle, email=found["email"],
                        source=found["source"], source_url=found["source_url"])
        return {"email": found["email"], "full_name": None, "followers": None,
                "source": found["source"], "source_url": found["source_url"], "error": None}

    if INSTAGRAM_FALLBACK and ig_handle:
        result = fetch_instagram_business_email(ig_handle)
        source = "Instagram" if result["email"] else None
        store_cached_ig(
            genius_id, name, ig_handle,
            email=result["email"], full_name=result["full_name"],
            followers=result["followers"], error=result["error"], source=source,
        )
        return {"email": result["email"], "full_name": result["full_name"], "followers": result["followers"],
                "source": source, "source_url": None, "error": result["error"]}

    store_cached_ig(genius_id, name, ig_handle)
    return {"email": None, "full_name": None, "followers": None, "source": None, "source_url": None, "error": None}


@app.route("/api/login", methods=["POST"])
def login():
    data = request.get_json(silent=True) or {}
    email = accounts.normalize_email(data.get("email"))
    if not accounts.verify_login(email, data.get("password") or ""):
        return jsonify({"error": "Wrong email or password."}), 401
    return jsonify({"token": accounts.issue_token(email)})


@app.route("/api/newsletter", methods=["POST"])
def newsletter():
    data = request.get_json(silent=True) or {}
    email = accounts.normalize_email(data.get("email"))
    if not accounts.is_valid_email(email):
        return jsonify({"error": "Enter a valid email address."}), 400
    accounts.add_newsletter_signup(email)
    return jsonify({"ok": True})


@app.route("/api/search", methods=["GET"])
@accounts.login_required
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
@accounts.login_required
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

        def look_up(credit):
            genius_id = credit.get("genius_id")
            profile = get_artist_profile(genius_id)
            if profile:
                credit["instagram_handle"] = profile["instagram"]
                credit["instagram_data"] = get_contact_email(genius_id, credit["name"], profile)

        with ThreadPoolExecutor(max_workers=LOOKUP_WORKERS) as pool:
            list(pool.map(look_up, all_credits.values()))

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
