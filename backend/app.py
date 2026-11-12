"""
CreditCrate - Backend
Requirements:
    pip install flask flask-cors requests instaloader gunicorn

Einmaliges Setup für Instagram (Instaloader-Session statt RapidAPI):
    python login.py
    -> legt ein Session-File für IG_SESSION_USERNAME an, das app.py danach
       bei jedem Start wiederverwendet (kein erneuter Login pro Request).
"""

from functools import wraps
import os
import re
import time
import random
import secrets
import sqlite3
import json
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
import mailing
import admin
import billing

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


ALBUM_CACHE_DAYS = 7


def _artist_songs(artist_id, limit=100):
    """The artist's most popular songs (Genius allows listing songs, but not listing albums)."""
    songs = []
    for page in (1, 2):
        resp = requests.get(f"{GENIUS_BASE}/artists/{artist_id}/songs", headers=HEADERS,
                            params={"per_page": 50, "page": page, "sort": "popularity"}, timeout=10)
        if resp.status_code != 200:
            break
        batch = resp.json()["response"]["songs"]
        songs += batch
        if len(batch) < 50:
            break
    return songs[:limit]


def _song_album(song_id):
    resp = requests.get(f"{GENIUS_BASE}/songs/{song_id}", headers=HEADERS, timeout=10)
    if resp.status_code != 200:
        return None
    song = resp.json()["response"]["song"]
    album = song.get("album")
    if not album:
        return None
    return {
        "id": album["id"],
        "name": album["name"],
        "artist": (song.get("primary_artist") or {}).get("name", ""),
        "cover_art": album.get("cover_art_url"),
        "songs": [],
    }


def _cached_albums(artist_id):
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute("SELECT albums, updated_at FROM artist_albums WHERE artist_id = ?", (artist_id,)).fetchone()
    if row and time.time() - row[1] < ALBUM_CACHE_DAYS * 86400:
        return json.loads(row[0])
    return None


def artist_album_list(artist_id):
    """Every album the artist's popular songs appear on. Cached for a week per artist."""
    cached = _cached_albums(artist_id)
    if cached is not None:
        return cached
    albums = {}
    songs = _artist_songs(artist_id)
    with ThreadPoolExecutor(max_workers=4) as pool:
        for album in pool.map(_song_album, [song["id"] for song in songs]):
            if album and album["id"] not in albums:
                albums[album["id"]] = album
    result = list(albums.values())
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("INSERT OR REPLACE INTO artist_albums (artist_id, albums, updated_at) VALUES (?, ?, ?)",
                     (artist_id, json.dumps(result), time.time()))
    return result


def _rank_albums(albums, wanted):
    wanted = wanted.strip().lower()
    def key(album):
        name = album["name"].strip().lower()
        if not wanted:
            return (0, name)
        if name == wanted:
            return (0, name)
        if name.startswith(wanted):
            return (1, name)
        return (2, name)
    return sorted((a for a in albums if not wanted or wanted in a["name"].lower()), key=key)


def find_albums(query):
    """One search box: an album, an artist, or both ("Iceman Drake")."""
    query = query.strip()
    resp = requests.get(f"{GENIUS_BASE}/search", headers=HEADERS, params={"q": query, "per_page": 10}, timeout=10)
    resp.raise_for_status()
    hits = resp.json()["response"]["hits"]
    lowered = query.lower()

    # An artist named in the query wins, longest name first ("A$AP Rocky" before "Rocky")
    artists = {}
    for hit in hits:
        primary = hit["result"].get("primary_artist") or {}
        if primary.get("id"):
            artists.setdefault(primary["id"], primary.get("name", ""))
    named = sorted(((aid, name) for aid, name in artists.items() if name and name.lower() in lowered),
                   key=lambda item: -len(item[1]))
    if named:
        artist_id, artist_name = named[0]
        wanted = lowered.replace(artist_name.lower(), "")
    else:
        # Otherwise use the artist of the songs whose title matches the query
        titled = [hit["result"].get("primary_artist") or {} for hit in hits
                  if lowered and lowered in hit["result"].get("title", "").lower()]
        if not titled:
            return []
        artist_id = max(((p.get("id"), sum(1 for t in titled if t.get("id") == p.get("id"))) for p in titled),
                        key=lambda item: item[1])[0]
        wanted = lowered
    return _rank_albums(artist_album_list(artist_id), wanted)


def genius_search_album(album_name, artist_name=""):
    return find_albums(f"{album_name} {artist_name}".strip())


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


