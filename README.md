# CREDITS — Album Production Lookup Tool

Search any album → see every producer & engineer → find their Instagram → get their email.

---

## Setup

### 1. Get a Genius API Token (free)
1. Go to https://genius.com/api-clients
2. Create a new app (name/URL can be anything)
3. Copy your **Client Access Token**

### 2. Backend (Python)

```bash
cd backend
pip install -r requirements.txt

# Set your Genius token
export GENIUS_ACCESS_TOKEN="your_token_here"

python app.py
```

Server runs at `http://localhost:5000`

### 3. Frontend

Just open `frontend/index.html` in a browser. No build step needed.

> If you want to serve it properly:
> ```bash
> cd frontend
> python -m http.server 8080
> ```
> Then open http://localhost:8080

---

## How It Works

1. **Search** — You type an album name (+ optional artist). The backend hits the Genius search API.
2. **Pick Album** — You select the right album from results.
3. **Credits Fetch** — For every track, the backend:
   - Fetches the Genius song page and extracts `custom_performances` (producer, engineer, mixer, etc.)
   - Visits each credited person's Genius artist page to find their linked Instagram handle
   - Uses **instaloader** to fetch the Instagram bio and extract any email address
4. **Results** — You see a table with name, roles, tracks, Instagram link, and email. Export to CSV anytime.

---

## Instagram Scraping Notes

Instagram is aggressive about blocking bots. Tips:
- **Run instaloader slowly** — the backend already adds delays between requests
- If you get rate-limited, wait ~15 min before trying again
- For better reliability, you can log instaloader into an Instagram account:

```python
# Add to app.py before app.run()
loader = instaloader.Instaloader()
loader.login("your_ig_username", "your_ig_password")
```

Or use the CLI:
```bash
instaloader --login your_ig_username
```

- Emails are only found if the person has put their email directly in their Instagram bio
- Some profiles are private or don't have emails in their bio — those will show "not in bio"

---

## Folder Structure

```
album-credits-tool/
├── backend/
│   ├── app.py            # Flask API server
│   └── requirements.txt
└── frontend/
    └── index.html        # Single-file React web app
```

---

## API Endpoints

| Endpoint | Description |
|---|---|
| `GET /api/search?album=...&artist=...` | Search for albums on Genius |
| `GET /api/album/{id}/credits` | Get all credits for an album |
| `GET /api/health` | Check server + instaloader status |
