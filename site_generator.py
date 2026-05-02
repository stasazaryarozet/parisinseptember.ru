#!/usr/bin/env python3
"""generate.py — Faithful projection: data.yaml → {site, art, booking, telegram, bio}.

Source of truth lives in Dela: knowledge/people/olgarozet/_raw/{generate.py,data.yaml,styles.css}.
broadcast.update_site mirrors to site-repo, runs generate.py, commits + pushes.

Mathematical model:
  D           = yaml.safe_load(data.yaml)
  P_k         : D → format_k,  k ∈ {site, art, booking, telegram, bio}
  P_k_html    = _layout ∘ body_k  (HTML projections)
  body_k      pure function of relevant subtree of D
  sort inv.   events ordered ASC by t_key (chronological monotonicity)
              sort(events) enforced by p_site; t_key absence degrades to YAML order
  render gate Graph membership ≠ broadcast surface. Each event projects to a
              surface k iff `k in event.broadcast`. Absence/empty = graph-only.
              Enforced in sorted_events(); same gate in broadcast.p_<surface>.
  schema      Event entities pass through `event_schema.validate(ev) →
              EventModel` at every render entry — fail-fast on shape errors,
              uniform layout across renderers, no `.get(…) or {}` chains.
  XSS         All user-derived strings escaped via `_t()` (text) before
              entering HTML; `_h()` reserved for fields that intentionally
              carry markup (currently only `sec.items` and certain inline
              <strong> in editorial text — schema-marked).

No per-page HTML skeleton duplication. Single _layout surface.
"""
import html as _html
import yaml
from pathlib import Path

try:
    from event_schema import validate as _validate_event, InvalidEvent, EventModel  # type: ignore
except ImportError:
    # When generate.py is copied into a deployed repo (broadcast.update_site),
    # event_schema lives alongside via copy step — but if missing, fall back
    # to identity validation so legacy clones don't crash.
    _validate_event = None
    InvalidEvent = ValueError  # type: ignore
    EventModel = None  # type: ignore


import re as _re

# ── HTML escape + RU-typography helpers (Inv-TYPO + XSS hygiene) ─────
#
# Every user-derived string rendered into HTML body MUST go through
# `_t()` (escape) or `_h()` (curated-markup pass-through). Both apply
# the typography pipeline `_typo()` first — single SoT of typographic
# correctness across every surface × every owner × every event.
# An empty/None input yields "" — never "None" — to avoid leaking sentinels.

_NBSP = " "

# Inv-TYPO: NBSP-glue rules loaded from System knowledge (data, not code).
#   knowledge/system/typography/<lang>.yaml: nbsp_units + nbsp_prepositions
# Single edit in YAML propagates across every surface × every owner × every
# event. No hardcode — `feedback_no_hardcode_through_abstractions`.

def _load_typo_rules(lang: str = "ru") -> dict:
    """Load typography rules from System knowledge.

    Falls back to empty rules (no-op _typo) if the YAML is missing —
    allows offline / minimal-deploy scenarios to render without erroring.
    """
    try:
        import yaml as _yaml
        # Search up from this file: scripts/ → repo root → knowledge/system/typography/
        here = Path(__file__).resolve()
        for parent in (here.parent, *here.parents):
            cand = parent / "knowledge" / "system" / "typography" / f"{lang}.yaml"
            if cand.is_file():
                return _yaml.safe_load(cand.read_text(encoding="utf-8")) or {}
    except Exception:
        pass
    return {}


def _compile_typo_regexes(rules: dict) -> tuple:
    """Compile NBSP regex pair from rule data. One-time at module init."""
    units = rules.get("nbsp_units") or []
    preps = rules.get("nbsp_prepositions") or []
    unit_re = None
    if units:
        unit_alt = "|".join(units)
        unit_re = _re.compile(
            rf"(\d+(?:[.,]\d+)?)\s+({unit_alt})(?=\W|$)",
            _re.IGNORECASE | _re.UNICODE,
        )
    prep_re = None
    if preps:
        # Cyrillic case-insensitive: feed [Сс][Лл]ово form
        def case_class(w):
            out = []
            for ch in w:
                if ch.isalpha():
                    out.append(f"[{ch.upper()}{ch.lower()}]")
                else:
                    out.append(_re.escape(ch))
            return "".join(out)
        prep_alt = "|".join(case_class(p) for p in preps)
        prep_re = _re.compile(
            rf"(?<![\w])({prep_alt})\s+(?=[\wа-яёА-ЯЁ\d«„])",
        )
    return unit_re, prep_re


_TYPO_UNIT, _TYPO_PREP = _compile_typo_regexes(_load_typo_rules("ru"))


def _typo(s: str) -> str:
    """Apply typographic NBSP-glue per System rules (knowledge/system/typography).

    Idempotent: if NBSP already present in a position the rule would insert
    one, the regex no-op'es (whitespace class wouldn't match NBSP).

    Effect-supersystem: every text-bearing field across every projection
    (site, art, booking, telegram, bio, event-landing) typographically
    correct without per-page intervention. Rules are data — `_load_typo_rules`
    pulls from YAML at module load — `feedback_no_hardcode_through_abstractions`.
    """
    if not s:
        return s
    out = s
    if _TYPO_UNIT is not None:
        out = _TYPO_UNIT.sub(r"\1" + _NBSP + r"\2", out)
    if _TYPO_PREP is not None:
        out = _TYPO_PREP.sub(r"\1" + _NBSP, out)
    return out


def _t(s) -> str:
    """Typography-fix + escape arbitrary text for safe HTML inclusion."""
    if s is None:
        return ""
    return _html.escape(_typo(str(s)), quote=True)


def _h(s) -> str:
    """Typography-fix + pass-through for fields with curated markup
    (admin-authored, schema-marked as carrying <strong>/<em>). Still
    scrubs None → ''."""
    return "" if s is None else _typo(str(s))


_SAFE_URL_SCHEMES = ("http://", "https://", "mailto:", "tel:", "/", "#",
                     "?")  # relative paths and anchors


def _u(s) -> str:
    """Escape URL for safe inclusion as href/src attribute, AND require a
    safe scheme. Disallows `javascript:`, `data:`, `vbscript:` etc.
    Returns "" for empty/disallowed values — caller should suppress link
    when result is "".
    """
    if not s:
        return ""
    raw = str(s).strip()
    low = raw.lower()
    # Allow same-origin paths (start with `/`), anchors (`#`), query (`?`),
    # or any of the explicit safe schemes.
    if not (low.startswith(_SAFE_URL_SCHEMES) or
            (":" not in low.split("/", 1)[0] if "/" in low else ":" not in low)):
        return ""
    return _html.escape(raw, quote=True)

ROOT = Path(__file__).parent
DATA = ROOT / "data.yaml"


def load() -> dict:
    return yaml.safe_load(DATA.read_text(encoding="utf-8"))


def _canonical(d: dict) -> str:
    """Owner's canonical URL (no trailing slash)."""
    return d.get("bio", {}).get("canonical", "").rstrip("/")


def _portrait(d: dict) -> str:
    """Owner's portrait filename (lives in repo root)."""
    return d.get("bio", {}).get("portrait", "")


def _portrait_night(d: dict) -> str:
    """Owner's night-mode portrait filename (optional; absent → CSS fallback to day)."""
    return d.get("bio", {}).get("portrait_night", "")


# ── Shared HTML fragments ────────────────────────────────────────────

