"""
Public contact finder: looks for an email address a person has published
themselves, without logging in anywhere.

Sources, in order of trust:
1. The Genius artist bio (text, and websites linked from it)
2. Link-in-bio pages (Linktree and similar) at the person's known handles
3. SoundCloud and YouTube profiles at the same handles

A page found by guessing a handle only counts if it links back to the
person's known Instagram or Twitter handle. Handles are not unique across
sites, so without that check we would pick up strangers' emails.

Websites linked from a verified page (or from the Genius bio) are checked
for an email on their homepage and contact page.
"""

import re
import html
import logging
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import unquote, urljoin, urlparse

import requests

logger = logging.getLogger(__name__)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"
)
TIMEOUT = 8
MAX_PAGE_BYTES = 2_000_000

LINK_IN_BIO_SITES = [
    ("Linktree", "https://linktr.ee/{}"),
    ("solo.to", "https://solo.to/{}"),
    ("bio.link", "https://bio.link/{}"),
    ("hoo.be", "https://hoo.be/{}"),
    ("lnk.bio", "https://lnk.bio/{}"),
    ("Stan", "https://stan.store/{}"),
    ("Beacons", "https://beacons.ai/{}"),
]
PROFILE_SITES = [
    ("SoundCloud", "https://soundcloud.com/{}"),
    ("YouTube", "https://www.youtube.com/@{}/about"),
]

# Links to these are never someone's personal website
PLATFORM_DOMAINS = {
    "instagram.com", "twitter.com", "x.com", "facebook.com", "fb.com", "tiktok.com",
    "youtube.com", "youtu.be", "spotify.com", "apple.com", "music.apple.com",
    "soundcloud.com", "linktr.ee", "solo.to", "bio.link", "hoo.be", "lnk.bio",
    "stan.store", "beacons.ai", "genius.com", "twitch.tv", "snapchat.com", "threads.net",
    "onlyfans.com", "patreon.com", "cash.app", "paypal.com", "venmo.com", "beatstars.com",
    "airbit.com", "tidal.com", "deezer.com", "amazon.com", "bandcamp.com", "discord.gg",
    "discord.com", "linkedin.com", "pinterest.com", "google.com", "goo.gl", "bit.ly",
    "linkin.bio", "wikipedia.org", "shopify.com", "squarespace.com", "wix.com",
    # Music platforms and smart-link services: their pages list the platform's own emails
    "audiomack.com", "pandora.com", "napster.com", "boomplay.com", "shazam.com", "anghami.com",
    "bandsintown.com", "songkick.com", "ticketmaster.com", "livenation.com", "eventbrite.com",
    "seated.com", "lnk.to", "ffm.to", "smarturl.it", "found.ee", "fanlink.to", "show.co",
    "orcd.co", "hypeddit.com", "toneden.io", "distrokid.com", "untitled.stream", "lnkfi.re",
    "feature.fm", "push.fm", "vevo.com", "allmusic.com", "discogs.com", "imdb.com",
    "medium.com", "substack.com", "reddit.com", "telegram.me", "t.me", "whatsapp.com",
    "wa.me", "vimeo.com", "dropbox.com", "drive.google.com", "gofundme.com", "ko-fi.com",
}

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,24}")
# "name [at] domain [dot] com", "name (at) domain.com"
OBFUSCATED_RE = re.compile(
    r"([A-Za-z0-9._%+-]+)\s*[\[(]\s*at\s*[\])]\s*([A-Za-z0-9-]+(?:\s*(?:[\[(]\s*dot\s*[\])]|\.)\s*[A-Za-z0-9-]+)+)",
    re.I,
)
JUNK_EMAIL_PARTS = (
    "example.", "example@", "sentry", "wixpress", "domain.com", "email.com", "yourname", "name@",
    "@2x", "@3x", "noreply", "no-reply", "privacy@", "abuse@", "support@linktr",
    "@linktr.ee", "@soundcloud", "@youtube", "@google", "@sentry", "@w3.org",
    "enable-javascript", "mysite.", "@email.", "test@", "user@", "support@", "help@",
)
JUNK_EMAIL_SUFFIXES = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".js", ".css")
FREEMAIL_DOMAINS = {
    "gmail.com", "googlemail.com", "yahoo.com", "outlook.com", "hotmail.com", "live.com",
    "icloud.com", "me.com", "aol.com", "proton.me", "protonmail.com", "gmx.de", "gmx.net",
    "web.de", "mail.com", "yandex.com",
}
HREF_RE = re.compile(r"""(?:href=|"url":\s*)["'](https?://[^"'\s<>]+)""", re.I)

_session = requests.Session()
_session.headers.update({"User-Agent": USER_AGENT, "Accept-Language": "en-US,en;q=0.9"})
# What YouTube's "Reject all" button sets; without it EU servers get the consent page
_session.cookies.set("SOCS", "CAI", domain=".youtube.com")


def fetch(url):
    """Returns (final_url, text) for a 200 response, else None."""
    try:
        resp = _session.get(url, timeout=TIMEOUT, allow_redirects=True, stream=True)
        if resp.status_code != 200:
            return None
        if "text/html" not in resp.headers.get("Content-Type", "text/html"):
            return None
        body = resp.raw.read(MAX_PAGE_BYTES, decode_content=True)
        return resp.url, body.decode(resp.encoding or "utf-8", errors="replace")
    except requests.RequestException as e:
        logger.debug(f"fetch failed {url}: {e}")
        return None


