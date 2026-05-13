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
import functools as _functools
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
from dataclasses import dataclass, field as _dc_field
from functools import lru_cache as _lru_cache

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
        # Lookahead: any non-whitespace next-char triggers binding. Earlier strict
        # class [\wа-яёА-ЯЁ\d«„] missed [ (markdown link), < (HTML tag), digits
        # с em-dash, brackets — orphan slipped through these. Relaxed к \S covers
        # все non-whitespace destinations (admin 2026-05-10 strict audit).
        prep_re = _re.compile(
            rf"(?<![\w])({prep_alt})\s+(?=\S)",
        )
    return unit_re, prep_re


@_lru_cache(maxsize=16)
def _typo_compiled(lang: str) -> tuple:
    """Per-language compiled NBSP regexes. Cached — first call per lang
    loads YAML + compiles; subsequent calls reuse. Adding new language =
    drop knowledge/system/typography/<lang>.yaml; no code change.

    Spec: knowledge/system/specifications/text/typography.md
          (Inv-TYPO-no-hanging-words, Inv-TYPO-thin-space-numbers).
    """
    return _compile_typo_regexes(_load_typo_rules(lang))


def _typo(s: str, lang: str = "ru") -> str:
    """Apply typographic NBSP-glue per System rules (knowledge/system/typography).

    Idempotent: NBSP в input pass-through (regex \\s class ≠ NBSP).
    Language parameter dispatches к per-lang compiled rules; cached.
    Default 'ru' (current owner Olga); render-call sites can override
    via event.languages.host или explicit lang.

    Effect-supersystem: every text-bearing field across every projection
    typographically correct without per-page intervention. Rules — data
    (YAML, single SoT per lang); per `feedback_no_hardcode_through_abstractions`.
    """
    if not s:
        return s
    unit_re, prep_re = _typo_compiled(lang)
    out = s
    if unit_re is not None:
        out = unit_re.sub(r"\1" + _NBSP + r"\2", out)
    if prep_re is not None:
        out = prep_re.sub(r"\1" + _NBSP, out)
    # Inv-TYPO-apostrophe-curly: straight ' → curly ’ (U+2019).
    # Conservative: only between alphanumeric boundaries (don't touch code/quotes).
    out = _re.sub(r"(\w)'(\w)", r"\1’\2", out, flags=_re.UNICODE)
    # Inv-TYPO-en-dash-vs-em — Spec proof is `deferred` (Phase 3: text-scan + admin
    # discipline). The earlier eager `(\d)-(\d)→\1–\2` substitution mangled ISO dates
    # «2026-05-13»→«2026–05–13» and phone numbers system-wide; removed to match the Spec.
    return out


def _t(s) -> str:
    """Typography-fix + escape arbitrary text для safe HTML inclusion.

    Formal law: HTML := AttributeLanguage ⊔ BodyLanguage (disjoint grammars).
        _t      : str → AttributeSafe(HTML)   (escape + typo; NO span markup)
        _inline : str → BodySafe(HTML)        (escape + typo + math-rel wrap)
        BodySafe ⊄ AttributeSafe  (wrap injects `"` that breaks attribute quoting)

    Use _t для HTML attribute values (content=, alt=, aria-label=, …) и для
    plaintext-equivalent inclusion (meta description). Use _inline для inside-
    element body content где math-rel span CSS-aligns relation glyphs.

    Regression history: 2026-05-12 conflated _t with math-rel wrap → meta description
    content="…<span class="math-rel">↔</span>…" broke parser via quote collision;
    span markup escaped к viewport. Playwright visual subagent caught it. Split
    enforced as formal law."""
    if s is None:
        return ""
    return _html.escape(_typo(str(s)), quote=True)


def _h(s) -> str:
    """Typography-fix + pass-through for fields with curated markup
    (admin-authored, schema-marked as carrying <strong>/<em>). Still
    scrubs None → ''."""
    return "" if s is None else _typo(str(s))


_HTML_TAG_RE = _re.compile(r"(<[^>]+>)")

# Inv-LDG-graph-augment-word-boundary: token wrap must not split mid-word.
# «Парижский» / «Парижа» share prefix «Париж»; plain str.replace would
# wrap the prefix and leave a dangling Cyrillic suffix outside the <em>,
# producing `<em…>Париж</em>ский` — broken semantics + visual hierarchy
# bug. Boundary check uses a Unicode letter-class lookbehind/lookahead
# Foreign-name marking subsystem retired 2026-05-12 (admin: «не нужна Спецификация
# выделения иностранных слов»). Eliminated: `_h_aug`, `_em_loc_attrs`,
# `_build_lang_resolver`, `_wrap_at_word_boundary`, the `*name*` markdown-marker
# в `_inline`, the registry-augmentation block в p_event_landing. CSS rule
# `.article-wrapper em.loc` removed from styles.css; Specs Inv-TYPO-body-inline-
# emphasis-weight-bound and Inv-SEM-lang-of-parts retired. Hover-tooltips, when
# needed, attach as generic mechanism (e.g., <abbr title>) — not foreign-name
# auto-wrap.


_MD_LINK_RE = _re.compile(r'\[([^\]]+)\]\(([^)]+)\)')


def _md_links(s: str) -> str:
    """Convert markdown-style [text](url) → <a href="url">text</a>.
    Applied AFTER html-escape (square brackets/parens preserved by escape).
    Used in places admin authors anchor-markup в data.yaml prose (subevent
    description, contact, etc.)."""
    def _repl(m):
        text = m.group(1)
        url = _u(m.group(2))
        return f'<a href="{url}">{text}</a>'
    return _MD_LINK_RE.sub(_repl, s)


def _inline(s) -> str:
    """Render text → BODY-safe HTML: html-escape + typographic normalisation (_typo) +
    math-rel wrap (Inv-TYPO-math-rel-aligned).

    Formal: _inline : str → BodySafe(HTML). BodySafe contains `<span class="math-rel">`
    markup що legal в element body but ILLEGAL в attribute value (quote collision —
    see _t docstring). Use _inline ТОЛЬКО for content inserted between `>…<` tags;
    never inside `="…"`.

    Math-rel wrap is data-driven (text/typography.md::enforcement_data.math_symbols.
    relation_codepoints); CSS rule `.math-rel { vertical-align: var(--math-rel-shift); }`
    corrects per-font baseline drift. New glyph = data edit (codepoint to relation_codepoints).

    Earlier proper-noun marker (`*name*` → em.loc + graph-augment) retired 2026-05-12 —
    foreign-name highlighting decommissioned."""
    return "" if not s else _wrap_math_rel(_html.escape(_typo(str(s)), quote=True))



def _paras(text) -> list[str]:
    """Normalize a text field to a list of paragraph strings.

    Inv-SEMANTIC-WHITESPACE — admin's blank-line separators in source
    (md `\\n\\n` between beats, or yaml list[str] explicit) become distinct
    paragraphs everywhere. Renderers iterate this list and emit one
    block element (`<p>`, `<dd>`, `<li>`) per paragraph.

    Accepts: None, str, list[str|None]. Returns: list[str] (possibly empty).
    """
    if not text:
        return []
    if isinstance(text, list):
        return [str(p).strip() for p in text if p and str(p).strip()]
    # String input: split on blank-line separator (Inv-SEMANTIC-WHITESPACE).
    # YAML `text: |` blocks preserve `\n\n` breaks — those become distinct
    # paragraphs in render. Single-paragraph strings (no `\n\n`) trivially
    # return a one-element list.
    return [p.strip() for p in str(text).split("\n\n") if p.strip()]


# ── Render-time placeholders & block-close typography ────────────────
_CURRENCY_GLYPH: "dict[str, str]" = {"EUR": "€", "USD": "$", "RUB": "₽", "GBP": "£"}  # ISO-4217 → symbol; единый SoT, не inline-литерал
_PLACEHOLDER_RE = _re.compile(r"\{\{\s*([a-z_][a-z0-9_]*)\s*\}\}")


def _resolve_placeholders(text: str, ph: "dict[str, str]") -> str:
    """Substitute {{name}} → ph[name] (leaves unknown tokens literal). Lets admin's prose
    reference computed display-values — e.g. {{team_fee_half}} (admin 2026-05-11 «для
    программного разрешения»)."""
    if not text or not ph or "{{" not in text:
        return text
    return _PLACEHOLDER_RE.sub(lambda m: ph.get(m.group(1), m.group(0)), text)


@_lru_cache(maxsize=1)
def _no_terminal_period_cfg() -> "tuple[object, object]":
    """Inv-TYPO-no-terminal-period-block — config from the Spec, NOT hardcoded here:
    knowledge/system/specifications/text/typography.md::enforcement_data.no_terminal_period_block
    → (strip_re, keep_abbrev_re). Sole SoT for the char / abbreviation lists.
    The block-size gate retired 2026-05-12 (admin «Развлечение» symptom) — rule fires
    on any non-empty paragraph chain's last element, including single-string fragments."""
    here = Path(__file__).resolve()
    cfg: dict = {}
    for parent in here.parents:
        spec = parent / "knowledge" / "system" / "specifications" / "text" / "typography.md"
        if spec.is_file():
            chunks = spec.read_text(encoding="utf-8").split("---", 2)
            if len(chunks) >= 3:
                fm = yaml.safe_load(chunks[1]) or {}
                cfg = (fm.get("enforcement_data") or {}).get("no_terminal_period_block") or {}
            break
    strip_char = str(cfg.get("strip") or ".")
    keep_chars = list(cfg.get("keep_terminal") or ["?", "!", "…", "»", "”", ")", ":"])
    abbrevs = list(cfg.get("keep_if_abbrev") or ["г", "гг", "руб", "р", "км", "м"])
    no_strip_cls = "".join(_re.escape(c) for c in (*keep_chars, strip_char))
    esc_strip = _re.escape(strip_char)            # «.» → «\.» — already a literal-match atom
    strip_re = _re.compile(rf"(?<=[^{no_strip_cls}]){esc_strip}$")
    abbr_alt = "|".join(_re.escape(a) for a in sorted(abbrevs, key=len, reverse=True))
    keep_re = _re.compile(rf"(?:^|\s|\()(?:{abbr_alt}){esc_strip}$", _re.I) if abbr_alt else None
    return strip_re, keep_re


def _text_close_no_period(s: str) -> str:
    """The rule on string shape — strip the single terminal «.» from a closed fragment,
    keeping sentence-terminal punctuation and abbreviation-dots. Idempotent. Spec-driven.
    The list-shape wrapper is `_drop_block_close_period`."""
    if not s:
        return s
    strip_re, keep_re = _no_terminal_period_cfg()
    return s if (keep_re is not None and keep_re.search(s)) else strip_re.sub("", s)


@_lru_cache(maxsize=1)
def _math_symbols_cfg() -> dict:
    """Inv-TYPO-math-rel-aligned + Inv-TYPO-comparator-symbolic config from Spec:
    knowledge/system/specifications/text/typography.md::enforcement_data.math_symbols.
    Returns dict with relation_codepoints (list), comparator_glyphs (dict),
    comparator_prose (dict[locale][comp]), css_class (str). Sole SoT — no hardcode."""
    here = Path(__file__).resolve()
    cfg: dict = {}
    for parent in here.parents:
        spec = parent / "knowledge" / "system" / "specifications" / "text" / "typography.md"
        if spec.is_file():
            chunks = spec.read_text(encoding="utf-8").split("---", 2)
            if len(chunks) >= 3:
                fm = yaml.safe_load(chunks[1]) or {}
                cfg = (fm.get("enforcement_data") or {}).get("math_symbols") or {}
            break
    return cfg


@_lru_cache(maxsize=1)
def _math_rel_wrap_re() -> "_re.Pattern":
    """Compiled regex для relation-codepoint span-wrap. Codepoint set declared в Spec
    (data, не code); regex built once at module-load. Adding new glyph = data edit."""
    codepoints = _math_symbols_cfg().get("relation_codepoints") or []
    if not codepoints:
        return _re.compile(r"(?!)")   # never-match fallback
    esc = "".join(_re.escape(c) for c in codepoints)
    return _re.compile(f"([{esc}])")


def _wrap_math_rel(s: str) -> str:
    """Inv-TYPO-math-rel-aligned — wrap each relation-glyph occurrence в
    <span class="math-rel">…</span>. Pure: no I/O. Skips HTML tags via simple split
    (substitutes only в text portions between «<…>» tags). Idempotent: already-wrapped
    glyphs sit inside <span>…</span> tag-portions which are skipped on re-pass."""
    if not s:
        return s
    pat = _math_rel_wrap_re()
    css_class = _math_symbols_cfg().get("css_class") or "math-rel"
    parts = _re.split(r"(<[^>]+>)", s)
    for i, part in enumerate(parts):
        if part and not part.startswith("<"):
            parts[i] = pat.sub(rf'<span class="{css_class}">\1</span>', part)
    return "".join(parts)


def _comparator_glyph(comp: str) -> str:
    """Inv-TYPO-comparator-symbolic — comparator name → math glyph (locale-agnostic).
    Default identity если comparator не в таблице (graceful degrade). SoT в Spec."""
    return (_math_symbols_cfg().get("comparator_glyphs") or {}).get(str(comp).lower(), str(comp))


def _comparator_prose(comp: str, locale: str = "ru") -> str:
    """Inv-TYPO-comparator-symbolic — comparator name → locale prose (для aria-label / SEO).
    Empty string fallback если locale/comparator не в таблице."""
    table = (_math_symbols_cfg().get("comparator_prose") or {}).get(str(locale).lower(), {})
    return table.get(str(comp).lower(), "")


def _drop_block_close_period(paras: "list[str]") -> "list[str]":
    """The rule on list shape — strip the last paragraph's terminal «.». For any non-empty
    chain (block of ≥1 paragraph). Idempotent. One abstraction for sections / day-notes /
    sub-event descriptions / «Об Организаторах». Single-string callers (top-banner) use
    `_text_close_no_period` directly."""
    return list(paras[:-1]) + [_text_close_no_period(paras[-1])] if paras else []


# ── Document outline (heading-tree) ──────────────────────────────────
#
# Pure function over already-rendered HTML. Used by audit tools and by
# accessibility checks (Inv-DOC-OUTLINE: exactly one h1; no skipped
# levels; no empty headings). Owner-agnostic, surface-agnostic.
#
# Data model:
#   Heading := {level: int, text: str, id: str, attrs: str, children: list}
#   Tree    := list[Heading], rooted at every level-1 heading; deeper
#             headings nest under nearest preceding shallower heading
#             (well-formed if no level skips upward by >1).

_HEADING_TAG_RE = _re.compile(r"^h[1-6]$")


def _serialize_attrs(tag) -> str:
    """Reconstruct the original attribute-string for a BS4 tag.

    The outline result preserves the `attrs` field (tag-only attribute
    string, as it appeared inside `<…>`) so auditors can distinguish
    `<h3 class="day-theme">` from a generic h3. BeautifulSoup parses
    attributes into a dict; we re-emit `key="value"` pairs in source
    order. Boolean attributes (no value) emitted as bare names.
    """
    parts: list[str] = []
    for k, v in tag.attrs.items():
        if v is None or v is True:
            parts.append(k)
            continue
        if isinstance(v, list):  # class, rel, etc. → space-joined
            v = " ".join(v)
        parts.append(f'{k}="{_html.escape(str(v), quote=True)}"')
    return " ".join(parts)