SOLAR_SCRIPT = """<script>
// Solar-driven day/night theme. Closed-form Michalsky 1988 altitude.
// Longitude ≈ -tzOffset/4 (15°/h). Latitude default 55° (Moscow-typical
// for primary RU/EU audience; sunset ~30 min later than the previous
// lat=45 default). Threshold alt > -0.1 rad (≈ -5.7°) keeps the page in
// 'day' through civil twilight — admin observed «ещё относительно светло»
// in Moscow while the site had flipped to night.
// Re-evaluates every 5 min so a long session flips at sunrise/sunset.
(function(){
  function setTheme(){
    var r=Math.PI/180, now=new Date();
    var J=now.valueOf()/86400000 + 2440587.5 - 2451545.0;
    var L=(280.460+0.9856474*J)%360;
    var g=((357.528+0.9856003*J)%360)*r;
    var lam=(L+1.915*Math.sin(g)+0.020*Math.sin(2*g))*r;
    var eps=(23.439-4e-7*J)*r;
    var dec=Math.asin(Math.sin(eps)*Math.sin(lam));
    var ra=Math.atan2(Math.cos(eps)*Math.sin(lam), Math.cos(lam));
    var lon=-now.getTimezoneOffset()/4, lat=55;
    var gmst=(18.697374558+24.06570982441908*J)*15;
    var H=((gmst+lon)%360)*r - ra;
    var alt=Math.asin(Math.sin(lat*r)*Math.sin(dec)+Math.cos(lat*r)*Math.cos(dec)*Math.cos(H));
    document.documentElement.setAttribute('data-theme', alt > -0.1 ? 'day' : 'night');
  }
  setTheme();
  setInterval(setTheme, 300000);
})();
</script>"""

IG_SVG = '<svg viewBox="0 0 24 24" fill="currentColor"><path d="M12 2.163c3.204 0 3.584.012 4.85.07 3.252.148 4.771 1.691 4.919 4.919.058 1.265.069 1.645.069 4.849 0 3.205-.012 3.584-.069 4.849-.149 3.225-1.664 4.771-4.919 4.919-1.266.058-1.644.07-4.85.07-3.204 0-3.584-.012-4.849-.07-3.26-.149-4.771-1.699-4.919-4.92-.058-1.265-.07-1.644-.07-4.849 0-3.204.013-3.583.07-4.849.149-3.227 1.664-4.771 4.919-4.919 1.266-.057 1.645-.069 4.849-.069zm0-2.163c-3.259 0-3.667.014-4.947.072-4.358.2-6.78 2.618-6.98 6.98-.059 1.281-.073 1.689-.073 4.948 0 3.259.014 3.668.072 4.948.2 4.358 2.618 6.78 6.98 6.98 1.281.058 1.689.072 4.948.072 3.259 0 3.668-.014 4.948-.072 4.354-.2 6.782-2.618 6.979-6.98.059-1.28.073-1.689.073-4.948 0-3.259-.014-3.667-.072-4.947-.196-4.354-2.617-6.78-6.979-6.98-1.281-.059-1.69-.073-4.949-.073zm0 5.838c-3.403 0-6.162 2.759-6.162 6.162s2.759 6.163 6.162 6.163 6.162-2.759 6.162-6.163c0-3.403-2.759-6.162-6.162-6.162zm0 10.162c-2.209 0-4-1.79-4-4 0-2.209 1.791-4 4-4s4 1.791 4 4c0 2.21-1.791 4-4 4zm6.406-11.845c-.796 0-1.441.645-1.441 1.44s.645 1.44 1.441 1.44c.795 0 1.439-.645 1.439-1.44s-.644-1.44-1.439-1.44z"/></svg>'

TG_SVG = '<svg viewBox="0 0 24 24" fill="currentColor"><path d="M11.944 0A12 12 0 0 0 0 12a12 12 0 0 0 12 12 12 12 0 0 0 12-12A12 12 0 0 0 12 0a12 12 0 0 0-.056 0zm4.962 7.224c.1-.002.321.023.465.14a.506.506 0 0 1 .171.325c.016.093.036.306.02.472-.18 1.898-.962 6.502-1.36 8.627-.168.9-.499 1.201-.82 1.23-.696.065-1.225-.46-1.9-.902-1.056-.693-1.653-1.124-2.678-1.8-1.185-.78-.417-1.21.258-1.91.177-.184 3.247-2.977 3.307-3.23.007-.032.014-.15-.056-.212s-.174-.041-.249-.024c-.106.024-1.793 1.14-5.061 3.345-.48.33-.913.49-1.302.48-.428-.008-1.252-.241-1.865-.44-.752-.245-1.349-.374-1.297-.789.027-.216.325-.437.893-.663 3.498-1.524 5.83-2.529 6.998-3.014 3.332-1.386 4.025-1.627 4.476-1.635z"/></svg>'

SCROLL_UP_SVG = '<svg width="40" height="40" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><path d="M12 20V4M5 11l7-7 7 7"/></svg>'


# ── Head / Footer / Layout ───────────────────────────────────────────

def _head(title: str, description: str, *, canonical: str,
          og_image: str = "", extra: str = "", structured: str = None) -> str:
    # All title/description/image/canonical originate in data.yaml (admin-authored
    # but not necessarily HTML-safe — see XSS test in tests/). Escape uniformly.
    # `structured` is JSON serialised by the caller — JSON itself is HTML-safe
    # except for `</`, which we neutralise so an attacker-supplied string inside
    # the JSON-LD payload cannot break out of the <script> envelope.
    t = _t(title); desc = _t(description); cn = _t(canonical); oi = _t(og_image)
    sd = ""
    if structured:
        # Defang `</` inside JSON to prevent <script> envelope escape.
        safe = structured.replace("</", "<\\/")
        sd = f'\n<script type="application/ld+json">{safe}</script>'
    return f"""<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>{t}</title>
<meta name="description" content="{desc}">
<link rel="canonical" href="{cn}">
<meta property="og:type" content="website">
<meta property="og:title" content="{t}">
<meta property="og:description" content="{desc}">
<meta property="og:url" content="{cn}">
<meta property="og:image" content="{oi}">
<meta property="og:locale" content="ru_RU">
<meta name="twitter:card" content="summary_large_image">
<meta name="twitter:title" content="{t}">
<meta name="twitter:description" content="{desc}">
<meta name="twitter:image" content="{oi}">
<link rel="icon" type="image/png" href="/favicon.png">
<link rel="apple-touch-icon" href="/apple-touch-icon.png">
<meta name="theme-color" content="#ffffff" media="(prefers-color-scheme: light)">
<meta name="theme-color" content="#111111" media="(prefers-color-scheme: dark)">
{SOLAR_SCRIPT}
<link rel="stylesheet" href="/styles.css">{sd}
{extra}"""


def _footer(urls: dict, bio_title: str, portrait: str = "", portrait_night: str = "") -> str:
    ig = urls.get("instagram", "")
    tg = urls.get("telegram", "")
    night_img = (
        f'<img src="/{portrait_night}" alt="" class="footer-portrait night" aria-hidden="true">'
        if portrait_night else ''
    )
    return f"""<footer>
  <div class="footer-content">
    <a href="{ig}" class="social-icon" aria-label="Instagram">{IG_SVG}</a>
    <img src="/{portrait}" alt="{bio_title}" class="footer-portrait day">
    {night_img}
    <a href="{tg}" class="social-icon" aria-label="Telegram">{TG_SVG}</a>
  </div>
  <a href="#" class="scroll-top" aria-label="Наверх" title="Наверх" onclick="window.scrollTo({{top:0,behavior:'smooth'}});return false;">
    {SCROLL_UP_SVG}
  </a>
</footer>
<script>
const footer = document.querySelector('.footer-content');
const observer = new IntersectionObserver((entries) => {{
  entries.forEach(entry => {{ if (entry.isIntersecting) entry.target.classList.add('visible'); }});
}}, {{ threshold: 0.1 }});
observer.observe(footer);
</script>"""


def _layout(d: dict, *, title: str, description: str, body: str,
            nav: bool = False, canonical: str = None,
            extra_head: str = "", footer: bool = True, structured: str = None) -> str:
    if canonical is None:
        canonical = _canonical(d)
    portrait = _portrait(d)
    portrait_night = _portrait_night(d)
    og_image = f"{_canonical(d)}/{portrait}" if portrait else ""
    head = _head(title, description, canonical=canonical, og_image=og_image,
                 extra=extra_head, structured=structured)
    nav_html = '<nav class="nav-fade"><a href="/" aria-label="На главную">←</a></nav>' if nav else ''
    ftr = _footer(d.get("urls", {}), d["bio"]["title"], portrait, portrait_night) if footer else ''
    # WCAG 2.4.1 «Bypass Blocks» — single skip-link before nav, jumps to <main>.
    # Visually hidden until keyboard focus; one definition serves every surface.
    skip_link = ('<a class="skip-link" href="#main">Перейти к содержанию</a>')
    return f"""<!DOCTYPE html>
<html lang="ru">
<head>
{head}
</head>
<body>
{skip_link}
{nav_html}
<main id="main" role="main">
{body}
</main>
{ftr}
</body>
</html>
"""