def init_history_db():
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS search_history (
                user_email TEXT,
                album_id INTEGER,
                name TEXT,
                artist TEXT,
                cover_art TEXT,
                credits TEXT,
                searched_at REAL,
                PRIMARY KEY (user_email, album_id)
            )
        """)


init_history_db()


def init_album_cache_db():
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS artist_albums (
                artist_id INTEGER PRIMARY KEY,
                albums TEXT,
                updated_at REAL
            )
        """)


init_album_cache_db()
mailing.register(app, DB_PATH)
admin.register(app)
billing.register(app, DB_PATH)
import auth_flow
auth_flow.register(app)
import inbox
inbox.register(app)


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
    # Every attempt counts, right or wrong, so passwords cannot be guessed at speed
    if accounts.limited(f"login-email:{email}", 10, 900) or accounts.limited(f"login-ip:{accounts.client_ip()}", 30, 900):
        return jsonify({"error": "Too many sign-in attempts. Try again in 15 minutes."}), 429
    if not accounts.verify_login(email, data.get("password") or ""):
        return jsonify({"error": "Wrong email or password."}), 401
    if not accounts.is_verified(email):
        return jsonify({"error": "Check your email and open the link to verify your account.", "code": "unverified"}), 403
    return jsonify({"token": accounts.issue_token(email)})


@app.route("/api/newsletter", methods=["POST"])
def newsletter():
    data = request.get_json(silent=True) or {}
    email = accounts.normalize_email(data.get("email"))
    if not accounts.is_valid_email(email):
        return jsonify({"error": "Enter a valid email address."}), 400
    if accounts.limited(f"newsletter-ip:{accounts.client_ip()}", 5, 3600):
        return jsonify({"error": "Too many sign-ups from this connection. Try again later."}), 429
    accounts.add_newsletter_signup(email)
    return jsonify({"ok": True})


def _search_limit(view):
    """Each signed-in account can run 60 searches an hour: every search costs a Genius API call."""
    @wraps(view)
    def wrapper(*args, **kwargs):
        if accounts.limited(f"search:{accounts.current_email()}", 60, 3600):
            return jsonify({"error": "You have run many searches in the last hour. Try again later."}), 429
        return view(*args, **kwargs)
    return wrapper


@app.route("/api/search", methods=["GET"])
@accounts.login_required
@_search_limit
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


def build_album_credits(album_id, on_progress=None):
    """All credits for an album, with the public emails found. on_progress(done, total) is optional."""
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

    total = len(all_credits)
    done = 0
    progress_lock = threading.Lock()
    if on_progress:
        on_progress(0, total)

    def look_up(credit):
        nonlocal done
        genius_id = credit.get("genius_id")
        try:
            profile = get_artist_profile(genius_id)
            if profile:
                credit["instagram_handle"] = profile["instagram"]
                credit["instagram_data"] = get_contact_email(genius_id, credit["name"], profile)
        finally:
            with progress_lock:
                done += 1
                if on_progress:
                    on_progress(done, total)

    with ThreadPoolExecutor(max_workers=LOOKUP_WORKERS) as pool:
        list(pool.map(look_up, all_credits.values()))

    return list(all_credits.values())


# Credits lookups run in the background, so the browser can poll instead of waiting minutes
# for one request (proxies cut requests that take more than about a minute or two).
_jobs = {}
_jobs_lock = threading.Lock()


def _start_job(owner, target, *args):
    job_id = secrets.token_urlsafe(16)
    with _jobs_lock:
        _jobs[job_id] = {"owner": owner, "status": "running", "done": 0, "total": 0,
                         "credits": None, "result": None, "error": None, "created": time.time()}

    def run():
        try:
            value = target(*args)
            with _jobs_lock:
                _jobs[job_id].update(status="done", result=value)
        except Exception as e:
            logger.error(f"Job {job_id} failed: {e}", exc_info=True)
            with _jobs_lock:
                _jobs[job_id].update(status="error", error=str(e))

    threading.Thread(target=run, daemon=True).start()
    return job_id


@app.route("/api/inbox/start", methods=["POST"])
@accounts.login_required
def start_inbox_scan():
    try:
        address, password = inbox._connected_account()
    except inbox.gmail.CredentialError as e:
        return jsonify({"error": str(e)}), 409
    if not address:
        return jsonify({"error": "Connect your Gmail account in Settings first."}), 409
    return jsonify({"job": _start_job(accounts.current_email(), inbox.list_messages, address, password)})