def document_outline(html: str) -> list[dict]:
    """Extract the heading-tree from rendered HTML.

    Returns a forest (list of root nodes); nodes have shape
        {'level': 1, 'text': '...', 'id': '...', 'attrs': '...', 'children': [...]}.
    Tag-only attribute-string is preserved (lets auditors distinguish
    `<h3 class="day-theme">` from a generic h3).

    Uses BeautifulSoup4 (html.parser) — handles nested tags, attribute
    quoting variants, malformed HTML, and document-order traversal
    correctly. Strips inner tags from heading text via `get_text()`.
    """
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html or "", "html.parser")
    flat: list[dict] = []
    for tag in soup.find_all(_HEADING_TAG_RE):
        level = int(tag.name[1])
        text = tag.get_text(separator="").strip()
        attrs_str = _serialize_attrs(tag)
        flat.append({
            "level": level,
            "text": text,
            "id": tag.get("id") or "",
            "attrs": attrs_str,
            "children": [],
        })
    # Build forest via stack: pop until top has shallower level.
    nodes: list[dict] = []
    stack: list[dict] = []
    for n in flat:
        while stack and stack[-1]["level"] >= n["level"]:
            stack.pop()
        if stack:
            stack[-1]["children"].append(n)
        else:
            nodes.append(n)
        stack.append(n)
    return nodes


def outline_audit(tree: list[dict]) -> list[dict]:
    """Inv-DOC-OUTLINE checks. Returns issues; empty list = clean.

    Each issue: {kind: str, where: str, detail: str}.
    Kinds: 'multiple_h1' | 'skipped_level' | 'empty_heading'.
    """
    issues: list[dict] = []

    def walk(nodes: list[dict], parent_level: int = 0):
        for n in nodes:
            if not n["text"]:
                issues.append({"kind": "empty_heading",
                               "where": f"h{n['level']}",
                               "detail": "heading element has no text"})
            if parent_level and n["level"] > parent_level + 1:
                issues.append({"kind": "skipped_level",
                               "where": f"h{n['level']} «{n['text'][:40]}»",
                               "detail": f"jump from h{parent_level} to h{n['level']}"})
            walk(n["children"], n["level"])

    h1s = [n for n in tree if n["level"] == 1]
    if len(h1s) > 1:
        issues.append({"kind": "multiple_h1",
                       "where": "document",
                       "detail": f"{len(h1s)} h1 elements; expect exactly 1"})
    walk(tree, 0)
    return issues


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

# Cover-line labels — schema-tokens → human-readable Russian label.
# Single SoT for catalogue-eyebrow text on event-landings (.cover-line).
# Future: migrate to data.yaml.format_labels if other owners need
# different vocabulary. Today's owner-set (olgarozet) uses these.
_FORMAT_LABELS = {
    "travel":   "Дизайн-Путешествие",
    "meeting":  "Встреча",
    "course":   "Курс",
    "lecture":  "Лекция",
    "walk":     "Прогулка",
    "workshop": "Мастер-класс",
    "trip":     "Путешествие",
}
_LANG_LABELS = {
    "ru": "По-русски",
    "en": "In English",
}


def load() -> dict:
    return yaml.safe_load(DATA.read_text(encoding="utf-8"))


# ── Event sibling .md — content body, NOT entity-graph (SoT-separation) ──
#
# Architectural contract (post-2026-05-07 SoT-migration):
#   data.yaml events[].id == <slug>     — canonical for every entity-graph
#                                         field: title, t_key, date, status,
#                                         organizers, locations, places,
#                                         schedule, route_map, web_addresses,
#                                         broadcast, format, cohort, pricing,
#                                         signup, contact, sections, days,
#                                         lead, lines, inclusions, etc.
#   <slug>.md (sibling of data.yaml)    — supplementary free-form prose.
#                                         Frontmatter MUST be empty (or
#                                         contain only meta-keys NEVER used
#                                         in the yaml event-entry).
#
# Inv-EV-no-overlap (enforced by scripts/test_event_invariants.py):
#   for every event id E with a sibling .md, the set of frontmatter keys
#   and the set of yaml-entry keys MUST be disjoint. Field-level overlap
#   between the two SoTs is an architectural violation.
#
# `merge_event_with_md` is now a pure dict-extend: yaml event-entry plus a
# `body` slot carrying the raw markdown body. No field-level overlay, no
# H1/H2 parsing into structured fields. The body is currently archival —
# p_event_landing renders only from yaml-entry fields. A future renderer
# may consume `body` for long-form prose without re-introducing overlay
# (the slot name `body` is reserved and never appears in yaml-entry).


def _split_event_md(text: str) -> tuple[dict, str]:
    """Split <slug>.md into (frontmatter_dict, body_str). Pure function.

    Frontmatter delimiter is the standard YAML `---` fence pair. A file
    without frontmatter raises — the contract requires the fence even when
    frontmatter is empty (signals «author saw the empty-frontmatter
    invariant intentionally»).
    """
    if not text.lstrip().startswith("---"):
        raise ValueError("event .md missing YAML frontmatter (---…---)")
    lines = text.split("\n")
    start = next(i for i, l in enumerate(lines) if l.strip() == "---")
    end = next(i for i, l in enumerate(lines[start + 1:], start + 1)
               if l.strip() == "---")
    fm_text = "\n".join(lines[start + 1:end])
    body = "\n".join(lines[end + 1:])
    fm = yaml.safe_load(fm_text) or {}
    if not isinstance(fm, dict):
        raise ValueError("event .md frontmatter must be a YAML mapping (or empty)")
    return fm, body


