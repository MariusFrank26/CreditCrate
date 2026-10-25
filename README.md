# CreditCrate — Album Production Lookup

Search an album → see every producer, engineer and mixer → find their Instagram → get their public contact email.

## Setup

### 1. Genius API token
1. Create an app at https://genius.com/api-clients (name and URL can be anything).
2. Copy the **Client Access Token**.

### 2. Environment file
Create `.env` in the repo root (it is git-ignored):

```env
GENIUS_ACCESS_TOKEN=your_token_here
IG_SESSION_USERNAME=your_scraper_ig_username
```

`IG_SESSION_USERNAME` must match the account you log in with in step 3.

Add a random secret for signing sign-in tokens (generate one with `python -c "import secrets; print(secrets.token_hex(32))"`):

```env
AUTH_SECRET=long_random_string
```

Without it the backend still starts, but everyone is signed out on every restart.

### 2b. Create an account
The search tool is only available to signed-in users. Sign-up is closed, so accounts are created on the server:

```bash
cd backend
python create_user.py
```

Running it again for an existing email resets that password.

### 3. Instagram session (one time)
Instagram needs a logged-in session for reliable profile lookups.

```bash
cd backend
pip install -r requirements.txt
python login.py
```

Enter the scraper account's password when prompted. It is never stored; only the session file is saved to `~/.config/instaloader/session-<username>`, and the backend reuses it on every start.

### 4a. Run with Docker (recommended)
```bash
docker compose up -d --build
```
- Backend: http://localhost:5000
- Frontend: http://localhost:8085

The SQLite cache and Instagram session live in Docker volumes, so they survive rebuilds.

### 4b. Run locally without Docker
```bash
cd backend
GENIUS_ACCESS_TOKEN=... IG_SESSION_USERNAME=... python app.py
```
Then serve the frontend: `cd frontend && python -m http.server 8080`, and open http://localhost:8080.

## How it works
1. **Search** — the backend queries the Genius search API.
2. **Pick album** — you choose the right release from the results.
3. **Credits** — for every track the backend:
   - fetches the Genius song page and reads `custom_performances` (producer, engineer, mixer, …),
   - visits each credited person's Genius artist page to find their linked Instagram handle,
   - uses the logged-in Instaloader session to read the profile's public business/contact email. Nothing else is read.
4. **Results** — a table of name, roles, tracks, Instagram and email. Filter by role, copy emails, export CSV.

Results are cached by Genius ID in `creditcrate.db` (`artist_cache` table), so repeat searches never hit Instagram again.

## Instagram lookup notes
- Instagram blocks bots aggressively. One shared session is reused for all requests, which avoids the repeated logins that usually trigger blocks.
- Successful lookups, including "no email set", are cached indefinitely. Failed lookups (rate limit, connection error) are retried after 12 hours.
- A 2–5 s random delay applies only on cache misses.
- Private profiles, non-business accounts and profiles without a contact email show as "not available".

## Project structure
```
CreditCrate/
├── backend/
│   ├── app.py             # Flask API server, Genius + Instagram lookups, SQLite cache
│   ├── accounts.py        # Sign-in tokens, login gate, newsletter sign-ups
│   ├── create_user.py     # Creates or resets a CreditCrate account
│   ├── login.py           # One-time Instagram session setup
│   ├── requirements.txt
│   └── Dockerfile
├── frontend/
│   ├── index.html         # Single-file React app (no build step)
│   └── Dockerfile         # nginx static server
├── docker-compose.yml
└── requirements.txt
```

## API

| Endpoint | Description |
|---|---|
| `POST /api/login` | Sign in with email and password, returns a token |
| `POST /api/newsletter` | Sign up for launch notifications (`{"email": …}`) |
| `GET /api/search?album=…&artist=…` | Search albums on Genius (sign-in required) |
| `GET /api/album/{id}/credits` | All credits for an album (sign-in required) |
| `GET /api/health` | Server and Instaloader status |
