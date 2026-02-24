"""
Album Credits Tool - Backend
Requirements:
    pip install flask flask-cors requests instaloader beautifulsoup4
"""

import os
import re
import time
import json
import logging
from flask import Flask, request, jsonify
from flask_cors import CORS
import requests
from bs4 import BeautifulSoup

try:
    import instaloader
    INSTALOADER_AVAILABLE = True
except ImportError:
    INSTALOADER_AVAILABLE = False

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)
CORS(app)

# ── CONFIG ── Paste your Genius token here:
GENIUS_ACCESS_TOKEN = "DN7-IZJBGU5-I0OkZDPI63hhApYFgi34XwBr5C4L8mkhZD7e4718lUxWs8oJNLdm"
GENIUS_BASE = "https://api.genius.com"
HEADERS = {"Authorization": f"Bearer {GENIUS_ACCESS_TOKEN}"}

BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
    "Connection": "keep-alive",
}


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

    # Credits are directly in the API response
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
        resp = requests.get(
            f"{GENIUS_BASE}/artists/{genius_artist_id}",
            headers=HEADERS,
            timeout=10,
        )
        if resp.status_code != 200:
            return None
        artist_data = resp.json().get("response", {}).get("artist", {})
        return artist_data.get("instagram_name")
    except Exception as e:
        logger.warning(f"Could not get Instagram for artist {genius_artist_id}: {e}")
    return None


_ig_loader = None

def get_instaloader():
    global _ig_loader
    if _ig_loader is None and INSTALOADER_AVAILABLE:
        _ig_loader = instaloader.Instaloader()
    return _ig_loader


def extract_email_from_bio(bio):
    if not bio:
        return None
    match = re.search(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}", bio)
    return match.group(0) if match else None


def get_instagram_email(ig_handle):
    if not ig_handle or not INSTALOADER_AVAILABLE:
        return None
    loader = get_instaloader()
    try:
        profile = instaloader.Profile.from_username(loader.context, ig_handle)
        bio = profile.biography or ""
        email = extract_email_from_bio(bio)
        ext_url = profile.external_url or ""
        if not email and "@" in ext_url:
            email = extract_email_from_bio(ext_url)
        return {"email": email, "full_name": profile.full_name, "bio": bio, "followers": profile.followers, "external_url": ext_url}
    except instaloader.exceptions.ProfileNotExistsException:
        return None
    except instaloader.exceptions.LoginRequiredException:
        return {"error": "Login required", "email": None}
    except Exception as e:
        logger.warning(f"Error fetching Instagram for {ig_handle}: {e}")
        return None


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
            genius_url = credit.get("genius_url")
            if genius_url:
                ig_handle = get_artist_instagram(credit.get("genius_id"))
                credit["instagram_handle"] = ig_handle
                if ig_handle:
                    logger.info(f"Found IG: @{ig_handle}")
                    credit["instagram_data"] = None
                time.sleep(0.3)

        return jsonify({"credits": list(all_credits.values())})

    except Exception as e:
        logger.error(f"Credits error: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500


@app.route("/api/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "instaloader": INSTALOADER_AVAILABLE, "genius_token_set": GENIUS_ACCESS_TOKEN != "YOUR_GENIUS_TOKEN_HERE"})


if __name__ == "__main__":
    app.run(debug=True, port=5000)
