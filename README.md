CREDITS — Album Production Lookup Tool

Search any album → see every producer & engineer → find their Instagram → get their email.

Setup
1. Get a Genius API Token (free)
Go to https://genius.com/api-clients
Create a new app (name/URL can be anything)
Copy your Client Access Token
2. Instagram Session (Instaloader)

Instagram requires a logged-in session for reliable profile lookups.

cd backend
Run python login.py once, enter your scraper account's username/password
This saves a session file to ~/.config/instaloader/session-<username> — the backend reuses it on every start, no repeated logins
3. Backend (Python)
bash
cd backend
pip install -r requirements.txt

# Set your Genius token
export GENIUS_ACCESS_TOKEN="your_token_here"
# Must match the username you logged in with in login.py
export IG_SESSION_USERNAME="your_scraper_ig_username"

python app.py

Server runs at http://localhost:5000

4. Frontend

Just open frontend/index.html in a browser. No build step needed.

If you want to serve it properly:

bash
cd frontend
python -m http.server 8080

Then open http://localhost:8080

How It Works
Search — You type an album name (+ optional artist). The backend hits the Genius search API.
Pick Album — You select the right album from results.
Credits Fetch — For every track, the backend:
Fetches the Genius song page and extracts custom_performances (producer, engineer, mixer, etc.)
Visits each credited person's Genius artist page to find their linked Instagram handle
Uses instaloader (logged-in session) to fetch the profile's public business/contact email — the same email shown behind the "Contact" button on the profile, nothing else is read
Results are cached by genius_id in creditcrate.db (artist_cache table), so a repeat search never re-hits Instagram
Results — You see a table with name, roles, tracks, Instagram link, and email. Export to CSV anytime.
Instagram Lookup Notes

Instagram is aggressive about blocking bots. How this project handles it:

Only the public business email is fetched — one lightweight profile request per handle, no bio scraping, no follower/post crawling
One shared logged-in session is reused across all requests (login.py sets it up once, app.py loads it on startup) — no repeated logins, which is what usually triggers blocks
Aggressive caching: any successful lookup (even "no email set") is cached indefinitely in creditcrate.db; only failed attempts (rate limit, connection error) are retried, and only after 12h
A small random delay (2–5s) is added only on an actual cache miss
Some profiles are private, not a Business/Creator account, or simply have no contact email set — those show as "not available"
Folder Structure
album-credits-tool/
├── backend/
│   ├── app.py            # Flask API server
│   └── requirements.txt
└── frontend/
    └── index.html        # Single-file React web app
API Endpoints
Endpoint	Description
GET /api/search?album=...&artist=...	Search for albums on Genius
GET /api/album/{id}/credits	Get all credits for an album
GET /api/health	Check server + instaloader status