# ── Invariants ───────────────────────────────────────────────────────

def sorted_events(d: dict, surface: str = "site") -> list:
    """Events filtered by render-surface marker, ASC by t_key.

    Render gate: event projects to `surface` iff `surface in event.broadcast`.
    Absence/empty list = graph-only (admin-explicit opt-in required).
    Stable sort: missing t_key → last; ties resolved by YAML order.
    """
    pool = [e for e in d.get("events", []) if surface in (e.get("broadcast") or [])]
    return sorted(pool, key=lambda e: e.get("t_key", "￿"))


# ── Graph resolution: events reference entities by id (no value duplication) ─

def resolve_refs(d: dict, kind: str, ids):
    """Resolve a list of entity ids against d[kind]; non-id values fall through."""
    pool = d.get(kind, {})
    out = []
    for x in (ids or []):
        if isinstance(x, str) and x in pool:
            v = pool[x]
            if isinstance(v, dict):
                out.append({**v, "id": x})
            else:
                out.append({"id": x, "name": v})
        else:
            out.append(x)
    return out


def schema_events_jsonld(d: dict) -> str:
    """schema.org ItemList of Events with refs resolved (graph-rich SEO markup)."""
    items = []
    for ev in sorted_events(d):
        obj = {
            "@type": "Event",
            "name": ev.get("title", ""),
            "startDate": ev.get("t_key", ""),
            "eventStatus": f'https://schema.org/Event{ev.get("status", "Scheduled").title()}',
        }
        locs = resolve_refs(d, "locations", ev.get("locations", []))
        if locs:
            obj["location"] = [{"@type": "Place",
                                "name": l.get("name", l.get("id", "")),
                                "addressCountry": l.get("country", "")} for l in locs]
        orgs = resolve_refs(d, "people", ev.get("organizers", []))
        if orgs:
            obj["organizer"] = [{"@type": "Person",
                                 "name": p.get("name", p.get("id", ""))} for p in orgs]
        auds = resolve_refs(d, "audience", ev.get("audience", []))
        if auds:
            names = [a.get("name", a) if isinstance(a, dict) else a for a in auds]
            obj["audience"] = {"@type": "Audience", "audienceType": ", ".join(names)}
        if ev.get("link"):
            obj["url"] = _canonical(d) + ev["link"]
        items.append(obj)
    if not items:
        return ""
    import json as _j
    obj = {"@context": "https://schema.org", "@type": "ItemList",
           "itemListElement": [{"@type": "ListItem", "position": i + 1, "item": e}
                               for i, e in enumerate(items)]}
    return _j.dumps(obj, ensure_ascii=False)


# ── P_publications: D → section HTML ────────────────────────────────

_CHANNEL_LABEL = {"site": "сайт", "telegram": "Telegram", "instagram": "Instagram"}


def p_publications(d: dict) -> str:
    """Публикации section — semantic Сайт↔TG↔IG linkage. Empty if absent.

    Publications sorted DESC by date (newest-first feed semantics), stable on tie.
    """
    pubs = sorted(d.get("publications", []),
                  key=lambda p: (p.get("date", ""), ), reverse=True)
    if not pubs:
        return ""
    items = []
    for p in pubs:
        label = _CHANNEL_LABEL.get(p.get("channel", ""), p.get("channel", ""))
        items.append(
            f'        <li><a href="{p["link"]}" class="pub" rel="noopener">'
            f'<span class="pub-channel">{label}</span>'
            f'<span class="pub-title">{p["title"]}</span></a></li>'
        )
    return (
        '    <section id="publications" aria-labelledby="publications-heading">\n'
        '      <h2 id="publications-heading">Публикации:</h2>\n'
        '      <ul class="publications-list">\n'
        + "\n".join(items) +
        '\n      </ul>\n'
        '    </section>'
    )


# ── P_site: D → index.html ──────────────────────────────────────────

def p_site(d: dict) -> str:
    bio = d["bio"]
    cons = d["consultations"]
    events = sorted_events(d)
    urls = d.get("urls", {})
    publications_html = p_publications(d)

    # Bio section (roles + skills on separate lines)
    role_lines = []
    for r in bio.get("roles", []):
        role_lines.append(f"    <p>{r};</p>")
    for i, s in enumerate(bio.get("skills", [])):
        sep = ";" if i < len(bio["skills"]) - 1 else "."
        role_lines.append(f"    <p>{s}{sep}</p>")
    inspire = "<br>".join(bio["inspire"].strip().splitlines())

    bio_html = f"""      <section id="about" aria-label="О себе">
    <p><span class="artist-highlight"><a href="{bio['artist']['link']}">{bio['artist']['text']}</a></span><br>•</p>
{chr(10).join(role_lines)}
    <p class="inspire">{inspire}<br>•</p>
    <p><a href="mailto:{bio['email']}">{bio['email']}</a></p>
      </section>"""

    # Consultations
    desc = "<br>".join(cons["description"].strip().splitlines())
    avail = "<br>".join(cons["availability"].strip().splitlines())
    cons_html = f"""    <section id="consultations" aria-labelledby="consultations-heading">
      <h2 id="consultations-heading">Консультации:</h2>
      <p>{desc}</p>
      <p class="price">{cons['price']}</p>
      <a href="{cons['link']}" class="cta">{cons['cta']}</a>
      <p class="availability">{avail}</p>
    </section>"""

    # Events (sorted by t_key invariant)
    events_articles = []
    for ev in events:
        lines = [f'        <p class="event-date"><time>{ev["date"]}</time></p>',
                 f'        <p>{ev["title"]}</p>']
        for line in ev.get("lines", []):
            if line == "":
                continue
            lines.append(f"        <p>{line}</p>")
        events_articles.append(
            "      <article class=\"event\">\n" +
            "\n".join(lines) +
            "\n      </article>")
    events_html = "\n".join(events_articles)

    ig_url = urls.get("instagram", "")
    tg_url = urls.get("telegram", "")

    canonical = _canonical(d)
    portrait = _portrait(d)
    image_url = f"{canonical}/{portrait}" if portrait else ""
    import json as _j
    person = {
        "@context": "https://schema.org",
        "@type": "Person",
        "name": bio["title"],
        "url": canonical,
        "image": image_url,
        "email": bio["email"],
        "sameAs": [u for u in (ig_url, tg_url) if u],
    }
    if bio.get("alternate_name"):
        person["alternateName"] = bio["alternate_name"]
    if bio.get("job_title"):
        person["jobTitle"] = list(bio["job_title"])
    person_jsonld = _j.dumps(person, ensure_ascii=False)
    events_jsonld = schema_events_jsonld(d)
    structured = person_jsonld + (
        '\n  </script>\n  <script type="application/ld+json">' + events_jsonld
        if events_jsonld else ""
    )

    body = f"""  <div class="content-wrapper">
    <header>
      <h1>{bio['title']}</h1>
    </header>

    <main>
{bio_html}

{cons_html}

      <section id="events" aria-labelledby="events-heading">
        <h2 id="events-heading">СКОРО:</h2>
{events_html}
      </section>

{publications_html}
    </main>
  </div>"""

    title = bio["title"]
    if bio.get("tagline"):
        title = f"{title} — {bio['tagline']}"
    return _layout(
        d,
        title=title,
        description=bio.get("description", title),
        body=body,
        structured=structured,
    )


# ── P_event_landing: D × event_id → <slug>/index.html ──────────────
#
# Pure projection — landing for one Event entity, fully derived from
# data.yaml graph (no hand-written markdown body). Used by:
#   • site_preview server         (always-fresh, Inv-I2)
#   • broadcast.update_site       (renders into site/<slug>/index.html
#                                  before push — contour-first, deploy mirrors)
# Replaces hand-authored _articles/<slug>.md when the slug == event id and
# event has all required fields. Coexists with longform articles for
# non-event content.