def extract_emails(text):
    """Emails in text, including mailto links and '[at]/[dot]' obfuscation, junk removed."""
    # Page JSON keeps line breaks and some characters escaped ("\n", "<"); without
    # decoding them an email runs into the next word ("me@gmail.comhttp")
    text = re.sub(r"\\u([0-9a-fA-F]{4})", lambda m: chr(int(m.group(1), 16)), text)
    text = re.sub(r"\\[nrt]", " ", text)
    text = html.unescape(unquote(text))
    found = []
    candidates = EMAIL_RE.findall(text)
    for user, domain in OBFUSCATED_RE.findall(text):
        domain = re.sub(r"\s*[\[(]\s*dot\s*[\])]\s*", ".", domain, flags=re.I).replace(" ", "")
        candidates.append(f"{user}@{domain}")
    for email in candidates:
        email = email.strip(".").lower()
        if any(part in email for part in JUNK_EMAIL_PARTS) or email.endswith(JUNK_EMAIL_SUFFIXES):
            continue
        tld = email.rsplit(".", 1)[-1]
        # A TLD glued to the next word ("comhttp", "combooking") means the text had no separator
        if len(tld) > 6 or any(tld.startswith(t) and tld != t for t in ("com", "net", "org")):
            continue
        if email not in found:
            found.append(email)
    return found


def links_in(text):
    return [html.unescape(u).rstrip("\\") for u in HREF_RE.findall(text.replace("\\/", "/"))]


def registered_domain(url):
    host = urlparse(url).netloc.lower().split(":")[0]
    if host.startswith("www."):
        host = host[4:]
    parts = host.split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else host


def is_personal_site(url):
    host = urlparse(url).netloc.lower()
    domain = registered_domain(url)
    return bool(host) and domain not in PLATFORM_DOMAINS and not any(host.endswith("." + d) for d in PLATFORM_DOMAINS)


def links_back(text, instagram=None, twitter=None):
    """True if the page links to the exact Instagram or Twitter handle we know."""
    text = unquote(text.replace("\\/", "/")).lower()
    checks = []
    if instagram:
        checks.append(r"instagram\.com/%s(?![a-z0-9._])" % re.escape(instagram.lower()))
    if twitter:
        checks.append(r"(?:twitter|x)\.com/%s(?![a-z0-9_])" % re.escape(twitter.lower()))
    return any(re.search(c, text) for c in checks)


def email_domain(email):
    return email.rsplit("@", 1)[-1]


def name_tokens(*names):
    """Lowercase alphanumeric chunks of a name or handle, 3+ chars ("Dez Wright" -> dez, wright)."""
    tokens = set()
    for n in names:
        if n:
            tokens.update(t for t in re.split(r"[^a-z0-9]+", n.lower()) if len(t) >= 3)
            squashed = re.sub(r"[^a-z0-9]", "", n.lower())
            if len(squashed) >= 3:
                tokens.add(squashed)
    return tokens


def check_website(url, allow_freemail):
    """Emails on a personal website's homepage, then its contact page.

    Only addresses at the site's own domain count (a news article lists its
    journalists, a label site its staff). Free-mail addresses count only when
    the site itself came from a verified profile."""
    root = f"{urlparse(url).scheme}://{urlparse(url).netloc}"
    site_domain = registered_domain(url)
    for page in dict.fromkeys((url, root + "/", urljoin(root, "/contact"))):
        result = fetch(page)
        if not result:
            continue
        emails = [
            e for e in extract_emails(result[1])
            if email_domain(e).endswith(site_domain) or (allow_freemail and email_domain(e) in FREEMAIL_DOMAINS)
        ]
        if emails:
            return emails, result[0]
    return [], None


def _candidate_pages(handles):
    pages = []
    for handle in handles:
        for source, pattern in LINK_IN_BIO_SITES + PROFILE_SITES:
            pages.append((source, pattern.format(handle)))
    return pages


def _check_page(source, url, instagram, twitter):
    result = fetch(url)
    if not result:
        return None
    final_url, text = result
    if not links_back(text, instagram, twitter):
        return None
    return {"source": source, "url": final_url, "emails": extract_emails(text),
            "sites": [u for u in links_in(text) if is_personal_site(u)]}


def find_contact(name, instagram=None, twitter=None, genius_bio="", genius_links=()):
    """
    Look for an email the person has published. Returns
    {"email", "emails", "source", "source_url"} or None if nothing was found.
    """
    # 1. Genius bio
    emails = extract_emails(genius_bio or "")
    if emails:
        return {"email": emails[0], "emails": emails, "source": "Genius", "source_url": None}

    # 2 + 3. Profiles at the person's handles, verified by a link back
    handles = []
    for h in (instagram, twitter):
        if h and h.lower() not in [x.lower() for x in handles]:
            handles.append(h)
    verified = []
    if handles and (instagram or twitter):
        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = [pool.submit(_check_page, s, u, instagram, twitter) for s, u in _candidate_pages(handles)]
            verified = [f.result() for f in futures if f.result()]

    for page in verified:
        if page["emails"]:
            return {"email": page["emails"][0], "emails": page["emails"],
                    "source": page["source"], "source_url": page["url"]}

    # 4. Personal websites. From a verified profile they are the person's own links.
    # Genius bio links are mostly citations (news articles), so one only counts if
    # its domain carries the person's name or handle ("alexlustig.com").
    tokens = name_tokens(name, instagram, twitter)
    sites = [(u, True) for page in verified for u in page["sites"]]
    sites += [
        (u, False) for u in genius_links
        if is_personal_site(u) and any(t in registered_domain(u).split(".")[0] for t in tokens)
    ]
    seen = set()
    for site, from_profile in sites:
        domain = registered_domain(site)
        if domain in seen:
            continue
        seen.add(domain)
        emails, found_at = check_website(site, allow_freemail=from_profile)
        if emails:
            return {"email": emails[0], "emails": emails, "source": "Website", "source_url": found_at}
        if len(seen) >= 3:
            break

    return None