def load_event_md_for(owner_site_dir, event_id: str):
    """Return (frontmatter, body) for `<owner_site_dir>/<event_id>.md`, or None.

    Returns None when the sibling file does not exist OR fails to parse —
    SoT remains the yaml event-entry; missing .md is not an error.
    """
    p = Path(owner_site_dir) / f"{event_id}.md"
    if not p.is_file():
        return None
    try:
        return _split_event_md(p.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        print(f"  event-md {p.name}: parse failed ({exc}) — using data.yaml entry only")
        return None


def merge_event_with_md(ev_yaml: dict, owner_site_dir) -> dict:
    """Extend yaml event-entry with sibling .md body. Pure dict-extend.

    Contract (Inv-EV-no-overlap, enforced by tests):
      • yaml event-entry is canonical for every entity-graph + content-as-data
        field (title, sections, days, lead, schedule, route_map, …).
      • <slug>.md is canonical only for free-form prose under the `body` key.
      • Zero field-level overlap between yaml-entry and md-frontmatter.

    Result shape: `{**ev_yaml, "body": <md_body_str>}` when sibling exists;
    plain `ev_yaml` (unmodified) otherwise. No field-level overlay, no
    H1/H2 parsing into structured fields. Frontmatter keys that survive
    the no-overlap test (none today) extend yaml only via this same
    pure-extend path — but the test ensures that set is disjoint from
    yaml-entry keys, so no key in yaml-entry can be silently shadowed.
    """
    res = load_event_md_for(owner_site_dir, ev_yaml.get("id", ""))
    if not res:
        return ev_yaml
    fm, body = res
    out = dict(ev_yaml)
    # Frontmatter extend (test asserts no overlap; extend is defensively
    # safe even if a stray key exists — yaml-entry would still win because
    # extend writes fm first then yaml).
    for k, v in fm.items():
        out.setdefault(k, v)
    out["body"] = body
    return out


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

def _theme_script(d: dict) -> str:
    """Day/night theme resolver — user-override-first, then solar-automatic.

    FOUC-critical: emitted inline in <head>, runs synchronously before paint
    (no defer/async). Resolution order at page load (Inv-IFACE-day-night-mode):
      1. localStorage[STORAGE_KEY] — if it's one of MODE_VALUES ('day'/'night')
         → that is the USER OVERRIDE, use it verbatim.
      2. else ('auto', absent, or any other value) → solar altitude:
         alt > alt_threshold ? 'day' : 'night' (the AUTOMATIC default).
    document.documentElement[MODE_ATTR] := resolved.

    Two config sources, both data-not-code:
      • Inv-SITE-solar-theme (text/site.md) — the SOLAR calibration:
        default_latitude_deg, default_alt_threshold_rad, refresh_ms. Per-owner
        override via data.yaml.bio.solar_calibration. (Site-specific — the
        solar calc is the site channel's realization of the auto default.)
      • Inv-IFACE-day-night-mode (text/interface.md) — the CROSS-CHANNEL bits:
        mode_attr, mode_values, storage_key, toggle_states. Falls back
        gracefully when the Spec is absent (same discipline as the solar block).

    Solar math: closed-form Michalsky 1988 altitude. Longitude ≈ -tzOffset/4
    (15°/h) — visitor's browser-reported tz drives the lon term; threshold
    alt > alt_threshold keeps the page 'day' through civil twilight (admin
    observed «ещё относительно светло» в Moscow while a naive 0 cutoff had
    already flipped to night).

    Exposes window.__applyTheme() so the toggle click-handler (in _layout) can
    re-resolve after the visitor changes the stored state. setInterval re-applies
    every refresh_ms so a long session in `auto` flips at sunrise/sunset (a
    fixed override is re-asserted harmlessly).
    """
    try:
        from spec_data import enforcement_data_for_invariant as _spec_enforcement_data
        solar = _spec_enforcement_data("Inv-SITE-solar-theme") or {}
    except Exception:
        solar = {}
    try:
        from spec_data import enforcement_data_for_invariant as _sed2
        iface = _sed2("Inv-IFACE-day-night-mode") or {}
    except Exception:
        iface = {}
    cal = ((d.get("bio") or {}).get("solar_calibration") or {}) \
        if isinstance(d, dict) else {}
    lat = float(cal.get("latitude_deg",
                        solar.get("default_latitude_deg", 55)))
    alt_thr = float(cal.get("alt_threshold_rad",
                            solar.get("default_alt_threshold_rad", -0.1)))
    refresh_ms = int(solar.get("refresh_ms", 300000))
    mode_attr = str(iface.get("mode_attr") or "data-theme")
    mode_values = list(iface.get("mode_values") or ["day", "night"])
    storage_key = str(iface.get("storage_key") or "dela.theme.v1")
    import json as _json
    mv_js = _json.dumps(mode_values)
    sk_js = _json.dumps(storage_key)
    ma_js = _json.dumps(mode_attr)
    return f"""<script>
// Day/night theme — user-override-first, then solar-automatic. FOUC-critical:
// runs synchronously in <head> before paint. Resolution: read {storage_key}
// from localStorage; if ∈ {mode_values} use it (user override) else solar
// altitude (lat {lat}° / alt-threshold {alt_thr} rad). Config: solar calc from
// spec.enforcement_data.Inv-SITE-solar-theme (+ data.yaml.bio.solar_calibration
// override); attr/key/values from spec.enforcement_data.Inv-IFACE-day-night-mode.
// No hardcoded constants in code — both Specs are SoT, code falls back gracefully.
(function(){{
  var MODE_ATTR={ma_js}, MODE_VALUES={mv_js}, STORAGE_KEY={sk_js};
  function solarTheme(){{
    var r=Math.PI/180, now=new Date();
    var J=now.valueOf()/86400000 + 2440587.5 - 2451545.0;
    var L=(280.460+0.9856474*J)%360;
    var g=((357.528+0.9856003*J)%360)*r;
    var lam=(L+1.915*Math.sin(g)+0.020*Math.sin(2*g))*r;
    var eps=(23.439-4e-7*J)*r;
    var dec=Math.asin(Math.sin(eps)*Math.sin(lam));
    var ra=Math.atan2(Math.cos(eps)*Math.sin(lam), Math.cos(lam));
    var lon=-now.getTimezoneOffset()/4, lat={lat};
    var gmst=(18.697374558+24.06570982441908*J)*15;
    var H=((gmst+lon)%360)*r - ra;
    var alt=Math.asin(Math.sin(lat*r)*Math.sin(dec)+Math.cos(lat*r)*Math.cos(dec)*Math.cos(H));
    return alt > {alt_thr} ? 'day' : 'night';
  }}
  // stored override ∈ MODE_VALUES → use it; else (incl. 'auto', absent, bad) → solar.
  window.__resolveTheme=function(){{
    var s=null; try{{ s=localStorage.getItem(STORAGE_KEY); }}catch(e){{}}
    return (MODE_VALUES.indexOf(s) !== -1) ? s : solarTheme();
  }};
  window.__applyTheme=function(){{
    document.documentElement.setAttribute(MODE_ATTR, window.__resolveTheme());
  }};
  window.__applyTheme();
  setInterval(window.__applyTheme, {refresh_ms});
}})();
</script>"""

IG_SVG = '<svg viewBox="0 0 24 24" fill="currentColor"><path d="M12 2.163c3.204 0 3.584.012 4.85.07 3.252.148 4.771 1.691 4.919 4.919.058 1.265.069 1.645.069 4.849 0 3.205-.012 3.584-.069 4.849-.149 3.225-1.664 4.771-4.919 4.919-1.266.058-1.644.07-4.85.07-3.204 0-3.584-.012-4.849-.07-3.26-.149-4.771-1.699-4.919-4.92-.058-1.265-.07-1.644-.07-4.849 0-3.204.013-3.583.07-4.849.149-3.227 1.664-4.771 4.919-4.919 1.266-.057 1.645-.069 4.849-.069zm0-2.163c-3.259 0-3.667.014-4.947.072-4.358.2-6.78 2.618-6.98 6.98-.059 1.281-.073 1.689-.073 4.948 0 3.259.014 3.668.072 4.948.2 4.358 2.618 6.78 6.98 6.98 1.281.058 1.689.072 4.948.072 3.259 0 3.668-.014 4.948-.072 4.354-.2 6.782-2.618 6.979-6.98.059-1.28.073-1.689.073-4.948 0-3.259-.014-3.667-.072-4.947-.196-4.354-2.617-6.78-6.979-6.98-1.281-.059-1.69-.073-4.949-.073zm0 5.838c-3.403 0-6.162 2.759-6.162 6.162s2.759 6.163 6.162 6.163 6.162-2.759 6.162-6.163c0-3.403-2.759-6.162-6.162-6.162zm0 10.162c-2.209 0-4-1.79-4-4 0-2.209 1.791-4 4-4s4 1.791 4 4c0 2.21-1.791 4-4 4zm6.406-11.845c-.796 0-1.441.645-1.441 1.44s.645 1.44 1.441 1.44c.795 0 1.439-.645 1.439-1.44s-.644-1.44-1.439-1.44z"/></svg>'

TG_SVG = '<svg viewBox="0 0 24 24" fill="currentColor"><path d="M11.944 0A12 12 0 0 0 0 12a12 12 0 0 0 12 12 12 12 0 0 0 12-12A12 12 0 0 0 12 0a12 12 0 0 0-.056 0zm4.962 7.224c.1-.002.321.023.465.14a.506.506 0 0 1 .171.325c.016.093.036.306.02.472-.18 1.898-.962 6.502-1.36 8.627-.168.9-.499 1.201-.82 1.23-.696.065-1.225-.46-1.9-.902-1.056-.693-1.653-1.124-2.678-1.8-1.185-.78-.417-1.21.258-1.91.177-.184 3.247-2.977 3.307-3.23.007-.032.014-.15-.056-.212s-.174-.041-.249-.024c-.106.024-1.793 1.14-5.061 3.345-.48.33-.913.49-1.302.48-.428-.008-1.252-.241-1.865-.44-.752-.245-1.349-.374-1.297-.789.027-.216.325-.437.893-.663 3.498-1.524 5.83-2.529 6.998-3.014 3.332-1.386 4.025-1.627 4.476-1.635z"/></svg>'

SCROLL_UP_SVG = '<svg width="40" height="40" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><path d="M12 20V4M5 11l7-7 7 7"/></svg>'


# ── Head / Footer / Layout ───────────────────────────────────────────

def _styles_cache_bust() -> str:
    """Content-hash querystring for /styles.css → defeat CDN max-age=600 stale.

    Pages serves with `cache-control: max-age=600`. Without a unique URL per
    deploy, browsers + edge caches show stale CSS for up to 10 minutes after
    a typography change — admin sees the old layout while the new HTML is
    already live. Content-addressed querystring forces fresh fetch on every
    actual edit, but stays stable while CSS is unchanged.
    """
    try:
        import hashlib as _h
        # Two call sites:
        #   1) Deployed bundle (generate.py + styles.css co-located in repo
        #      root): ROOT/styles.css exists.
        #   2) System call (broadcast_html.update_site / update_landing
        #      imports sg from scripts/): walk up to knowledge/people/*/site/.
        cand: list[Path] = [ROOT / "styles.css"]
        # Walk up from this file until we find Dela home, then collect any
        # owner site-dir styles.css (single SoT per owner). Owner-agnostic.
        here = Path(__file__).resolve()
        for parent in here.parents:
            if (parent / "knowledge" / "people").is_dir():
                people = parent / "knowledge" / "people"
                for owner_dir in people.iterdir():
                    cand.append(owner_dir / "site" / "styles.css")
                break
        for p in cand:
            if p.is_file():
                return _h.sha1(p.read_bytes()).hexdigest()[:10]
    except Exception:
        pass
    return ""


def _head(title: str, description: str, *, canonical: str,
          og_image: str = "", extra: str = "", structured: str = None,
          d: dict | None = None) -> str:
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
<link rel="manifest" href="/manifest.json">
<meta name="theme-color" content="#ffffff" media="(prefers-color-scheme: light)">
<meta name="theme-color" content="#111111" media="(prefers-color-scheme: dark)">
<!-- Inv-WEB-font-preconnect (text/site.md): early DNS+TLS handshake к font CDN.
     Reduces FCP/LCP by ~100-300ms на TLS-cold connections. -->
<link rel="preconnect" href="https://fonts.bunny.net" crossorigin>
<link rel="dns-prefetch" href="https://fonts.bunny.net">
{_theme_script(d or {})}
<link rel="stylesheet" href="/styles.css{('?v=' + _bust) if (_bust := _styles_cache_bust()) else ''}">{sd}
{extra}"""


def _cookie_banner(d: dict) -> str:
    """Project data.yaml.legal.cookie_consent + privacy_url → 152-ФЗ banner.

    Renders ONLY when required=true AND privacy_url set; missing privacy_url
    produces no banner (silent default would claim consent for a non-existent
    policy — 152-ФЗ violation). Bottom non-modal placement (RU7+V3 reactance
    avoidance). Explicit accept (active action per 152-ФЗ); buttons ≥44px
    (Inv-LDG-design-touch44). localStorage `dela.cookie.v1` carries decision.
    """
    legal = (d.get("legal") or {}) if isinstance(d, dict) else {}
    cc = legal.get("cookie_consent") or {}
    if cc.get("required") is False:
        return ""
    privacy_url = _u(legal.get("privacy_url") or "")
    if not privacy_url:
        return ""
    placement = cc.get("banner_placement") or "bottom"
    # Copy + storage-key live in spec.enforcement_data.Inv-COOKIE-banner —
    # single SoT, no inline RU strings. Fail-loud on missing keys (cookie
    # banner that ships «{{undefined}}» to users is a 152-ФЗ violation worse
    # than no banner). Required keys: storage_key, heading, body_template,
    # privacy_link_text, accept_label, decline_label.
    try:
        from spec_data import enforcement_data_for_invariant as _spec_enforcement_data
        copy = _spec_enforcement_data("Inv-COOKIE-banner") or {}
    except Exception:
        copy = {}
    required_keys = ("storage_key", "heading", "body_template",
                     "privacy_link_text", "accept_label", "decline_label")
    missing = [k for k in required_keys if not copy.get(k)]
    if missing:
        raise RuntimeError(
            f"spec.enforcement_data.Inv-COOKIE-banner missing keys: "
            f"{missing} — no fallback (single SoT principle)"
        )
    storage_key = copy["storage_key"]
    heading = _t(copy["heading"])
    # body+link concatenated FIRST, _typo applied к whole — иначе «в » trailing
    # space в body_template don't bind с link_text starting word (Inv-TYPO-no-hanging-words).
    body_link_combined = _typo(copy["body_template"] + copy["privacy_link_text"])
    # Split back at known boundary (privacy_link_text не содержит ' ' inside —
    # safe). NBSP-bound prepositions sit на seam.
    _link_text_typo = _typo(copy["privacy_link_text"])
    body_text = body_link_combined[:-len(_link_text_typo)] if body_link_combined.endswith(_link_text_typo) else _typo(copy["body_template"])
    body_text = _html.escape(body_text, quote=True)
    link_text = _html.escape(_link_text_typo, quote=True)
    accept_label = _t(copy["accept_label"])
    decline_label = _t(copy["decline_label"])
    # storage_key edges into HTML as data-attribute value — escape via _t (the
    # universal HTML-text escaper). External JS reads it via getAttribute, so
    # there's no JS-string-literal context anywhere → no `'unsafe-inline'`,
    # no JS-escape edge cases. CSP-clean by construction.
    # Behaviour lives in /cookie-banner.js (per-owner static asset, mirrored
    # by broadcast_html.update_site / update_landing alongside styles.css).
    # Admin may adopt `script-src 'self'` per audit_runtime.csp_recommended.
    sk_attr = _t(storage_key)
    return (
        f'<div class="cookie-banner cookie-banner--{placement}" '
        'role="dialog" aria-labelledby="cookie-h" aria-describedby="cookie-d" '
        f'data-cookie-banner data-key="{sk_attr}" hidden>'
        f'<h2 id="cookie-h" class="visually-hidden">{heading}</h2>'
        '<p id="cookie-d" class="cookie-text">'
        f'{body_text}<a href="{privacy_url}">{link_text}</a>.'
        '</p>'
        '<div class="cookie-actions">'
        '<button type="button" class="cookie-accept" data-cookie-accept>'
        f'{accept_label}</button>'
        '<button type="button" class="cookie-decline" data-cookie-decline>'
        f'{decline_label}</button>'
        '</div>'
        '</div>'
        '<script src="/cookie-banner.js" defer></script>'
    )


# Glyphs for the theme-toggle button — one per toggle state. Unicode (no extra
# asset, scales with font-size). ☀ = forced day · ☾ = forced night · ◐ = auto
# (following the sun). Realized in _theme_toggle below; the click-handler swaps
# the glyph + aria-label to match the new state. (If the Spec later carries a
# `toggle_glyphs` map these become a fallback, same as every other constant here.)
_THEME_GLYPHS = {"auto": "◐", "day": "☀", "night": "☾"}
# aria-label fragments — Russian (site host language). The handler rebuilds the
# label client-side from the same shape, so keep the JS copy in sync below.
_THEME_LABELS = {
    "auto": "Тема: авто (по солнцу)",
    "day": "Тема: дневная",
    "night": "Тема: ночная",
}


def _theme_toggle(d: dict | None = None) -> str:
    """Day/night toggle control — chrome, rendered on EVERY page by _layout
    (like the cookie banner). Independent of the `nav` flag: event-bound FQDN
    landings suppress `.nav-fade` but MUST still expose the theme toggle.

    A <button class="theme-toggle"> fixed top-right (the .nav-fade back-arrow is
    top-left when present — no overlap; on FQDN landings .nav-fade is absent so
    top-right is clear either way). Native keyboard operation (it's a <button> —
    Enter/Space); :focus-visible outline via CSS. The glyph reflects the CURRENT
    toggle state (◐ auto / ☀ day / ☾ night); aria-label states the state and
    that activating it cycles. The accompanying <script> (deferred — purely
    progressive-enhancement; the FOUC-critical resolve already ran in <head>):
    on click, advance auto→day→night→auto, persist to localStorage[STORAGE_KEY]
    ('auto' is written EXPLICITLY rather than removeItem — one code path, the
    head resolver treats any non-MODE_VALUES string incl. 'auto' as "use solar"),
    call window.__applyTheme(), and update the button's glyph + aria-label. The
    button stays focused after click, so the refreshed aria-label is announced by
    screen readers (no separate live region needed). Honours prefers-reduced-motion
    via CSS (.theme-toggle transition guarded by the media query).
    """
    try:
        from spec_data import enforcement_data_for_invariant as _spec_enforcement_data
        iface = _spec_enforcement_data("Inv-IFACE-day-night-mode") or {}
    except Exception:
        iface = {}
    toggle_states = list(iface.get("toggle_states") or ["auto", "day", "night"])
    storage_key = str(iface.get("storage_key") or "dela.theme.v1")
    mode_values = list(iface.get("mode_values") or ["day", "night"])
    # Glyph/label maps — Spec-overridable, code-default. Restrict to the states
    # the Spec actually declares (so a Spec edit to `toggle_states` is honoured).
    glyphs = {s: (iface.get("toggle_glyphs") or {}).get(s, _THEME_GLYPHS.get(s, "◐"))
              for s in toggle_states}
    labels = {s: (iface.get("toggle_labels") or {}).get(s, _THEME_LABELS.get(s, "Тема"))
              for s in toggle_states}
    initial = toggle_states[0] if toggle_states else "auto"
    suffix = " — переключить"
    init_glyph = _t(glyphs.get(initial, "◐"))
    init_label = _t(labels.get(initial, "Тема") + suffix)
    import json as _json
    states_js = _json.dumps(toggle_states)
    glyphs_js = _json.dumps(glyphs, ensure_ascii=False)
    labels_js = _json.dumps(labels, ensure_ascii=False)
    sk_js = _json.dumps(storage_key)
    mv_js = _json.dumps(mode_values)
    suffix_js = _json.dumps(suffix, ensure_ascii=False)
    return (
        f'<button type="button" class="theme-toggle" data-theme-toggle '
        f'aria-label="{init_label}">{init_glyph}</button>'
        '<script>'
        '(function(){'
        f'var STATES={states_js},GLYPHS={glyphs_js},LABELS={labels_js},'
        f'STORAGE_KEY={sk_js},MODE_VALUES={mv_js},SUFFIX={suffix_js};'
        'var btn=document.querySelector("[data-theme-toggle]");if(!btn)return;'
        # Current toggle-state from storage: a MODE_VALUE means "forced"; anything
        # else (incl. "auto", absent, garbage) is the "auto" state.
        'function current(){var s=null;try{s=localStorage.getItem(STORAGE_KEY);}catch(e){}'
        'return (MODE_VALUES.indexOf(s)!==-1)?s:STATES[0];}'
        'function paint(st){btn.textContent=GLYPHS[st]||GLYPHS[STATES[0]];'
        'btn.setAttribute("aria-label",(LABELS[st]||LABELS[STATES[0]])+SUFFIX);}'
        'paint(current());'
        'btn.addEventListener("click",function(){'
        'var i=STATES.indexOf(current());var next=STATES[(i+1)%STATES.length];'
        # Persist explicitly — write "auto" too (not removeItem); the head
        # resolver treats any non-MODE_VALUES string as "use solar".
        'try{localStorage.setItem(STORAGE_KEY,next);}catch(e){}'
        'if(window.__applyTheme)window.__applyTheme();paint(next);});'
        '})();'
        '</script>'
    )


def _legal_footer(d: dict) -> str:
    """Project data.yaml.legal → quiet colophon-block. Pure projection: any
    field absent → omitted. Empty → ''. Single SoT: data.yaml.legal is admin-fill;
    Inv-SITE-trust-base passes when privacy_url + entity present.
    """
    legal = (d.get("legal") or {}) if isinstance(d, dict) else {}
    if not legal:
        return ""
    entity = legal.get("entity") or {}
    parts: list[str] = []

    ent_bits = []
    name = (entity.get("name") or "").strip()
    inn = (entity.get("inn") or "").strip()
    ogrn = (entity.get("ogrn") or "").strip()
    addr = (entity.get("address") or "").strip()
    if name:
        ent_bits.append(_t(name))
    if inn:
        ent_bits.append(f"ИНН {_t(inn)}")
    if ogrn:
        ent_bits.append(f"ОГРН {_t(ogrn)}")
    if addr:
        ent_bits.append(_t(addr))
    if ent_bits:
        parts.append(f'<p class="legal-entity">{" · ".join(ent_bits)}</p>')

    doc_links = []
    privacy = _u(legal.get("privacy_url") or "")
    if privacy:
        doc_links.append(f'<a href="{privacy}">Политика конфиденциальности</a>')
    oferta = _u(legal.get("oferta_url") or "")
    if oferta:
        doc_links.append(f'<a href="{oferta}">Договор-оферта</a>')
    if doc_links:
        parts.append(f'<p class="legal-docs">{" · ".join(doc_links)}</p>')

    pay = (legal.get("payment") or {}).get("methods") or []
    if pay:
        # Labels live in spec.enforcement_data.Inv-SITE-trust-base.payment_labels —
        # single SoT, не code-level dict. Fail-loud on unknown code: silently
        # echoing the raw enum to user-visible HTML breaks trust hygiene.
        try:
            from spec_data import enforcement_data_for_invariant as _spec_enforcement_data
            trust_ed = _spec_enforcement_data("Inv-SITE-trust-base") or {}
        except Exception:
            trust_ed = {}
        labels = trust_ed.get("payment_labels") or {}
        if not labels:
            raise RuntimeError(
                "spec.enforcement_data.Inv-SITE-trust-base.payment_labels "
                "missing — no fallback (single SoT principle)"
            )
        bits: list[str] = []
        for m in pay:
            if m not in labels:
                raise RuntimeError(
                    f"data.yaml.legal.payment.methods has unknown code "
                    f"{m!r}; known labels: {sorted(labels.keys())}"
                )
            bits.append(labels[m])
        parts.append(f'<p class="legal-payment">Оплата: {_t(" · ".join(bits))}</p>')

    if not parts:
        return ""
    return f'<footer class="legal" aria-label="Реквизиты и юридическая информация">{"".join(parts)}</footer>'


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
            extra_head: str = "", footer: bool = True, structured: str = None,
            surface: str = "", cookie_banner_enabled: bool = True) -> str:
    if canonical is None:
        canonical = _canonical(d)
    portrait = _portrait(d)
    portrait_night = _portrait_night(d)
    og_image = f"{_canonical(d)}/{portrait}" if portrait else ""
    head = _head(title, description, canonical=canonical, og_image=og_image,
                 extra=extra_head, structured=structured, d=d)
    nav_html = '<nav class="nav-fade"><a href="/" aria-label="На главную">←</a></nav>' if nav else ''
    ftr = _footer(d.get("urls", {}), d["bio"]["title"], portrait, portrait_night) if footer else ''
    # WCAG 2.4.1 «Bypass Blocks» — single skip-link before nav, jumps to <main>.
    # Visually hidden until keyboard focus; one definition serves every surface.
    skip_link = (f'<a class="skip-link" href="#main">{_typo("Перейти к содержанию")}</a>')
    # Day/night toggle — chrome, present on EVERY page regardless of `nav`
    # (FQDN landings suppress .nav-fade but keep the theme toggle). Inv-IFACE-day-night-mode.
    theme_toggle = _theme_toggle(d)
    cookie_banner = _cookie_banner(d) if cookie_banner_enabled else ""
    # Inv-SEM-html-lang: document language от data.yaml.languages.host —
    # single SoT за document-level lang. Fallback "ru" preserved for legacy
    # data.yaml without languages block; TODO: tighten к fail-loud once all
    # owner data.yaml's carry languages.host explicitly (single-SoT discipline).
    lang = (d.get("languages") or {}).get("host") or "ru"
    # `data-surface` activates an alternative palette over the same semantic
    # token vocabulary (--surface, --ink, --accent…). Default = hub theme.
    # `editorial` = event-landings + static-pages (concrete-paper / Outremer).
    # Single SoT — no parallel `:root`/`:has(.article-wrapper)` cascade hack.
    surface_attr = f' data-surface="{_t(surface)}"' if surface else ''
    return f"""<!DOCTYPE html>
<html lang="{_t(lang)}"{surface_attr}>
<head>
{head}
</head>
<body>
{skip_link}
{nav_html}
{theme_toggle}
<main id="main" role="main">
{body}
</main>
{ftr}
{cookie_banner}
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
            # Schema.org: addressCountry is on PostalAddress, not Place.
            # Place.address → PostalAddress.addressCountry. (Inv-SEM-jsonld-valid)
            obj["location"] = [
                {"@type": "Place",
                 "name": l.get("name", l.get("id", "")),
                 **({"address": {"@type": "PostalAddress",
                                 "addressCountry": l.get("country", "")}}
                    if l.get("country") else {})}
                for l in locs
            ]
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
    Filters к status=published — planned/draft/failed not rendered (correct
    public-feed semantics; admin's «published» = the predicate that gates surface
    inclusion per entity-publication.md::Inv-PUB-status-lifecycle). URL resolution
    graceful: link OR url OR skip (consistent с the same fallback at line ~1754
    used by p_event_landing publications-list).

    Status literal validated against entity-publication.status_taxonomy
    (Spec single SoT) — drift catches Spec mismatch at first render.
    """
    from publication_invariants import _canonical_state as _pub_state
    _published = _pub_state("published")
    pubs = sorted(
        [p for p in (d.get("publications") or []) if p.get("status") == _published],
        key=lambda p: (p.get("uploaded_at", "") or p.get("date", ""),),
        reverse=True,
    )
    if not pubs:
        return ""
    items = []
    for p in pubs:
        label = _CHANNEL_LABEL.get(p.get("channel", ""), p.get("channel", ""))
        url = p.get("link") or p.get("url") or ""
        if not url:
            continue   # published entry без URL = data error elsewhere; skip render
        items.append(
            f'        <li><a href="{_t(url)}" class="pub" rel="noopener">'
            f'<span class="pub-channel">{label}</span>'
            f'<span class="pub-title">{_t(p.get("title", ""))}</span></a></li>'
        )
    if not items:
        return ""
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
        # Hub-card CTA-link к dedicated FQDN landing (admin 2026-05-12 feedback.txt:
        # «вписать Событие и Кампанию вокруг него»). Inv-CMP-STYLE-CTA-anchor-uniform —
        # hub-event-card carries same canonical URL as campaign-style.cta_anchor.
        # Universal: ANY event с web_addresses gains a clickable «Подробнее» CTA;
        # not paris-specific. No data-flag (Genius Simplification — presence of
        # web_addresses already declares «has dedicated landing»).
        addrs = ev.get("web_addresses") or []
        if addrs:
            landing_url = f"https://{addrs[0]}/"
            lines.append(
                f'        <p class="event-cta">'
                f'<a href="{_t(landing_url)}" class="cta">Подробнее</a></p>'
            )
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

_EVENT_SLUG_RE = _re.compile(r"[a-z0-9_-]+")


def event_signup_form(slug: str, label: str, email_fallback: str,
                      cta_label: str = "Оставить email") -> str:
    """Mailto-fallback email-capture form. Async POST upgrade if
    <slug>/signup.json::transport_url is set (zero-credential default).

    `cta_label` parametrises the heading + button text (e.g. «Забронировать»
    when admin frames signup as Бронирование, не lead-collect). Default
    «Оставить email» preserved for back-compat.

    Slug validation: must match `[a-z0-9_-]+` (DNS-safe, URL-path-safe,
    HTML-attr-safe by construction). Untrusted YAML carrying spaces or
    Cyrillic в slug → раннее explicit failure, не silent broken URL /
    signup.json fetch + corrupted form action attr.
    """
    if not isinstance(slug, str) or not _EVENT_SLUG_RE.fullmatch(slug):
        raise ValueError(
            f"invalid event slug: {slug!r} — must match [a-z0-9_-]+"
        )
    import json as _json
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
    cta_html = _t(cta_label)
    cta_js = _json.dumps(cta_label, ensure_ascii=False)
    mb = ("%D0%97%D0%B4%D1%80%D0%B0%D0%B2%D1%81%D1%82%D0%B2%D1%83%D0%B9%D1%82%D0%B5%2C%20%D0%9E%D0%BB%D1%8C%D0%B3%D0%B0.%0A%0A"
          f"%D0%9E%D1%81%D1%82%D0%B0%D0%B2%D0%BB%D1%8F%D1%8E%20%D0%BA%D0%BE%D0%BD%D1%82%D0%B0%D0%BA%D1%82%20%E2%80%94%20{label_q}.%0A%0A"
          "%D0%98%D0%BC%D1%8F:%20%0A%20Email:%20%0A"
          "%D0%9E%20%D1%81%D0%B5%D0%B1%D0%B5%20(%D1%81%D1%84%D0%B5%D1%80%D0%B0%2C%20%D0%B3%D0%BE%D1%80%D0%BE%D0%B4):%20%0A")
    # Slug is admin-controlled identifier — escape for safe HTML/attr/URL.
    slug_t = _t(slug)
    # Form labels — typography-cleaned (Inv-TYPO-no-hanging-words, NBSP-bind preps).
    lbl_name    = _typo("Имя")
    lbl_email   = _typo("Email")
    lbl_about   = _typo("Коротко о себе")
    lbl_about_h = _typo("(сфера, город — опционально)")
    lbl_consent = _typo("Согласен(-на) на обработку персональных данных для ответа по программе.")
    lbl_or      = _typo("Или напишите:")
    # Form heading is <h3> (parent <section class=signup-wrap> already
    # provides the section's <h2 «Лист ожидания»>). Heading hierarchy
    # h2 → h3 is WCAG-correct and screen-reader-friendly.
    return f'''<section id="signup" class="signup" aria-labelledby="signup-h">
  <h3 id="signup-h" class="signup-h3">{cta_html}</h3>
  <form id="signup-form" class="signup-form" novalidate
        aria-labelledby="signup-h"
        action="mailto:{email_q}?subject={subj_q}&amp;body={mb}"
        method="post" enctype="text/plain"
        data-slug="{slug_t}">
    <label class="signup-label" for="su-name">{lbl_name}</label>
    <input class="signup-input" id="su-name" name="name"
           autocomplete="name" required minlength="2" aria-required="true">
    <label class="signup-label" for="su-email">{lbl_email}</label>
    <input class="signup-input" id="su-email" name="email" type="email"
           autocomplete="email" required aria-required="true">
    <label class="signup-label" for="su-note">{lbl_about}
      <span class="signup-hint">{lbl_about_h}</span></label>
    <input class="signup-input" id="su-note" name="note" autocomplete="off">
    <label class="signup-consent" for="su-consent">
      <input type="checkbox" id="su-consent" name="consent" required
             aria-required="true"
             aria-label="{lbl_consent}">
      <span>{lbl_consent}</span>
    </label>
    <button class="signup-btn" type="submit" id="su-btn">{cta_html}</button>
  </form>
  <div class="signup-msg" id="signup-msg" role="status" aria-live="polite"></div>
  <noscript><p class="signup-note">{lbl_or} <a href="mailto:{email_q}">{_t(email_fallback)}</a></p></noscript>
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
          msg.textContent="Заявка принята. Свяжемся лично.";
        }}else{{msg.textContent="Ошибка. Попробуйте ещё раз или напишите на {email_q}.";
          btn.disabled=false;btn.textContent={cta_js};}}
      }})
      .catch(function(){{msg.textContent="Ошибка сети. Email ниже работает без формы.";
        btn.disabled=false;btn.textContent={cta_js};}});
  }});
}})();
</script>
</section>'''


# Schema.org EventStatus enum — Spec-driven SoT.
# Admin-side `status` (PLANNING/DRAFT/OPEN/CLOSED) is project-lifecycle,
# NOT Schema.org event-lifecycle. Mapping lives в
# entity-event.md::enforcement_data.schema_org_event_status.
# Adding a new lifecycle state = Spec edit only, no code change here
# (admin 2026-05-13 «без хардкода в любых проявлениях»).
@_functools.lru_cache(maxsize=1)
def _schema_event_status_map() -> dict[str, str]:
    try:
        from spec_data import enforcement_data as _spec_ed
        m = _spec_ed("entity-event").get("schema_org_event_status") or {}
        if not isinstance(m, dict) or not m:
            raise RuntimeError("entity-event Spec lacks enforcement_data.schema_org_event_status")
        return {str(k): str(v) for k, v in m.items()}
    except Exception:
        # Cold-boot fallback identical к Spec-declared mapping. Loud warn would belong
        # в caller; here we accept Phase-1 silent fallback to keep site renders alive
        # if spec_data import path broken (entity-event.md is still the SoT).
        return {
            "PLANNING": "https://schema.org/EventScheduled",
            "DRAFT": "https://schema.org/EventScheduled",
            "PRE_DRAFT": "https://schema.org/EventScheduled",
            "OPEN": "https://schema.org/EventScheduled",
            "CLOSED": "https://schema.org/EventScheduled",
            "POSTPONED": "https://schema.org/EventPostponed",
            "CANCELLED": "https://schema.org/EventCancelled",
            "MOVEDONLINE": "https://schema.org/EventMovedOnline",
            "PLANNED": "https://schema.org/EventScheduled",
        }


_SCHEMA_EVENT_STATUS = _schema_event_status_map()


def _schedule_end_iso(ev: dict) -> str:
    """Last calendar date of the event from typed schedule (ISO yyyy-mm-dd).

    Falls back to t_key (start date) when schedule is absent.
    """
    sched = (ev.get("schedule") or {}).get("slots") or []
    last_iso = ""
    for slot in sched:
        dt = slot.get("date")
        # YAML date → datetime.date; serialise to ISO.
        if dt:
            iso = dt.isoformat() if hasattr(dt, "isoformat") else str(dt)
            if iso > last_iso:
                last_iso = iso
    return last_iso or ev.get("t_key", "")


def _beat_subtype(beat: dict) -> str:
    """Schema.org Event subtype for a single beat (lecture/visit/etc.)."""
    kind = (beat.get("kind") or "").lower()
    if kind in ("lecture", "talk", "masterclass", "master_class", "workshop",
                "orientation"):
        return "EducationEvent"
    if kind in ("visit", "tour", "walk", "excursion"):
        return "VisualArtsEvent"  # museum/gallery/architectural visits
    return "Event"


def _day_subevent(d: dict, ev: dict, day: dict, slot: dict | None) -> dict:
    """Build a sub-Event for one day. Resolves places from typed schedule."""
    iso_date = ""
    if slot and slot.get("date"):
        dt = slot["date"]
        iso_date = dt.isoformat() if hasattr(dt, "isoformat") else str(dt)
    title = day.get("theme") or day.get("date") or f"День {day.get('day', '?')}"
    sub_type = "Event"
    locations: list[dict] = []
    if slot:
        beats = slot.get("beats") or []
        # Subtype = most-specific beat type (EducationEvent wins if any
        # lecture/orientation/master-class beat present — that's the
        # definitive learning-content marker for the day).
        beat_types = [_beat_subtype(b) for b in beats]
        if "EducationEvent" in beat_types:
            sub_type = "EducationEvent"
        elif beat_types:
            sub_type = beat_types[0]
        # Locations: each beat.place → Schema.org Place.
        places_table = d.get("places") or {}
        for b in beats:
            pid = b.get("place")
            if not pid or pid not in places_table:
                continue
            p = places_table[pid] or {}
            place_obj: dict = {"@type": "Place",
                               "name": p.get("name", pid)}
            if p.get("address"):
                place_obj["address"] = p["address"]
            geo = p.get("geo") or {}
            if isinstance(geo, dict) and geo.get("lat") is not None and geo.get("lon") is not None:
                place_obj["geo"] = {"@type": "GeoCoordinates",
                                    "latitude": geo["lat"],
                                    "longitude": geo["lon"]}
            locations.append(place_obj)
    obj: dict = {
        "@type": sub_type,
        "name": f"{title}",
    }
    if iso_date:
        obj["startDate"] = iso_date
        obj["endDate"] = iso_date
    if day.get("notes"):
        notes = day["notes"]
        if isinstance(notes, list):
            obj["description"] = " ".join(n for n in notes if n)
        else:
            obj["description"] = str(notes)
    if locations:
        obj["location"] = locations[0] if len(locations) == 1 else locations
    return obj


def _event_jsonld(d: dict, ev: dict) -> str:
    """schema.org structured-data — graph-resolved org + locations + audience.

    Two emission paths, one umbrella:
      • format includes 'travel' AND days[]/schedule present  → TouristTrip
        with `subEvent` (per-day Event/EducationEvent), `itinerary` ItemList
        of waypoint Places, organizer Persons, offers + priceValidUntil.
      • Otherwise → flat Event (legacy projection).

    Admin-side `status` (PLANNING/DRAFT/OPEN/CLOSED) is project lifecycle;
    Schema.org `eventStatus` requires a real lifecycle enum — see
    _SCHEMA_EVENT_STATUS map. Pre-publication states ⇒ EventScheduled.
    """
    import json as _j

    fmt = ev.get("format") or []
    days = ev.get("days") or []
    schedule = (ev.get("schedule") or {}).get("slots") or []
    is_trip = ("travel" in fmt) and (bool(days) or bool(schedule))

    name = f"{ev.get('title','')} {ev.get('date','')}".strip()
    description = ev.get("concept", "") or ev.get("lead", "")
    if isinstance(description, str):
        description = description.strip().split("\n\n")[0]
    start_iso = ev.get("t_key", "")
    end_iso = _schedule_end_iso(ev)
    status_raw = (ev.get("status") or "PLANNING").upper()
    # eventStatus: emit ONLY when status_raw maps to a known Schema.org enum.
    # Unknown raw → log warning + omit field (invalid eventStatus URI is worse
    # than absent: Schema.org consumers reject the entire Event when the URI
    # doesn't match the enum). Single-SoT discipline preserved — no silent
    # «EventScheduled» mask of misconfiguration.
    event_status = _SCHEMA_EVENT_STATUS.get(status_raw)
    if event_status is None:
        import logging as _logging
        _logging.warning(
            "site_generator._event_jsonld: unknown event_status %r for "
            "event %r — omitting eventStatus from JSON-LD (known: %s)",
            status_raw, ev.get("id") or ev.get("title") or "?",
            sorted(_SCHEMA_EVENT_STATUS.keys()),
        )

    # Type strategy: primary @type = Event (Google rich-results supported);
    # additionalType = TouristTrip URI when format=travel — signals the
    # more specific Schema.org class to AI summarisers / linked-data
    # consumers without sacrificing Event indexing. departureTime /
    # arrivalTime mirrored alongside startDate/endDate for Trip-aware
    # crawlers; both are valid co-existing properties.
    obj: dict = {
        "@context": "https://schema.org",
        "@type": "Event",
        "name": name,
        "description": description,
        "url": _event_canonical(d, ev),
    }
    if event_status is not None:
        obj["eventStatus"] = event_status
    if is_trip:
        obj["additionalType"] = "https://schema.org/TouristTrip"
    if start_iso:
        obj["startDate"] = start_iso
        if is_trip:
            obj["departureTime"] = start_iso
    if end_iso:
        obj["endDate"] = end_iso
        if is_trip:
            obj["arrivalTime"] = end_iso

    locs = resolve_refs(d, "locations", ev.get("locations", []))
    if locs:
        # Schema.org: addressCountry on PostalAddress, not Place. (Inv-SEM-jsonld-valid)
        loc_list = [
            {"@type": "Place",
             "name": l.get("name", l.get("id", "")),
             **({"address": {"@type": "PostalAddress",
                             "addressCountry": l.get("country", "")}}
                if l.get("country") else {})}
            for l in locs
        ]
        # TouristTrip: location is the trip's geographic scope.
        # Single-location trips render as one object (less syntactic noise).
        obj["location"] = loc_list[0] if len(loc_list) == 1 else loc_list

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
        offer: dict = {"@type": "Offer",
                       "price": str(fee["amount"]),
                       "priceCurrency": fee.get("currency", "EUR"),
                       "availability": "https://schema.org/InStock"
                       if status_raw in ("OPEN", "PLANNING", "DRAFT")
                       else "https://schema.org/SoldOut"}
        # priceValidUntil = trip start date (offers expire when the
        # trip begins). ISO yyyy-mm-dd is acceptable per Schema.org.
        if start_iso:
            offer["priceValidUntil"] = start_iso
        obj["offers"] = offer

    if is_trip:
        # Build subEvents from days[] (Inv-DAYS-IS-RENDERING-SoT in
        # p_event_landing); pair with schedule.slots[] when day numbers
        # match for typed beats (place graph resolution).
        slot_by_day = {s.get("day"): s for s in schedule if s.get("day")}
        sub_events: list[dict] = []
        for day in days:
            slot = slot_by_day.get(day.get("day"))
            sub_events.append(_day_subevent(d, ev, day, slot))
        if sub_events:
            obj["subEvent"] = sub_events

        # Itinerary — ordered ItemList of all distinct waypoint Places
        # from route_map (canonical waypoint order; spec
        # entity-travel-schedule-and-route-map). Falls back to union
        # of schedule beat-places when route_map absent.
        rm = (ev.get("route_map") or {}).get("waypoints") or []
        if not rm:
            seen: list[str] = []
            for s in schedule:
                for b in (s.get("beats") or []):
                    pid = b.get("place")
                    if pid and pid not in seen:
                        seen.append(pid)
            rm = seen
        places_table = d.get("places") or {}
        itin_items: list[dict] = []
        for pos, pid in enumerate(rm, start=1):
            p = places_table.get(pid) or {}
            place_obj: dict = {"@type": "Place",
                               "name": p.get("name", pid)}
            if p.get("address"):
                place_obj["address"] = p["address"]
            itin_items.append({"@type": "ListItem",
                               "position": pos,
                               "item": place_obj})
        if itin_items:
            obj["itinerary"] = {"@type": "ItemList",
                                "itemListElement": itin_items}

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


@dataclass
class _LandingCtx:
    """Shared render-state for `p_event_landing`'s phase helpers.

    Holds ONLY values ≥2 `_render_*` phases need. Phase-local derived values
    (landing_h1/_h2, amount/cur_glyph/amount_str/note, days/sections, …)
    stay computed inside their owning phase — the ctx is intentionally small.
    `ph` is the one mutable field: `_render_pricing_status` populates it
    (render-time {{name}} placeholders), `_render_sections_and_programme`
    consumes it. `inline` / `h_aug` / `breath` are the per-render closures
    (lang-resolver + proper-noun augmentation bound once).
    """
    d: dict
    ev: dict
    m: object              # event_schema.EventModel (or raw dict in deployed-repo edge case)
    slug: str
    bio: dict
    date_str: str
    org_ids: list
    inline: object         # _partial(_inline, …) | _inline
    h_aug: object          # = `_h` (curated-markup pass-through; foreign-name aug retired 2026-05-12)
    breath: object         # callable(text) -> str — «one breath per line»
    ph: "dict[str, str]" = _dc_field(default_factory=dict)


def _render_header(ctx: "_LandingCtx") -> "list[str]":
    """Phase (b) — top-banner, cover-line eyebrow, three-level header
    (concept-h1 / locus-h2 / organizers-h3, with single-h1 fallback),
    <time> microdata, lead paragraphs, cohort-cap line, legacy
    organizers-byline. Returns the header fragments ending with `</header>`."""
    d, ev, m = ctx.d, ctx.ev, ctx.m
    inline, _breath = ctx.inline, ctx.breath
    date_str = ctx.date_str
    parts: list[str] = []

    # Header — h1 + lead + organizers.
    # Semantic HTML5: emit <time datetime="…"> as visually-hidden a11y/SEO
    # microdata when t_key (ISO yyyy-mm-dd) is present. JSON-LD startDate
    # carries the structured event date in machine-readable form already;
    # the <time> element gives screen-readers + browser-time parsers the
    # canonical date without disrupting essay-flow visual layout.
    h1_title = m.title if hasattr(m, "title") else m.get("title", "Событие")
    if date_str and "·" not in h1_title:
        h1_title = f"{h1_title} · {date_str}"
    t_key = m.t_key if hasattr(m, "t_key") else m.get("t_key", "")
    parts.append("<header>")

    # Top banner — short brand-anchor at very top (admin: «аккуратной плашкой
    # в самый верх»). Supplied via event yaml `top_banner: "..."`. Emits
    # nothing if absent. Used для Дизайн-Путешествия three-axes anchor.
    # admin 2026-05-12 feedback.txt L3 «не ставить точки в конце фрагмента»: top-banner
    # = closed eyebrow fragment → Inv-TYPO-no-terminal-period-block via the string-shape
    # primitive `_text_close_no_period` (list-shape sibling is `_drop_block_close_period`).
    top_banner_text = m.top_banner if hasattr(m, "top_banner") else (m.get("top_banner") or "")
    if top_banner_text:
        parts.append(f'<p class="top-banner">{_t(_text_close_no_period(top_banner_text))}</p>')

    # Cover-line — schema-driven catalogue eyebrow ABOVE h1. ALL-CAPS
    # tracked metadata strip («ДИЗАЙН-ПУТЕШЕСТВИЕ · 4 ДНЯ · ДО 12 ЧЕЛОВЕК
    # · ПО-РУССКИ»). Owner-agnostic; sources: format / duration /
    # cohort.max / languages.host. Empty → suppressed (no chrome). admin
    # 2026-05-08 «больше прописных где уместно».
    cover_items: list[str] = []
    fmt_tokens = m.format if hasattr(m, "format") else (m.get("format") or [])
    if isinstance(fmt_tokens, str):
        fmt_tokens = [fmt_tokens]
    for ft in fmt_tokens or []:
        lbl = _FORMAT_LABELS.get(str(ft).strip().lower())
        if lbl:
            cover_items.append(lbl)
    dur = m.duration if hasattr(m, "duration") else (m.get("duration") or "")
    if dur:
        cover_items.append(str(dur).strip())
    cohort = m.cohort if hasattr(m, "cohort") else (m.get("cohort") or {})
    if isinstance(cohort, dict):
        mx = cohort.get("max")
        if mx:
            # Inv-TYPO-comparator-symbolic (text/typography.md) — math glyph, не word-form.
            # comparator default `lte` для cohort.max; explicit data.yaml override possible.
            cmp = cohort.get("comparator") or "lte"
            cover_items.append(f"{_comparator_glyph(cmp)} {mx} человек")
    host_lang = (d.get("languages") or {}).get("host", "ru")
    lang_lbl = _LANG_LABELS.get(host_lang)
    # Per-event opt-out (entity-event Spec, optional field): cover_line_suppress
    # = list of cover-line item-classes to drop. Currently supported: "language".
    # Editorial-only — graph entities themselves (format/duration/cohort/lang)
    # remain SoT; suppression affects only this projection-strip (cover-line).
    # ev (raw dict) carries the field; EventModel.extra empty unless populated
    _cls_suppress = (ev.get("cover_line_suppress") or []) if isinstance(ev, dict) else []
    _suppress_all = "*" in _cls_suppress
    if lang_lbl and "language" not in _cls_suppress:
        cover_items.append(lang_lbl)
    if cover_items and not _suppress_all:
        items_html = "".join(f"<li>{_t(it)}</li>" for it in cover_items)
        parts.append(f'<ul class="cover-line" aria-label="Формат">{items_html}</ul>')

    # Three-level header (admin 2026-05-10): landing_h1 (концепт-крупно, multi-line
    # уважается) / landing_h2 (locus + дата) / H3 (организаторы). Fallback к single
    # H1 «{title} · {date}» когда explicit landing_h1/_h2 не заданы.
    org_ids = m.organizers if hasattr(m, "organizers") else (m.get("organizers") or [])
    landing_h1 = ev.get("landing_h1") if isinstance(ev, dict) else (
        getattr(m, "landing_h1", "") if hasattr(m, "landing_h1") else "")
    landing_h2 = ev.get("landing_h2") if isinstance(ev, dict) else (
        getattr(m, "landing_h2", "") if hasattr(m, "landing_h2") else "")
    if landing_h1:
        h1_lines = [ln.strip() for ln in str(landing_h1).strip().splitlines() if ln.strip()]
        h1_inner = '<br>'.join(_t(ln) for ln in h1_lines)
        parts.append(f'<h1 class="concept-h1">{h1_inner}</h1>')
        if landing_h2:
            # admin 2026-05-10: landing_h2 multi-line уважается (e.g. «8–11 СЕНТЯБРЯ\nВ ПАРИЖЕ»).
            h2_lines = [ln.strip() for ln in str(landing_h2).strip().splitlines() if ln.strip()]
            h2_inner = '<br>'.join(_t(ln) for ln in h2_lines)
            parts.append(f'<h2 class="locus-h2">{h2_inner}</h2>')
        # H3 organizers — «С X и Y» (instrumental + caps surname; admin 2026-05-10
        # «РОЗЕТ» / «ЛОГИНОВОЙ» = institutional-canonical-marker в credit-line).
        # No trailing period — heading-credit, не sentence.
        # Auto-transform: rsplit name once, uppercase last token (surname).
        # people[].name_instrumental = SoT for grammatical case; surname-caps
        # applied programmatically на render.
        if org_ids:
            disp = []
            people_graph = d.get("people") or {}
            for pid in org_ids:
                p = people_graph.get(pid) if isinstance(people_graph, dict) else None
                nm_ins = (p.get("name_instrumental") if isinstance(p, dict) else None) \
                         or _person_display(d, pid)[0]
                # Surname-caps transform — split-and-uppercase last token.
                _split = str(nm_ins).rsplit(maxsplit=1)
                if len(_split) == 2:
                    nm_credit = f"{_split[0]} {_split[1].upper()}"
                else:
                    nm_credit = str(nm_ins)
                _nm, lk = _person_display(d, pid)
                safe_lk = _u(lk)
                disp.append(f'<a href="{safe_lk}">{_t(nm_credit)}</a>'
                            if safe_lk else _t(nm_credit))
            # Inv-TYPO-no-hanging-words: «С » preposition + «и » conjunction bind
            # via _NBSP — _typo regex requires trailing word, не работает на standalone
            # connector strings. Direct _NBSP application = «грамотная сквозная абстракция»:
            # constant referenced, не char hardcoded в template.
            joined = (" и" + _NBSP).join(disp)
            parts.append(f'<h3 class="organizers-h3">С{_NBSP}{joined}</h3>')
    else:
        # Legacy single-h1 path — preserved for non-paris-2026-09 events.
        parts.append(f"<h1>{inline(h1_title)}</h1>")
        if t_key:
            parts.append(f'<time datetime="{_t(t_key)}" class="date-stamp">'
                         f'{_t(date_str or t_key)}</time>')

    lead_raw = m.lead if hasattr(m, "lead") else m["lead"]
    for lead_para in _paras(lead_raw):
        parts.append(f'<p class="lead">{_breath(lead_para)}</p>')

    # Cohort cap — derived from cohort.max (no hardcode), rendered ALL-CAPS via the
    # Caps typeclass (.is-caps). admin 2026-05-11 (feedback.txt): «10 человек — прописными».
    # admin 2026-05-12 (feedback.txt): «верни математический символ вместо "до"» — Inv-
    # TYPO-comparator-symbolic. Glyph from text/typography.md::math_symbols.comparator_glyphs;
    # prose («по», «до», …) goes to aria-label (screen-reader / SEO); visible HTML carries
    # the math glyph wrapped в .math-rel (Inv-TYPO-math-rel-aligned vertical-align fix).
    _coh = m.cohort if hasattr(m, "cohort") else (m.get("cohort") or {})
    _coh_max = _coh.get("max") if isinstance(_coh, dict) else None
    if _coh_max:
        _coh_cmp = _coh.get("comparator") or "lte"
        _coh_glyph = _comparator_glyph(_coh_cmp)
        _coh_prose = _comparator_prose(_coh_cmp, "ru")
        _coh_aria = (f"{_coh_prose} {int(_coh_max)} человек"
                     if _coh_prose else f"{int(_coh_max)} человек")
        parts.append(
            f'<p class="lead is-caps" aria-label="{_t(_coh_aria)}">'
            f'<span class="math-rel">{_coh_glyph}</span> {_t(f"{int(_coh_max)} человек")}</p>'
        )

    # Legacy organizers-byline path (when landing_h1 absent — H3 already emitted above).
    abt = m.about_organizer if hasattr(m, "about_organizer") else (m.get("about_organizer") or {})
    abt_text = abt.get("text") if isinstance(abt, dict) else getattr(abt, "text", "")
    if not landing_h1 and org_ids and not abt_text:
        disp = []
        for pid in org_ids:
            nm, lk = _person_display(d, pid)
            safe_lk = _u(lk)
            disp.append(f'<a href="{safe_lk}">{_t(nm)}</a>' if safe_lk else _t(nm))
        label = "Организатор" if len(org_ids) == 1 else "Организаторы"
        parts.append(f'<p class="organizers">{" и ".join(disp)} — {label}.</p>')
    parts.append("</header>")
    return parts


def _render_pricing_status(ctx: "_LandingCtx") -> "list[str]":
    """Phases (c)+(d) — pricing-display `<aside>`, render-time {{name}}
    placeholder dict (written into `ctx.ph` for the sections phase), and the
    PLANNING/DRAFT status banner. Returns the (possibly empty) fragments."""
    m, ev = ctx.m, ctx.ev
    parts: list[str] = []

    # Pricing display strip — editorial cover-line, schema-driven.
    # Renders ev.pricing.team_fee.{amount,currency,note} as a hero figure
    # if amount is set. CSS in styles.css `.pricing-display` paints it.
    pricing = m.pricing if hasattr(m, "pricing") else (m.get("pricing") or {})
    team_fee = (pricing or {}).get("team_fee") or {}
    amount = team_fee.get("amount")
    if amount is not None:
        currency = team_fee.get("currency", "")
        cur_glyph = _CURRENCY_GLYPH.get(str(currency).upper(), _t(currency))
        amount_str = f"{int(amount):,}".replace(",", " ") \
            if isinstance(amount, (int, float)) and float(amount).is_integer() \
            else _t(amount)
        note = team_fee.get("note") or ""
        # Label-less display: amount + currency только. aria-label сохраняет
        # screen-reader semantics. Admin: «слово "стоимость" лишнее» — цифра
        # говорит сама.
        parts.append(
            '<aside class="pricing-display" aria-label="Стоимость">'
            f'<div class="pricing-amount">{amount_str}'
            f'<span class="currency">{cur_glyph}</span></div>'
            + (f'<div class="pricing-note">{_t(note)}</div>' if note else '')
            + '</aside>'
        )

    # Render-time placeholders for section prose ({{name}}) — admin 2026-05-11 (feedback.txt):
    # «[здесь автоматическая калькуляция] — для программного разрешения».
    if amount is not None and isinstance(amount, (int, float)):
        _half = amount / 2
        _half_disp = (f"{int(_half):,}" if float(_half).is_integer() else f"{_half:,.2f}").replace(",", " ")
        ctx.ph["team_fee_half"] = f"{_half_disp} {cur_glyph}".strip()
        ctx.ph["team_fee"] = f"{amount_str} {cur_glyph}".strip()

    # Status banner — DRAFT/PLANNING openly stated, congruent with «программа дописывается».
    # admin 2026-05-11 (feedback.txt) suppressed it for paris-2026-09 via `status_banner: false`;
    # default True keeps it for other PLANNING/DRAFT events.
    status = m.status if hasattr(m, "status") else m.get("status", "")
    _status_banner_on = ev.get("status_banner", True) if isinstance(ev, dict) else True
    if status in ("PLANNING", "DRAFT") and _status_banner_on:
        # WAI-ARIA: status banner is a non-critical live region. role=status
        # + aria-live=polite makes screen readers announce "Программа собирается"
        # when the page first reads, without interrupting other narration.
        parts.append('<p class="status-banner" role="status" aria-live="polite">'
                     'Программа собирается. Лист ожидания открыт.</p>')
    return parts


def _render_sections_and_programme(ctx: "_LandingCtx") -> "list[str]":
    """Phases (e)+(f) — kept together because they share `_admin_section_titles`
    / `programme_inserted`. (e) drops the explicit «Программа» section, iterates
    `m.sections` (title-only sentinels register & render nothing; prose →
    `<p>`s with `_drop_block_close_period` on a prose-only ≥-«крупнее абзаца»
    section; pairs → `<dl class="pairs">`; items → `<ul>`) and injects the
    structured-days programme block right after «Тема» (or after the first
    section, or at top). (f) emits auto-policy onboarding «Перед поездкой» +
    terms «Условия и сроки» from the `event_policy` graph node unless their
    title is already an admin-authored section."""
    m, d = ctx.m, ctx.d
    inline, h_aug, _breath = ctx.inline, ctx.h_aug, ctx.breath
    bio = ctx.bio
    _ph = ctx.ph
    parts: list[str] = []

    # Days — structured ev.days[] → editorial day-block list with CSS counter
    # day-numerals (.days `<ol>` in styles.css). When present, renders as the
    # primary programme surface; the textual `Программа` section in `sections[]`
    # is suppressed (graph-derived structured data wins; admin: «стройно
    # генерируемое из Памяти», feedback_no_hardcode_through_abstractions).
    days = m.days if hasattr(m, "days") else (m.get("days") or [])
    sections = m.sections if hasattr(m, "sections") else (m.get("sections") or [])

    def _render_programme_block() -> str:
        out: list[str] = ['<section class="programme"><h2>Программа</h2>'
                          '<ol class="days" aria-label="Программа по дням">']
        # Inv-PARIS-design-arc-per-day (text/event-paris-2026-09.md): каждый день-card
        # carries data-day=<index> атрибут — CSS picks per-day accent token
        # (--paris-day-{n}-accent). Day-arc visually congruent с program's three-modernism arc.
        for idx, day in enumerate(days, start=1):
            d_date = day.get("date", "")
            d_theme = day.get("theme", "")
            d_notes = day.get("notes", "")
            out.append(f'<li data-day="{idx}">')
            if d_date:
                out.append(f'<p class="day-date">{_t(d_date)}</p>')
            if d_theme:
                out.append(f'<h3 class="day-theme">{inline(d_theme)}</h3>')
            # day-notes can be str OR list[str] (paragraphs). Preserve
            # admin's blank-line separators (Inv-SEMANTIC-WHITESPACE).
            # Day-block = «фрагмент крупнее абзаца» → Inv-TYPO-no-terminal-period-block
            # drops terminal «.» on the day's last paragraph (admin 2026-05-11 feedback.txt).
            _day_paras: list[str] = list(d_notes) if isinstance(d_notes, list) else (
                [d_notes] if d_notes else []
            )
            _day_paras = [p for p in _day_paras if p]
            if _day_paras:
                _day_paras = _drop_block_close_period(_day_paras)
                for para in _day_paras:
                    out.append(f'<p class="day-notes">{inline(para)}</p>')
            out.append('</li>')
        out.append('</ol></section>')
        return "".join(out)

    # Inject programme: prefer position right after «Тема» if present;
    # else after first section; else at top. Drop any explicit text-only
    # «Программа» section — schema-derived days wins.
    sections = [s for s in sections
                if (s.title if hasattr(s, "title") else s.get("title", ""))
                   != "Программа"]
    programme_inserted = not bool(days)
    # Sections — schema variants (pair / text / items / intro).
    # Programme (days) inserts after «Тема» if present, else after first
    # section, else at top — narrative arc «концепт → программа → детали».
    for idx, sec in enumerate(sections):
        t = sec.title if hasattr(sec, "title") else sec.get("title", "")
        intro = sec.intro if hasattr(sec, "intro") else sec.get("intro", "")
        text = sec.text if hasattr(sec, "text") else sec.get("text", "")
        pairs = sec.pairs if hasattr(sec, "pairs") else (sec.get("pairs") or [])
        items = sec.items if hasattr(sec, "items") else (sec.get("items") or [])
        # Empty section = title-only override sentinel: registers in
        # _admin_section_titles to suppress matching auto-policy block,
        # but renders nothing visible. Used когда admin merges several
        # auto-blocks (e.g. «Перед поездкой» + «Условия и сроки»).
        if not (intro or text or pairs or items):
            # Programme insertion still respects ordering — title-only
            # «Тема» counts for «after Тема» anchor.
            if not programme_inserted and (t.strip() == "Тема" or idx == 0):
                parts.append(_render_programme_block())
                programme_inserted = True
            continue
        parts.append(f"<section><h2>{_t(t)}</h2>")
        # admin: «one breath per line» (per-line typography) + markdown links; {{name}}
        # placeholders resolved here. A section that is prose-only and «крупнее абзаца»
        # drops its terminal «.» (Inv-TYPO-no-terminal-period-block — same _drop_block_close_period
        # as about-organizer + sub-events).
        _prose = _paras(_resolve_placeholders(intro, _ph)) + _paras(_resolve_placeholders(text, _ph))
        if not items and not pairs:
            _prose = _drop_block_close_period(_prose)
        for p in _prose:
            parts.append(f"<p>{_md_links(_breath(p))}</p>")
        if pairs:
            parts.append('<dl class="pairs">')
            for pair in pairs:
                label = pair.label if hasattr(pair, "label") else pair.get("label", "")
                ptext = pair.text if hasattr(pair, "text") else pair.get("text", "")
                parts.append(f'<dt>{inline(label)}</dt><dd>{inline(ptext)}</dd>')
            parts.append('</dl>')
        if items:
            lis = "".join(f"<li>{h_aug(x)}</li>" for x in items)
            parts.append(f"<ul>{lis}</ul>")
        parts.append("</section>")
        # Insert programme (days) right after «Тема» if it exists; else
        # after the first section. Both branches set programme_inserted.
        if not programme_inserted and (
            t.strip() == "Тема" or idx == 0
        ):
            parts.append(_render_programme_block())
            programme_inserted = True
    if not programme_inserted:
        parts.append(_render_programme_block())

    # ── System-policy-derived sections ────────────────────────────────
    # Pulled from `event_policy` graph node (top-level d) and rendered
    # automatically when applicable. Single SoT — no per-event duplication.
    # Closes typical traveler-questions (onboarding, payment timing,
    # language, accessibility) without hand-writing copy on every
    # Design-Travels landing.
    #
    # Auto-policy suppression rule: if yaml event-entry `sections[]` already
    # contains a section with the same title (e.g. «Перед поездкой» or
    # «Условия и сроки»), the auto-policy block is suppressed — admin's
    # explicit yaml-section text wins. Inv-EV-no-overlap (SoT-migration
    # 2026-05-07) makes yaml the single SoT for sections; the sibling .md
    # body no longer contributes structured fields, so the title-match
    # check below operates on yaml-entry sections only.
    _admin_section_titles = {
        (s.title if hasattr(s, "title") else (s.get("title", "") or "")).strip()
        for s in sections
    }
    fmt = m.format if hasattr(m, "format") else (m.get("format") or [])
    policy = d.get("event_policy") or {}
    dt_policy = policy.get("design_travel") or {} if "travel" in (fmt or []) else {}

    # «Перед поездкой» — pre-travel onboarding (Design-Travels-class).
    # Source: event_policy.design_travel.onboarding {interview, intro_meeting}.
    # Sets traveler expectations: short online interview + mandatory online
    # intro-meeting with Olga (offline-when-possible, in addition not in place).
    onboarding = dt_policy.get("onboarding") or {}
    if onboarding and "Перед поездкой" not in _admin_section_titles:
        intro_lines: list[str] = []
        iv = onboarding.get("interview") or {}
        if iv:
            iv_purpose = iv.get("purpose", "знакомство")
            intro_lines.append(
                f"Онлайн-собеседование с Организаторами — "
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
                f"Встреча-знакомство-занятие с Ольгой — "
                f"{modes_phrase}."
            )
        if intro_lines:
            parts.append('<section class="onboarding"><h2>Перед поездкой</h2><ul>')
            for it in intro_lines:
                parts.append(f"<li>{h_aug(it)}</li>")
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
    if terms_items and "Условия и сроки" not in _admin_section_titles:
        parts.append('<section class="terms"><h2>Условия и сроки</h2><ul>')
        for it in terms_items:
            parts.append(f"<li>{h_aug(it)}</li>")
        parts.append('</ul></section>')
    return parts


def _has_landing_terminal(d: dict, slug: str) -> bool:
    """Inv-LANDING-terminal-block (admin 2026-05-11 feedback.txt): predicate ∃se in
    `d.events` declaring itself the final content «блок» of slug's landing —
    se.parent_id == slug ∧ landing_section ∈ se.broadcast ∧ se.landing_terminal.

    Total over (d, slug); pure (no I/O, no mutation). Lifts «после блока про X — конец»
    из per-event hardcode в a generic data-driven invariant: any sub-event с broadcast
    [landing_section] может объявить себя terminal — content-tail (open_questions,
    signup, contact, about_organizer) skips for that parent's landing. Chrome (legal
    footer + cookie banner) — не «блок», renders unconditionally."""
    return any(
        _se.get("parent_id") == slug
        and "landing_section" in (_se.get("broadcast") or [])
        and _se.get("landing_terminal")
        for _se in (d.get("events") or [])
    )


def _render_subevents(ctx: "_LandingCtx") -> "list[str]":
    """Phase (g) — `landing_section`-broadcast sub-events of this event →
    `<section class="subevent …">` blocks (description `<p>`s, an optional
    `url`/`url_text` link line, a meta `<ul>` unless `suppress_meta`).
    Rendered after the content sections, before signup/contact/about so
    «Об Организаторах» stays the last block."""
    d, slug = ctx.d, ctx.slug
    inline, _breath = ctx.inline, ctx.breath
    parts: list[str] = []

    # ── Sub-event auto-injection ─────────────────────────────────────
    # Sub-events (e.g. the IG-Live preshow) that declare `broadcast: [landing_section]`
    # are rendered as standalone sections, right after the content sections (before
    # signup/contact/about) — the «Об Организаторах» block must stay the last block
    # (admin 2026-05-11: «после блока про Наталью Логинову — конец»; «блок про Наталью»
    # = Об Организаторах). Source: entity-event Spec §parent_id + Inv-EV-parent-resolves.
    all_events = d.get("events") or []
    sub_events = [
        se for se in all_events
        if se.get("parent_id") == slug
        and "landing_section" in (se.get("broadcast") or [])
    ]
    _subev_parts: list[str] = []
    for se in sub_events:
        se_type = se.get("type", "event")
        se_title = se.get("title", "")
        se_desc = se.get("description", "")
        se_url = se.get("url")
        _subev_parts.append(f'<section class="subevent subevent-{_u(se_type)}">')
        _subev_parts.append(f'<h2>{inline(se_title)}</h2>')
        if se_desc:
            # admin: «one breath per line» (per-line typography) + markdown links; «крупнее
            # абзаца» description drops its terminal «.» (Inv-TYPO-no-terminal-period-block).
            for se_para in _drop_block_close_period(_paras(se_desc)):
                _subev_parts.append(f'<p>{_md_links(_breath(se_para))}</p>')
        if se_url:
            _u_url = _u(str(se_url))
            _u_text = inline(str(se.get("url_text") or se_url))
            _subev_parts.append(f'<p class="subevent-link"><a href="{_u_url}" rel="noopener">{_u_text}</a></p>')
        # admin opt-out: suppress_meta=true hides Когда/Продолжительность/Организаторы list
        # (admin 2026-05-10 paris-landing.md: meta излишен когда orgs в parent H3 + Programme).
        if se.get("suppress_meta"):
            _subev_parts.append('</section>')
            continue
        meta_bits = []
        se_when = se.get("when", "")
        if se_when and se_when != "TBD":
            meta_bits.append(f"Когда: {inline(se_when)}")
        se_dur = se.get("duration_min")
        if se_dur:
            meta_bits.append(f"Продолжительность: {se_dur} мин")
        se_orgs = se.get("organizers") or []
        if se_orgs:
            org_names = []
            for oid in se_orgs:
                nm, lk = _person_display(d, oid)
                safe_lk = _u(lk)
                org_names.append(f'<a href="{safe_lk}">{inline(nm)}</a>' if safe_lk else inline(nm))
            meta_bits.append(f"Организаторы: {', '.join(org_names)}")
        if meta_bits:
            _subev_parts.append('<ul class="subevent-meta">')
            for mb in meta_bits:
                _subev_parts.append(f'<li>{mb}</li>')
            _subev_parts.append('</ul>')
        _subev_parts.append('</section>')
    # Rendered here. The terminal-block convention (Inv-LANDING-terminal-block) lets a
    # sub-event declare itself the final block of the landing — phases (h)-(k) then skip.
    # admin 2026-05-11 (feedback.txt, re-read 2026-05-12): «блок про Наталью» = IG-Live
    # sub-event (на её канале), не Об Организаторах. Prior interpretation corrected.
    parts.extend(_subev_parts)
    return parts


def _render_open_questions(ctx: "_LandingCtx") -> "list[str]":
    """Phase (h) — `m.open_questions` grouped by frozen addressee-set →
    `<section class="open-questions">` blocks (one shared block per joint
    addressing, no synthetic per-person split)."""
    m = ctx.m
    inline = ctx.inline
    d = ctx.d
    parts: list[str] = []

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
                names.append(f'<a href="{safe_lk}">{inline(nm)}</a>' if safe_lk else inline(nm))
            head = " и ".join(names)
            lis = "".join(f"<li>{inline(q)}</li>" for q in qs)
            parts.append(f'<div class="q-group"><h3>К {head}</h3>'
                         f'<ul>{lis}</ul></div>')
        parts.append("</section>")
    return parts


def _render_signup(ctx: "_LandingCtx") -> "list[str]":
    """Phase (i) — `<section class="signup-wrap">` + the embedded
    `<form id="signup-form">` (built by `event_signup_form`)."""
    m, slug, bio, date_str = ctx.m, ctx.slug, ctx.bio, ctx.date_str
    inline = ctx.inline
    parts: list[str] = []

    # Signup
    signup = m.signup if hasattr(m, "signup") else m.get("signup")
    if signup:
        s_title = signup.title if hasattr(signup, "title") else signup.get("title", "Записаться")
        s_note = signup.note if hasattr(signup, "note") else signup.get("note", "")
        s_cta = signup.cta_label if hasattr(signup, "cta_label") else signup.get("cta_label", "Оставить email")
        parts.append(f'<section class="signup-wrap"><h2>{inline(s_title)}</h2>')
        if s_note:
            parts.append(f'<p>{inline(s_note)}</p>')
        ev_label = f"{m.title if hasattr(m,'title') else m.get('title','Событие')} {date_str}".strip()
        parts.append(event_signup_form(
            slug,
            ev_label,
            bio.get("email", "info@example.com"),
            cta_label=s_cta,
        ))
        parts.append("</section>")
    return parts


def _render_contact(ctx: "_LandingCtx") -> "list[str]":
    """Phase (j) — `<section class="contact">` from `m.contact`
    {prompt, text, email}, rendered iff at least one field set."""
    m = ctx.m
    parts: list[str] = []

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
    return parts


def _render_about_organizer(ctx: "_LandingCtx") -> "list[str]":
    """Phase (k) — `<footer class="about-organizer">`: either admin's
    `about_organizer.text` (split into preamble + per-organizer cards via
    name-prefix detection) or auto-synth from the people-bio graph; bio
    paras get `_drop_block_close_period`; an `org-link` tail."""
    m, d = ctx.m, ctx.d
    parts: list[str] = []

    # About organizers — admin's explicit `about_organizer.text` wins;
    # else auto-synth from organizers people-bio graph.
    # Plural «Организаторы» when ≥2 (project_natalia_equal_organizer:
    # paritetary).
    org_ids = (m.organizers if hasattr(m, "organizers")
               else (m.get("organizers") or []))
    about = m.about_organizer if hasattr(m, "about_organizer") else m.get("about_organizer")
    a_link_url = ""
    a_link_text = ""
    a_text_paras: list[str] = []
    if about:
        a_link_url = about.link_url if hasattr(about, "link_url") else about.get("link_url", "")
        a_link_text = about.link_text if hasattr(about, "link_text") else about.get("link_text", "")
        a_text_paras = _paras(about.text if hasattr(about, "text") else about.get("text", ""))
    # Inv-TYPO-no-terminal-period-block (admin 2026-05-11): the Об Организаторах footer is a
    # block «крупнее абзаца» — its last sentence carries no terminal «.».
    a_text_paras = _drop_block_close_period(a_text_paras)
    organizer_paragraphs: list[str] = []
    if not a_text_paras:
        # Auto-synth from people-bio graph only when admin has not authored text.
        for pid in org_ids:
            person = (d.get("people") or {}).get(pid) or {}
            nm = person.get("name") or pid
            person_bio = person.get("bio") or ""
            if person_bio:
                # Inv-TYPO-no-bold-in-body: name-stamp без <strong>; emphasis через
                # span.org-name CSS-class (caps + tracking, не weight). admin 2026-05-10.
                organizer_paragraphs.append(
                    f'<p><span class="org-name">{_t(nm)}</span> — {_t(person_bio)}.</p>'
                )
    title = _typo("Об Организаторах") if len(org_ids) > 1 else _typo("Об Организаторе")
    link_html = ""
    safe_link = _u(a_link_url)
    if safe_link:
        link_html = (f'<p class="org-link"><a href="{safe_link}">'
                     f'{_t(a_link_text or a_link_url)}</a></p>')
    if a_text_paras:
        # Bullet-paragraphs (whole paragraph = `- item` lines) → <ul>;
        # else → <p>. Supports Об Организаторах structured semantics:
        # linear-bio prose + competence-bullets + operative-quote + role-line.
        # Name-stamp detection: paragraphs that match an organizer's
        # canonical name (from people-graph) get <p class="org-name">,
        # which CSS treats as a wall-label heading marker. Two-organizer
        # events with these markers become two-column on wide screens
        # (paritetary treatment per project_natalia_equal_organizer).
        org_names_norm: list[str] = []
        for pid in org_ids:
            person = (d.get("people") or {}).get(pid) or {}
            nm = (person.get("name") or "").strip().rstrip(".")
            if nm and nm not in org_names_norm:
                org_names_norm.append(nm)
        # Longest first — prefix-match safety («Иван Иванов-Сидоров» не
        # съедается «Иван Иванов»).
        org_names_norm.sort(key=len, reverse=True)

        def _split_name_bio(p: str) -> tuple[str, str]:
            """If paragraph starts with an organizer's canonical name
            (followed by «— », «. », end-of-string, or «.»), split into
            (name, bio_remainder). Else returns ("", p).

            Handles flavours admin authors:
                "Ольга Розет."             → ("Ольга Розет", "")
                "Ольга Розет"              → ("Ольга Розет", "")
                "Ольга Розет — bio…"      → ("Ольга Розет", "bio…")
                "Ольга Розет. Bio…"       → ("Ольга Розет", "Bio…")
            """
            s = (p or "").strip()
            for nm in org_names_norm:
                if s == nm or s == nm + ".":
                    return nm, ""
                if s.startswith(nm + " — "):
                    return nm, s[len(nm) + 3:].strip()
                if s.startswith(nm + ". "):
                    return nm, s[len(nm) + 2:].strip()
            return "", s

        def _org_para(p: str, name_class: bool = False) -> str:
            lines = [l.strip() for l in p.split("\n") if l.strip()]
            if lines and all(l.startswith("- ") for l in lines):
                return ("<ul>" + "".join(
                    f"<li>{_t(l[2:].strip())}</li>" for l in lines) + "</ul>")
            cls = ' class="org-name"' if name_class else ""
            return f"<p{cls}>{_t(p)}</p>"

        # Split paragraphs into preamble + per-organizer cards anchored
        # by name-prefix detection. Paragraphs without a name-prefix
        # before the first card → preamble; after a card start →
        # attach to current card (closing taglines bind to the last
        # organizer's column on wide screens).
        preamble: list[str] = []
        cards: list[list[tuple[str, str]]] = []
        current: list[tuple[str, str]] | None = None
        for p in a_text_paras:
            name, rest = _split_name_bio(p)
            if name:
                current = [(name, rest)]
                cards.append(current)
            elif current is None:
                preamble.append(p)
            else:
                current.append(("", p))

        body_parts: list[str] = []
        if preamble:
            preamble_html = "".join(_org_para(p) for p in preamble)
            body_parts.append(f'<div class="org-preamble">{preamble_html}</div>')
        for card in cards:
            inner_parts: list[str] = []
            for nm, body_p in card:
                if nm:
                    inner_parts.append(f'<p class="org-name">{_t(nm)}</p>')
                    if body_p:
                        inner_parts.append(_org_para(body_p))
                else:
                    inner_parts.append(_org_para(body_p))
            body_parts.append(f'<div class="org-card">{"".join(inner_parts)}</div>')
        if not cards:
            # No name-prefixes detected → flat rendering, full-width column
            body_parts = [_org_para(p) for p in a_text_paras]

        body = "".join(body_parts)
        parts.append(f'<footer class="about-organizer"><h2>{title}</h2>'
                     f'{body}{link_html}</footer>')
    elif organizer_paragraphs:
        parts.append(f'<footer class="about-organizer"><h2>{title}</h2>'
                     f'{"".join(organizer_paragraphs)}{link_html}</footer>')
    elif about:
        # Fallback for events without organizers ↔ people-bio graph
        a_text = about.text if hasattr(about, "text") else about.get("text", "")
        a_paras = _paras(a_text)
        if a_paras:
            link_html = ""
            safe_link = _u(a_link_url)
            if safe_link:
                link_html = (f'<br><a href="{safe_link}">'
                             f'{_t(a_link_text or a_link_url)}</a>')
            # Each paragraph as separate <p>; link tail attaches to the last.
            p_blocks = "".join(f"<p>{_t(p)}</p>" for p in a_paras[:-1])
            p_blocks += f"<p>{_t(a_paras[-1])}{link_html}</p>"
            parts.append('<footer class="about-organizer">'
                         f'<h2>Об Организаторе</h2>{p_blocks}</footer>')

    # (sub-events were already appended above — Об Организаторах stays the last block.)
    return parts


def _render_legal(ctx: "_LandingCtx") -> "list[str]":
    """Phase (l) — `_legal_footer(d)` or a minimal `legal-min` footer
    (gated on `suppress_legal_footer`; the privacy link survives suppression
    per Inv-SITE-trust-base)."""
    m, d = ctx.m, ctx.d
    parts: list[str] = []

    # Per-event opt-out: admin может скрыть legal-footer для конкретного
    # landing'а (`suppress_legal_footer: true` в yaml event-entry). Owner-
    # level legal block остаётся — siblings других editions рендерят.
    #
    # Privacy-link exception: Inv-SITE-trust-base requires `has_privacy_link`
    # on every site/landing universally — privacy is jurisdiction-agnostic.
    # `suppress_legal_footer` hides the RU-entity disclosures (INN/OGRN/address)
    # + payment methods, NOT the privacy link. When suppressed, render a
    # minimal `legal-min` footer with the privacy link alone, iff privacy_url
    # is admin-set in data.yaml.legal.
    suppress_legal = m.get("suppress_legal_footer", False) if hasattr(m, "get") else getattr(m, "suppress_legal_footer", False)
    if not suppress_legal:
        legal_html = _legal_footer(d)
        if legal_html:
            parts.append(legal_html)
    else:
        privacy_url = _u(((d.get("legal") or {}).get("privacy_url")) or "")
        if privacy_url:
            parts.append(
                f'<footer class="legal-min" aria-label="Юридическое">'
                f'<p><a href="{privacy_url}">Политика конфиденциальности</a></p>'
                f'</footer>'
            )
    return parts


def p_event_landing(d: dict, ev: dict) -> str:
    """Project one Event from the graph to a standalone landing HTML page.

    Single render path: schema-validated essay layout. No legacy fallback.
    Schema (see event_schema.EventModel for the source of truth):
      lead              — single sentence, italic, frames the page
      organizers        — list of person ids; rendered as «N1 и N2 — Организаторы.»
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

    Render pipeline: validate → build `_LandingCtx` once → compose the phase
    helpers (`_render_header` … `_render_legal`) → wrap in `.article-wrapper`
    → `_layout`. The phase helpers carry the load-bearing comments (admin
    directives, Inv-* references, incident notes) verbatim.
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

    # Render-time text functions. Foreign-name marker subsystem retired 2026-05-12
    # (admin: «не нужна Спецификация выделения иностранных слов ни на каком уровне»);
    # `inline` = plain escape+typo, `h_aug` = `_h` (curated-markup pass-through).
    inline = _inline
    h_aug = _h

    def _breath(text: object) -> str:
        """admin's «one breath per line»: each \\n is a DELIBERATE break, so typography
        (NBSP-glue, hanging-words) is applied PER LINE, never across the <br> — a one-word
        pivot line can't glue forward. Used for lead / section prose / sub-event descriptions."""
        return "<br>".join(inline(_ln) for _ln in str(text).split("\n"))

    org_ids = m.organizers if hasattr(m, "organizers") else (m.get("organizers") or [])

    ctx = _LandingCtx(
        d=d, ev=ev, m=m, slug=slug, bio=bio, date_str=date_str,
        org_ids=org_ids, inline=inline, h_aug=h_aug, breath=_breath,
    )
    _is_terminal = _has_landing_terminal(d, slug)
    _content_tail: list[str] = [] if _is_terminal else [
        *_render_open_questions(ctx),
        *_render_signup(ctx),
        *_render_contact(ctx),
        *_render_about_organizer(ctx),
    ]
    # admin 2026-05-12 reconsider (Natalia-terminal): root-resolution shifted from
    # «reorder parts to put legal before subevent» (commit fedd0aab — awkward mid-page
    # footer-styling) К cleaner architectural fix:
    # - suppress_legal_footer: true → _render_legal emits only minimal privacy-link
    #   footer (Inv-SITE-trust-base requires persistent privacy link)
    # - payment-methods strip moved to Бронирование section в data.yaml (semantically
    #   payment belongs to booking flow)
    # - cookie-banner overlay separately decoupled (suppress_cookie_banner field)
    # Order remains canonical: subevents → content_tail (empty if terminal) → legal-min.
    # Natalia subevent is last CONTENT block; legal-min is footer-styled minimal privacy.
    parts: list[str] = [
        *_render_header(ctx),
        *_render_pricing_status(ctx),
        *_render_sections_and_programme(ctx),
        *_render_subevents(ctx),
        *_content_tail,
        *_render_legal(ctx),
    ]

    body = f'  <article class="article-wrapper">{"".join(parts)}</article>'

    lead_text = m.lead if hasattr(m, "lead") else m.get("lead", "")
    # SEO meta-description must be a single string — collapse paragraphs
    # for description only; the rendered lead keeps its paragraph breaks.
    lead_meta = " ".join(_paras(lead_text))
    # Per-event landing chrome control — two decoupled flags (admin 2026-05-12
    # reconsider: legal-footer и cookie-banner — orthogonal concerns):
    #   suppress_legal_footer  — hides .legal block (ИНН/ОГРН/payment-methods strip)
    #                            Use case: landing_terminal events где payment ушёл
    #                            в Бронирование section + Natalia subevent — literally
    #                            последний flow block («не должно быть других блоков»).
    #   suppress_cookie_banner — hides cookie-consent overlay (privacy/consent dialog)
    #                            Use case: only где no data collection on landing.
    # Fallback chain preserves backward-compat — если `suppress_cookie_banner` НЕ задан,
    # ridесь старый-style использования `suppress_legal_footer` как coupled flag.
    suppress_legal = m.suppress_legal_footer if hasattr(m, "suppress_legal_footer") else m.get("suppress_legal_footer", False)
    suppress_cookie_explicit = (m.get("suppress_cookie_banner") if hasattr(m, "get")
                                else getattr(m, "suppress_cookie_banner", None))
    suppress_cookie = suppress_cookie_explicit if suppress_cookie_explicit is not None else suppress_legal
    # nav-back arrow useful только когда landing rendered как owner-domain sub-page
    # (olgarozet.ru/<event-id>/ → back к owner root). Event-bound FQDN landings
    # (parisinseptember.ru) — back-arrow к / leads к same page (sole content) →
    # noise. Admin direct 2026-05-11 «сверху стрелка не нужна». Conditional:
    # event has dedicated web_addresses → suppress nav-back.
    _has_dedicated_fqdn = bool(ev.get("web_addresses"))
    return _layout(
        d,
        title=title_full,
        description=(lead_meta or m.concept if hasattr(m, "concept") else m.get("concept", title_full))[:160],
        body=body,
        nav=not _has_dedicated_fqdn,
        canonical=_event_canonical(d, ev),
        structured=_event_jsonld(d, ev),
        # Owner-portrait footer belongs to owner-site (olgarozet.ru) only —
        # admin directive 2026-05-02. Event landings render their own
        # contact/about-organizer block; no shared portrait/social-icons.
        footer=False,
        surface="editorial",
        cookie_banner_enabled=not suppress_cookie,
    )


