"""Wrap the page fragments into standalone documents.

The artifact platform wraps a page fragment in a document skeleton at publish time.
Files opened straight from a folder have no such wrapper, so this adds the same one
(doctype, charset, viewport, small reset) and moves the leading title/meta/link/style
block into <head>. Output goes to ./dist next to this script.
"""
import pathlib
import re

HERE = pathlib.Path(__file__).resolve().parent
DIST = HERE / "dist"

# ---- fill these in; anything left empty is simply not shown on the site ----
LINKEDIN = "https://www.linkedin.com/in/fasterflash"   # e.g. https://www.linkedin.com/in/your-handle
GITHUB = "https://github.com/FasterFlash/reflex"     # e.g. https://github.com/your-handle  (or the repo URL)
INSTAGRAM = "https://www.instagram.com/hmmmm__nothinglikethat/"
EMAIL = ""      # public contact + feedback address
INSPIRATION_LEDE = "I paid for YouTube Premium for months and couldn&rsquo;t point to one thing that got better. So I built the thing I wished it had."
INSPIRATION = [  # (label, title, note)
    ("The itch", "Premium, with nothing to show.", "Months of paying, and not one benefit I could actually see as a viewer."),
    ("The benchmark", "Spotify kept shipping.", "I paid for it for three years and something new landed almost every quarter. Wrapped is the one everybody shares."),
    ("The idea", "A Wrapped for YouTube. Deeper.", "Same flex, more analytics, in a safe place that protects your data. For the app everybody already uses."),
    ("Next up", "YouTube Music.", "It&rsquo;s coming to the analysis soon, for music only. Stay tuned."),
]