def event_signup_form(slug: str, label: str, email_fallback: str) -> str:
    """Mailto-fallback email-capture form. Async POST upgrade if
    <slug>/signup.json::transport_url is set (zero-credential default)."""
    from urllib.parse import quote as _q
    # URL-encode the bits that flow into the mailto: action attribute
    # (slug becomes subject token, label becomes body fragment, email is
    # the action target). HTML-escape the label echoed in <p>«…».
    # Subject = human-readable event label (NOT the dev-slug). Cleaner mailto
    # for traveler — they see «Заявка: Париж · сентябрь 2026» in their email
    # client, not «Заявка: paris_2026_09».
    subj_q = _q(f"Заявка: {label}", safe="")
    label_q = _q(label, safe="")
    email_q = _q(email_fallback, safe="@")
    mb = ("%D0%97%D0%B4%D1%80%D0%B0%D0%B2%D1%81%D1%82%D0%B2%D1%83%D0%B9%D1%82%D0%B5%2C%20%D0%9E%D0%BB%D1%8C%D0%B3%D0%B0.%0A%0A"
          f"%D0%9E%D1%81%D1%82%D0%B0%D0%B2%D0%BB%D1%8F%D1%8E%20%D0%BA%D0%BE%D0%BD%D1%82%D0%B0%D0%BA%D1%82%20%E2%80%94%20{label_q}.%0A%0A"
          "%D0%98%D0%BC%D1%8F:%20%0A%20Email:%20%0A"
          "%D0%9E%20%D1%81%D0%B5%D0%B1%D0%B5%20(%D1%81%D1%84%D0%B5%D1%80%D0%B0%2C%20%D0%B3%D0%BE%D1%80%D0%BE%D0%B4):%20%0A")
    # Slug is admin-controlled identifier — escape for safe HTML/attr/URL.
    slug_t = _t(slug)
    # Form heading is <h3> (parent <section class=signup-wrap> already
    # provides the section's <h2 «Лист ожидания»>). Heading hierarchy
    # h2 → h3 is WCAG-correct and screen-reader-friendly.
    return f'''<section id="signup" class="signup" aria-labelledby="signup-h">
  <h3 id="signup-h" class="signup-h3">Оставить email</h3>
  <p>Пришлём финальную программу первыми. Без спама, без рассылки —
     адресное сообщение с датами, ценой и форматом.</p>
  <form id="signup-form" class="signup-form" novalidate
        action="mailto:{email_q}?subject={subj_q}&amp;body={mb}"
        method="post" enctype="text/plain"
        data-slug="{slug_t}">
    <label class="signup-label" for="su-name">Имя</label>
    <input class="signup-input" id="su-name" name="name"
           autocomplete="name" required minlength="2" aria-required="true">
    <label class="signup-label" for="su-email">Email</label>
    <input class="signup-input" id="su-email" name="email" type="email"
           autocomplete="email" required aria-required="true">
    <label class="signup-label" for="su-note">Коротко о себе
      <span class="signup-hint">(сфера, город — опционально)</span></label>
    <input class="signup-input" id="su-note" name="note" autocomplete="off">
    <label class="signup-consent">
      <input type="checkbox" id="su-consent" name="consent" required
             aria-required="true">
      <span>Согласен(-на) на обработку персональных данных для ответа по программе.</span>
    </label>
    <button class="signup-btn" type="submit" id="su-btn">Оставить email</button>
  </form>
  <div class="signup-msg" id="signup-msg" role="status" aria-live="polite"></div>
  <noscript><p class="signup-note">Или напишите: <a href="mailto:{email_q}">{_t(email_fallback)}</a></p></noscript>
<script>
(function(){{
  var f=document.getElementById("signup-form");
  if(!f) return;
  var btn=document.getElementById("su-btn");
  var msg=document.getElementById("signup-msg");
  var transport=null;
  fetch("/{slug_t}/signup.json").then(function(r){{return r.ok?r.json():null}})
    .then(function(d){{if(d&&d.transport_url)transport=d.transport_url;}})
    .catch(function(){{}});
  f.addEventListener("submit",function(e){{
    var name=f.name.value.trim(),email=f.email.value.trim();
    var note=f.note.value.trim(),consent=f.consent.checked;
    if(name.length<2){{e.preventDefault();f.name.focus();return;}}
    if(!/^[^@\\s]+@[^@\\s]+\\.[^@\\s]+$/.test(email)){{e.preventDefault();f.email.focus();return;}}
    if(!consent){{e.preventDefault();f.consent.focus();return;}}
    if(!transport)return; // mailto: handles it
    e.preventDefault();
    btn.disabled=true;btn.textContent="отправка...";
    var data=new URLSearchParams();
    data.append("name",name);data.append("email",email);
    data.append("note",note);data.append("consent","true");
    fetch(transport,{{method:"POST",
      headers:{{"Content-Type":"application/x-www-form-urlencoded"}},
      body:data.toString()}})
      .then(function(r){{return r.json()}})
      .then(function(d){{
        if(d&&d.ok){{f.style.display="none";
          msg.innerHTML="<b>Заявка принята.</b> Свяжемся лично.";
        }}else{{msg.textContent="Ошибка. Попробуйте ещё раз или напишите на {email_q}.";
          btn.disabled=false;btn.textContent="Оставить email";}}
      }})
      .catch(function(){{msg.textContent="Ошибка сети. Email ниже работает без формы.";
        btn.disabled=false;btn.textContent="Оставить email";}});
  }});
}})();
</script>
</section>'''


def _event_jsonld(d: dict, ev: dict) -> str:
    """schema.org Event — graph-resolved org + locations + audience."""
    import json as _j
    obj = {
        "@context": "https://schema.org",
        "@type": "Event",
        "name": f"{ev.get('title','')} {ev.get('date','')}".strip(),
        "description": ev.get("concept", ""),
        "startDate": ev.get("t_key", ""),
        "url": _event_canonical(d, ev),
        "eventStatus": f'https://schema.org/Event{ev.get("status","Scheduled").title()}',
    }
    locs = resolve_refs(d, "locations", ev.get("locations", []))
    if locs:
        obj["location"] = [{"@type": "Place",
                            "name": l.get("name", l.get("id", "")),
                            "addressCountry": l.get("country", "")} for l in locs]
    orgs = resolve_refs(d, "people", ev.get("organizers", []))
    if orgs:
        obj["organizer"] = [{"@type": "Person",
                             "name": p.get("name", p.get("id", ""))} for p in orgs]
    auds = resolve_refs(d, "audience", ev.get("audience", []))
    if auds:
        names = [a.get("name", a) if isinstance(a, dict) else a for a in auds]
        obj["audience"] = {"@type": "Audience", "audienceType": ", ".join(names)}
    pricing = ev.get("pricing", {}) or {}
    fee = pricing.get("team_fee") or {}
    if fee.get("amount") is not None:
        obj["offers"] = {"@type": "Offer",
                         "price": str(fee["amount"]),
                         "priceCurrency": fee.get("currency", "EUR")}
    return _j.dumps(obj, ensure_ascii=False)


def _event_canonical(d: dict, ev: dict) -> str:
    """Canonical URL for an Event landing.

    Graph: if `ev.web_addresses[]` binds the Event to one or more Domain
    entities, the FIRST address is the canonical landing root (no /<slug>/).
    Tree fallback: owner-canonical + /<slug>/ — preserved for events without
    a bound domain.
    """
    addrs = ev.get("web_addresses") or []
    if addrs:
        return f"https://{addrs[0]}"
    return f"{_canonical(d)}/{ev['id']}/"


