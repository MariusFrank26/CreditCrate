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

## Contacts, packs and campaigns (mailing)
Save contacts (CSV import or by hand), keep sample and beat pack links (Dropbox or Google Drive), and email a campaign to a filtered list with the pack link.

Sign-in is required for these tools. The title bar switches between Credits, Contacts, Packs, Campaigns and Deal Desk.

Safety rules, all enforced on the server:
- **Preview first.** A send request without `"confirm": true` only reports how many people it would reach.
- **Sending uses each user's own Gmail account** with an app password (2-Step Verification, then App passwords in the Google account). The password is stored encrypted with `CREDENTIAL_KEY`, a Fernet key set on the server. Generate one with `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`.
- **Daily cap:** 500 emails per Gmail account in any 24 hours. Sends run in the background, a few seconds apart, and the Campaigns app shows progress. `MAIL_SENDER_ADDRESS` (your postal address) is required, by anti-spam law.
- **Every email** includes an unsubscribe link and a `List-Unsubscribe` header. Unsubscribed addresses are never mailed again, and re-importing a CSV does not clear that.
- **Open and click tracking is ours, not Mailtrack's.** Each email carries a 1×1 image and rewritten links. Both use signed tokens, so the click redirect only goes to links we wrote into an email. Set `track: false` on a campaign to turn it off. Opens are estimates, because some mail apps load images automatically.
- **Each person gets a campaign once.** Re-sending skips anyone already sent to.
- **Packs are links only.** Files stay in Dropbox or Google Drive. Only `dropbox.com` and `drive.google.com`/`docs.google.com` https links are accepted. Nothing is uploaded to the server.

Endpoints (sign-in required unless noted): `POST /api/contacts`, `POST /api/contacts/import` (CSV with an `email` column; optional `name, instagram, roles, tags`), `GET /api/contacts?role=&tag=`, `POST /api/packs` (`name`, `url`), `GET /api/packs`, `DELETE /api/packs/{id}`, `POST /api/campaigns`, `GET /api/campaigns`, `POST /api/campaigns/{id}/send` (`role`, `tag`, `confirm`), and public `GET /api/unsubscribe`, `GET /api/t/o/{token}/px.gif` (open pixel) and `GET /api/t/c/{token}` (click redirect). `GET /api/campaigns/{id}/stats` returns sent, opened and clicked counts plus clicks per link.

Sends run inside the request, about 0.6 s per email. Lists are capped at 500 per request, so large lists need a background worker later.

## Deal Desk (local tool)
Deal Desk builds split sheets, producer agreements and invoices. It lives at `/tools/dealdesk/` and is deliberately kept apart from the library:
- All data stays in the browser's localStorage. Nothing is sent to the backend, and it never reads or writes contacts, packs or campaigns.
- It makes no API calls. The only external request is the Google Fonts stylesheet.

## Project structure
```
CreditCrate/
├── backend/
│   ├── app.py             # Flask API server, Genius + Instagram lookups, SQLite cache
│   ├── accounts.py        # Sign-in tokens, login gate, newsletter sign-ups
│   ├── mailing.py         # Contacts, CSV import, packs, campaigns, unsubscribe, sending
│   ├── create_user.py     # Creates or resets a CreditCrate account
│   ├── login.py           # One-time Instagram session setup
│   ├── requirements.txt
│   └── Dockerfile
├── frontend/
│   ├── index.html         # CreditCrate search app (single-file React, no build step)
│   ├── tools/
│   │   └── dealdesk/
│   │       └── index.html # Deal Desk: split sheets, agreements, invoices. Local-only, no server calls
│   └── Dockerfile         # nginx static server, serves the whole frontend folder
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