SKELETON = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<style>*,*::before,*::after{box-sizing:border-box}:root{color-scheme:light;padding-top:env(safe-area-inset-top,0px);padding-bottom:env(safe-area-inset-bottom,0px)}body{margin:0;font:14px system-ui,sans-serif}img{max-width:100%}[hidden]{display:none!important}</style>
@@HEAD@@
</head>
<body>
@@BODY@@
</body>
</html>
"""


BIGFOOT_CSS = """
/* ---------- big footer (added by build.py, same on every page) ---------- */
.bigfoot{position:relative;z-index:60;isolation:isolate;overflow:hidden;background:var(--ink,#10141A);color:var(--paper,#F5F7F9);padding:clamp(3rem,6vw,5rem) max(1.5rem,env(safe-area-inset-right,0px)) 2rem max(1.5rem,env(safe-area-inset-left,0px))}
.bigfoot::before{content:"";position:absolute;z-index:-1;inset:-40% -10% auto 35%;height:140%;background:radial-gradient(50% 50% at 70% 20%,rgba(84,112,255,.32),rgba(84,112,255,0) 70%);pointer-events:none}
.bf-in{max-width:68rem;margin-inline:auto;display:flex;flex-direction:column;gap:clamp(1.5rem,3.5vw,2.75rem)}
.bf-word{margin:0;font-family:var(--font-display,"Instrument Serif",Georgia,serif);font-weight:400;font-size:calc(min(100vw - 3rem, 68rem) * 1.15 / (var(--n) + 1));line-height:.88;padding-bottom:.14em;letter-spacing:-.035em;color:#E9EDF1;white-space:nowrap;overflow:visible}
.bf-word i{font-style:normal;color:#5470FF}
.bf-row{display:flex;flex-wrap:wrap;align-items:flex-end;justify-content:space-between;gap:1.5rem 3rem;padding-top:1.5rem;border-top:1px solid rgba(245,247,249,.16)}
.bf-def{margin:0;max-width:34rem;font-family:var(--font-mono,"IBM Plex Mono",monospace);font-size:.78rem;line-height:1.6;letter-spacing:.04em;color:#B4BDC8}
.bf-def em{font-style:italic;color:#8D97A3;margin-right:.35rem}
.bf-nav{display:flex;flex-wrap:wrap;gap:.5rem 1.5rem;font-family:var(--font-ui,"IBM Plex Sans",system-ui,sans-serif);font-size:.9rem}
.bf-nav a{color:#D7DCE3;text-decoration:none;border-bottom:1px solid transparent;padding-bottom:1px;transition:color .2s,border-color .2s}
.bf-nav a:hover,.bf-nav a:focus-visible{color:#fff;border-color:#fff;outline:none}
.bf-note{margin:0;font-family:var(--font-mono,"IBM Plex Mono",monospace);font-size:.68rem;letter-spacing:.05em;color:#8D97A3}
@media (max-width:560px){.bf-row{flex-direction:column;align-items:flex-start}}
@media (min-width:1500px){.bf-in{max-width:80rem}.bf-word{font-size:calc(min(100vw - 3rem, 80rem) * 1.15 / (var(--n) + 1))}}
/* ---------- slim floating header (added by build.py) ---------- */
.top{position:sticky;top:.75rem;z-index:40;padding:.5rem .6rem .5rem 1.25rem;border:1px solid rgba(200,207,216,.9);border-radius:999px;background:rgba(245,247,249,.74);-webkit-backdrop-filter:saturate(1.4) blur(14px);backdrop-filter:saturate(1.4) blur(14px);box-shadow:0 1px 0 rgba(255,255,255,.7) inset,0 8px 24px -14px rgba(16,20,26,.28)}
.top .word{font-size:1.45rem;line-height:1;letter-spacing:-.01em;text-decoration:none}
.top .word::after{content:".";color:var(--accent,#2B4BF2)}
.top .nav a:not(.go):hover{color:var(--ink)}
.top .nav a.go{border-radius:999px;padding:.5rem 1.2rem}
.page{overflow:clip}
@media (max-width:640px){.top{border-radius:18px;padding:.5rem .75rem;top:.5rem;gap:.35rem}.top .nav{width:100%;flex-wrap:nowrap;overflow-x:auto;gap:.5rem;font-size:.74rem;justify-content:space-between;scrollbar-width:none}.top .nav::-webkit-scrollbar{display:none}.top .nav a.go{padding:.35rem .8rem}}
"""

FOOT_RE = re.compile(r'\s*<footer class="foot">(.*?)</footer>', re.S)
DEFN_RE = re.compile(r'\s*<p class="defn">(.*?)</p>', re.S)
DEF_RE = re.compile(r'<b>(.*?)</b>\s*<i>\((.*?)\)</i>\s*(?:&mdash;|\u2014)\s*(.*?)(?:<span|</p>|$)', re.S)
def _nav() -> str:
    links = [("inspiration.html", "Inspiration", ""), ("feedback.html", "Feedback", ""), ("contact.html", "Contact", ""), ("privacy.html", "Privacy", "")]
    out = "".join(f'<a href="{h}">{t}</a>' for h, t, _ in links)
    if LINKEDIN:
        out += f'<a href="{LINKEDIN}" target="_blank" rel="noopener noreferrer">LinkedIn &#8599;</a>'
    if GITHUB:
        out += f'<a href="{GITHUB}" target="_blank" rel="noopener noreferrer">GitHub &#8599;</a>'
    if INSTAGRAM:
        out += f'<a href="{INSTAGRAM}" target="_blank" rel="noopener noreferrer">Instagram &#8599;</a>'
    return out


def bigfoot(src: str) -> str:
    """Pull the page's one-line definition footer out and rebuild it as a full-width dark footer."""
    m = FOOT_RE.search(src) or DEFN_RE.search(src)
    if not m:
        return src
    d = DEF_RE.search(m.group(1))
    if not d:
        return src
    word, pos, text = (x.strip() for x in d.groups())
    n = max(len(re.sub(r"<[^>]+>|&\w+;", "", word)), 6)
    html = (
        '<footer class="bigfoot">\n  <div class="bf-in">\n'
        f'    <p class="bf-word" style="--n:{n}" aria-label="{word}">{word}<i>.</i></p>\n'
        '    <div class="bf-row">\n'
        f'      <p class="bf-def"><em>({pos})</em>{text}</p>\n'
        f'      <div><nav class="bf-nav" aria-label="Footer">{_nav()}</nav>\n'
        '      <p class="bf-note" style="margin-top:1rem">Made by Tarun &middot; Not affiliated with YouTube or Google.</p></div>\n'
        '    </div>\n  </div>\n</footer>\n'
    )
    src = src[:m.start()] + src[m.end():]
    for anchor in ('<div class="sheen"', '<div id="tip"'):
        i = src.find(anchor)
        if i != -1:
            src = src[:i] + html + src[i:]
            break
    else:
        src = src + html
    return src.replace("</style>", BIGFOOT_CSS + "</style>", 1)


def wrap(src: str) -> str:
    src = bigfoot(src)
    end = src.index("</style>") + len("</style>")
    return SKELETON.replace("@@HEAD@@", src[:end]).replace("@@BODY@@", src[end:].lstrip("\n"))


def extra_pages() -> dict:
    """Inspiration, Feedback and Contact are small text pages built on About's look."""
    base = (HERE / "about.src.html").read_text(encoding="utf-8")
    head = base[: base.index("</style>")]
    head = re.sub(r"<title>.*?</title>", "<title>Reflex</title>", head, flags=re.S)
    head += """
body{min-height:100vh;display:flex;flex-direction:column}.page{flex:1}
.doc{max-width:46rem;display:flex;flex-direction:column;gap:1rem}
.doc .title{margin:0;font-family:var(--font-display);font-weight:400;font-size:clamp(2.6rem,7vw,5rem);line-height:.98;letter-spacing:-.025em}
.doc .title em{font-style:italic}
.doc .lede{margin:0;max-width:44ch;font-size:1.05rem;line-height:1.55;color:var(--ink-soft)}
.stack{list-style:none;margin:1.25rem 0 0;padding:0;display:flex;flex-direction:column;gap:.75rem}
.stack>li{display:block}
.card{display:grid;grid-template-columns:minmax(0,10rem) minmax(0,1fr) auto;gap:.5rem 1.5rem;align-items:baseline;padding:1.2rem 1.4rem;background:var(--paper);border:1px solid var(--line);border-radius:6px;color:var(--ink);text-decoration:none;transition:background .3s var(--ease),color .3s var(--ease),border-color .3s var(--ease),transform .3s var(--ease)}
.card .k{font-family:var(--font-mono);font-size:.68rem;letter-spacing:.1em;text-transform:uppercase;color:var(--ink-faint)}
.card .v{font-family:var(--font-display);font-size:1.5rem;line-height:1.15;letter-spacing:-.01em}
.card .n{font-size:.88rem;color:var(--ink-soft);line-height:1.5}
.card .go{font-size:1.1rem}
a.card:hover,a.card:focus-visible,.card.open:hover{background:var(--ink);border-color:var(--ink);color:#fff;transform:translateY(-2px);outline:none}
a.card:hover .k,a.card:focus-visible .k{color:#8D97A3}a.card:hover .n,a.card:focus-visible .n,.card.open:hover .n{color:#C3CAD3}
.card.off{opacity:.55}
.doc .aside{margin:1.75rem 0 0;font-family:var(--font-display);font-size:clamp(1.5rem,3vw,2.1rem);line-height:1.15;letter-spacing:-.01em;color:var(--ink)}
.doc .aside em{display:block;font-style:italic;color:var(--ink-soft)}
.fb{display:flex;flex-direction:column;gap:.9rem;margin-top:1rem}
.fb label{font-family:var(--font-mono);font-size:.68rem;letter-spacing:.1em;text-transform:uppercase;color:var(--ink-soft)}
.fb select,.fb textarea,.fb input[type=text]{width:100%;font:inherit;font-size:.95rem;color:var(--ink);background:var(--paper);border:1px solid var(--line);border-radius:6px;padding:.8rem 1rem}
.fb textarea{min-height:9rem;resize:vertical}
.fb select:focus,.fb textarea:focus,.fb input:focus{outline:2px solid var(--accent);outline-offset:2px}
.fb button{align-self:flex-start;font:inherit;font-weight:500;font-size:.9rem;color:var(--paper);background:var(--ink);border:0;border-radius:999px;padding:.8rem 1.5rem;cursor:pointer}
.fb button:disabled{opacity:.45;cursor:not-allowed}
.kinds{border:0;margin:0 0 .5rem;padding:0;display:flex;flex-wrap:wrap;gap:.6rem}
.kinds legend{padding:0;margin-bottom:.7rem;font-family:var(--font-mono);font-size:.68rem;letter-spacing:.1em;text-transform:uppercase;color:var(--ink-soft)}
.kinds legend span{text-transform:none;letter-spacing:0;color:var(--ink-faint)}
.chip{position:relative;cursor:pointer;font-family:var(--font-ui)!important;text-transform:none!important;letter-spacing:0!important}
.chip input{position:absolute;opacity:0;inset:0;margin:0;cursor:pointer}
.chip span{display:block;padding:.7rem 1.1rem;border:1px solid var(--line);border-radius:999px;background:var(--paper);font-size:.9rem;color:var(--ink-soft);transition:background .2s var(--ease),color .2s var(--ease),border-color .2s var(--ease),transform .2s var(--ease)}
.chip:hover span{border-color:var(--ink-soft);color:var(--ink)}
.chip input:checked+span{background:var(--ink);border-color:var(--ink);color:#fff}
.chip input:focus-visible+span{outline:2px solid var(--accent);outline-offset:2px}
.rate{border:0;margin:0 0 .4rem;padding:0}
.rate legend{padding:0;margin-bottom:.7rem;font-family:var(--font-mono);font-size:.68rem;letter-spacing:.1em;text-transform:uppercase;color:var(--ink-soft)}
.rate legend span{text-transform:none;letter-spacing:0;color:var(--ink-faint)}
.stars{display:flex;gap:.35rem}
.fb .stars button{align-self:auto;background:transparent;color:var(--line);border:0;padding:0 .1rem;font-size:2.6rem;line-height:1;border-radius:6px;cursor:pointer;transition:color .15s var(--ease),transform .15s var(--ease)}
.fb .stars button:hover{transform:scale(1.12)}
.fb .stars button.on{color:var(--accent)}
.fb .stars button:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
.rate-note{margin:.5rem 0 0;min-height:1.2em;font-size:.85rem;color:var(--ink-soft)}
.fb.rate-only .kinds,.fb.rate-only .opt-contact{display:none}
.fb-msg{margin:0;min-height:1.2em;font-size:.9rem}.fb-msg.good{color:#1F7A4D}.fb-msg.bad{color:#B3261E}
.fb small{color:var(--ink-faint);font-size:.8rem;line-height:1.5}
@media (max-width:640px){.card{grid-template-columns:1fr}}
</style>"""
    top = (
        '<div class="page">\n  <div class="wrap">\n\n'
        '    <header class="top rise"><a class="word" href="index.html">Reflex</a><nav class="nav" aria-label="Primary">'
        '<a href="about.html">What it is</a><a href="preview.html">Preview</a><a href="how.html">How it works</a>'
        '<a href="privacy.html">Privacy</a><a class="go" href="upload.html">Upload</a></nav></header>\n\n'
    )
    def foot(word, pos, text):
        return (f'\n    <footer class="foot"><p class="foot-def"><b>{word}</b> <i>({pos})</i> &mdash; {text}</p></footer>\n\n  </div>\n</div>\n')

    # ----- inspiration (content comes from the author) -----
    insp_items = INSPIRATION
    insp = "".join(
        f'<li><div class="card"><span class="k">{k}</span><span><span class="v">{v}</span><br><span class="n">{n}</span></span><span></span></div></li>'
        for k, v, n in insp_items
    ) or '<li><div class="card"><span class="k">Soon</span><span class="n">This page is waiting for its content.</span><span></span></div></li>'
    p_insp = head + "\n" + top + (
        '    <section class="doc rise" style="--d:.08s">\n      <h1 class="title">What got me <em>here.</em></h1>\n'
        f'      <p class="lede">{INSPIRATION_LEDE}</p>\n      <ul class="stack">{insp}</ul>\n    </section>\n'
    ) + foot("rise", "v.", "to start from a small itch and build anyway.")

    # ----- contact -----
    def row(k, v, href, note):
        if not href:
            return ""
        ext = ' target="_blank" rel="noopener noreferrer"' if href.startswith("http") else ""
        return f'<li><a class="card" href="{href}"{ext}><span class="k">{k}</span><span><span class="v">{v}</span><br><span class="n">{note}</span></span><span class="go" aria-hidden="true">&rarr;</span></a></li>'
    rows = (row("Email", EMAIL, f"mailto:{EMAIL}" if EMAIL else "", "Best for anything longer than a line.")
            + row("LinkedIn", "Say hi", LINKEDIN, "Work stuff, collaborations.")
            + row("GitHub", "Read the code", GITHUB, "Issues and ideas welcome.")
            + row("Instagram", "Slide in", INSTAGRAM, "The quickest way to reach me."))
    rows = rows or '<li><div class="card"><span class="k">Soon</span><span class="n">Contact details are on their way.</span><span></span></div></li>'
    p_contact = head + "\n" + top + (
        '    <section class="doc rise" style="--d:.08s">\n      <h1 class="title">Say <em>hello.</em></h1>\n'
        '      <p class="lede">Built by one person. If something broke, surprised you, or you just want to talk shop, this is where.</p>\n'
        f'      <ul class="stack">{rows}</ul>\n'
        '      <p class="aside">You can give me informal feedback too. <em>I&rsquo;m available always!</em></p>\n    </section>\n'
    ) + foot("reach", "v.", "to get in touch, no awkward intro needed.")

    # ----- feedback (composes an email; nothing is sent from the site) -----
    form = (
        '<form class="fb" id="fb" novalidate><fieldset class="rate"><legend>How would you rate Reflex? <span>(optional)</span></legend>'
        '<div class="stars" role="radiogroup" aria-label="Rating out of 5" id="stars">'
        + "".join(f'<button type="button" role="radio" aria-checked="false" aria-label="{n} star{"s" if n > 1 else ""}" data-n="{n}">&#9733;</button>' for n in range(1, 6))
        + '</div><p class="rate-note" id="ratenote"></p></fieldset>'
        '<fieldset class="kinds"><legend>What kind <span>(pick any, if you have something to say)</span></legend>'
        '<label class="chip"><input type="checkbox" name="kind" value="broke"><span>Something broke</span></label>'
        '<label class="chip"><input type="checkbox" name="kind" value="confused"><span>Something confused me</span></label>'
        '<label class="chip"><input type="checkbox" name="kind" value="idea"><span>An idea</span></label>'
        '<label class="chip"><input type="checkbox" name="kind" value="hello"><span>Just saying hi</span></label></fieldset>'
        '<label for="msg" id="msglab">Tell me <span style="text-transform:none;letter-spacing:0">(optional with a rating)</span></label><textarea id="msg" maxlength="2000" placeholder="Be blunt. Please don&rsquo;t paste your watch history."></textarea>'
        '<label for="contact" class="opt-contact">Want a reply? <span style="text-transform:none;letter-spacing:0">(optional)</span></label>'
        '<input id="contact" class="opt-contact" type="text" maxlength="120" autocomplete="off" placeholder="Email or @handle">'
        '<input id="website" type="text" tabindex="-1" autocomplete="off" aria-hidden="true" style="position:absolute;left:-9999px;opacity:0">'
        '<button type="submit" id="send">Send</button><p class="fb-msg" id="fbmsg" role="status" aria-live="polite"></p>'
        '<small>I store your rating, the message, the type, and your reply address if you give one, and the time. No IP address, and nothing from your report.</small></form>'
        '<script>(function(){var f=document.getElementById("fb"),b=document.getElementById("send"),m=document.getElementById("fbmsg"),rating=0,rateOnly=/[?&]from=(share|tile)/.test(location.search);'
        'var sb=[].slice.call(document.querySelectorAll("#stars button")),rn=document.getElementById("ratenote"),W=["","Rough.","Meh.","Fine.","Good.","Loved it."];'
        'function setStars(n){rating=n;sb.forEach(function(x){var on=+x.dataset.n<=n;x.classList.toggle("on",on);x.setAttribute("aria-checked",+x.dataset.n===n?"true":"false")});rn.textContent=n?W[n]:""}'
        'sb.forEach(function(x){x.addEventListener("click",function(){setStars(rating===+x.dataset.n?0:+x.dataset.n)})});'
        'if(rateOnly){f.classList.add("rate-only");var h=document.querySelector(".doc .title"),l=document.querySelector(".doc .lede");if(h)h.innerHTML="How was <em>that?</em>";if(l)l.textContent="Stars and a line, both optional. It takes ten seconds.";var ml=document.getElementById("msglab");ml.firstChild.nodeValue="Anything to add? ";ml.lastChild.textContent="(optional)";document.getElementById("send").textContent="Send"}'
        'f.addEventListener("submit",function(e){e.preventDefault();var t=document.getElementById("msg").value.trim();'
        'var ks=[].map.call(f.querySelectorAll("input[name=kind]:checked"),function(i){return i.value});'
        'if(!rating){if(!ks.length&&!(rateOnly&&t.length>=5)){m.textContent=rateOnly?"Tap a star, or write a few words.":"Pick at least one type, or leave a star rating.";m.className="fb-msg bad";return}'
        'if(t.length<5){m.textContent="Write at least a few words first.";m.className="fb-msg bad";return}}'
        'if(rateOnly&&!ks.length&&t.length>=5)ks=["hello"];'
        'b.disabled=true;m.className="fb-msg";m.textContent="Sending...";'
        'fetch("/api/feedback",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({kinds:ks,message:t,rating:rating||undefined,contact:document.getElementById("contact").value,website:document.getElementById("website").value})})'
        '.then(function(r){return r.json().then(function(j){return{ok:r.ok,j:j}})})'
        '.then(function(x){if(x.ok){f.reset();setStars(0);m.className="fb-msg good";m.textContent="Got it. Thank you, I read every one.";}'
        'else{m.className="fb-msg bad";m.textContent=(x.j&&x.j.error&&x.j.error.message)||"That didn\'t go through.";}b.disabled=false})'
        '.catch(function(){m.className="fb-msg bad";m.textContent="Couldn\'t reach the server. Try again in a moment.";b.disabled=false})});})();</script>'
    )
    p_fb = head + "\n" + top + (
        '    <section class="doc rise" style="--d:.08s">\n      <h1 class="title">Tell me what <em>sucked.</em></h1>\n'
        '      <p class="lede">Bugs, confusing bits, ideas. Blunt is fine. It goes straight to me.</p>\n'
        f'      {form}\n    </section>\n'
    ) + foot("reflect", "v.", "to look back honestly, then say what you saw.")

    return {"inspiration.html": p_insp, "contact.html": p_contact, "feedback.html": p_fb}


def main() -> None:
    DIST.mkdir(exist_ok=True)
    pages = {"index.html": "index.html", "preview.src.html": "preview.html", "how.src.html": "how.html", "privacy.src.html": "privacy.html", "upload.src.html": "upload.html", "about.src.html": "about.html", "report.src.html": "report.html"}
    for source, target in pages.items():
        text = (HERE / source).read_text(encoding="utf-8")
        (DIST / target).write_text(wrap(text), encoding="utf-8")
        print("wrote", DIST / target)
    for target, text in extra_pages().items():
        (DIST / target).write_text(wrap(text), encoding="utf-8")
        print("wrote", DIST / target)


if __name__ == "__main__":
    main()