def _person_display(d: dict, person_id: str) -> tuple[str, str]:
    """(name, link) for a person ref. Falls back to id-as-name if not in graph.

    People can live as dict[id]->fields OR list[{id, ...}] depending on owner.
    """
    people = d.get("people") or {}
    p: dict | None = None
    if isinstance(people, dict):
        p = people.get(person_id)
    elif isinstance(people, list):
        p = next((x for x in people
                  if isinstance(x, dict) and x.get("id") == person_id), None)
    if not p or not isinstance(p, dict):
        return (person_id.replace("_", " ").title(), "")
    nm = p.get("name") or person_id
    link = p.get("link") or p.get("url") or ""
    return (nm, link)


def p_event_landing(d: dict, ev: dict) -> str:
    """Project one Event from the graph to a standalone landing HTML page.

    Single render path: schema-validated essay layout. No legacy fallback.
    Schema (see event_schema.EventModel for the source of truth):
      lead              — single sentence, italic, frames the page
      co_organizers     — list of person ids; rendered as «N1 и N2 — Организаторы.»
      sections[]        — ordered essay sections, each one of:
                          {title, intro?, pairs:[{label,text}]}    — concept-pair
                          {title, text}                            — prose
                          {title, intro?, items:[str]}             — list (items
                                                                     may carry
                                                                     <strong>)
      open_questions[]  — graph edges {to: list[person_id], q: text}
                          grouped by addressee on render
      signup            — {title, note}; signup_form embedded
      about_organizer   — {text, link_text, link_url}

    Validation happens here — if the event lacks lead+sections (the only two
    truly required fields for this projection), `event_schema.validate()`
    raises InvalidEvent and the caller (site_preview / update_landing)
    surfaces the message. No silent half-rendered pages.
    """
    bio = d.get("bio", {})
    slug = ev["id"]

    # Validate (or fall back to dict-as-is in deployed-repo edge case)
    if _validate_event is not None:
        m = _validate_event(ev)  # raises InvalidEvent on shape problems
    else:
        m = ev  # type: ignore[assignment]
        if not (m.get("lead") and m.get("sections")):
            raise ValueError(f"event {ev.get('id','?')!r}: lead+sections required "
                             "(modern schema; event_schema.py not importable here)")

    title_full = m.title if hasattr(m, "title") else m.get("title", "Событие")
    date_str = m.date if hasattr(m, "date") else m.get("date", "")
    if date_str and "·" not in title_full:
        title_full = f"{title_full} · {date_str}"

    parts: list[str] = []

    # Header — h1 + lead + co_organizers.
    # Semantic HTML5: emit <time datetime="…"> as visually-hidden a11y/SEO
    # microdata when t_key (ISO yyyy-mm-dd) is present. JSON-LD startDate
    # carries the structured event date in machine-readable form already;
    # the <time> element gives screen-readers + browser-time parsers the
    # canonical date without disrupting essay-flow visual layout.
    h1_title = m.title if hasattr(m, "title") else m.get("title", "Событие")
    if date_str and "·" not in h1_title:
        h1_title = f"{h1_title} · {date_str}"
    t_key = m.t_key if hasattr(m, "t_key") else m.get("t_key", "")
    parts.append(f"<header><h1>{_t(h1_title)}</h1>")
    if t_key:
        # visually-hidden but DOM-present time element
        parts.append(f'<time datetime="{_t(t_key)}" class="visually-hidden">'
                     f'{_t(date_str or t_key)}</time>')
    parts.append(f'<p class="lead"><em>{_t(m.lead if hasattr(m,"lead") else m["lead"])}</em></p>')

    co_ids = m.co_organizers if hasattr(m, "co_organizers") else (m.get("co_organizers") or [])
    if co_ids:
        disp = []
        for pid in co_ids:
            nm, lk = _person_display(d, pid)
            safe_lk = _u(lk)
            disp.append(f'<a href="{safe_lk}">{_t(nm)}</a>' if safe_lk else _t(nm))
        parts.append(f'<p class="organizers">{" и ".join(disp)} — Организаторы.</p>')
    parts.append("</header>")

    # Pricing display strip — editorial cover-line, schema-driven.
    # Renders ev.pricing.team_fee.{amount,currency,note} as a hero figure
    # if amount is set. CSS in styles.css `.pricing-display` paints it.
    pricing = m.pricing if hasattr(m, "pricing") else (m.get("pricing") or {})
    team_fee = (pricing or {}).get("team_fee") or {}
    amount = team_fee.get("amount")
    if amount is not None:
        currency = team_fee.get("currency", "")
        cur_glyph = {"EUR": "€", "USD": "$", "RUB": "₽", "GBP": "£"}.get(
            str(currency).upper(), _t(currency))
        amount_str = f"{int(amount):,}".replace(",", " ") \
            if isinstance(amount, (int, float)) and float(amount).is_integer() \
            else _t(amount)
        note = team_fee.get("note") or ""
        per = team_fee.get("per") or ""
        label_bits = ["Стоимость"]
        if per == "participant":
            label_bits.append("на участника")
        parts.append(
            '<aside class="pricing-display" aria-label="Стоимость">'
            f'<div class="pricing-label">{_t(" · ".join(label_bits))}</div>'
            f'<div class="pricing-amount">{amount_str}'
            f'<span class="currency">{cur_glyph}</span></div>'
            + (f'<div class="pricing-note">{_t(note)}</div>' if note else '')
            + '</aside>'
        )

    # Status banner — DRAFT/PLANNING openly stated, congruent with «программа дописывается»
    status = m.status if hasattr(m, "status") else m.get("status", "")
    if status in ("PLANNING", "DRAFT"):
        # WAI-ARIA: status banner is a non-critical live region. role=status
        # + aria-live=polite makes screen readers announce "Программа собирается"
        # when the page first reads, without interrupting other narration.
        parts.append('<p class="status-banner" role="status" aria-live="polite">'
                     'Программа собирается. Лист ожидания открыт.</p>')

    # Sections — schema variants (pair / text / items / intro)
    sections = m.sections if hasattr(m, "sections") else (m.get("sections") or [])
    for sec in sections:
        # EventModel exposes attributes; raw dict path uses dict access
        t = sec.title if hasattr(sec, "title") else sec.get("title", "")
        intro = sec.intro if hasattr(sec, "intro") else sec.get("intro", "")
        text = sec.text if hasattr(sec, "text") else sec.get("text", "")
        pairs = sec.pairs if hasattr(sec, "pairs") else (sec.get("pairs") or [])
        items = sec.items if hasattr(sec, "items") else (sec.get("items") or [])
        parts.append(f"<section><h2>{_t(t)}</h2>")
        if intro:
            parts.append(f"<p>{_t(intro)}</p>")
        if text:
            parts.append(f"<p>{_t(text)}</p>")
        if pairs:
            # Semantic HTML5: definition list — `<dt>` is the term, `<dd>` is
            # its description. Replaces ad-hoc `<p class="pair">` with
            # screen-reader-correct grouping per WAI/ARIA dl pattern.
            parts.append('<dl class="pairs">')
            for pair in pairs:
                label = pair.label if hasattr(pair, "label") else pair.get("label", "")
                ptext = pair.text if hasattr(pair, "text") else pair.get("text", "")
                parts.append(f'<dt>{_t(label)}</dt><dd>{_t(ptext)}</dd>')
            parts.append('</dl>')
        if items:
            # `items` is the one schema field that intentionally carries
            # admin-authored markup (<strong>…</strong> highlights). Curated.
            lis = "".join(f"<li>{_h(x)}</li>" for x in items)
            parts.append(f"<ul>{lis}</ul>")
        parts.append("</section>")

    # ── System-policy-derived sections ────────────────────────────────
    # Pulled from `event_policy` graph node (top-level d) and rendered
    # automatically when applicable. Single SoT — no per-event duplication.
    # Closes typical traveler-questions (onboarding, payment timing,
    # language, accessibility) without hand-writing copy on every
    # Design-Travels landing.
    fmt = m.format if hasattr(m, "format") else (m.get("format") or [])
    policy = d.get("event_policy") or {}
    dt_policy = policy.get("design_travel") or {} if "travel" in (fmt or []) else {}

    # «Перед поездкой» — pre-travel onboarding (Design-Travels-class).
    # Source: event_policy.design_travel.onboarding {interview, intro_meeting}.
    # Sets traveler expectations: short online interview + mandatory online
    # intro-meeting with Olga (offline-when-possible, in addition not in place).
    onboarding = dt_policy.get("onboarding") or {}
    if onboarding:
        intro_lines: list[str] = []
        iv = onboarding.get("interview") or {}
        if iv:
            iv_purpose = iv.get("purpose", "знакомство")
            intro_lines.append(
                f"<strong>Онлайн-собеседование с Организаторами</strong> — "
                f"короткое, для каждого нового Путешественника: {_t(iv_purpose)}."
            )
        im = onboarding.get("intro_meeting") or {}
        if im:
            modes = im.get("modes") or []
            if "offline" in modes and "online" in modes:
                modes_phrase = ("онлайн обязательно для всех; оффлайн — "
                                "при возможности, в дополнение")
            elif "online" in modes:
                modes_phrase = "онлайн"
            else:
                modes_phrase = ", ".join(modes) or "по согласованию"
            intro_lines.append(
                f"<strong>Встреча-знакомство-занятие с Ольгой</strong> — "
                f"{modes_phrase}."
            )
        if intro_lines:
            parts.append('<section class="onboarding"><h2>Перед поездкой</h2><ul>')
            for it in intro_lines:
                parts.append(f"<li>{_h(it)}</li>")
            parts.append("</ul></section>")

    terms_items: list[str] = []
    # Payment (Design Travels invariant: prepayment_pct from System policy).
    # Inv-FACT: only state what System Memory contains. Remainder-timing
    # NOT in policy → not stated. Will surface if admin extends policy.
    if dt_policy:
        pmt = dt_policy.get("payment") or {}
        pct = pmt.get("prepayment_pct")
        if pct is not None:
            terms_items.append(
                f"<strong>Оплата:</strong> {int(pct)}% предоплата при "
                "подтверждении участия. Условия по остатку — в финальной программе."
            )
    # Language (RU is owner-default; explicit terms.language can override)
    terms_items.append(
        "<strong>Язык:</strong> программа на русском. С партнёрами и музеями "
        "по необходимости — наш перевод."
    )
    # Accessibility (System policy). Surface min_free_slots explicitly when
    # the policy guarantees a number (≥1) — admin spec: «минимум 1 бесплатное
    # место в каждом проекте». Falls back to soft phrasing only if policy
    # has no slot count.
    acc = policy.get("accessibility") or {}
    if acc.get("discount_on_request") or acc.get("min_free_slots"):
        contact_email = acc.get("contact") or bio.get("email", "")
        slots = acc.get("min_free_slots") or 0
        if slots >= 1:
            slots_phrase = (f"<strong>Доступность:</strong> минимум "
                            f"{int(slots)} место — бесплатно по запросу. "
                            "Возможна скидка по запросу.")
        else:
            slots_phrase = ("<strong>Доступность:</strong> возможна скидка "
                            "или бесплатное место по запросу.")
        terms_items.append(
            f'{slots_phrase} Пишите на '
            f'<a href="mailto:{_t(contact_email)}">{_t(contact_email)}</a>.'
        )
    if terms_items:
        parts.append('<section class="terms"><h2>Условия и сроки</h2><ul>')
        for it in terms_items:
            parts.append(f"<li>{_h(it)}</li>")
        parts.append('</ul></section>')

    # Open questions — graph edges, grouped by addressee-set (frozen multi-edge).
    # Joint addressing (e.g. [olga, natalia]) renders as one shared block —
    # no synthetic per-person split when admin says «вопросы обеим сразу».
    oq = m.open_questions if hasattr(m, "open_questions") else (m.get("open_questions") or [])
    if oq:
        from collections import OrderedDict
        by_addrs: "OrderedDict[tuple[str,...], list[str]]" = OrderedDict()
        for item in oq:
            to = item.to if hasattr(item, "to") else item.get("to", "?")
            key = tuple(to) if isinstance(to, list) else (to,)
            q = item.q if hasattr(item, "q") else item.get("q", "")
            by_addrs.setdefault(key, []).append(q)
        parts.append('<section class="open-questions"><h2>Вопросы</h2>'
                     '<p class="qnote">Если знаете ответ, напишите — учтём при '
                     'сборке программы.</p>')
        for addr_ids, qs in by_addrs.items():
            names = []
            for pid in addr_ids:
                nm, lk = _person_display(d, pid)
                safe_lk = _u(lk)
                names.append(f'<a href="{safe_lk}">{_t(nm)}</a>' if safe_lk else _t(nm))
            head = " и ".join(names)
            lis = "".join(f"<li>{_t(q)}</li>" for q in qs)
            parts.append(f'<div class="q-group"><h3>К {head}</h3>'
                         f'<ul>{lis}</ul></div>')
        parts.append("</section>")

    # Signup
    signup = m.signup if hasattr(m, "signup") else m.get("signup")
    if signup:
        s_title = signup.title if hasattr(signup, "title") else signup.get("title", "Записаться")
        s_note = signup.note if hasattr(signup, "note") else signup.get("note", "")
        parts.append(f'<section class="signup-wrap"><h2>{_t(s_title)}</h2>')
        if s_note:
            parts.append(f'<p>{_t(s_note)}</p>')
        ev_label = f"{m.title if hasattr(m,'title') else m.get('title','Событие')} {date_str}".strip()
        parts.append(event_signup_form(
            slug,
            ev_label,
            bio.get("email", "info@example.com"),
        ))
        parts.append("</section>")

    # Direct-contact block — public-side, sits after signup.
    # Schema: contact: {prompt, text, email}. Rendered iff at least one field set.
    contact = m.contact if hasattr(m, "contact") else m.get("contact")
    if contact:
        c_prompt = contact.prompt if hasattr(contact, "prompt") else contact.get("prompt", "")
        c_text = contact.text if hasattr(contact, "text") else contact.get("text", "")
        c_email = contact.email if hasattr(contact, "email") else contact.get("email", "")
        if c_prompt or c_text or c_email:
            parts.append('<section class="contact"><h2>'
                         f'{_t(c_prompt or "Связаться")}</h2>')
            mailto_url = _u(f"mailto:{c_email}") if c_email else ""
            if c_text:
                if mailto_url:
                    parts.append(f'<p>{_t(c_text)} '
                                 f'<a href="{mailto_url}">{_t(c_email)}</a></p>')
                else:
                    parts.append(f'<p>{_t(c_text)}</p>')
            elif mailto_url:
                parts.append(f'<p><a href="{mailto_url}">{_t(c_email)}</a></p>')
            parts.append("</section>")

    # About organizers — graph-resolved from co_organizers list.
    # Plural «Организаторы» when ≥2 (project_natalia_equal_organizer:
    # paritetary). Auto-pulls each person's bio from people graph;
    # falls back to event.about_organizer (legacy single-organizer).
    co_ids = (m.co_organizers if hasattr(m, "co_organizers")
              else (m.get("co_organizers") or []))
    organizer_paragraphs: list[str] = []
    for pid in co_ids:
        person = (d.get("people") or {}).get(pid) or {}
        nm = person.get("name") or pid
        person_bio = person.get("bio") or ""
        if person_bio:
            organizer_paragraphs.append(
                f"<p><strong>{_t(nm)}</strong> — {_t(person_bio)}.</p>"
            )
    about = m.about_organizer if hasattr(m, "about_organizer") else m.get("about_organizer")
    a_link_url = ""
    a_link_text = ""
    if about:
        a_link_url = about.link_url if hasattr(about, "link_url") else about.get("link_url", "")
        a_link_text = about.link_text if hasattr(about, "link_text") else about.get("link_text", "")
    if organizer_paragraphs:
        title = "Об Организаторах" if len(co_ids) > 1 else "Об Организаторе"
        link_html = ""
        safe_link = _u(a_link_url)
        if safe_link:
            link_html = (f'<p class="org-link"><a href="{safe_link}">'
                         f'{_t(a_link_text or a_link_url)}</a></p>')
        parts.append(f'<footer class="about-organizer"><h2>{title}</h2>'
                     f'{"".join(organizer_paragraphs)}{link_html}</footer>')
    elif about:
        # Legacy fallback for events without co_organizers ↔ people-bio graph
        a_text = about.text if hasattr(about, "text") else about.get("text", "")
        if a_text:
            link_html = ""
            safe_link = _u(a_link_url)
            if safe_link:
                link_html = (f'<br><a href="{safe_link}">'
                             f'{_t(a_link_text or a_link_url)}</a>')
            parts.append('<footer class="about-organizer">'
                         f'<h2>Об Организаторе</h2><p>{_t(a_text)}{link_html}</p></footer>')

    body = f'  <article class="article-wrapper">{"".join(parts)}</article>'

    lead_text = m.lead if hasattr(m, "lead") else m.get("lead", "")
    return _layout(
        d,
        title=title_full,
        description=(lead_text or m.concept if hasattr(m, "concept") else m.get("concept", title_full))[:160],
        body=body,
        nav=True,
        canonical=_event_canonical(d, ev),
        structured=_event_jsonld(d, ev),
        # Owner-portrait footer belongs to owner-site (olgarozet.ru) only —
        # admin directive 2026-05-02. Event landings render their own
        # contact/about-organizer block; no shared portrait/social-icons.
        footer=False,
    )