# ── P_static_page: D × <slug>.md → <slug>/index.html ────────────────
#
# Pure projection — generic abstraction for owner-side standalone pages
# that are NOT events: privacy policy, oferta, manifesto-type docs.
# Single SoT: knowledge/people/<owner>/site/<slug>.md (frontmatter + body).
# Same `_layout` surface as every other projection → footer.legal,
# cookie-banner, head metadata, typography are shared by construction.
#
# Used by:
#   • site_preview server          (always-fresh; .md re-read every GET)
#   • broadcast_html.update_site   (renders to site/<slug>/index.html
#                                   contour-first; deploy mirrors)
#   • broadcast_html.update_landing (mirrors to event-bound fqdn-repos so
#                                    privacy_url cross-host link resolves)
#
# Markdown subset (lapidary, sufficient for legal/manifesto docs):
#   YAML frontmatter (---…---) → title, description, slug
#   `# H1` / `## H2` / `### H3` → headings
#   `- item`                    → <ul><li>          (consecutive lines)
#   blank-separated paragraphs  → <p>               (HTML inline pass-through;
#                                                    admin-authored, schema-trusted)
#   `<!-- … -->`                → admin-fill markers, suppressed in render
#                                  (visible in source for admin handoff).

def _md_static_to_html(md_body: str) -> str:
    """Render a constrained markdown subset → HTML body fragment.

    Pure function. No external markdown library — the subset is small and
    bounded by the legal-doc / manifesto class. Inline HTML in source is
    passed through verbatim (admin-authored, single-SoT trusted; no L0
    untrusted input flows here). HTML comments are stripped — they carry
    admin-fill placeholders meant for the source file, not for visitors.
    """
    body = _re.sub(r"<!--.*?-->", "", md_body, flags=_re.DOTALL)

    out: list[str] = []
    paragraph: list[str] = []
    list_buf: list[str] = []

    def _flush_paragraph():
        if paragraph:
            text = " ".join(paragraph).strip()
            if text:
                out.append(f"<p>{_typo(text)}</p>")
            paragraph.clear()

    def _flush_list():
        if list_buf:
            items_html = "".join(f"<li>{_typo(li)}</li>" for li in list_buf)
            out.append(f"<ul>{items_html}</ul>")
            list_buf.clear()

    def _flush_all():
        _flush_paragraph()
        _flush_list()

    for raw_line in body.split("\n"):
        line = raw_line.rstrip()
        if line.startswith("### "):
            _flush_all()
            out.append(f"<h3>{_typo(line[4:].strip())}</h3>")
            continue
        if line.startswith("## "):
            _flush_all()
            out.append(f"<h2>{_typo(line[3:].strip())}</h2>")
            continue
        if line.startswith("# "):
            _flush_all()
            out.append(f"<h1>{_typo(line[2:].strip())}</h1>")
            continue
        if line.lstrip().startswith("- "):
            _flush_paragraph()
            list_buf.append(line.lstrip()[2:].strip())
            continue
        if not line.strip():
            _flush_all()
            continue
        _flush_list()
        paragraph.append(line.strip())
    _flush_all()
    return "\n".join(out)


