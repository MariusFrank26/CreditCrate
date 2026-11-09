import os, subprocess
HERE = os.path.dirname(os.path.abspath(__file__))
EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"

LOGO = '''<svg viewBox="0 0 64 64" xmlns="http://www.w3.org/2000/svg">
  <path d="M47.56 47.56 A22 22 0 1 1 47.56 16.44" fill="none" stroke="#ffffff" stroke-width="5" stroke-linecap="round"/>
  <circle cx="32" cy="32" r="5" fill="#f3d58b"/>
</svg>'''

BASE_CSS = """
*{box-sizing:border-box;margin:0;padding:0}
html,body{width:%(w)spx;height:%(h)spx;overflow:hidden;background:#08090a;color:#d0d6e0;font-family:'Inter',system-ui,-apple-system,'Segoe UI',Roboto,sans-serif;letter-spacing:-0.011em}
.grid{position:absolute;inset:0;background-image:linear-gradient(rgba(208,214,224,.035) 1px,transparent 1px),linear-gradient(90deg,rgba(208,214,224,.035) 1px,transparent 1px);background-size:%(grid)spx %(grid)spx;mask-image:radial-gradient(ellipse 80%% 70%% at 50%% 30%%,#000 20%%,transparent 80%%)}
.eyebrow{font-family:'JetBrains Mono',ui-monospace,Consolas,monospace;text-transform:uppercase;letter-spacing:.12em;color:#62666d}
.accent{color:#f3d58b}
.brand{display:flex;align-items:center;gap:%(brand_gap)spx}
.brand svg{width:%(logo)spx;height:%(logo)spx}
.brand span{font-weight:600;letter-spacing:-0.02em;color:#ffffff}
.card{background:#0f1011;border:1px solid #23252a;border-radius:%(radius)spx}
.pill{display:inline-block;border:1px solid #383b3f;border-radius:999px;color:#d0d6e0}
"""

def page(name, w, h, body, extra_css="", **sizes):
    grid = sizes.pop("grid", 48)
    css = BASE_CSS % dict(w=w, h=h, grid=grid, brand_gap=sizes.pop("brand_gap", 18),
                         logo=sizes.pop("logo", 56), radius=sizes.pop("radius", 24))
    html = f"""<!doctype html><html><head><meta charset="utf-8"><style>{css}{extra_css}</style></head>
<body><div class="grid"></div>{body}</body></html>"""
    path = os.path.join(HERE, "src", f"{name}.html")
    open(path, "w", encoding="utf-8").write(html)
    return path

def brand(size=56, text=True, gap=18):
    t = '<span style="font-size:%dpx">CreditCrate</span>' % int(size * 0.5) if text else ''
    sized = LOGO.replace("<svg", '<svg width="%d" height="%d"' % (size, size), 1)
    return '<div class="brand" style="gap:%dpx">' % gap + sized + t + '</div>'

def render(path, w, h, out):
    url = "file:///" + path.replace("\\", "/")
    args = [EDGE, "--headless=new", "--disable-gpu", "--hide-scrollbars", f"--window-size={w},{h}",
            f"--screenshot={out}", url]
    subprocess.run(args, capture_output=True, timeout=120)
    return os.path.exists(out)

SIZES = {}
def make(name, w, h, body, extra_css="", **kw):
    path = page(name, w, h, body, extra_css, **kw)
    out = os.path.join(HERE, f"{name}.png")
    ok = render(path, w, h, out)
    SIZES[name] = (w, h, ok)
    print(name, w, h, "ok" if ok else "FAILED")

# 1. Profile picture, 1080 x 1080 (Instagram crops it to a circle, so the logo sits well inside)
BIG_LOGO = LOGO.replace("<svg", '<svg width="520" height="520"', 1)
make("01-profile-picture", 1080, 1080,
     '<div style="position:absolute;inset:0;display:flex;align-items:center;justify-content:center">'
     '<div style="width:520px;height:520px">' + BIG_LOGO + '</div></div>',
     grid=64, radius=0)

# 2. Instagram post, 1080 x 1080: the tool, explained
make("02-post-find-the-producers", 1080, 1080,
     '<div style="position:absolute;left:84px;top:84px;right:84px;bottom:84px;display:flex;flex-direction:column;justify-content:space-between">'
     '<div class="eyebrow" style="font-size:26px">Credits · Find who made the record</div>'
     '<div><div style="font-size:112px;font-weight:600;line-height:1.02;letter-spacing:-0.035em;color:#fff">'
     'Who made<br>the record?<br><span class="accent">Find out.</span></div>'
     '<div style="margin-top:44px;font-size:34px;line-height:1.45;color:#8a8f98;max-width:820px">'
     'Search any album and see every producer, engineer and mixer behind it, with their public contact.</div></div>'
     f'<div style="display:flex;align-items:center;justify-content:space-between">{brand(70, gap=20)}'
     '<span class="eyebrow" style="font-size:24px">creditcrate.app</span></div></div>',
     grid=56, radius=0)