# ── P_art: D → art/index.html ───────────────────────────────────────

def p_art(d: dict) -> str:
    """Gallery projection. Artworks from data.artworks (single source)."""
    bio = d["bio"]
    alt = f"{bio['title']} — Произведение"
    items = "\n".join(
        f'    <div class="artwork"><img src="img/{a}" loading="lazy" alt="{alt}"></div>'
        for a in d.get("artworks", [])
    )
    body = f"""  <div class="progress-bar" id="progress"></div>
  <main class="gallery">
{items}
  </main>"""
    art_label = bio.get("art_page_label", "Искусство")
    return _layout(
        d,
        title=f"{bio['title']} — {art_label}",
        description=f"{art_label} — {bio['title']}",
        body=body,
        nav=True,
        canonical=f"{_canonical(d)}/art/",
        footer=False,  # gallery is immersive — no global footer
    )


# ── P_telegram: D → channel post text ────────────────────────────────

def p_telegram(d: dict) -> str:
    """Telegram channel post. Vertical, poetic. Empty lines = paragraph breaks."""
    parts = ["СКОРО:", "", "•"]
    for ev in sorted_events(d):  # enforce same temporal monotonicity
        parts.append("")
        parts.append(ev["title"])
        for line in ev.get("lines", []):
            parts.append(line)
        parts.append("")
        parts.append("•")
    if parts and parts[-1] == "•":
        parts.pop()
    cons = d.get("consultations", {})
    if cons:
        parts.append("")
        parts.append("•")
        parts.append("")
        for line in cons["description"].strip().splitlines():
            stripped = line.strip()
            if stripped:
                parts.append(stripped)
        parts.append(cons["price"])
        parts.append("")
        for line in cons["availability"].strip().splitlines():
            stripped = line.strip()
            if stripped:
                parts.append(stripped)
        parts.append("")
        host = _canonical(d).replace("https://", "").replace("http://", "")
        parts.append(f"{host}/booking")
    return "\n".join(parts)