def parse_static_md(text: str) -> tuple[dict, str]:
    """Split frontmatter + body from a static-page markdown source.

    Mirrors `_split_event_md`'s frontmatter rule (---…---). Frontmatter is
    optional for static pages; absence yields ({}, full-text). Pure function.
    """
    if not text.lstrip().startswith("---"):
        return {}, text
    lines = text.split("\n")
    try:
        start = next(i for i, l in enumerate(lines) if l.strip() == "---")
        end = next(i for i, l in enumerate(lines[start + 1:], start + 1)
                   if l.strip() == "---")
    except StopIteration:
        return {}, text
    fm = yaml.safe_load("\n".join(lines[start + 1:end])) or {}
    body = "\n".join(lines[end + 1:])
    return fm, body


def p_static_page(d: dict, md_text: str) -> str:
    """Project (D, static.md) → standalone HTML page.

    Pure projection. Front-matter `title` drives <title>/<h1>; `description`
    drives meta-description. Body rendered via `_md_static_to_html`. Layout
    inherits the owner's footer.legal + cookie banner + skip-link surface
    — single SoT for trust-base across every page (Inv-SITE-trust-base).
    """
    fm, body_md = parse_static_md(md_text)
    title = fm.get("title") or ""
    description = fm.get("description") or title
    slug = fm.get("slug") or ""
    body_html = _md_static_to_html(body_md)
    # footer.legal block — Inv-SITE-trust-base. Same projection used by
    # p_event_landing (line ~2055) so the legal colophon is byte-equivalent
    # across every surface (event landing, owner site, static page).
    legal_html = _legal_footer(d)
    article = (f'  <article class="article-wrapper">{body_html}'
               f'{legal_html}</article>')
    canonical = ""
    base_canon = _canonical(d)
    if base_canon and slug:
        canonical = f"{base_canon}/{slug}/"
    return _layout(
        d,
        title=(title or "Страница"),
        description=description[:160],
        body=article,
        nav=True,
        canonical=canonical or None,
        # Static pages are owner-level (legal/manifesto). Owner-portrait
        # footer suppressed to mirror event-landing convention — legal-footer
        # in _layout still emits for trust-base discoverability.
        footer=False,
        surface="editorial",
    )