# 3. Instagram story, 1080 x 1920
make("03-story-samples", 1080, 1920,
     '<div style="position:absolute;left:90px;right:90px;top:150px;bottom:150px;display:flex;flex-direction:column;justify-content:space-between">'
     '<div>' + brand(72, gap=20) + '</div>'
     '<div><p class="eyebrow" style="font-size:28px">Everything you do outside your DAW</p>'
     '<div style="margin-top:28px;font-size:124px;font-weight:600;line-height:1.0;letter-spacing:-0.04em;color:#fff">'
     'Your samples,<br><span class="accent">in one place.</span></div>'
     '<div style="margin-top:50px;display:flex;flex-direction:column;gap:22px">'
     + ''.join(f'<div class="card" style="padding:28px 34px;font-size:36px;color:#d0d6e0;border-radius:22px">{t}</div>' for t in [
         'Find every producer on an album', 'Keep your contacts and packs together',
         'Send from your own Gmail', 'See who opened your email']) +
     '</div></div>'
     '<div class="eyebrow" style="font-size:30px;color:#d0d6e0">creditcrate.app · Free for 7 days</div></div>',
     grid=64, radius=22)

# 4. Banner for X and LinkedIn, 1500 x 500
make("04-banner", 1500, 500,
     '<div style="position:absolute;left:110px;right:110px;top:0;bottom:0;display:flex;align-items:center;justify-content:space-between">'
     f'<div>{brand(84, gap=22)}<p style="margin-top:26px;font-size:46px;font-weight:600;letter-spacing:-0.02em;color:#fff">'
     'You already make the music.</p><p style="margin-top:8px;font-size:30px;color:#8a8f98">Let\'s get it to the people who need to hear it.</p></div>'
     '<span class="pill eyebrow" style="font-size:24px;padding:14px 26px">creditcrate.app</span></div>',
     grid=50, radius=0)

# 5. YouTube thumbnail, 1280 x 720
make("05-youtube-thumbnail", 1280, 720,
     '<div style="position:absolute;left:80px;top:80px;right:80px;bottom:80px;display:flex;flex-direction:column;justify-content:space-between">'
     '<div class="eyebrow" style="font-size:24px">Walkthrough</div>'
     '<div style="font-size:104px;font-weight:600;line-height:1.02;letter-spacing:-0.035em;color:#fff">'
     'From album to<br><span class="accent">inbox.</span></div>'
     f'<div style="display:flex;align-items:center;gap:20px">{brand(64, gap=18)}</div></div>',
     grid=48, radius=0)

# 6. Link preview image for the site, 1200 x 630
make("06-link-preview", 1200, 630,
     '<div style="position:absolute;left:80px;right:80px;top:70px;bottom:70px;display:flex;flex-direction:column;justify-content:space-between">'
     f'{brand(72, gap=20)}'
     '<div><div style="font-size:70px;font-weight:600;line-height:1.05;letter-spacing:-0.03em;color:#fff">'
     'Everything you do outside your DAW.</div>'
     '<p style="margin-top:22px;font-size:30px;color:#8a8f98">Find the producers, send your samples, and track what gets opened.</p></div>'
     '<div class="eyebrow" style="font-size:22px">creditcrate.app</div></div>',
     grid=48, radius=0)

# 7. Instagram post, 1080 x 1080: all six tools in one image
TOOLS = [
    ("01", "Credits", "Find who made any album."),
    ("02", "Contacts", "Keep your list in one place."),
    ("03", "Packs", "Your samples, saved as links."),
    ("04", "Inbox", "Samples from your Gmail, in one place."),
    ("05", "Campaigns", "Send from your own Gmail. See who opens."),
    ("06", "Deal Desk", "Split sheets and invoices."),
]
cards = "".join(
    f'<div class="card" style="padding:22px 26px;display:flex;flex-direction:column;gap:10px;border-radius:20px">'
    f'<span class="eyebrow" style="font-size:20px">{n}</span>'
    f'<span style="font-size:36px;font-weight:600;letter-spacing:-0.02em;color:#fff">{t}</span>'
    f'<span style="font-size:24px;line-height:1.35;color:#8a8f98">{d}</span></div>'
    for n, t, d in TOOLS
)
make("07-post-six-tools", 1080, 1080,
     '<div style="position:absolute;left:72px;right:72px;top:72px;bottom:72px;display:flex;flex-direction:column;justify-content:space-between">'
     '<div><p class="eyebrow" style="font-size:24px">Everything you do outside your DAW</p>'
     '<div style="margin-top:14px;font-size:76px;font-weight:600;line-height:1.02;letter-spacing:-0.035em;color:#fff">'
     'Six tools.<br><span class="accent">One hub.</span></div></div>'
     '<div style="display:grid;grid-template-columns:1fr 1fr;gap:16px">' + cards + '</div>'
     '<div style="display:flex;align-items:center;justify-content:space-between">' + brand(60, gap=16) +
     '<span class="eyebrow" style="font-size:22px">creditcrate.app</span></div></div>',
     grid=56, radius=20)

print(SIZES)