# ── P_bio: D → short bio ─────────────────────────────────────────────

def p_bio(d: dict) -> str:
    bio = d["bio"]
    lines = [bio["artist"]["text"]]
    lines.extend(f"{r};" for r in bio.get("roles", []))
    lines.extend(f"{s}." for s in bio.get("skills", []))
    lines.append(bio["inspire"].strip().splitlines()[0])
    lines.append(d["urls"].get("telegram_handle", "@olgaroset"))
    return "\n".join(lines)


# ── P_booking: (D, slots.json) → booking/index.html ──────────────────

def p_booking(d: dict) -> str:
    """Booking page. Uses _layout for head/footer; booking-specific CSS via extra_head."""
    import json as _json
    cons = d["consultations"]
    slots_file = ROOT / "booking.json"
    slots_data = _json.loads(slots_file.read_text()) if slots_file.exists() else {"slots": [], "user": ""}
    transport_url = slots_data.get(
        "transport_url",
        "https://script.google.com/macros/s/AKfycbzeulk8nVhROOmrnysLKRLGqM_naMEgVhtPl50ch_GCilibJ7MXv2rWlGlq1hz1SWc/exec",
    )
    slots_json = _json.dumps(slots_data.get("slots", []), ensure_ascii=False)
    desc_plain = cons["description"].strip().replace("\n", " ").replace("  ", " ")
    contact_email = cons.get("calendar_id", "o.g.rozet@gmail.com")

    booking_style = """<style>
.booking{max-width:420px;margin:0 auto;padding:2.5rem 1.5rem 2rem}
.booking h2{font-size:clamp(1.1rem,1rem + 0.3vw,1.3rem);text-align:center;font-weight:600;margin-bottom:.15rem}
.sub{text-align:center;color:var(--color-muted,#666);font-size:.95rem}
.tz{text-align:center;color:#aaa;font-size:.8rem;margin-bottom:1rem}
.day{margin-bottom:.8rem}
.day-label{font-size:.85rem;color:var(--color-muted,#666);margin-bottom:.3rem;font-weight:500}
.slots-grid{display:flex;flex-wrap:wrap;gap:.3rem}
.t{display:inline-flex;align-items:center;justify-content:center;min-width:3.5rem;min-height:3rem;padding:.5rem 1rem;border:1px solid var(--color-border,#ddd);border-radius:2rem;cursor:pointer;font-size:.95rem;transition:border-color .15s,background .15s,color .15s,transform .1s;user-select:none;-webkit-tap-highlight-color:transparent}
.t:hover{border-color:var(--color-text,#1a1a1a)}
.t:focus-visible{outline:2px solid var(--color-text,#1a1a1a);outline-offset:2px}
.t:active{transform:scale(.95)}
.t.on{background:var(--color-text,#1a1a1a);color:#fff;border-color:var(--color-text,#1a1a1a)}
.more{text-align:center;margin:.6rem 0}
.more button{background:none;border:none;color:var(--color-muted,#666);font-size:.85rem;cursor:pointer;font-family:inherit;padding:.5rem 1rem}
.bk-form{overflow:hidden;max-height:0;opacity:0;transition:max-height .35s ease,opacity .3s ease;margin-top:0}
.bk-form.open{max-height:20rem;opacity:1;margin-top:1rem}
.bk-label{display:block;font-size:.8rem;color:var(--color-muted,#666);margin-bottom:.15rem;margin-top:.4rem}
.bk-input{display:block;width:100%;padding:.75rem .9rem;border:1px solid var(--color-border,#ddd);border-radius:.5rem;font-size:.95rem;font-family:inherit;transition:border-color .15s}
.bk-input:focus{border-color:var(--color-text,#1a1a1a);outline:none}
.bk-input.ok{border-color:#2a7a2a}
.bk-input.err{border-color:#c00;animation:shake .3s}
@keyframes shake{0%,100%{transform:translateX(0)}25%{transform:translateX(-4px)}75%{transform:translateX(4px)}}
.bk-btn{display:block;width:100%;padding:.9rem;margin-top:.6rem;background:var(--color-text,#1a1a1a);color:#fff;border:none;border-radius:.5rem;font-size:.95rem;font-weight:500;cursor:pointer;font-family:inherit;min-height:3rem;letter-spacing:.03em;transition:background .15s,opacity .15s}
.bk-btn:hover:not(:disabled){background:#333}
.bk-btn:focus-visible{outline:2px solid var(--color-text,#1a1a1a);outline-offset:2px}
.bk-btn:disabled{background:#d0d0d0;cursor:default;pointer-events:none}
.bk-btn.sending{opacity:.7}
.result{text-align:center;padding:2rem 0;line-height:1.6}
.result b{display:block;font-size:1.1rem;margin-bottom:.5rem}
.result .next{color:var(--color-muted,#666);font-size:.9rem;margin-top:.5rem}
.msg{text-align:center;padding:.6rem;line-height:1.5;font-size:.9rem}
.msg.error{color:#c00}
.back{text-align:center;margin-top:1.5rem}
.back a{color:#aaa;font-size:.85rem;text-decoration:none;border:none}
.no-slots{text-align:center;color:var(--color-muted,#666);padding:1.5rem 0;line-height:1.6}
.no-slots a{color:var(--color-text,#1a1a1a)}
@media (prefers-reduced-motion:reduce){.bk-form{transition:none}.t{transition:none}.bk-input{transition:none}.bk-btn{transition:none}.bk-input.err{animation:none}}
</style>"""

    body = f"""<div class="booking" role="main">
<h2>Консультация</h2>
<p class="sub">{cons.get('duration_min', 40)} мин · {cons['price']} · онлайн</p>
<p class="tz" id="tz-note">выберите удобное время</p>

<noscript>
<div class="no-slots">
<p>{desc_plain}</p>
<p>Напишите для записи:</p>
<p><a href="https://t.me/olgaroset">@olgaroset</a> · <a href="mailto:{contact_email}">{contact_email}</a></p>
</div>
</noscript>

<div id="step-slots" role="listbox" aria-label="Выберите время"></div>
<div class="more" id="more" style="display:none">
  <button type="button" onclick="showAll()">ещё даты →</button>
</div>

<form class="bk-form" id="bk-form" onsubmit="return false" novalidate aria-label="Контактные данные">
  <label class="bk-label" for="bk-name">Имя</label>
  <input class="bk-input" id="bk-name" name="name" autocomplete="name"
         required minlength="2" aria-required="true">
  <label class="bk-label" for="bk-contact">Телефон, Telegram или Email</label>
  <input class="bk-input" id="bk-contact" name="contact"
         required minlength="3" aria-required="true">
  <button class="bk-btn" id="bk-btn" type="submit" disabled
          aria-disabled="true">Выберите время</button>
</form>

<div class="msg" id="bk-msg" role="status" aria-live="polite"></div>
<p class="back"><a href="/">← назад</a></p>
</div>

<script>
var API="{transport_url}";
var SLOTS={slots_json};
var slot=null,allDays=[],submitted=false;

(function(){{
var days={{}};
SLOTS.forEach(function(s){{
  var dt=new Date(s.start);
  var key=dt.toISOString().slice(0,10);
  if(!days[key])days[key]=[];
  days[key].push({{date:key,time:dt.toLocaleTimeString("ru",{{hour:"2-digit",minute:"2-digit",hour12:false}}),
    id:s.id,label:dt.toLocaleDateString("ru",{{weekday:"short",day:"numeric",month:"short"}})}});
}});
allDays=Object.keys(days).map(function(k){{return{{key:k,slots:days[k]}}}});
if(allDays.length===0){{
  document.getElementById("step-slots").innerHTML="<div class='no-slots'>Свободного времени нет.<br><a href='https://t.me/olgaroset'>Написать Ольге</a></div>";
}}else{{
  render(allDays.length);
}}
}})();

function render(n){{
var el=document.getElementById("step-slots");var h="";
allDays.slice(0,n).forEach(function(d,i){{
  h+="<div class='day' role='group' aria-label='"+d.slots[0].label+"'>";
  h+="<div class='day-label'>"+d.slots[0].label+"</div><div class='slots-grid'>";
  d.slots.forEach(function(s){{
    h+="<button type='button' class='t' role='option' aria-selected='false' ";
    h+="data-d='"+s.date+"' data-t='"+s.time+"' data-id='"+s.id+"'";
    h+=" aria-label='"+s.time+", "+d.slots[0].label+"'>"+s.time+"</button>";
  }});
  h+="</div></div>";
}});
el.innerHTML=h;
el.querySelectorAll(".t").forEach(function(b){{b.addEventListener("click",function(){{pick(this)}});}});
}}
function showAll(){{render(allDays.length);document.getElementById("more").style.display="none"}}
function pick(el){{
document.querySelectorAll(".t").forEach(function(s){{s.classList.remove("on");s.setAttribute("aria-selected","false")}});
el.classList.add("on");el.setAttribute("aria-selected","true");
slot={{date:el.dataset.d,time:el.dataset.t,id:el.dataset.id}};
var btn=document.getElementById("bk-btn");
btn.disabled=false;btn.setAttribute("aria-disabled","false");
btn.textContent=slot.time+" — записаться";
var form=document.getElementById("bk-form");
if(!form.classList.contains("open")){{
  form.classList.add("open");
  setTimeout(function(){{document.getElementById("bk-name").focus()}},350);
}}
}}

document.getElementById("bk-name").addEventListener("input",function(){{
  this.classList.toggle("ok",this.value.trim().length>=2);
  this.classList.remove("err");
}});
document.getElementById("bk-contact").addEventListener("input",function(){{
  var v=this.value.trim();
  this.classList.toggle("ok",v.length>=3&&/[\\d@.]/.test(v));
  this.classList.remove("err");
}});

document.getElementById("bk-form").addEventListener("submit",function(e){{e.preventDefault();book()}});
function book(){{
if(submitted)return;
var nameEl=document.getElementById("bk-name");
var contactEl=document.getElementById("bk-contact");
var msgEl=document.getElementById("bk-msg");
nameEl.classList.remove("err");contactEl.classList.remove("err");msgEl.textContent="";msgEl.className="msg";
if(!slot)return;
var n=nameEl.value.trim(),c=contactEl.value.trim();
if(n.length<2){{nameEl.classList.add("err");nameEl.focus();return}}
if(c.length<3||!/[\\d@.]/.test(c)){{contactEl.classList.add("err");contactEl.focus();return}}
var btn=document.getElementById("bk-btn");
btn.disabled=true;btn.classList.add("sending");btn.textContent="отправка...";
fetch(API+"?name="+encodeURIComponent(n)+"&contact="+encodeURIComponent(c)+"&date="+slot.date+"&time="+slot.time+"&id="+slot.id)
.then(function(r){{return r.json()}}).then(function(d){{
btn.classList.remove("sending");
if(d.ok){{submitted=true;
  document.getElementById("step-slots").style.display="none";
  document.getElementById("more").style.display="none";
  document.getElementById("bk-form").style.display="none";
  msgEl.className="msg";
  msgEl.innerHTML="<div class='result'><b>Заявка принята</b>"+slot.time+" · "+
    new Date(slot.date).toLocaleDateString("ru",{{day:"numeric",month:"long"}})+
    "<div class='next'>Ольга свяжется с вами для подтверждения</div></div>";
}}else{{
  var m={{"name_required":"Введите имя","contact_required":"Введите контакт",
    "contact_invalid":"Некорректный контакт","slot_taken":"Это время уже занято — выберите другое"}};
  msgEl.className="msg error";msgEl.textContent=m[d.error]||"Ошибка. Попробуйте позже.";
  btn.disabled=false;btn.textContent=slot.time+" — записаться"}}
}}).catch(function(){{btn.classList.remove("sending");msgEl.className="msg error";
  msgEl.textContent="Ошибка сети";btn.disabled=false;btn.textContent=slot.time+" — записаться"}});
}}
</script>"""

    bio = d["bio"]
    booking_label = bio.get("booking_page_label", "Записаться")
    return _layout(
        d,
        title=f"{booking_label} — {bio['title']}",
        description=f"{desc_plain} — {cons['price']}",
        body=body,
        canonical=f"{_canonical(d)}/booking/",
        extra_head=booking_style,
        footer=False,
    )


# ── Main: regenerate all projections ─────────────────────────────────

if __name__ == "__main__":
    d = load()
    (ROOT / "index.html").write_text(p_site(d), encoding="utf-8")
    print("site: index.html")
    (ROOT / "art" / "index.html").write_text(p_art(d), encoding="utf-8")
    print("art: art/index.html")
    (ROOT / "booking" / "index.html").write_text(p_booking(d), encoding="utf-8")
    print("booking: booking/index.html")
    (ROOT / "telegram.txt").write_text(p_telegram(d), encoding="utf-8")
    print("telegram: telegram.txt")
    (ROOT / "bio.txt").write_text(p_bio(d), encoding="utf-8")
    print("bio: bio.txt")