def discover_static_pages(site_dir) -> list[tuple[str, "Path"]]:
    """List (slug, path) for every site/<slug>.md that is NOT an event override.

    Event override convention: <event_id>.md sibling to data.yaml — handled by
    `merge_event_with_md`. Static pages are everything else: privacy.md,
    oferta.md, manifesto.md, etc. Pure function. Sorted for deterministic
    deploy ordering. Slug = filename stem.
    """
    site_dir = Path(site_dir)
    if not site_dir.is_dir():
        return []
    excluded: set[str] = set()
    dy = site_dir / "data.yaml"
    if dy.is_file():
        try:
            d = yaml.safe_load(dy.read_text(encoding="utf-8")) or {}
            for ev in (d.get("events") or []):
                eid = ev.get("id")
                if eid:
                    excluded.add(eid)
        except Exception:
            pass
    out: list[tuple[str, Path]] = []
    for p in sorted(site_dir.iterdir()):
        if not p.is_file() or p.suffix != ".md":
            continue
        if p.name.startswith("_") or p.name.startswith("."):
            continue
        slug = p.stem
        if slug in excluded:
            continue
        out.append((slug, p))
    return out


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
    """Booking page. Uses _layout for head/footer; booking-specific CSS via extra_head.

    transport_url SoT (priority order, fail-loud if absent — no hardcode fallback):
      1. data.yaml.booking.transport_url
      2. <ROOT>/booking.json::transport_url   (legacy slots-bundle path)
      3. <ROOT>/engage.json::transport_url    (engage_transport.push_site path)
    Slots source: same files (booking.json or engage.json), `slots` key. Empty list OK.
    """
    import json as _json
    cons = d["consultations"]
    # Slots-bundle file (engage_transport writes engage.json; legacy: booking.json).
    slots_data: dict = {"slots": [], "user": ""}
    for cand in (ROOT / "booking.json", ROOT / "engage.json"):
        if cand.exists():
            slots_data = _json.loads(cand.read_text())
            break
    # transport_url resolution — fail-loud if neither SoT carries it.
    transport_url = (
        ((d.get("booking") or {}).get("transport_url"))
        or slots_data.get("transport_url")
    )
    if not transport_url:
        owner = (d.get("bio") or {}).get("canonical") or (d.get("bio") or {}).get("title") or "<unknown>"
        raise RuntimeError(
            f"booking transport_url required (data.yaml.booking.transport_url "
            f"or booking.json/engage.json::transport_url) for owner {owner!r}"
        )
    slots_json = _json.dumps(slots_data.get("slots", []), ensure_ascii=False)
    desc_plain = cons["description"].strip().replace("\n", " ").replace("  ", " ")
    contact_email = cons.get("calendar_id", "o.g.rozet@gmail.com")

    booking_style = """<style>
.booking{max-width:420px;margin:0 auto;padding:2.5rem 1.5rem 2rem}
.booking h2{font-size:clamp(1.1rem,1rem + 0.3vw,1.3rem);text-align:center;font-weight:600;margin-bottom:.15rem}
.sub{text-align:center;color:var(--muted,#666);font-size:.95rem}
.tz{text-align:center;color:#aaa;font-size:.8rem;margin-bottom:1rem}
.day{margin-bottom:.8rem}
.day-label{font-size:.85rem;color:var(--muted,#666);margin-bottom:.3rem;font-weight:500}
.slots-grid{display:flex;flex-wrap:wrap;gap:.3rem}
.t{display:inline-flex;align-items:center;justify-content:center;min-width:3.5rem;min-height:3rem;padding:.5rem 1rem;border:1px solid var(--rule,#ddd);border-radius:2rem;cursor:pointer;font-size:.95rem;transition:border-color .15s,background .15s,color .15s,transform .1s;user-select:none;-webkit-tap-highlight-color:transparent}
.t:hover{border-color:var(--ink,#1a1a1a)}
.t:focus-visible{outline:2px solid var(--ink,#1a1a1a);outline-offset:2px}
.t:active{transform:scale(.95)}
.t.on{background:var(--ink,#1a1a1a);color:#fff;border-color:var(--ink,#1a1a1a)}
.more{text-align:center;margin:.6rem 0}
.more button{background:none;border:none;color:var(--muted,#666);font-size:.85rem;cursor:pointer;font-family:inherit;padding:.5rem 1rem}
.bk-form{overflow:hidden;max-height:0;opacity:0;transition:max-height .35s ease,opacity .3s ease;margin-top:0}
.bk-form.open{max-height:20rem;opacity:1;margin-top:1rem}
.bk-label{display:block;font-size:.8rem;color:var(--muted,#666);margin-bottom:.15rem;margin-top:.4rem}
.bk-input{display:block;width:100%;padding:.75rem .9rem;border:1px solid var(--rule,#ddd);border-radius:.5rem;font-size:.95rem;font-family:inherit;transition:border-color .15s}
.bk-input:focus{border-color:var(--ink,#1a1a1a);outline:none}
.bk-input.ok{border-color:#2a7a2a}
.bk-input.err{border-color:#c00;animation:shake .3s}
@keyframes shake{0%,100%{transform:translateX(0)}25%{transform:translateX(-4px)}75%{transform:translateX(4px)}}
.bk-btn{display:block;width:100%;padding:.9rem;margin-top:.6rem;background:var(--ink,#1a1a1a);color:#fff;border:none;border-radius:.5rem;font-size:.95rem;font-weight:500;cursor:pointer;font-family:inherit;min-height:3rem;letter-spacing:.03em;transition:background .15s,opacity .15s}
.bk-btn:hover:not(:disabled){background:#333}
.bk-btn:focus-visible{outline:2px solid var(--ink,#1a1a1a);outline-offset:2px}
.bk-btn:disabled{background:#d0d0d0;cursor:default;pointer-events:none}
.bk-btn.sending{opacity:.7}
.result{text-align:center;padding:2rem 0;line-height:1.6}
.result b{display:block;font-size:1.1rem;margin-bottom:.5rem}
.result .next{color:var(--muted,#666);font-size:.9rem;margin-top:.5rem}
.msg{text-align:center;padding:.6rem;line-height:1.5;font-size:.9rem}
.msg.error{color:#c00}
.back{text-align:center;margin-top:1.5rem}
.back a{color:#aaa;font-size:.85rem;text-decoration:none;border:none}
.no-slots{text-align:center;color:var(--muted,#666);padding:1.5rem 0;line-height:1.6}
.no-slots a{color:var(--ink,#1a1a1a)}
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
  msgEl.innerHTML="<div class='result'><span class='result-headline'>Заявка принята</span> "+slot.time+" · "+
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