@app.route("/api/search/start", methods=["POST"])
@accounts.login_required
@_search_limit
def start_search():
    query = ((request.get_json(silent=True) or {}).get("q") or "").strip()
    if not query:
        return jsonify({"error": "Type an album, an artist, or both."}), 400
    return jsonify({"job": _start_job(accounts.current_email(), find_albums, query)})


def _run_credits_job(job_id, album_id):
    def progress(done, total):
        with _jobs_lock:
            _jobs[job_id].update(done=done, total=total)

    try:
        credits = build_album_credits(album_id, progress)
        with _jobs_lock:
            _jobs[job_id].update(status="done", credits=credits)
    except Exception as e:
        logger.error(f"Credits job {job_id} failed: {e}", exc_info=True)
        with _jobs_lock:
            _jobs[job_id].update(status="error", error=str(e))


@app.route("/api/album/<int:album_id>/credits/start", methods=["POST"])
@accounts.login_required
@_search_limit
def start_album_credits(album_id):
    job_id = secrets.token_urlsafe(16)
    now = time.time()
    with _jobs_lock:
        # forget finished jobs after an hour
        for old_id in [k for k, v in _jobs.items() if v["status"] != "running" and now - v["created"] > 3600]:
            del _jobs[old_id]
        _jobs[job_id] = {"owner": accounts.current_email(), "status": "running", "done": 0, "total": 0,
                         "credits": None, "error": None, "created": now}
    threading.Thread(target=_run_credits_job, args=(job_id, album_id), daemon=True).start()
    return jsonify({"job": job_id})


@app.route("/api/jobs/<job_id>", methods=["GET"])
@accounts.login_required
def get_job(job_id):
    with _jobs_lock:
        job = _jobs.get(job_id)
        if not job or job["owner"] != accounts.current_email():
            return jsonify({"error": "Job not found."}), 404
        return jsonify({key: job.get(key) for key in ("status", "done", "total", "credits", "result", "error")})


@app.route("/api/album/<int:album_id>/credits", methods=["GET"])
@accounts.login_required
def get_album_credits(album_id):
    try:
        return jsonify({"credits": build_album_credits(album_id)})
    except Exception as e:
        logger.error(f"Credits error: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500


@app.route("/api/history", methods=["GET"])
@accounts.login_required
def list_history():
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute(
            "SELECT album_id, name, artist, cover_art, credits, searched_at FROM search_history "
            "WHERE user_email = ? ORDER BY searched_at DESC LIMIT 100",
            (accounts.current_email(),),
        ).fetchall()
    searches = []
    for album_id, name, artist, cover_art, credits, searched_at in rows:
        people = json.loads(credits)
        searches.append({
            "id": album_id,
            "name": name,
            "artist": artist,
            "cover_art": cover_art,
            "searched_at": searched_at,
            "people": len(people),
            "emails": sum(1 for c in people if (c.get("instagram_data") or {}).get("email")),
        })
    return jsonify({"searches": searches})


@app.route("/api/history", methods=["POST"])
@accounts.login_required
def save_history():
    data = request.get_json(silent=True) or {}
    album_id = data.get("id")
    credits = data.get("credits")
    if not isinstance(album_id, int) or not isinstance(credits, list):
        return jsonify({"error": "Send the album id and its credits."}), 400
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO search_history "
            "(user_email, album_id, name, artist, cover_art, credits, searched_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (accounts.current_email(), album_id, str(data.get("name", ""))[:200],
             str(data.get("artist", ""))[:200], data.get("cover_art"), json.dumps(credits), time.time()),
        )
    return jsonify({"ok": True})


@app.route("/api/history/<int:album_id>", methods=["GET"])
@accounts.login_required
def get_history(album_id):
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(
            "SELECT name, artist, cover_art, credits FROM search_history WHERE user_email = ? AND album_id = ?",
            (accounts.current_email(), album_id),
        ).fetchone()
    if not row:
        return jsonify({"error": "Search not found."}), 404
    name, artist, cover_art, credits = row
    return jsonify({"id": album_id, "name": name, "artist": artist, "cover_art": cover_art, "credits": json.loads(credits)})


# The landing page's preview shows the Iceman cover and producer photos, taken from Genius
# through the API (the same token and search the Credits app uses). Cached for an hour.
def _norm(text):
    return re.sub(r"[^a-z0-9]", "", (text or "").lower())


# Producers shown in the landing preview. Their photos come from Genius: first from the album's own
# track credits, then from a search for the name.
ICEMAN_ALBUM_ID = 1395699  # Genius album "ICEMAN"
DEMO_PRODUCERS = [
    ("Hanz (NOR)", ["Hanz (NOR)"], {"hanznor"}),
    ("Sem0r", ["Sem0r"], {"sem0r"}),
    ("Gordo", ["Gordo"], {"gordo"}),
]
_demo_cache = {"at": 0.0, "data": None}


def _genius_get(path, params=None):
    """A Genius request that waits and retries once if Genius asks it to slow down."""
    for attempt in range(2):
        resp = requests.get(f"{GENIUS_BASE}{path}", headers=HEADERS, params=params, timeout=10)
        if resp.status_code == 429 and attempt == 0:
            time.sleep(2)
            continue
        resp.raise_for_status()
        return resp.json()["response"]
    raise RuntimeError("Genius is busy")


def _album_producer_photos():
    """Music producers credited on the album's tracks, with their Genius photo where there is one."""
    photos = {}
    for track in _genius_get(f"/albums/{ICEMAN_ALBUM_ID}/tracks", {"per_page": 50})["tracks"]:
        song = _genius_get(f"/songs/{track['song']['id']}")["song"]
        for perf in song.get("custom_performances") or []:
            label = perf.get("label") or ""
            if "Producer" not in label or "Video" in label:
                continue
            for artist in perf.get("artists") or []:
                if artist.get("image_url") and artist.get("name"):
                    photos.setdefault(artist["name"], artist["image_url"])
    return photos


def _producer_photo(queries, names):
    """The photo of the Genius artist with one of these names. Tries the search result first,
    then the song's own artist record, which always carries the picture."""
    for query in queries:
        for hit in _genius_get("/search", {"q": query, "per_page": 10})["hits"]:
            primary = hit["result"].get("primary_artist") or {}
            if _norm(primary.get("name")) not in names:
                continue
            if primary.get("image_url"):
                return primary["image_url"]
            song = _genius_get(f"/songs/{hit['result']['id']}")["song"]
            image = (song.get("primary_artist") or {}).get("image_url")
            if image:
                return image
    return None


DEMO_ARTWORK_FILE = os.path.join(DB_DIR, "demo_artwork.json")  # the picture addresses only, not the pictures


def _load_demo_artwork():
    try:
        with open(DEMO_ARTWORK_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


@app.route("/api/demo/artwork", methods=["GET"])
def demo_artwork():
    # Once a complete answer is saved, it is reused and Genius is not asked again
    saved = _load_demo_artwork()
    if saved is not None:
        return jsonify(saved)
    data = {"album": None, "producers": []}
    problems = []
    try:
        album = next((a for a in find_albums("Iceman Drake") if a.get("name", "").lower() == "iceman"), None)
        if album and album.get("cover_art"):
            data["album"] = {"name": album["name"], "artist": album.get("artist", ""), "image_url": album["cover_art"]}
        else:
            # Fall back to the artwork of one of its songs, which is the album cover for album tracks
            for hit in _genius_get("/search", {"q": "Make Them Cry Drake", "per_page": 10})["hits"]:
                song = hit["result"]
                if (song.get("primary_artist") or {}).get("name", "").lower() == "drake" and song.get("song_art_image_url"):
                    data["album"] = {"name": "Iceman", "artist": "Drake", "image_url": song["song_art_image_url"]}
                    break
    except Exception as e:
        problems.append(f"album: {e}")
    try:
        album_photos = _album_producer_photos()
    except Exception as e:
        album_photos = {}
        problems.append(f"album credits: {e}")
    for display, queries, names in DEMO_PRODUCERS:
        image = album_photos.get(display)
        if not image:
            try:
                image = _producer_photo(queries, names)
            except Exception as e:
                problems.append(f"{display}: {e}")
        data["producers"].append({"name": display, "image_url": image})
    data["missing"] = [p["name"] for p in data["producers"] if not p["image_url"]]
    if problems:
        logger.warning(f"Demo artwork incomplete: {problems}")
        data["problems"] = problems  # visible at /api/demo/artwork to see what Genius did not return
    else:
        try:
            os.makedirs(DB_DIR, exist_ok=True)
            with open(DEMO_ARTWORK_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f)  # only a complete answer is kept
        except OSError as e:
            logger.warning(f"Demo artwork could not be saved: {e}")
    return jsonify(data)


@app.route("/api/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"})

if __name__ == "__main__":
    # WICHTIG: host='0.0.0.0' ist nötig, damit Docker Anfragen annimmt
    app.run(host="0.0.0.0", port=5000)
