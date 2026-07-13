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
from typing import Any, Callable

_validate_event: Any
InvalidEvent: Any
EventModel: Any
try:
    from event_schema import validate as _validate_event, InvalidEvent, EventModel
except ImportError:
    # When generate.py is copied into a deployed repo (broadcast.update_site),
    # event_schema lives alongside via copy step — but if missing, fall back
    # to identity validation so legacy clones don't crash.
    _validate_event = None
    InvalidEvent = ValueError
    EventModel = None


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

import logging as _logging
_LOG = _logging.getLogger("site_generator")


def _spec_ed(inv: str) -> dict:
    """enforcement_data for `inv` from the Spec. Fail-LOUD (Inv-CS-fail-loud): a read failure is
    LOGGED (never a silent swallow) then degrades to {} so the projection stays total. Collapses
    the former per-call try/except-empty pattern (5 call sites) to ONE seam."""
    try:
        from spec_data import enforcement_data_for_invariant
        return enforcement_data_for_invariant(inv) or {}
    except Exception as e:
        _LOG.warning("spec enforcement_data(%s) unread (%s) — degrading to {}", inv, type(e).__name__)
        return {}


def _spec_fm(name: str) -> dict:
    """frontmatter for Spec `name`. Fail-LOUD (log then {}). Collapses the per-call
    try/except-empty frontmatter reads (flagged sites)."""
    try:
        from spec_data import frontmatter
        return frontmatter(name) or {}
    except Exception as e:
        _LOG.warning("spec frontmatter(%s) unread (%s) — degrading to {}", name, type(e).__name__)
        return {}


def _load_typo_rules(lang: str = "ru") -> dict[str, Any]:
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


def _compile_typo_regexes(rules: dict[str, Any]) -> tuple[Any, ...]:
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
        def case_class(w: str) -> str:
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
    # Третий класс правил (данные, generic): NBSP-скрепление сепараторов.
    # nbsp_before: пробел ПЕРЕД знаком → NBSP («слово —» не рвётся: тире не
    # открывает строку — 36 разрывных на конспекте, Σ 2026-07-11);
    # nbsp_around: NBSP с обеих сторон («·», «×» — мета-сепараторы).
    # Пары нормализации набора (данные): напр. «т.ч.»→«т. ч.» (NBSP внутри) —
    # орфография НАБОРА, не голоса (тот же класс, что typographic-quotes).
    replacements = [(str(a), str(b)) for a, b in (rules.get("typo_replacements") or [])]
    glue_before = rules.get("nbsp_before") or []
    glue_around = rules.get("nbsp_around") or []
    before_re = None
    if glue_before or glue_around:
        alt = "|".join(_re.escape(c) for c in [*glue_before, *glue_around])
        before_re = _re.compile(rf" (?=(?:{alt}))")
    around_re = None
    if glue_around:
        alt2 = "|".join(_re.escape(c) for c in glue_around)
        around_re = _re.compile(rf"((?:{alt2})) ")
    return unit_re, prep_re, before_re, around_re, tuple(replacements)


@_lru_cache(maxsize=16)
def _typo_compiled(lang: str) -> tuple[Any, ...]:
    """Per-language compiled NBSP regexes. Cached — first call per lang
    loads YAML + compiles; subsequent calls reuse. Adding new language =
    drop knowledge/system/typography/<lang>.yaml; no code change.

    Spec: knowledge/system/specifications/text/typography.md
          (Inv-TYPO-no-hanging-words, Inv-TYPO-thin-space-numbers).
    """
    return _compile_typo_regexes(_load_typo_rules(lang))


@_lru_cache(maxsize=1)
def _vulgar_fraction_table() -> "tuple[dict[str, str], str]":
    """Inv-TYPO-vulgar-fraction-glyph data — table of «N/M» → Unicode vulgar
    fraction glyph; plus fraction_slash codepoint (U+2044) для non-standard pairs."""
    cfg = _math_symbols_cfg()
    return (cfg.get("vulgar_fractions") or {}, cfg.get("fraction_slash") or "⁄")


_VULGAR_FRAC_RE = _re.compile(r"(?<![\d/\w])(\d+)/(\d+)(?![\d/])")


def _vulgar_fractions_apply(s: str) -> str:
    """Convert ASCII «N/M» → Unicode vulgar fraction glyph (½, ⅓, ¾, ...) per
    Inv-TYPO-vulgar-fraction-glyph. Table-driven via Spec data; non-standard
    pairs fallback к fraction-slash form `N⁄M` (U+2044, font kern'd-fraction).
    Conservative lookbehind/lookahead excludes dates (1/2/2026), versions
    (v1/2), digit-runs. Idempotent."""
    table, slash = _vulgar_fraction_table()
    if not table:
        return s
    def repl(m: "_re.Match[str]") -> str:
        key = f"{m.group(1)}/{m.group(2)}"
        if key in table:
            return table[key]
        return f"{m.group(1)}{slash}{m.group(2)}"
    return _VULGAR_FRAC_RE.sub(repl, s)


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
    unit_re, prep_re, before_re, around_re, replacements = _typo_compiled(lang)
    out = s
    for _a, _b in replacements:
        out = out.replace(_a, _b)
    if unit_re is not None:
        out = unit_re.sub(r"\1" + _NBSP + r"\2", out)
    if prep_re is not None:
        out = prep_re.sub(r"\1" + _NBSP, out)
    if before_re is not None:
        out = before_re.sub(_NBSP, out)
    if around_re is not None:
        out = around_re.sub(r"\1" + _NBSP, out)
    # Inv-TYPO-apostrophe-curly: straight ' → curly ’ (U+2019).
    # Conservative: only between alphanumeric boundaries (don't touch code/quotes).
    out = _re.sub(r"(\w)'(\w)", r"\1’\2", out, flags=_re.UNICODE)
    # Inv-TYPO-typographic-quotes: ASCII " → locale's outer guillemets via pair-walk.
    # Governing locale = text's overall locale (admin 2026-05-11). Pair-walk depth:
    # 0 = next " is OPEN, 1 = next " is CLOSE. Idempotent (no ASCII " → no-op).
    rules = _load_typo_rules(lang)
    quotes = (rules.get("quotes") or {}).get("outer") or []
    if quotes and len(quotes) == 2 and '"' in out:
        q_open, q_close = quotes
        buf, depth = [], 0
        for ch in out:
            if ch == '"':
                buf.append(q_open if depth == 0 else q_close)
                depth ^= 1
            else:
                buf.append(ch)
        out = "".join(buf)
    # Inv-TYPO-em-dash-not-hyphen (compound case): «\w+—\w+» tight em-dash between
    # word-chars (no surrounding spaces) = compound word with WRONG em-dash; fix к hyphen.
    # Spaces around em-dash preserved (parenthetical/dialogue context).
    out = _re.sub(r"(?<=\w)—(?=\w)", "-", out, flags=_re.UNICODE)
    # Inv-TYPO-vulgar-fraction-glyph: «1/2» → «½», «3/4» → «¾», etc.
    # Non-standard pairs (5/9, 7/13, …) → fraction-slash form `N⁄M`.
    out = _vulgar_fractions_apply(out)
    # Inv-TYPO-en-dash-vs-em — Spec proof is `deferred` (Phase 3: text-scan + admin
    # discipline). The earlier eager `(\d)-(\d)→\1–\2` substitution mangled ISO dates
    # «2026-05-13»→«2026–05–13» and phone numbers system-wide; removed to match the Spec.
    return out


def _t(s: Any) -> str:
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


def _h(s: Any) -> str:
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
_HAND_RE = _re.compile(r'\{hand:([^}]+)\}')


def _md_handwriting(s: str) -> str:
    """`{hand:text}` → `<span class="handwriting">text</span>` (editorial accent —
    handwriting font CSS class applied at render-time). Pure shortcode substitution;
    applied AFTER html-escape so braces survive the escape pass. Admin 2026-05-13
    «"в культовом ресторане" рукописно»."""
    return _HAND_RE.sub(
        lambda m: f'<span class="handwriting">{m.group(1)}</span>', s)


def _md_links(s: str) -> str:
    """Convert markdown-style [text](url) → <a href="url">text</a>.
    Applied AFTER html-escape (square brackets/parens preserved by escape).
    Used in places admin authors anchor-markup в data.yaml prose (subevent
    description, contact, etc.)."""
    def _repl(m: "_re.Match[str]") -> str:
        text = m.group(1)
        url = _u(m.group(2))
        return f'<a href="{url}">{text}</a>'
    return _MD_LINK_RE.sub(_repl, s)


def _inline(s: Any) -> str:
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
    return "" if not s else _md_handwriting(_wrap_math_rel(_html.escape(_typo(str(s)), quote=True)))



def _paras(text: Any) -> list[str]:
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
def _no_terminal_period_cfg() -> "tuple[_re.Pattern[str], _re.Pattern[str] | None]":
    """Inv-TYPO-no-terminal-period-block — config from the Spec, NOT hardcoded here:
    knowledge/system/specifications/text/typography.md::enforcement_data.no_terminal_period_block
    → (strip_re, keep_abbrev_re). Sole SoT for the char / abbreviation lists.
    The block-size gate retired 2026-05-12 (admin «Развлечение» symptom) — rule fires
    on any non-empty paragraph chain's last element, including single-string fragments."""
    here = Path(__file__).resolve()
    cfg: dict[str, Any] = {}
    for parent in here.parents:
        spec = parent / "knowledge" / "system" / "specifications" / "text" / "typography.md"
        if spec.is_file():
            from spec_data import split_frontmatter   # canonical line-boundary split
            parts = split_frontmatter(spec.read_text(encoding="utf-8"))
            if parts is not None:
                fm = yaml.safe_load(parts[1]) or {}
                cfg = (fm.get("enforcement_data") or {}).get("no_terminal_period_block") or {}
            break
    strip_char = str(cfg.get("strip") or ".")
    abbrevs = list(cfg.get("keep_if_abbrev") or ["г", "гг", "руб", "р", "км", "м"])
    esc_strip = _re.escape(strip_char)            # «.» → «\.» — already a literal-match atom
    # Strip the terminal «.» whenever it is the last char of the fragment.
    # Spec `keep_terminal` (?!…»”):) is informational — if the last char is one
    # of those, the regex simply does not match `\.$` and the strip is a no-op.
    # We do NOT block the strip via lookbehind: «(…)». inside a sentence ends
    # with sentence-terminal «.» that still must be removed (admin 2026-05-13:
    # day-1 «Pierre Chareau).» vs day-2 «инсталляции.» inconsistency — `)`
    # before terminal `.` was wrongly treated as a no-strip marker).
    strip_re = _re.compile(rf"{esc_strip}$")
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
def _math_symbols_cfg() -> dict[str, Any]:
    """Inv-TYPO-math-rel-aligned + Inv-TYPO-comparator-symbolic config from Spec:
    knowledge/system/specifications/text/typography.md::enforcement_data.math_symbols.
    Returns dict with relation_codepoints (list), comparator_glyphs (dict),
    comparator_prose (dict[locale][comp]), css_class (str). Sole SoT — no hardcode."""
    here = Path(__file__).resolve()
    cfg: dict[str, Any] = {}
    for parent in here.parents:
        spec = parent / "knowledge" / "system" / "specifications" / "text" / "typography.md"
        if spec.is_file():
            from spec_data import split_frontmatter   # canonical line-boundary split
            parts = split_frontmatter(spec.read_text(encoding="utf-8"))
            if parts is not None:
                fm = yaml.safe_load(parts[1]) or {}
                cfg = (fm.get("enforcement_data") or {}).get("math_symbols") or {}
            break
    return cfg


@_lru_cache(maxsize=1)
def _math_rel_wrap_re() -> "_re.Pattern[str]":
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
    result: str = (_math_symbols_cfg().get("comparator_glyphs") or {}).get(str(comp).lower(), str(comp))
    return result


def _comparator_prose(comp: str, locale: str = "ru") -> str:
    """Inv-TYPO-comparator-symbolic — comparator name → locale prose (для aria-label / SEO).
    Empty string fallback если locale/comparator не в таблице."""
    table = (_math_symbols_cfg().get("comparator_prose") or {}).get(str(locale).lower(), {})
    result: str = table.get(str(comp).lower(), "")
    return result


_WEEKDAY_RU_PREP = ["В понедельник", "Во вторник", "В среду", "В четверг",
                    "В пятницу", "В субботу", "В воскресенье"]


def _when_relative_phrase(when_iso: "str | None") -> str:
    """ISO ts → «Сегодня» / «Завтра» / «В <weekday>» relative phrase.
    Resolves the `{when_relative}` placeholder в subevent description (admin
    2026-05-13). Fallback к weekday-prep когда parse fails or ts is missing."""
    from datetime import datetime as _dt, date as _date, timedelta as _td
    if not when_iso or not isinstance(when_iso, str):
        return ""
    try:
        dt = _dt.fromisoformat(when_iso)
        d_target = dt.date()
    except (ValueError, TypeError):       # malformed iso — narrow, not a silent catch-all
        return ""
    d_today = _date.today()
    if d_target == d_today:
        return "Сегодня"
    if d_target == d_today + _td(days=1):
        return "Завтра"
    return _WEEKDAY_RU_PREP[d_target.weekday()]


def _event_heading(ev: "dict[str, Any] | None", key: str, default: str) -> str:
    """SoT for editorial-overridable section headings (Программа / Перед поездкой /
    Условия и сроки / Об Организаторах). Reads `ev.headings.<key>`; falls back
    to `default` when key absent. Explicit empty-string override = suppress
    signal — caller emits no <h2>. Editable via landing-text-projection
    (`=== headings.<key> ===`)."""
    if not isinstance(ev, dict):
        return default
    headings = ev.get("headings") or {}
    if key not in headings:
        return default
    h = headings.get(key)
    return h.strip() if isinstance(h, str) else default


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


def _serialize_attrs(tag: Any) -> str:
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


def document_outline(html: str) -> list[dict[str, Any]]:
    """Extract the heading-tree from rendered HTML.

    Returns a forest (list of root nodes); nodes have shape
        {'level': 1, 'text': '...', 'id': '...', 'attrs': '...', 'children': [...]}.
    Tag-only attribute-string is preserved (lets auditors distinguish
    `<h3 class="day-theme">` from a generic h3).

    Uses BeautifulSoup4 (html.parser) — handles nested tags, attribute
    quoting variants, malformed HTML, and document-order traversal
    correctly. Strips inner tags from heading text via `get_text()`.
    """
    from bs4 import BeautifulSoup  # type: ignore[import-not-found]

    soup = BeautifulSoup(html or "", "html.parser")
    flat: list[dict[str, Any]] = []
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
    nodes: list[dict[str, Any]] = []
    stack: list[dict[str, Any]] = []
    for n in flat:
        while stack and stack[-1]["level"] >= n["level"]:
            stack.pop()
        if stack:
            stack[-1]["children"].append(n)
        else:
            nodes.append(n)
        stack.append(n)
    return nodes


def outline_audit(tree: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Inv-DOC-OUTLINE checks. Returns issues; empty list = clean.

    Each issue: {kind: str, where: str, detail: str}.
    Kinds: 'multiple_h1' | 'skipped_level' | 'empty_heading'.
    """
    issues: list[dict[str, Any]] = []

    def walk(nodes: list[dict[str, Any]], parent_level: int = 0) -> None:
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


def _u(s: Any) -> str:
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


def load() -> dict[str, Any]:
    data: dict[str, Any] = yaml.safe_load(DATA.read_text(encoding="utf-8"))
    return data


def _booking_disabled(d: dict[str, Any], owner: str = "olgarozet") -> bool:
    """Computed predicate. Disabled iff:
      (a) data.yaml::booking_disabled = true  — admin's explicit lock, OR
      (b) .state/engage/<owner>/slots.json::slots is empty — substrate dry.

    Auto-derive (b) removes the manual sync burden between substrate state
    and admin's flag. Admin removes booking_disabled flag → predicate
    falls к slots.json check → reflects actual availability.

    Single-action restore path: populate ANY tier (oauth re-grant OR SA OR
    data.yaml::booking.manual_slots) → engage_sync writes slots.json with
    events → predicate flips false → site renders booking page on next
    build. No second admin action required for the flag.

    Fail-safe: missing slots.json ⇒ disabled (no orphan booking links).
    Inv-PROV-substrate-diversity (provider.md) — substrate cascade reflected
    architecturally в the predicate.
    """
    if d.get("booking_disabled"):
        return True
    try:
        import json as _json, os as _os
        from config import DELA_HOME as _DH
        slots_path = (_DH / ".state" /
                      "engage" / owner / "slots.json")
        if not slots_path.is_file():
            return True
        slots = _json.loads(slots_path.read_text(encoding="utf-8")).get("slots") or []
        return not slots
    except Exception:
        return True


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


def _split_event_md(text: str) -> tuple[dict[str, Any], str]:
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


def load_event_md_for(owner_site_dir: str | Path, event_id: str) -> tuple[dict[str, Any], str] | None:
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


def merge_event_with_md(ev_yaml: dict[str, Any], owner_site_dir: str | Path) -> dict[str, Any]:
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


def _canonical(d: dict[str, Any]) -> str:
    """Owner's canonical URL (no trailing slash)."""
    result: str = d.get("bio", {}).get("canonical", "").rstrip("/")
    return result


def _portrait(d: dict[str, Any]) -> str:
    """Owner's portrait filename (lives in repo root)."""
    result: str = d.get("bio", {}).get("portrait", "")
    return result


def _portrait_night(d: dict[str, Any]) -> str:
    """Owner's night-mode portrait filename (optional; absent → CSS fallback to day)."""
    result: str = d.get("bio", {}).get("portrait_night", "")
    return result


# ── Shared HTML fragments ────────────────────────────────────────────

def _theme_script(d: dict[str, Any]) -> str:
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
    solar = _spec_ed("Inv-SITE-solar-theme")
    iface = _spec_ed("Inv-IFACE-day-night-mode")
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

YT_SVG = '<svg viewBox="0 0 24 24" fill="currentColor"><path d="M23.498 6.186a3.016 3.016 0 0 0-2.122-2.136C19.505 3.545 12 3.545 12 3.545s-7.505 0-9.377.505A3.017 3.017 0 0 0 .502 6.186C0 8.07 0 12 0 12s0 3.93.502 5.814a3.016 3.016 0 0 0 2.122 2.136c1.871.505 9.376.505 9.376.505s7.505 0 9.377-.505a3.015 3.015 0 0 0 2.122-2.136C24 15.93 24 12 24 12s0-3.93-.502-5.814zM9.545 15.568V8.432L15.818 12l-6.273 3.568z"/></svg>'

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
          og_image: str = "", extra: str = "", structured: str | None = None,
          d: dict[str, Any] | None = None) -> str:
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


def _cookie_banner(d: dict[str, Any], placement: "str | None" = None) -> str:
    """Project data.yaml.legal.cookie_consent + privacy_url → 152-ФЗ banner.

    Renders ONLY when required=true AND privacy_url set; missing privacy_url
    produces no banner (silent default would claim consent for a non-existent
    policy — 152-ФЗ violation). Explicit accept (active action per 152-ФЗ);
    buttons ≥44px (Inv-LDG-design-touch44). localStorage `dela.cookie.v1`.

    `placement="inline"` — блок В ПОТОКЕ у signup-формы (admin 2026-07-11:
    «сообщение о персональных данных — только у формы регистрации на
    посадочной»): сообщение живёт там, где персональные данные СОБИРАЮТСЯ;
    страничный overlay не эмитится нигде (_layout default False).
    """
    legal = (d.get("legal") or {}) if isinstance(d, dict) else {}
    cc = legal.get("cookie_consent") or {}
    if cc.get("required") is False:
        return ""
    privacy_url = _u(legal.get("privacy_url") or "")
    if not privacy_url:
        return ""
    placement = placement or cc.get("banner_placement") or "bottom"
    # Copy + storage-key live in spec.enforcement_data.Inv-COOKIE-banner —
    # single SoT, no inline RU strings. Fail-loud on missing keys (cookie
    # banner that ships «{{undefined}}» to users is a 152-ФЗ violation worse
    # than no banner). Required keys: storage_key, heading, body_template,
    # privacy_link_text, accept_label, decline_label.
    copy = _spec_ed("Inv-COOKIE-banner")
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


def _theme_toggle(d: dict[str, Any] | None = None) -> str:
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
    iface = _spec_ed("Inv-IFACE-day-night-mode")
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


def _legal_footer(d: dict[str, Any]) -> str:
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
        trust_ed = _spec_ed("Inv-SITE-trust-base")
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


#: Канал → иконка. ДАННЫЕ: добавить канал = одна строка. Отсутствие иконки НЕ ГЛОТАЕТСЯ
#: (ровно этот тихий пропуск и держал YouTube невидимым) — канал выходит в мир текстовой
#: ссылкой и виден как недооформленный, а не исчезает.
_CHANNEL_ICON = {"instagram": IG_SVG, "telegram": TG_SVG, "youtube": YT_SVG}
_CHANNEL_LABEL_SOCIAL = {"instagram": "Instagram", "telegram": "Telegram", "youtube": "YouTube"}


def channels(urls: dict[str, Any]) -> list[tuple[str, str]]:
    """Каналы владельца — ВЫВЕДЕНЫ из объявленного адресного пространства, не перечислены.

    Стол lumen [0627ce36] + директива админа 2026-07-12 («максимально грамотное обеспечение
    проводки в т.ч. к шаблонам»). Прежде футер и schema.org::sameAs держали РУЧНОЙ СПИСОК из
    двух каналов — `(ig_url, tg_url)`. YouTube Ольги был объявлен в data.yaml::urls, выведен
    Системой в её address_space — и НЕ ВИДЕН МИРУ: ни человеку в футере, ни машине в sameAs.
    Объявить канал в графе данных и не увидеть его в мире — это и есть общий генератор
    дефектов сессии: ПОТРЕБИТЕЛЬ ПЕРЕЧИСЛЯЕТ ВМЕСТО ТОГО, ЧТОБЫ ВЫВОДИТЬ.

    Носитель — `urls{}`: значение-URL есть КАНАЛ; `*_handle` (@olgaroset) — тождество, а не
    путь действия (entity-publication::Addr), и каналом не становится. Порядок — порядок
    объявления: он и есть решение владельца о старшинстве."""
    return [(k, v) for k, v in (urls or {}).items()
            if isinstance(v, str) and v.startswith(("http://", "https://"))]


def _social_link(kind: str, url: str) -> str:
    label = _CHANNEL_LABEL_SOCIAL.get(kind, kind.capitalize())
    icon = _CHANNEL_ICON.get(kind)
    if icon:
        return f'<a href="{_t(url)}" class="social-icon" aria-label="{label}">{icon}</a>'
    return f'<a href="{_t(url)}" class="social-icon social-text" aria-label="{label}">{label}</a>'


def _footer(urls: dict[str, Any], bio_title: str, portrait: str = "", portrait_night: str = "") -> str:
    night_img = (
        f'<img src="/{portrait_night}" alt="" class="footer-portrait night" aria-hidden="true">'
        if portrait_night else ''
    )
    # Портрет — центр композиции; каналы расходятся вокруг него. При двух каналах раскладка
    # ТОЖДЕСТВЕННА прежней ([IG] портрет [TG]) — новый канал не перерисовывает страницу, он
    # в неё ВСТАЁТ. Композиция ВЫВОДИТСЯ из числа каналов, поэтому четвёртый не потребует
    # ничьей правки шаблона.
    chans = channels(urls)
    half = (len(chans) + 1) // 2
    left = "\n    ".join(_social_link(k, u) for k, u in chans[:half])
    right = "\n    ".join(_social_link(k, u) for k, u in chans[half:])
    return f"""<footer>
  <div class="footer-content">
    {left}
    <img src="/{portrait}" alt="{bio_title}" class="footer-portrait day">
    {night_img}
    {right}
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


def _layout(d: dict[str, Any], *, title: str, description: str, body: str,
            nav: bool = False, canonical: str | None = None,
            extra_head: str = "", footer: bool = True, structured: str | None = None,
            surface: str = "", cookie_banner_enabled: bool = False,
            slug: str = "") -> str:
    if canonical is None:
        canonical = _canonical(d)
    portrait = _portrait(d)
    portrait_night = _portrait_night(d)
    og_image = f"{_canonical(d)}/{portrait}" if portrait else ""
    # Inject `<meta name="dela:slug">` для pageview pingback script in doc
    # skeleton (entity-statistics G-Set; admin 2026-05-13 «считает статистику»).
    _slug_meta = f'<meta name="dela:slug" content="{_t(slug)}">\n' if slug else ""
    head = _head(title, description, canonical=canonical, og_image=og_image,
                 extra=(_slug_meta + extra_head), structured=structured, d=d)
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
<script>
/* Display-window indicator 1[from ≤ now < until) per viewer clock
   (Inv-STF-window-derived, surface-temporal-fixpoint.md). The reveal
   (`data-visible-from`) is the exact dual of the original auto-hide
   (`data-visible-until`, admin 2026-05-13: «пусть блок исчезнет после 20:30
   по Москве») — one indicator covers both: a block appears at its window
   start and disappears at its window end, so a page deployed before a
   boundary still transitions at the precise moment without a redeploy.
   Runs at page-load and every 30s thereafter — covers tabs left open across
   a boundary. An unparseable/absent bound fails open on that side (a parse
   error must never hide content). */
(function(){{
  function _applyWindows(){{
    var nodes = document.querySelectorAll('[data-visible-until],[data-visible-from]');
    var now = Date.now();
    for (var i = 0; i < nodes.length; i++) {{
      var from = Date.parse(nodes[i].getAttribute('data-visible-from') || '');
      var until = Date.parse(nodes[i].getAttribute('data-visible-until') || '');
      var vis = (isNaN(from) || now >= from) && (isNaN(until) || now < until);
      nodes[i].style.display = vis ? '' : 'none';
    }}
  }}
  _applyWindows();
  setInterval(_applyWindows, 30000);
}})();

/* Pageview pingback — entity-statistics G-Set event (admin 2026-05-13 «считает
   статистику»). One event per page-load → CF Worker /pv → DELA_STATS KV.
   No cookies, no client-id; idempotency via cf-ray (CF generates per request).
   No-op gracefully if Worker unreachable (sendBeacon + fallback fetch). */
(function(){{
  var slug = (document.querySelector('meta[name="dela:slug"]') || {{}}).content;
  if (!slug) return;
  var payload = JSON.stringify({{
    p: slug,
    r: (window.crypto && crypto.randomUUID) ? crypto.randomUUID() :
       (Date.now() + '-' + Math.random().toString(36).slice(2)),
    ref: document.referrer || ''
  }});
  var url = 'https://dela-edge.azaryarozet.workers.dev/pv';
  try {{
    var blob = new Blob([payload], {{ type: 'application/json' }});
    if (!navigator.sendBeacon || !navigator.sendBeacon(url, blob)) {{
      fetch(url, {{
        method: 'POST', body: payload, keepalive: true,
        headers: {{ 'Content-Type': 'application/json' }}
      }}).catch(function(){{}});
    }}
  }} catch (e) {{ /* swallow */ }}
}})();
</script>
</body>
</html>
"""


# ── Invariants ───────────────────────────────────────────────────────

@_functools.lru_cache(maxsize=1)
def _ongoing_eligible() -> frozenset[str]:
    """Spec-loaded stored-stage set eligible for ONGOING derivation — reads
    entity-event.md::stage_time_derivation.ongoing_eligible_stages (the same
    set the rule's condition declares; machine-readable so the code never
    re-states it). Fallback mirrors _all_stages_non_terminal's resilience
    pattern: the prior contract, used only if the Spec key is unreadable."""
    try:
        fm = _spec_fm("entity-event")
        stages = (fm.get("enforcement_data", {})
                    .get("stage_time_derivation", {})
                    .get("ongoing_eligible_stages", []))
        if stages:
            return frozenset(stages)
    except Exception:
        pass
    return frozenset({"OPEN", "CLOSED"})


def _effective_stage(event: dict[str, Any], now_iso: str | None = None) -> str:
    """Inv-EV-stage-time-derived (entity-event.md::stage_time_derivation),
    datetime-precise per Inv-STF-datetime-precise (surface-temporal-fixpoint.md):

      - now ≥ end(t_end)                → CONCLUDED  (the moment the event ends —
                                          same-day for a datetime t_end; live-2 class)
      - start(t_key) ≤ now < end(t_end) → ONGOING    (when stored ∈ ongoing_eligible_stages)
      - otherwise                        → stored stage unchanged

    Anchors via the canonical datetime_parsers.anchor_dt (datetime = exact
    moment; date-only = its whole day/month/year). `now_iso` — ISO string, date
    or datetime, tz-aware ok (normalised к naive-UTC); a date-only now means
    that day's 00:00. Defaults к the current UTC instant. Witness:
    tests/test_effective_stage_datetime_precise.py (cross-owner grid + live-2 pin).
    """
    from datetime_parsers import anchor_dt, now_utc_naive, parse_iso_ts
    stored = (event.get("status") or event.get("lifecycle", {}).get("stage") or "PLANNING")
    if now_iso is None:
        now = now_utc_naive()
    else:
        now = parse_iso_ts(now_iso, naive_utc=True)
        if now is None:
            return stored               # unknown 'now' — nothing to derive from
    end = anchor_dt(event.get("t_end"), end=True)
    if end and now >= end:
        return "CONCLUDED"
    start = anchor_dt(event.get("t_key"))
    if start and end and start <= now < end and stored in _ongoing_eligible():
        return "ONGOING"
    return stored


@_functools.lru_cache(maxsize=1)
def _renderable_for() -> dict[str, frozenset[str]]:
    """Spec-loaded per-surface stage gate. Reads entity-event.md::enforcement_data
    .renderable_for. Cached — Spec is immutable per process."""
    fm = _spec_fm("entity-event")
    data = fm.get("enforcement_data", {}).get("renderable_for", {})
    return {surface: frozenset(stages) for surface, stages in data.items()}


@_functools.lru_cache(maxsize=1)
def _all_stages_non_terminal() -> frozenset[str]:
    """Universal fallback: lifecycle_status_taxonomy \\ {PRE_DRAFT, CONCLUDED}.
    Spec-loaded — adding new stage = YAML edit, no code change."""
    try:
        from spec_data import frontmatter
        fm = frontmatter("entity-event")
        taxonomy = fm.get("enforcement_data", {}).get("lifecycle_status_taxonomy", [])
        return frozenset(s for s in taxonomy if s not in ("PRE_DRAFT", "CONCLUDED"))
    except Exception:
        return frozenset({"PLANNING", "DRAFT", "OPEN", "CLOSED", "POSTPONED",
                          "MOVEDONLINE", "CANCELLED", "ONGOING", "PLANNED"})


def sorted_events(d: dict[str, Any], surface: str = "site", now_iso: str | None = None) -> list[Any]:
    """Events filtered by render-surface marker AND effective stage, ASC by t_key.

    Render gate (Inv-EV-stage-time-derived):
      visible(E, σ) ⇔ σ ∈ broadcast(E) ∧ effective_stage(E, now) ∈ renderable_for[σ]

    CONCLUDED stage (now > t_end) auto-excluded from ALL surfaces except `archive`
    — admin'ское «должно быть исключено Системно-математически». Surfaces
    without explicit renderable_for[σ] table entry fall back к excluding только
    PRE_DRAFT и CONCLUDED (broadcast field stays primary gate).

    NO-HARDCODE: renderable_for[σ] table loaded from entity-event.md Spec
    (admin'ское «без хардкода в каких-либо проявлениях на каждом уровне и насквозь»).

    Chronology: sort by t_key — the event's temporal HORIZON (precise date, coarse period,
    OR relative «до осени» = лето). EVERY upcoming event has a horizon; a t_key must be
    forward-honest. Σ 2026-07-06: the эфир 8 июля sat below Онлайн-Встреча/Рассказ-Показ NOT
    because the sort was wrong but because their placeholder t_key was STALE — a PAST date
    (май/июнь) for events the admin re-scoped «до наступления осени» = summer. Root = the
    DATA's lie, not the algorithm; the earlier «undated trails» patch treated the symptom
    (and wrongly shoved them past 2027) — reverted. The stale-placeholder class is now caught
    by event_invariants.check_event_temporal (upcoming event with t_key < today → surfaced),
    so it cannot recur silently. Missing t_key → last. Stable sort: ties by YAML order.
    """
    allowed = _renderable_for().get(surface, _all_stages_non_terminal())

    pool = []
    for e in d.get("events", []):
        if surface not in (e.get("broadcast") or []):
            continue
        if _effective_stage(e, now_iso) not in allowed:
            continue
        pool.append(e)
    return sorted(pool, key=lambda e: e.get("t_key", "￿"))


# ── Graph resolution: events reference entities by id (no value duplication) ─

def resolve_refs(d: dict[str, Any], kind: str, ids: Any) -> list[Any]:
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


def schema_events_jsonld(d: dict[str, Any]) -> str:
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
            obj["location"] = [_place_jsonld(l) for l in locs]
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


def p_publications(d: dict[str, Any]) -> str:
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
    if d.get("suppress_publications"):
        return ""
    from publication_invariants import _canonical_state as _pub_state
    _published = _pub_state("published")
    # `status=published` is a WRITE-TIME CLAIM, and this section is the public
    # Сайт↔TG↔IG link graph: the medium can lose a post afterwards (the admin deletes
    # it from the app — DacK_Q3Cn0T, and its v1/v2 before it) and we would render a
    # DEAD EDGE into Olga's public graph. Inclusion therefore derives from the WITNESSED
    # status, through the one derivation (Inv-PRES-consumer-derived), never from the
    # twin alone. Fail-open: unwitnessed ⊥ ⇒ still 'live' ⇒ renders exactly as today.
    from plan_status import derive_output_status  # lazy — plan_status imports us back
    pubs = sorted(
        [p for p in (d.get("publications") or [])
         if p.get("status") == _published
         and derive_output_status({"kind": "publication", "id": p.get("id")}, d)[0] == "live"],
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

def p_site(d: dict[str, Any]) -> str:
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
    # admin 2026-05-15: «Никакой ссылки на Бронирование, пока не восстановим. Текст
    # под надписью тоже становится не релевантным. Конгруэтной табличкой» —
    # description/price/availability promote service, which user can't book; они
    # дезинформируют. booking_disabled → section показывает heading + standalone
    # card «пока времён нет» (Inv-PROV-substrate-diversity restore = remove gate).
    #
    # Lumen 2026-05-15: predicate computes from BOTH admin-explicit flag AND
    # substrate state. Either:
    #   (a) data.yaml::booking_disabled=true              → force-disabled (admin lock)
    #   (b) .state/engage/<owner>/slots.json::slots == [] → auto-disabled (no availability)
    # Restoration of any substrate tier (oauth re-grant OR SA OR manual_slots)
    # populates slots.json. Admin removes booking_disabled flag → auto-derive
    # kicks in. Single-action restore.
    if _booking_disabled(d):
        cons_html = """    <section id="consultations" aria-labelledby="consultations-heading">
      <h2 id="consultations-heading">Консультации:</h2>
      <aside class="booking-empty" role="status" aria-live="polite">
        <p class="empty-eyebrow">пока времён нет<span class="rule" aria-hidden="true"></span></p>
      </aside>
    </section>"""
    else:
        desc = "<br>".join(cons["description"].strip().splitlines())
        avail = "<br>".join(cons["availability"].strip().splitlines())
        cons_html = f"""    <section id="consultations" aria-labelledby="consultations-heading">
      <h2 id="consultations-heading">Консультации:</h2>
      <p>{desc}</p>
      <p class="price">{cons['price']}</p>
      <a href="{cons['link']}" class="cta">{cons['cta']}</a>
      <p class="availability">{avail}</p>
    </section>"""

    # Events Skoro digest — delegates к skoro.render (monoidal functor, Genius Simplification C).
    # Per Inv-CMP-STYLE-CTA-anchor-uniform: hub-event-card CTA points к canonical FQDN landing
    # (event.web_addresses[0]). SkoroSpec encapsulates entry formatting per Surface; site
    # variant reads same Spec table.
    from skoro import render as render_skoro_digest_dispatch
    events_html = render_skoro_digest_dispatch(d, "site")

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
        # sameAs — КАНОНИЧЕСКАЯ машинная декларация «это те же самые каналы этого лица»
        # (schema.org). Держала тот же ручной список из двух: YouTube был объявлен и невидим
        # не только человеку в футере, но и машине. Одна деривация — оба потребителя.
        "sameAs": [u for _k, u in channels(urls)],
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
                      cta_label: str = "Оставить email",
                      lead_capture: dict[str, Any] | None = None,
                      transport_url: str = "") -> str:
    """Lead-capture form с direct POST к transport_url. Per
    `Inv-LDG-FORMS-NO-MAILTO-LOSSY-FALLBACK` (text/landing.md) — no mailto
    fallback (silently lost leads — admin 2026-05-13 empirical: 3h live → 0
    KV entries). Form action= the real transport URL; JS upgrades к AJAX
    submit for in-place «Спасибо» UX, но no-JS path также submits-and-lands.

    `cta_label` parametrises the heading + button text. `transport_url` —
    canonical lead endpoint (resolved by caller от secrets_manager
    signup_capture_url). Slug validation: must match `[a-z0-9_-]+`.
    """
    if not isinstance(slug, str) or not _EVENT_SLUG_RE.fullmatch(slug):
        raise ValueError(
            f"invalid event slug: {slug!r} — must match [a-z0-9_-]+"
        )
    import json as _json
    from urllib.parse import quote as _q
    # ── External-form provider branch (declarative; 152-ФЗ class) ─────────────
    # lead_capture.provider + lead_capture.form_url в data.yaml ⇒ the signup
    # block renders the EXTERNAL form (RU-jurisdiction collection point —
    # Яндекс-Формы для 152-ФЗ) instead of the native POST form. Pure data-edit
    # migration: no per-provider code, любой внешний form-провайдер = те же два
    # ключа. Native pipeline (Worker/lead_receiver) остаётся для событий без
    # provider. iframe + прямая ссылка (no-JS/no-iframe fallback — never lossy).
    _lc0 = lead_capture if isinstance(lead_capture, dict) else {}
    _ext_url = str(_lc0.get("form_url") or "").strip()
    if _ext_url and str(_lc0.get("provider") or "").strip():
        _u = _t(_ext_url)
        _head = _t(str(_lc0.get("submit_label") or cta_label))
        # form_id: explicit ⊔ derived from …/u/<id>… (single source; provider-agnostic).
        _fid = str(_lc0.get("form_id") or "").strip()
        if not _fid:
            _m = _re.search(r"/u/([A-Za-z0-9]+)", _ext_url)
            _fid = _m.group(1) if _m else ""
        # Yandex canonical embed: embed.js auto-resizes the iframe matched by
        # name="ya-form-<id>" (admin-supplied contract). No JS / no-iframe ⇒ the
        # direct link is the never-lossy fallback (Inv-LDG-FORMS-NO-MAILTO-LOSSY).
        _provider = str(_lc0.get("provider") or "").strip().lower()
        _embed = ('<script src="https://forms.yandex.ru/_static/embed.js"></script>'
                  if _provider == "yandex-forms" else "")
        _name = f' name="ya-form-{_t(_fid)}"' if (_embed and _fid) else ""
        return (
            f'<section class="signup" id="signup">'
            f'<h2>{_head}</h2>'
            f'{_embed}'
            f'<iframe src="{_u}" frameborder="0"{_name} loading="lazy" '
            f'style="width:100%;min-height:560px;border:0;border-radius:8px" '
            f'title="{_head}"></iframe>'
            f'<p class="signup-ext-fallback"><a href="{_u}" target="_blank" '
            f'rel="noopener">Открыть форму в новой вкладке</a></p>'
            f'</section>'
        )
    # URL-encode the bits that flow into the mailto: action attribute
    # (slug becomes subject token, label becomes body fragment, email is
    # the action target). HTML-escape the label echoed in <p>«…».
    # Subject = human-readable event label (NOT the dev-slug). Cleaner mailto
    # for traveler — they see «Заявка: Париж · сентябрь 2026» in their email
    # client, not «Заявка: paris_2026_09».
    subj_q = _q(f"Заявка: {label}", safe="")
    label_q = _q(label, safe="")
    email_q = _q(email_fallback, safe="@")
    # Resolve user-visible labels from lead_capture (data.yaml SoT) с fallback
    # to canonical defaults. Round-trip through landing_text_proj.py:
    #   lead_capture.fields.<name>.label   → form input label
    #   lead_capture.submit_label          → form button text + heading override
    #   lead_capture.consent_text          → consent checkbox label
    # Submit-button text precedence: lead_capture.submit_label → signup.cta_label
    # (the `cta_label` arg passed in by _render_signup) → "Оставить email".
    lc = lead_capture if isinstance(lead_capture, dict) else {}
    lc_fields = lc.get("fields") or {}
    def _lc_label(field_key: str, default: str) -> str:
        entry = lc_fields.get(field_key) or {}
        return str(entry.get("label") or default).strip() or default
    submit_text = (str(lc.get("submit_label") or "").strip() or cta_label)
    cta_html = _t(submit_text)
    cta_js = _json.dumps(submit_text, ensure_ascii=False)
    # Slug is admin-controlled identifier — escape for safe HTML/attr/URL.
    slug_t = _t(slug)
    # Form labels — typography-cleaned (Inv-TYPO-no-hanging-words, NBSP-bind preps).
    # «about» field is opt-in: rendered only когда lead_capture.fields.about
    # объявлен в data.yaml (admin 2026-05-13 — paris-2026-09 dropped the field).
    _raw_name    = _lc_label("name",  "Имя")
    _raw_email   = _lc_label("email", "Эл. Почта")
    lbl_name    = _typo(_raw_name)
    lbl_email   = _typo(_raw_email)
    _about_present = bool(lc_fields.get("about"))
    # Mailto fallback body — data-driven from lead_capture.fields.<key>.label.
    # Each field produces a «<Label>: \n» row в pre-populated mail body.
    # name/email always rendered; about-row only when admin declared the field.
    _mb_lines = ["Здравствуйте, Ольга.", "",
                 f"Оставляю контакт — {label}.", "",
                 f"{_raw_name}: ",
                 f"{_raw_email}: "]
    if _about_present:
        _raw_about = _lc_label("about", "Коротко о себе (сфера, город — опционально)")
        _mb_lines.append(f"{_raw_about}: ")
        # Combined label may contain a parenthetical hint inline; split keeps
        # the two-span shape («main + hint» on the same `<label>`).
        if "(" in _raw_about:
            _main, _, _hint = _raw_about.partition("(")
            _about_main = _main.strip()
            _about_hint = "(" + _hint.strip()
        else:
            _about_main, _about_hint = _raw_about.strip(), ""
        lbl_about   = _typo(_about_main)
        lbl_about_h = _typo(_about_hint)
    else:
        lbl_about = lbl_about_h = ""
    mb = _q("\n".join(_mb_lines), safe="")
    lbl_consent = _typo(str(lc.get("consent_text") or
                            "Обрабатывайте персональные данные").strip())
    lbl_or      = _typo("Или напишите:")
    _about_row = (
        f'<label class="signup-label" for="su-note">{lbl_about}'
        f'<span class="signup-hint">{lbl_about_h}</span></label>'
        f'<input class="signup-input" id="su-note" name="note" autocomplete="off">'
    ) if _about_present else ""
    # Form heading is <h3> (parent <section class=signup-wrap> already
    # provides the section's <h2 «Лист ожидания»>). Heading hierarchy
    # h2 → h3 is WCAG-correct and screen-reader-friendly.
    # Form action = canonical transport URL (CF Worker /lead). NO mailto —
    # Inv-LDG-FORMS-NO-MAILTO-LOSSY-FALLBACK. Empty action ('') falls back к
    # current URL submit when transport unknown (graceful — fails loudly с
    # 200/JSON on GH Pages instead of silently losing к user's email client).
    _action = _t(transport_url) if transport_url else ""
    _action_attr = f'action="{_action}"' if _action else 'action=""'
    # Inv-LLC-source-tag + Inv-LLC-no-hardcode: form carries edition + variant
    # из data.yaml lead_capture в скрытых полях → Worker template `{body.edition}|
    # {body.variant}` resolves data-driven (no per-event literal в substrate).
    # Substrate-agnostic equivalence с Python lead_receiver.py:223 (same body
    # field semantics).
    _lc_edition = _t(str(lc.get("edition") or "").strip())
    _lc_variant = _t(str(lc.get("variant") or slug).strip())
    _hidden_inputs = (
        f'<input type="hidden" name="edition" value="{_lc_edition}">'
        f'<input type="hidden" name="variant" value="{_lc_variant}">'
    ) if _lc_edition else ""
    return f'''<section id="signup" class="signup" aria-labelledby="signup-h">
  <h3 id="signup-h" class="signup-h3">{cta_html}</h3>
  <form id="signup-form" class="signup-form" novalidate
        aria-labelledby="signup-h"
        {_action_attr}
        method="post" enctype="application/x-www-form-urlencoded"
        data-slug="{slug_t}">
    {_hidden_inputs}
    <label class="signup-label" for="su-name">{lbl_name}</label>
    <input class="signup-input" id="su-name" name="name"
           autocomplete="name" required minlength="2" aria-required="true">
    <label class="signup-label" for="su-email">{lbl_email}</label>
    <input class="signup-input" id="su-email" name="email" type="email"
           autocomplete="email" required aria-required="true">
    {_about_row}
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
/* AJAX-upgrade: FormData captures ALL named inputs (incl. hidden edition+
   variant). consent checkbox state forced к "true" canonical. Both no-JS
   и JS paths persist via Worker KV (Inv-LDG-FORMS-NO-MAILTO). */
(function(){{
  var f=document.getElementById("signup-form");
  if(!f) return;
  var btn=document.getElementById("su-btn");
  var msg=document.getElementById("signup-msg");
  var transport=f.getAttribute("action") || "";
  /* Optional /<slug>/signup.json override — single SoT для transport_url. */
  fetch("/{slug_t}/signup.json").then(function(r){{return r.ok?r.json():null}})
    .then(function(d){{if(d&&d.transport_url)transport=d.transport_url;}})
    .catch(function(){{}});
  f.addEventListener("submit",function(e){{
    var name=f.name.value.trim(),email=f.email.value.trim();
    if(name.length<2){{e.preventDefault();f.name.focus();return;}}
    if(!/^[^@\\s]+@[^@\\s]+\\.[^@\\s]+$/.test(email)){{e.preventDefault();f.email.focus();return;}}
    if(!f.consent.checked){{e.preventDefault();f.consent.focus();return;}}
    if(!transport)return; /* no-JS path: form action handles it */
    e.preventDefault();
    btn.disabled=true;btn.textContent="отправка...";
    var fd=new FormData(f);fd.set("consent","true");
    fd.set("slug",f.dataset.slug||"");
    fetch(transport,{{method:"POST",
      headers:{{"Content-Type":"application/x-www-form-urlencoded"}},
      body:new URLSearchParams(fd).toString()}})
      .then(function(r){{return r.json()}})
      .then(function(d){{
        if(d&&d.ok){{f.style.display="none";
          msg.innerHTML="Спасибо!<br>Свяжемся.";
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
def _place_jsonld(loc: dict[str, Any], fallback_name: str = "") -> dict[str, Any]:
    """Schema.org Place — single SoT used by schema_events_jsonld + _event_jsonld
    flat-path + _event_jsonld itinerary-path. Inv-SEM-jsonld-valid:
    addressCountry на PostalAddress (not Place); free-form address strings
    pass through unchanged. fallback_name для places_table entries where
    the dict key (not "id" field) is the canonical reference."""
    obj: dict[str, Any] = {"@type": "Place",
                 "name": loc.get("name") or loc.get("id") or fallback_name}
    addr = loc.get("address")
    if isinstance(addr, str) and addr:
        obj["address"] = addr
    elif loc.get("country"):
        obj["address"] = {"@type": "PostalAddress",
                          "addressCountry": loc["country"]}
    return obj


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


def _schedule_end_iso(ev: dict[str, Any]) -> str:
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


def _beat_subtype(beat: dict[str, Any]) -> str:
    """Schema.org Event subtype for a single beat (lecture/visit/etc.)."""
    kind = (beat.get("kind") or "").lower()
    if kind in ("lecture", "talk", "masterclass", "master_class", "workshop",
                "orientation"):
        return "EducationEvent"
    if kind in ("visit", "tour", "walk", "excursion"):
        return "VisualArtsEvent"  # museum/gallery/architectural visits
    return "Event"


def _day_subevent(d: dict[str, Any], ev: dict[str, Any], day: dict[str, Any], slot: dict[str, Any] | None) -> dict[str, Any]:
    """Build a sub-Event for one day. Resolves places from typed schedule."""
    iso_date = ""
    if slot and slot.get("date"):
        dt = slot["date"]
        iso_date = dt.isoformat() if hasattr(dt, "isoformat") else str(dt)
    title = day.get("theme") or day.get("date") or f"День {day.get('day', '?')}"
    sub_type = "Event"
    locations: list[dict[str, Any]] = []
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
            place_obj: dict[str, Any] = {"@type": "Place",
                               "name": p.get("name", pid)}
            if p.get("address"):
                place_obj["address"] = p["address"]
            geo = p.get("geo") or {}
            if isinstance(geo, dict) and geo.get("lat") is not None and geo.get("lon") is not None:
                place_obj["geo"] = {"@type": "GeoCoordinates",
                                    "latitude": geo["lat"],
                                    "longitude": geo["lon"]}
            locations.append(place_obj)
    obj: dict[str, Any] = {
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


def _event_jsonld(d: dict[str, Any], ev: dict[str, Any]) -> str:
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
    obj: dict[str, Any] = {
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
        # TouristTrip: location is the trip's geographic scope.
        # Single-location trips render as one object (less syntactic noise).
        loc_list = [_place_jsonld(l) for l in locs]
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
        offer: dict[str, Any] = {"@type": "Offer",
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
        sub_events: list[dict[str, Any]] = []
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
        itin_items: list[dict[str, Any]] = []
        for pos, pid in enumerate(rm, start=1):
            p = places_table.get(pid) or {}
            itin_items.append({"@type": "ListItem",
                               "position": pos,
                               "item": _place_jsonld(p, fallback_name=pid)})
        if itin_items:
            obj["itinerary"] = {"@type": "ItemList",
                                "itemListElement": itin_items}

    return _j.dumps(obj, ensure_ascii=False)


def _event_canonical(d: dict[str, Any], ev: dict[str, Any]) -> str:
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


def _person_display(d: dict[str, Any], person_id: str) -> tuple[str, str]:
    """(name, link) for a person ref. Falls back to id-as-name if not in graph.

    People can live as dict[id]->fields OR list[{id, ...}] depending on owner.
    """
    people = d.get("people") or {}
    p: dict[str, Any] | None = None
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


def _person_link_html(d: dict[str, Any], person_id: str, escape: Callable[[Any], str]) -> str:
    """Anchor-wrapped person name. escape ∈ {_t, _inline}; single SoT для 3 sites."""
    nm, lk = _person_display(d, person_id)
    safe_lk = _u(lk)
    return f'<a href="{safe_lk}">{escape(nm)}</a>' if safe_lk else escape(nm)


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
    d: dict[str, Any]
    ev: dict[str, Any]
    m: Any                 # event_schema.EventModel (or raw dict in deployed-repo edge case)
    slug: str
    bio: dict[str, Any]
    date_str: str
    org_ids: list[str]
    inline: Callable[[Any], str]   # _partial(_inline, …) | _inline
    h_aug: Callable[[Any], str]    # = `_h` (curated-markup pass-through; foreign-name aug retired 2026-05-12)
    breath: Callable[[str], str]   # callable(text) -> str — «one breath per line»
    ph: "dict[str, str]" = _dc_field(default_factory=dict)
    pricing_html: str = ""  # rendered pricing-display aside; reused for duplicate-before-includes


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
            disp.append(_person_link_html(d, pid, _t))
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
        _pricing_aside = (
            '<aside class="pricing-display" aria-label="Стоимость">'
            f'<div class="pricing-amount">{amount_str}'
            f'<span class="currency">{cur_glyph}</span></div>'
            + (f'<div class="pricing-note">{_t(note)}</div>' if note else '')
            + '</aside>'
        )
        parts.append(_pricing_aside)
        # Stash for optional duplication before «Входит:» section
        # (admin 2026-05-14: «важно сразу называть сумму; принимаю решение
        # дублировать элемент» — оригинал у верха страницы остаётся, дубликат
        # рендерится перед секцией «Входит:» в _render_sections_and_programme).
        ctx.pricing_html = _pricing_aside

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
        # Evening-recur registry — admin 2026-05-13: «Вечер — вернисажи …
        # повторяющийся элемент». Single SoT, ноль дублирования inline.
        # Source = raw `ctx.ev` dict (EventModel validator strips unknown
        # fields; пока evenings_recurring не lifted в EventModel — reach
        # в ctx.ev directly). TODO: lift в event_schema.EventModel.
        _raw_ev = ctx.ev if isinstance(ctx.ev, dict) else {}
        _evenings_reg = _raw_ev.get("evenings_recurring") or {}
        _h_progr = _event_heading(_raw_ev, "programme", "Программа")
        _h_progr_html = f'<h2>{_t(_h_progr)}</h2>' if _h_progr else ''
        _h_progr_aria = _t(_h_progr) if _h_progr else "Программа"
        out: list[str] = [f'<section class="programme">{_h_progr_html}'
                          f'<ol class="days" aria-label="{_h_progr_aria} по дням">']
        # Inv-PARIS-design-arc-per-day (text/event-paris-2026-09.md): каждый день-card
        # carries data-day=<index> атрибут — CSS picks per-day accent token
        # (--paris-day-{n}-accent). Day-arc visually congruent с program's three-modernism arc.
        for idx, day in enumerate(days, start=1):
            d_date = day.get("date", "")
            d_theme = day.get("theme", "")
            d_notes = day.get("notes", "")
            d_evening = day.get("evening", "")
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
            # Evening-recur tile — rendered after notes если day declares
            # evening: <key> и key есть в registry.
            if d_evening and isinstance(_evenings_reg, dict) and d_evening in _evenings_reg:
                _ev = _evenings_reg[d_evening] or {}
                _ev_prefix = _ev.get("prefix", "Вечер")
                _ev_text = _ev.get("text", "")
                if _ev_text:
                    out.append(
                        f'<p class="evening-recur" data-recur="{d_evening}">'
                        f'<span class="evening-label">{_t(_ev_prefix)}</span>'
                        f' — {inline(_ev_text)}</p>'
                    )
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
        # Duplicate pricing-display aside перед секцией «Входит:» (admin 2026-05-14).
        if t.strip() == "Входит:" and ctx.pricing_html:
            parts.append(ctx.pricing_html)
        parts.append(f"<section><h2>{_t(t)}</h2>" if t and t.strip() else "<section>")
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
            _h_onb = _event_heading(ctx.ev if isinstance(ctx.ev, dict) else None,
                                    "onboarding", "Перед поездкой")
            parts.append(f'<section class="onboarding"><h2>{_t(_h_onb)}</h2><ul>')
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
        _h_terms = _event_heading(ctx.ev if isinstance(ctx.ev, dict) else None,
                                  "terms", "Условия и сроки")
        parts.append(f'<section class="terms"><h2>{_t(_h_terms)}</h2><ul>')
        for it in terms_items:
            parts.append(f"<li>{h_aug(it)}</li>")
        parts.append('</ul></section>')
    return parts


def _has_landing_terminal(d: dict[str, Any], slug: str) -> bool:
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
    from datetime_parsers import anchor_dt as _adt
    d, slug = ctx.d, ctx.slug
    inline, _breath = ctx.inline, ctx.breath
    parts: list[str] = []

    # ── Sub-event auto-injection ─────────────────────────────────────
    # Sub-events (e.g. the IG-Live preshow) that declare `broadcast: [landing_section]`
    # are rendered as standalone sections, right after the content sections (before
    # signup/contact/about) — the «Об Организаторах» block must stay the last block
    # (admin 2026-05-11: «после блока про Наталью Логинову — конец»; «блок про Наталью»
    # = Об Организаторах). Source: entity-event Spec §parent_id + Inv-EV-parent-resolves.
    # deployed(σ) ≡ render(graph, now): the SAME Spec-driven gate as every other
    # surface — sorted_events(d, "landing_section") = broadcast marker ∧
    # effective_stage ∈ renderable_for[landing_section] (entity-event.md; excludes
    # CONCLUDED and PRE_DRAFT), t_key-chronological. A concluded sub-event leaves
    # the page at render time — no manual broadcast-list surgery after the event
    # (the live-2 manual removal, admin mandate 2026-07-08); pages deployed BEFORE
    # the boundary transition client-side via the display-window attributes below.
    sub_events = [se for se in sorted_events(d, "landing_section")
                  if se.get("parent_id") == slug]
    _subev_parts: list[str] = []
    for se in sub_events:
        se_type = se.get("type", "event")
        se_title = se.get("title", "")
        se_desc = se.get("description", "")
        se_url = se.get("url")
        # Resolve `{when_relative}` placeholder from subevent.when ts vs today —
        # «Сегодня» / «Завтра» / weekday-phrase otherwise (admin 2026-05-13:
        # «Не "В среду", а "Сегодня"» — render-time, не data-yaml hardcode).
        if se_desc and "{when_relative}" in se_desc:
            se_desc = se_desc.replace("{when_relative}", _when_relative_phrase(se.get("when")))
        # Section hide-boundary DERIVED from the event's own geometry
        # (Inv-STF-window-derived): until = t_end as the EXCLUSIVE boundary (a
        # date-only t_end spans its whole day). visible_until remains an OPTIONAL
        # override for when the display window ≠ the event window (admin 2026-05-13:
        # «пусть блок исчезнет после 20:30 по Москве»), never a required duplication
        # of t_end (the live-2 manual edit this derives away). visible_from on the
        # section is override-only — an announcement block is visible from deploy;
        # the timed REVEAL belongs to the stream link below. Server render is too
        # coarse — the page IS deployed before the boundary; the JS window indicator
        # transitions at the precise moment per viewer's clock.
        _vis_until = se.get("visible_until") or _adt(se.get("t_end"), end=True)
        _vis_attr = ""
        if se.get("visible_from"):
            _vis_attr += f' data-visible-from="{_t(str(se.get("visible_from")))}"'
        if _vis_until:
            _us = _vis_until.isoformat() + "Z" if hasattr(_vis_until, "isoformat") else str(_vis_until)
            _vis_attr += f' data-visible-until="{_t(_us)}"'
        _subev_parts.append(f'<section class="subevent subevent-{_u(se_type)}"{_vis_attr}>')
        _subev_parts.append(f'<h2>{inline(se_title)}</h2>')
        if se_desc:
            # admin: «one breath per line» (per-line typography) + markdown links; «крупнее
            # абзаца» description drops its terminal «.» (Inv-TYPO-no-terminal-period-block).
            for se_para in _drop_block_close_period(_paras(se_desc)):
                _subev_parts.append(f'<p>{_md_links(_breath(se_para))}</p>')
        # Opt-out: subevent.suppress_link_block=true когда url используется inline
        # в description (admin 2026-05-13 paris-2026-09-ig-live), и отдельный
        # `<p.subevent-link>` блок дублирует ссылку.
        if se_url and not se.get("suppress_link_block"):
            _u_url = _u(str(se_url))
            _u_text = inline(str(se.get("url_text") or se_url))
            # Inv-STF-link-is-projection: the stream target is a DECLARED field the
            # page REVEALS during the event's own window [when|t_key, ·) — the dual
            # of the hide, killing the manual «drop the link вовремя» gesture. The
            # section's until-boundary already closes the window; before `when` the
            # link paragraph stays hidden per viewer clock.
            _lnk_from = _adt(se.get("when")) or _adt(se.get("t_key"))
            _lnk_iso = _lnk_from.isoformat() + "Z" if _lnk_from else ""
            _lnk_attr = f' data-visible-from="{_t(_lnk_iso)}"' if _lnk_iso else ""
            _subev_parts.append(f'<p class="subevent-link"{_lnk_attr}><a href="{_u_url}" rel="noopener">{_u_text}</a></p>')
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
                org_names.append(_person_link_html(d, oid, inline))
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
                names.append(_person_link_html(d, pid, inline))
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
        lc = m.lead_capture if hasattr(m, "lead_capture") else m.get("lead_capture")
        # Inv-LDG-FORMS-NO-MAILTO-LOSSY-FALLBACK: form action = real
        # transport URL (CF Worker /lead). Resolved via secrets_manager
        # signup_capture_url (canonical lead endpoint, configurable per
        # deployment). No-JS submit lands at Worker; JS upgrades AJAX UX.
        _transport_url = ""
        try:
            import sys as _sys, os as _os
            # НЕ хардкодить дом: путь приходит из СВОЕГО расположения (модуль
            # знает, где он лежит) — иначе archive-mode теряет изоляцию и
            # подтягивает код с ОТСТАВШЕГО диска реплики (Σ 2026-07-11:
            # деплой из архива импортировал старый broadcast_relation с FP).
            _sys.path.insert(0, str(Path(__file__).resolve().parent))
            from secrets_manager import secrets as _secrets
            _transport_url = _secrets.get_key("signup_capture_url") or ""
        except Exception as _e:
            _LOG.warning("signup_capture_url unread (%s) — empty transport", type(_e).__name__)
            _transport_url = ""
        parts.append(event_signup_form(
            slug,
            ev_label,
            bio.get("email", "info@example.com"),
            cta_label=s_cta,
            lead_capture=lc if isinstance(lc, dict) else None,
            transport_url=_transport_url,
        ))
        # Сообщение о персональных данных — У ФОРМЫ, единственное место
        # (admin 2026-07-11); страничный overlay отключён всюду (_layout).
        _consent = _cookie_banner(ctx.d, placement="inline")
        if _consent:
            parts.append(_consent)
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
    _default_aoh = "Об Организаторах" if len(org_ids) > 1 else "Об Организаторе"
    _ev_for_h = ctx.ev if isinstance(ctx.ev, dict) else None
    title = _typo(_event_heading(_ev_for_h, "about_organizer", _default_aoh))
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
            _h_aoh_alt = _event_heading(ctx.ev if isinstance(ctx.ev, dict) else None,
                                        "about_organizer", "Об Организаторе")
            parts.append('<footer class="about-organizer">'
                         f'<h2>{_t(_h_aoh_alt)}</h2>{p_blocks}</footer>')

    # (sub-events were already appended above — Об Организаторах stays the last block.)
    return parts


def _render_landing_footer_image(ctx: "_LandingCtx") -> "list[str]":
    """Phase between content blocks and legal footer — single editorial image
    «в самый низ Посадочной». Admin 2026-05-13: «максимально конгруэтная
    длинная ваза на белом фоне» + «футер ночной темы» (companion-piece для
    night surface). Optional — rendered iff ev.landing_footer_image declares
    {path[, path_night], alt}.

    Day/night variants — when path_night provided, both <img> rendered;
    CSS [data-theme] selectors show one and hide the other (small bytes cost
    < theme-swap-via-JS complexity OR re-render-on-theme-toggle).
    Sits AFTER content blocks BEFORE legal-min (Inv-SITE-trust-base preserved).
    """
    _raw_ev = ctx.ev if isinstance(ctx.ev, dict) else {}
    img_cfg = _raw_ev.get("landing_footer_image") or {}
    if not isinstance(img_cfg, dict) or not img_cfg.get("path"):
        return []
    path = str(img_cfg["path"]).strip().lstrip("/")
    path_night = str(img_cfg.get("path_night") or "").strip().lstrip("/")
    alt = str(img_cfg.get("alt") or "").strip()
    # alt_night optional — night variant may show different object (admin
    # 2026-05-13 «другой объект»); falls back к day alt when not declared.
    alt_night = str(img_cfg.get("alt_night") or alt).strip()
    if path_night:
        return [
            f'<figure class="landing-footer-image">'
            f'<img class="theme-day" src="/{_t(path)}" alt="{_t(alt)}" loading="lazy">'
            f'<img class="theme-night" src="/{_t(path_night)}" alt="{_t(alt_night)}" loading="lazy">'
            f'</figure>'
        ]
    return [
        f'<figure class="landing-footer-image">'
        f'<img src="/{_t(path)}" alt="{_t(alt)}" loading="lazy">'
        f'</figure>'
    ]


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
    # admin 2026-05-13: «низ Посадочной неприемлем — пока ничего после "2024 года"
    # не должно быть». suppress_legal_min: true → даже минимальный privacy-link
    # footer не рендерится. Inv-SITE-trust-base временно ослаблен по admin
    # директиве (binding «пока»). Cookie-banner отдельно через suppress_cookie_banner.
    # Read raw `ctx.ev` (EventModel validator strips unknown fields — see same
    # pattern с evenings_recurring earlier; TODO: lift suppress_legal_min к
    # EventModel когда Rule-of-Three tipped).
    _raw_ev = ctx.ev if isinstance(ctx.ev, dict) else {}
    suppress_legal_min = bool(_raw_ev.get("suppress_legal_min", False))
    if not suppress_legal:
        legal_html = _legal_footer(d)
        if legal_html:
            parts.append(legal_html)
    elif not suppress_legal_min:
        privacy_url = _u(((d.get("legal") or {}).get("privacy_url")) or "")
        if privacy_url:
            parts.append(
                f'<footer class="legal-min" aria-label="Юридическое">'
                f'<p><a href="{privacy_url}">Политика конфиденциальности</a></p>'
                f'</footer>'
            )
    return parts


def p_event_landing(d: dict[str, Any], ev: dict[str, Any]) -> str:
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
    m: Any
    if _validate_event is not None:
        m = _validate_event(ev)  # raises InvalidEvent on shape problems
    else:
        m = ev
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
    # arc-band footer-legend retired 2026-05-13: filled colour-bars recreate
    # Day-2 visual peak через Outremer's natural saturation/luminance, что
    # противоречит Inv-LDG-PARIS-days-equipotent (admin «зачем выделен один
    # день?»). Negative space на terminus — by design (suppress_legal_footer);
    # пусть дышит до privacy link. Polychromie-31 attribution живёт в Spec
    # (event-paris-2026-09.md::references), не на rendered page.
    parts: list[str] = [
        *_render_header(ctx),
        *_render_pricing_status(ctx),
        *_render_sections_and_programme(ctx),
        *_render_subevents(ctx),
        *_content_tail,
        *_render_landing_footer_image(ctx),
        *_render_legal(ctx),
    ]

    body = f'  <article class="article-wrapper">{"".join(parts)}</article>'

    lead_text = m.lead if hasattr(m, "lead") else m.get("lead", "")
    # SEO meta-description must be a single string — collapse paragraphs
    # for description only; the rendered lead keeps its paragraph breaks.
    # Punct-aware join (Inv-SITE-meta-word-boundary): beat boundaries that
    # end mid-phrase get « — », not a false space-glue.
    lead_meta = _meta_join(_paras(lead_text))
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
        description=_meta_trim(lead_meta or m.concept if hasattr(m, "concept") else m.get("concept", title_full)),
        body=body,
        nav=not _has_dedicated_fqdn,
        canonical=_event_canonical(d, ev),
        structured=_event_jsonld(d, ev),
        # Owner-portrait footer belongs to owner-site (olgarozet.ru) only —
        # admin directive 2026-05-02. Event landings render their own
        # contact/about-organizer block; no shared portrait/social-icons.
        footer=False,
        surface="editorial",
        # Страничный cookie-overlay отключён ВЕЗДЕ (admin 2026-07-11) —
        # сообщение живёт inline у signup-формы (_render_signup). suppress_cookie
        # сохранён выше как исторический тумблер прежнего overlay-слоя.
        cookie_banner_enabled=False,
        slug=(ev.get("id") if isinstance(ev, dict) else getattr(ev, "id", "")) or "",
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
# Markdown subset (lapidary, sufficient for legal/manifesto/konspekt docs):
#   YAML frontmatter (---…---) → title, description, slug, canonical
#   `# H1` / `## H2` / `### H3` → headings
#   `- item` (+ indented lines) → <ul><li>          (hanging-indent continuation
#                                                    stays inside the item)
#   blank-separated paragraphs  → <p>               (HTML inline pass-through;
#                                                    admin-authored, schema-trusted)
#   source newline inside block → <br>              (line-fidelity: админ правит
#                                                    построчно — перенос авторский)
#   `*em*` / `**strong**`       → <em>/<strong>     (pair may span source lines)
#   `<!-- … -->`                → admin-fill markers, suppressed in render
#                                  (visible in source for admin handoff).

_MD_STRONG_RE = _re.compile(r"\*\*([^*]+?)\*\*")
_MD_EM_RE = _re.compile(r"\*([^*]+?)\*")
# Inv-SITE-amp-normal: lone `&` (не начало entity) → `&amp;`. Слой — BODY-эмиссия
# статик-рендера, где текст уходит в HTML без экранирования; НЕ _typo: _t = typo∘escape,
# и амп-правило в разделяемом типографском слое даёт `&amp;amp;` на атрибутном пути
# (пойман смок-рендером 2026-07-11). Entity-preserving lookahead ⇒ идемпотентно.
_AMP_BARE_RE = _re.compile(
    r"&(?![a-zA-Z][a-zA-Z0-9]{1,31};|#\d{1,7};|#x[0-9a-fA-F]{1,6};)")


def _amp_normal(s: str) -> str:
    return _AMP_BARE_RE.sub("&amp;", s)


_INLINE_TAG_RE = _re.compile(r"</?(em|strong|a)\b[^>]*>")
_H_PUNCT_RE = _re.compile(r"([.!?…:;])\s*$")


#: РУ→ЛАТ для публичного адреса подтекста. Таблица — ДАННЫЕ, не политика-в-коде: адрес едет
#: в пост Телеграма и в bio Instagram, где %D1%81%D0%BB… — не адрес, а мусор. Существующие
#: якоря сайта тоже ASCII (publications-heading, events-heading).
_TRANSLIT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e",
    "ж": "zh", "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m",
    "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
    "ф": "f", "х": "h", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "sch",
    "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
}


def anchor(heading: str, seen: "set[str] | None" = None) -> str:
    """АДРЕС ПОДТЕКСТА — выведен из заголовка, а не назначен рукой (entity-link.md::
    Inv-LINK-address-derived).

    Директива админа 2026-07-12: «кроме самого верхнего уровня чего бы то ни было все время
    имеем дело с ПодТекстами… адресация может происходить гибко: скажем, на абзацы конспекта,
    и на сам конспект». Верхний уровень (страница) адресуем URL-ом; каждый подтекст под ним
    был НЕАДРЕСУЕМ — рендерер отдавал голый <h2>, и связь могла указать лишь на документ.

    Живёт ЗДЕСЬ, в нижнем слое: этот рендерер (_md_static_to_html) — тот, что доезжает до
    мира (site_preview ∪ update_site → задеплоенные страницы). broadcast_html импортирует
    его отсюда: ОДНА деривация, два потребителя — иначе адрес разойдётся сам с собой.

    Спека класса «Конспект» (заголовок ≤ 3 слов) — то, что делает такой адрес коротким и
    осмысленным: два закона стола держат друг друга.

    Стабилен к правкам ВНЕ секции (не смещение, не порядковый номер). `seen` разводит
    совпадающие заголовки (-2, -3…): неуникальный адрес ведёт НЕ ТУДА."""
    s = _re.sub(r"[*_`\[\]]", "", heading).strip().lower()
    s = "".join(_TRANSLIT.get(ch, ch) for ch in s)
    s = _re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    if not s:                      # заголовок без единого адресуемого знака (эмодзи/пунктуация)
        s = "s"                    # ⊥ не подделываем: адрес вырожден и ВИДЕН как вырожденный
    if seen is None:
        return s
    base, n = s, 2
    while s in seen:
        s = f"{base}-{n}"
        n += 1
    seen.add(s)
    return s


def _h_punct(heading_html: str) -> str:
    """Заголовочный типограф static-страниц. Двухчастный, чистый, идемпотентный:
    (1) конечный знак → span.h-punct — caps-трекинг раздвигает зазор и перед
        знаком («Т Е З И С .»); CSS компенсирует тем же токеном (-tracking-caps);
    (2) дефис → неразрывный U+2011 — балансировка caps-заголовка не рвёт
        дефисное слово («ПОЧЕМУ ВСЁ- / ТАКИ ПАРИЖ», Σ 2026-07-11)."""
    heading_html = heading_html.replace("-", "\u2011")
    return _H_PUNCT_RE.sub(r'<span class="h-punct">\1</span>', heading_html)



def _wrap_lines(joined: str) -> str:
    """Авторские строки (\\n-joined, после _md_inline) → span.l блоки.

    span.l — типографский регистр «авторская строка» (CSS: висячий отступ
    её переносов — verse-overflow; см. styles.css + токен --verse-hang).
    Inline-пара, пересекающая границу строк, БАЛАНСИРУЕТСЯ на границе
    (закрыть/переоткрыть): block-span внутри inline-тега — невалидная вложенность.
    Грамматика закрыта — вход порождён нашим же _md_inline (em/strong/a, вложенность
    корректна) ⇒ стек-балансировка тотальна. Стек хранит ПОЛНЫЙ открывающий тег, а не
    имя: переоткрытие `<a>` без href потеряло бы адрес — ссылка на границе строк стала
    бы мёртвым якорем (Σ 2026-07-12: `a` вошёл в грамматику вместе с перелинковкой).
    """
    out, stack = [], []                       # [(имя, полный открывающий тег)]
    for part in joined.split("\n"):
        prefix = "".join(t for _n, t in stack)
        for m in _INLINE_TAG_RE.finditer(part):
            if m.group(0).startswith("</"):
                if stack:
                    stack.pop()
            else:
                stack.append((m.group(1), m.group(0)))
        suffix = "".join(f"</{n}>" for n, _t in reversed(stack))
        out.append(f'<span class="l">{prefix}{part}{suffix}</span>')
    # Dual-render (Inv-SITE-reader-ready): <br>+\n МЕЖДУ строками — носитель
    # авторской строки в РАЗМЕТКЕ (как _breath у лендинга). Styled-слой гасит
    # его (.l + br {display:none}) — блочность даёт span.l; reader-режимы
    # (CSS сорван, спаны инлайн) читают br и \n → строки живы, слова не
    # склеиваются (класс «приведётнас» жил бы в reader для ВСЕХ строк).
    return "<br>\n".join(out)


# ── ГРАММАТИКА статик-рендера — ОДНА ТАБЛИЦА, ДВЕ ПРОЕКЦИИ ──────────────────────────────
#
# Продукция = (имя, ЧТО ловим, ВО ЧТО превращаем). Из НЕЁ выводятся ОБА органа:
#     render(text) = ∘ [sub(p) : p ∈ GRAMMAR]          -- производитель
#     assert(html) = ∀ p ∈ GRAMMAR: ¬match(p, html)    -- СУДЬЯ, над той же таблицей
# Производитель и судья, делящие деривацию, НЕ МОГУТ разойтись. Новая продукция = ОДНА
# строка данных ⇒ и рендер, и закон получают её ДАРОМ.
#
# Σ 2026-07-12 (второй заход — и стоп-линия). Утром рендерер не знал ССЫЛОК и молча пропустил
# их насквозь: `[Ольга Розет](https://…)` уехало на публичную страницу БУКВАМИ. Я добавил
# ссылку — и написал закон, проверяющий ОДНУ продукцию, ссылку. Через несколько часов
# понадобилась КАРТИНКА, и оказалось:
#     ![Модулёр](img/x.svg)  →  !<a href="img/x.svg">Модулёр</a>
# картинка стала ССЫЛКОЙ с осиротевшим «!» — и мой закон этого НЕ ЛОВИТ (сырого markdown в
# выходе не осталось). Это ВТОРОЙ патч в одно место, и по правилу — стоп: недостаёт не
# продукции, а АБСТРАКЦИИ. Я кодировал перечень (strong, em, link) там, где факт — ГРАММАТИКА.
#
# Порядок ЗНАЧИМ и потому объявлен ДАННЫМИ, а не порядком вызовов: image ПЕРЕД link (иначе
# `![alt](src)` съедается ссылочной продукцией и «!» остаётся сиротой — ровно измеренный дефект);
# strong перед em (`**` — это две `*`).

def _img_repl(m: "_re.Match[str]") -> str:
    """INLINE-проекция картинки — голый <img> (валиден внутри <p>/<span class="l">).
    Строка, ЦЕЛИКОМ являющаяся картинкой, — это БЛОК, и её собирает блочный диспетч ниже:
    <figure> — блочный элемент, внутри <p> он невалиден. Уровни разведены, как в самой
    грамматике markdown (block-level ⊥ inline-level), а не слиты в одну подстановку."""
    alt, src = m.group(1), _u(m.group(2))
    return f'<img src="{src}" alt="{alt}" loading="lazy">' 


_MD_IMG_RE = _re.compile(r'!\[([^\]]*)\]\(([^)]+)\)')

#: name → (pattern, replacement).  ONE table; render and law are its two projections.
_GRAMMAR: "list[tuple[str, Any, Any]]" = [
    ("image",  _MD_IMG_RE,     _img_repl),                    # BEFORE link — `![…](…)` ⊃ `[…](…)`
    ("strong", _MD_STRONG_RE,  r"<strong>\1</strong>"),       # BEFORE em   — `**` ⊃ `*`
    ("em",     _MD_EM_RE,      r"<em>\1</em>"),
    ("link",   _MD_LINK_RE,    lambda m: f'<a href="{_u(m.group(2))}">{m.group(1)}</a>'),
]


def _md_inline(html_text: str) -> str:
    """Inline markdown → HTML — the RENDER projection of `_GRAMMAR`.

    Runs AFTER the per-line _typo + <br>-join, so a pair may span source lines (the konspekt has
    such pairs). `[^*]+` never crosses another asterisk → an unpaired asterisk stays literal
    (fail-open, never eats text).

    ЕДИНСТВЕННАЯ деривация inline-разметки в Системе (`_md_links`/`_MD_LINK_RE` — та же, что у
    лендинга): их было ДВЕ поверхности с одним фактом, и расхождение уехало на публичную
    страницу. Один факт — одна таблица."""
    for _name, pat, rep in _GRAMMAR:
        html_text = pat.sub(rep, html_text)
    return html_text


def _inline_svg(src: str) -> str:
    """Векторная фигура ВСТАВЛЯЕТСЯ В ДОКУМЕНТ, а не грузится через `<img src>`.

    `<img>`-SVG — ОТДЕЛЬНЫЙ, ИЗОЛИРОВАННЫЙ документ: он не видит ни токенов страницы, ни её
    РАНТАЙМ-темы (`[data-theme]` — по солнцу + тумблер + localStorage). Значит ЛЮБАЯ его
    самотемизация есть ДОГАДКА, и она расходится с поверхностью структурно, а не случайно:
      · `currentColor` в `<img>` резолвится в initial-чёрный ⇒ чёрное по чёрному (Σ 07-12);
      · `prefers-color-scheme` — сигнал ОС, а в CSS сайта его НОЛЬ ⇒ ночная страница при
        светлой ОС даёт снова чёрное по чёрному (Σ 07-13, админ: «не вижу иллюстрации»).
    Чинили КРАСКУ и переставляли отказ. Чинить надо НОСИТЕЛЬ.

    Inline ⇒ `currentColor` резолвится против вычисленного `color` страницы, который ведут
    `[data-theme]`-токены из Спеки (Inv-CSS-tokens-from-Spec) ⇒ ОДИН источник темы, и
    расхождение невозможно по построению. ФИГУРА ОБЪЯВЛЯЕТ ГЕОМЕТРИЮ, ПОВЕРХНОСТЬ — ЧЕРНИЛА.

    Owner-agnostic резолв (тот же обход, что и `_css_v`). Пусто ⇒ вызывающий падает на
    `<img>` (растр, чужой хост, файла нет) — это НЕ отказ, а иной носитель."""
    if not src.startswith("/") or not src.lower().endswith(".svg"):
        return ""                                   # не локальный вектор — обычный <img>
    rel = src.lstrip("/")
    here = Path(__file__).resolve()
    for parent in here.parents:
        people = parent / "knowledge" / "people"
        if not people.is_dir():
            continue
        for owner_dir in sorted(people.iterdir()):
            f = owner_dir / "site" / rel
            try:
                if not f.is_file():
                    continue
                text = f.read_text(encoding="utf-8")
            except OSError as e:                    # причина СОХРАНЯЕТСЯ: молчаливый ⊥ = ложь
                _logging.getLogger(__name__).warning("inline-svg: %s нечитаем: %r", f, e)
                continue
            i = text.find("<svg")
            return text[i:] if i >= 0 else ""       # без xml-декларации: она невалидна в HTML
        break
    return ""


def _md_static_to_html(md_body: str, line_mode: str = "verse") -> str:
    """Render a constrained markdown subset → HTML body fragment.

    line_mode — СЕМАНТИКА переноса строки, объявляемая ДОКУМЕНТОМ (frontmatter
    `line_mode:`), не угадываемая рендерером:
      verse (default) — перенос авторский: строка → span.l блок (висячий
              отступ её wrap'ов — CSS-регистр). Класс: построчно-правленные
              тексты (конспект; засвидетельствовано 2026-07-10).
      flow  — перенос редакторский: строки склеиваются пробелом (классический
              markdown). Класс: legal/manifesto-документы, набранные с
              wrap-ом по удобству. Закрывает квантификационную дыру
              line-fidelity (инвариант предполагал verse у ВСЕХ static-md).

    Pure function. No external markdown library — the subset is small and
    bounded by the legal-doc / manifesto / konspekt class. Inline HTML in
    source is passed through verbatim (admin-authored, single-SoT trusted;
    no L0 untrusted input flows here). HTML comments are stripped — they
    carry admin-fill placeholders meant for the source file, not visitors.

    Line-fidelity contract (Inv-SITE-line-fidelity): admin edits these
    files построчно (Релевантное Окно) — a newline inside a paragraph or a
    list item is an AUTHORED break and renders as <br>; the published page
    must show the exact line structure the admin approved. Blank line =
    paragraph boundary, как прежде. The 2026-07-10 konspekt render collapsed
    authored lines and split every multi-line bullet into <ul>+<p> fragments
    mid-sentence — this contract is the permanent constraint against both.
    """
    body = _re.sub(r"<!--.*?-->", "", md_body, flags=_re.DOTALL)

    out: list[str] = []
    paragraph: list[str] = []
    list_buf: list[list[str]] = []
    seen_ids: set[str] = set()      # адреса подтекстов ЭТОГО документа — уникальны в его пределах
    seen_h1 = False
    seen_section = False   # первый h2/h3 закрывает «шапку статьи»

    def _block(lines: list[str]) -> str:
        # _typo + amp-normal per source line (boundaries are authored, real);
        # emphasis резолвится над \n-joined текстом (пары через перенос), затем
        # verse: каждая авторская строка — span.l (эргономический регистр:
        # висячий отступ её переносов; <br> давал +45 рваных обрывков на
        # mobile_375); flow: строки — одно течение (пробел).
        joined = _md_inline("\n".join(_amp_normal(_typo(l)) for l in lines))
        return _wrap_lines(joined) if line_mode == "verse" else joined.replace("\n", " ")

    def _flush_paragraph() -> None:
        # «Шапка статьи» — структурное правило (не контентное): абзацы ПОСЛЕ
        # h1 ДО первого h2/h3 несут meta-регистр (дата, байлайн — CSS muted).
        if paragraph:
            cls = ' class="article-meta"' if (seen_h1 and not seen_section) else ""
            out.append(f"<p{cls}>{_block(paragraph)}</p>")
            paragraph.clear()

    def _flush_list() -> None:
        if list_buf:
            items_html = "".join(f"<li>{_block(li)}</li>" for li in list_buf)
            out.append(f"<ul>{items_html}</ul>")
            list_buf.clear()

    def _flush_all() -> None:
        _flush_paragraph()
        _flush_list()

    for raw_line in body.split("\n"):
        line = raw_line.rstrip()
        m_img = _MD_IMG_RE.fullmatch(line.strip())      # СТРОКА-КАРТИНКА = БЛОК (figure+caption)
        if m_img:
            _flush_all()
            alt, src = m_img.group(1), _u(m_img.group(2))
            cap = _md_inline(_amp_normal(_typo(alt))) if alt else ""
            # ВЕКТОР — В ДОКУМЕНТ (иначе тема — догадка изолированного документа); растр — <img>.
            svg = _inline_svg(m_img.group(2))
            if svg:
                # ОДНО ОПИСАНИЕ, ОДИН НОСИТЕЛЬ. У вставленной фигуры описание уже есть —
                # `aria-label` внутри неё. Дублировать его <figcaption>'ом значит держать
                # ОДИН факт в ДВУХ носителях, и подпись начинает жить своей жизнью: она
                # утверждала то, чего в источнике не было (Σ 2026-07-13, админ: «подписей
                # вокруг иллюстрации не запрашивал»). Растру figcaption оставлен: у <img>
                # своего описания в документе нет, alt его не отображает.
                out.append(f"<figure>{svg}</figure>")
            else:
                out.append(f'<figure><img src="{src}" alt="{alt}" loading="lazy">'
                           + (f"<figcaption>{cap}</figcaption>" if cap else "") + "</figure>")
            continue
        # КАЖДЫЙ заголовок несёт АДРЕС своего подтекста (Inv-LINK-address-derived). Голый
        # <hN> оставлял адресуемым только верхний уровень — единственный, который админ и
        # назвал исключением. Адрес выводится из ТЕКСТА заголовка (до типографики), поэтому
        # новая секция получает его сама, и `#slaydy-pyatnadtsat-protsentov` доезжает до мира.
        if line.startswith("### "):
            _flush_all()
            seen_section = True
            _raw = line[4:].strip()
            out.append(f'<h3 id="{anchor(_raw, seen_ids)}">'
                       f"{_h_punct(_md_inline(_amp_normal(_typo(_raw))))}</h3>")
            continue
        if line.startswith("## "):
            _flush_all()
            seen_section = True
            _raw = line[3:].strip()
            out.append(f'<h2 id="{anchor(_raw, seen_ids)}">'
                       f"{_h_punct(_md_inline(_amp_normal(_typo(_raw))))}</h2>")
            continue
        if line.startswith("# "):
            _flush_all()
            seen_h1 = True
            _raw = line[2:].strip()
            out.append(f'<h1 id="{anchor(_raw, seen_ids)}">'
                       f"{_h_punct(_md_inline(_amp_normal(_typo(_raw))))}</h1>")
            continue
        if line.lstrip().startswith("- "):
            _flush_paragraph()
            list_buf.append([line.lstrip()[2:].strip()])
            continue
        if not line.strip():
            _flush_all()
            continue
        if list_buf and raw_line[:1] in (" ", "\t"):
            # Hanging indent → continuation of the OPEN bullet, not a new <p>.
            list_buf[-1].append(line.strip())
            continue
        _flush_list()
        paragraph.append(line.strip())
    _flush_all()
    html = "\n".join(out)
    _assert_rendered(html)
    return html


def _assert_rendered(html: str) -> None:
    """Inv-SITE-no-raw-markdown — рендер НЕ ОТГРУЖАЕТ публике то, чего не понял.

    Σ 2026-07-12: рендерер не знал ссылок и молча пропустил их насквозь — `[Ольга Розет](…)`
    уехало в мир БУКВАМИ. Он не сломался и не пожаловался: ⊥ («не знаю такой разметки») было
    отдано как ∅ («ничего особенного») — тот же дефект кодомена, что у consult и dela_notes.
    Рендерер, не умеющий ОТКАЗАТЬ, делает свою потерю ненаблюдаемой — и публикует её.

    СУДЬЯ — ПРОЕКЦИЯ ТОЙ ЖЕ ГРАММАТИКИ, что и рендер: `∀ p ∈ _GRAMMAR: ¬match(p, html)`.
    Первый заход судил ОДНУ продукцию (ссылку) — и пропустил бы КАРТИНКУ, чей `![…](…)`
    ссылочная продукция съедает, оставляя сироту «!»: закон, перечисляющий продукции, есть
    ВТОРОЕ кодирование грамматики, и оно отстаёт от первого ровно на ту продукцию, которой ещё
    нет. Квантификация по таблице снимает класс: новая продукция судится ДАРОМ."""
    for name, pat, _rep in _GRAMMAR:
        m = pat.search(html)
        if m:
            raise ValueError(
                f"Inv-SITE-no-raw-markdown: неотрендеренная разметка ({name}) дошла до выхода — "
                f"{m.group(0)[:60]!r}. Рендер обязан ОТКАЗАТЬ, а не отгрузить её публике.")


def p_redirect(d: dict[str, Any], to: str, title: str = "") -> str:
    """Страница-редирект — ДЕРИВАТ, а не рукописный HTML (Σ 2026-07-12).

    Переименование сущности (konspekt → conspectus) меняет её адрес, а старый УЖЕ опубликован —
    и без редиректа ссылка, которую кто-то сохранил или переслал, становится 404. Редирект есть
    носитель ОТСТАВКИ адреса: у создания носитель есть (страница), у отставки — только отсутствие,
    и отсутствие неотличимо от «никогда не было». Редирект даёт отставке носитель.

    ДЕРИВАТ, потому что редирект — ТОЖЕ СТРАНИЦА САЙТА, и публикационный гейт судит её теми же
    Спеками (day-night контракт: storage_key · data-theme · resolver). Рукописный HTML их не несёт
    и справедливо отказан; воспроизводить их рукой значило бы кодировать контракт ВТОРОЙ раз.
    Наследуем `_theme_script` — единственную деривацию контракта."""
    return f"""<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<title>{_t(title or "Переехало")}</title>
<link rel="canonical" href="{_u(to)}">
<meta name="robots" content="noindex, follow">
<meta http-equiv="refresh" content="0; url={_u(to)}">
{_theme_script(d)}
<script>location.replace("{to}" + location.hash);</script>
</head>
<body><p>Страница переехала: <a href="{_u(to)}">{_t(to)}</a></p></body>
</html>
"""


def _meta_trim(s: str, limit: int = 160) -> str:
    """Word-boundary meta-description truncation (Inv-SITE-meta-word-boundary).

    A blind `[:160]` cut the konspekt description mid-phrase («…зачем ехать,
    если» — 2026-07-10). ≤limit passes through untouched; longer text cuts at
    the last space before the limit, drops dangling punctuation, adds «…».
    """
    s = " ".join(s.split())
    if len(s) <= limit:
        return s
    cut = s[:limit - 1]
    if " " in cut:
        cut = cut.rsplit(" ", 1)[0]
    return cut.rstrip(" ,;:·—–-") + "…"


def _meta_join(parts: list[str] | tuple[str, ...], lang: str = "ru") -> str:
    """Join versified/line-structured parts into ONE meta-description string
    (Inv-SITE-meta-word-boundary, join leg).

    A bare space-join glues beats into false syntax («…вариантов модернизма
    4 дня на стыке…» — 2026-07-10 landing meta). Rule, total over any parts:
    inner whitespace (incl. authored newlines) collapses to single spaces;
    a part ending WITHOUT a terminal mark is separated from the next by the
    beat-separator; otherwise by a space. Terminal-mark set and separator —
    DATA (knowledge/system/typography/<lang>.yaml `meta_join`, same SoT and
    loader as every typography rule); built-ins are the fail-open fallback.
    """
    mj = (_load_typo_rules(lang).get("meta_join") or {})
    terminal = str(mj.get("terminal") or ".!?…:;»")
    separator = str(mj.get("separator") or " — ")
    cleaned = [" ".join(str(p).split()) for p in parts]
    cleaned = [p for p in cleaned if p]
    out: list[str] = []
    for i, p in enumerate(cleaned):
        out.append(p)
        if i < len(cleaned) - 1:
            out.append(" " if p[-1] in terminal else separator)
    return "".join(out)


def p_sitemap(base_url: str, paths: "list[str] | tuple[str, ...]") -> str:
    """Project the deployed page-set → sitemap.xml (Inv-SITE-sitemap-derived).

    Pure projection of the SAME page graph the deploy emits — never a
    hand-enumerated list (the 2026-07-10 landing sitemap listed only «/»
    while /2026-stream-konspekt/ was live; owner-site had no sitemap at
    all). Paths are normalized to exactly one leading slash; duplicates
    collapse preserving first-seen order; root always present and first.
    """
    base = base_url.rstrip("/")
    seen: dict[str, None] = {"/": None}
    for p in paths:
        norm = "/" + str(p).strip("/")
        if norm != "/":
            norm += "/"
        seen.setdefault(norm, None)
    urls = "\n".join(f"  <url><loc>{base}{p if p != '/' else '/'}</loc></url>"
                     for p in seen)
    return ('<?xml version="1.0" encoding="UTF-8"?>\n'
            '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
            f"{urls}\n</urlset>\n")


def parse_static_md(text: str) -> tuple[dict[str, Any], str]:
    """Делегация в дом файловых сущностей (broadcast_relation.parse_front_matter)
    — одна реализация frontmatter-парса на Систему."""
    import broadcast_relation as _br
    return _br.parse_front_matter(text)


def p_static_page(d: dict[str, Any], md_text: str, slug: str = "") -> str:
    """Project (D, static.md) → standalone HTML page.

    Pure projection. Front-matter `title` drives <title>/<h1>; `description`
    drives meta-description. Body rendered via `_md_static_to_html`. Layout
    inherits the owner's footer.legal + cookie banner + skip-link surface
    — single SoT for trust-base across every page (Inv-SITE-trust-base).

    `slug` comes from the caller (discover_static_pages stem / URL route);
    front-matter `slug` overrides. It feeds the canonical URL AND the
    dela:slug meta (pageview pingback — without it the page is invisible to
    entity-statistics, konspekt 2026-07-10 gap). Front-matter `canonical`
    overrides the derived one for pages whose primary home is another
    Web-Broadcasting host (konspekt: canonical → parisinseptember.ru while
    mirrored on olgarozet.ru — mirror must not self-canonicalize).
    """
    fm, body_md = parse_static_md(md_text)
    title = fm.get("title") or ""
    description = fm.get("description") or title
    slug = fm.get("slug") or slug
    body_html = _md_static_to_html(
        body_md, line_mode=str(fm.get("line_mode") or "verse"))
    # footer.legal block — Inv-SITE-trust-base. Same projection used by
    # p_event_landing (line ~2055) so the legal colophon is byte-equivalent
    # across every surface (event landing, owner site, static page).
    # Юр-подвал на static-страницах — OPT-IN (frontmatter legal_footer: true).
    # Admin 2026-07-11: «Подвал там не уместен» — реквизиты/оплата уместны на
    # коммерческих поверхностях (лендинг), не на текстовых (конспект/manifesto);
    # сама политика-страница тем более не ссылается на себя.
    legal_html = _legal_footer(d) if fm.get("legal_footer") is True else ""
    article = (f'  <article class="article-wrapper">{body_html}'
               f'{legal_html}</article>')
    base_canon = _canonical(d)
    canonical = fm.get("canonical") or (
        f"{base_canon}/{slug}/" if base_canon and slug else "")
    # Inv-SITE-reader-ready: frontmatter author → meta[name=author] — стандартный
    # byline-крюк Readability-семейства (Safari/Firefox/Chrome reader).
    author_meta = (f'<meta name="author" content="{_t(fm.get("author"))}">\n'
                   if fm.get("author") else "")
    return _layout(
        d,
        title=(title or "Страница"),
        description=_meta_trim(description),
        body=article,
        nav=True,
        canonical=canonical or None,
        # Static pages are owner-level (legal/manifesto). Owner-portrait
        # footer suppressed to mirror event-landing convention — legal-footer
        # in _layout still emits for trust-base discoverability.
        footer=False,
        surface="editorial",
        slug=slug,
        extra_head=author_meta,
    )


def broadcast_assignments(d: dict[str, Any], site_dir: "str | Path") -> "list[dict[str, Any]]":
    """Делегация в ДОМ отношения — broadcast_relation.assignments (анти-shadow:
    генератор — потребитель отношения, не владелец). Сигнатура сохранена."""
    import broadcast_relation as _br
    return _br.assignments(d, site_dir)


def surface_matches(surfaces: "set[str]", leg: str, fqdn: str = "") -> bool:
    import broadcast_relation as _br
    return _br.surface_matches(surfaces, leg, fqdn)


def discover_static_pages(site_dir: "str | Path") -> "list[tuple[str, Path]]":
    """Делегация в ДОМ отношения (broadcast_relation) — единственная
    реализация derived-само-деклараций; здесь только совместимое имя."""
    import broadcast_relation as _br
    return _br.discover_static_pages(site_dir)

def p_art(d: dict[str, Any]) -> str:
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


# ── P_telegram: D → channel post text (Skoro digest) ─────────────────

def _telegram_channel_url(d: dict[str, Any]) -> str | None:
    """Resolve TG channel URL для anchor link insertion.

    Source priority:
      1. data.yaml::urls.telegram         (canonical, e.g. https://t.me/olga_rozet)
      2. data.yaml::urls.telegram_handle  (e.g. @olgaroset → derive https://t.me/olgaroset)
      3. None (no link insertion possible)
    """
    urls = d.get("urls") or {}
    url = urls.get("telegram")
    if url:
        stripped: str = url.rstrip("/")
        return stripped
    handle = urls.get("telegram_handle")
    if handle:
        h = handle.lstrip("@")
        return f"https://t.me/{h}"
    return None


# ── π_anchor : Event × Publications → (Channel → Maybe URLLocator) ────
#
# Genius Simplification 2026-05-15: stored event.anchors field eliminated as
# duplicative SoT. Single source = publications[].
#
# NO-HARDCODE 2026-05-15: per-Channel URL-locator extraction rules loaded from
# channel.md::enforcement_data.url_locator_extraction (Spec-driven). Adding new
# Channel = YAML edit, no code change. Canonical anchor key = channel-id itself
# (mapping для compatibility resolved at usage site).

import re as _re_anchor


def _load_anchor_extractors() -> dict[str, Any]:
    """Load per-Channel URL-locator extraction rules from channel.md Spec.

    Returns: channel-id → {'source', 'transform', 'regex'?} dict.
    """
    fm = _spec_fm("channel")
    rules: dict[str, Any] = fm.get("enforcement_data", {}).get("url_locator_extraction", {})
    return rules


_ANCHOR_RULES = _load_anchor_extractors()


def _anchor_key_for(channel: str) -> str:
    """Resolve anchor namespace key for Channel — Spec-loaded (channel.md::url_locator_extraction.<ch>.anchor_key).

    Lift 2026-05-15: was inline _ANCHOR_KEY_ALIASES table; now Spec-driven uniform с
    extraction rules. Fallback to channel-id itself когда anchor_key absent.
    """
    key: str = (_ANCHOR_RULES.get(channel) or {}).get("anchor_key", channel)
    return key


def _apply_extractor(rule: dict[str, Any], external_url: str | None, platform_id: Any) -> Any:
    """Apply Spec-defined extractor rule. Returns extracted locator OR None."""
    source = rule.get("source", "")
    transform = rule.get("transform", "passthrough")
    raw = external_url if source == "external_url" else platform_id
    if raw is None:
        return None
    if transform == "passthrough":
        return raw
    if transform == "int_or_none":
        try:
            s = str(raw).lstrip("-")
            return int(raw) if s.isdigit() else None
        except (ValueError, TypeError):
            return None
    if transform == "regex_extract":
        pattern = rule.get("regex", "")
        if not pattern:
            return None
        m = _re_anchor.search(pattern, raw or "")
        return m.group(1) if m else None
    return None


def event_anchors(d: dict[str, Any], event_id: str, only_live: bool = True) -> dict[str, Any]:
    """π_anchor functor: derive anchor URL-locators per Channel from publications[].

    Replaces stored event.anchors as single SoT = publications. For each Channel σ
    where event has Main Post: returns canonical anchor key + extracted locator.

    Bidirectional consistency check (Inv-EV-anchor-coherence): for any stored
    event.anchors, derived value MUST match (admin can override via stored field
    if publication entry lags, but mismatch = warning).

    only_live: if True, only publications с status=live count; else include planned.
    Default True (admin'ское «оформившееся событие имеет Главный Пост» — Main Post
    must be live к момент anchor-resolution).
    """
    anchors: dict[str, Any] = {}
    for p in d.get("publications", []):
        if p.get("kind") != "main_post":
            continue
        if p.get("target_event") != event_id:
            continue
        if only_live and p.get("status") != "live":
            continue
        channel = p.get("channel")
        rule = _ANCHOR_RULES.get(channel)
        if not rule:
            continue
        canonical_key = _anchor_key_for(channel)
        locator = _apply_extractor(rule, p.get("external_url"), p.get("platform_id"))
        if locator is not None:
            anchors[canonical_key] = locator
    # Augment с landing URL when explicit publication absent but event.web_addresses present.
    # admin'ская модель: «Кампания основную информацию вещает через Посадочную» —
    # landing is canonical и rarely needs separate Publication entry.
    if "landing" not in anchors:
        ev = next((e for e in d.get("events", []) if e.get("id") == event_id), None)
        if ev:
            web_addrs = ev.get("web_addresses") or []
            if web_addrs:
                addr = web_addrs[0]
                anchors["landing"] = addr if addr.startswith("http") else f"https://{addr}"
    return anchors


def p_telegram(d: dict[str, Any]) -> str:
    """Telegram channel Skoro digest — delegates к skoro.render (monoidal functor).

    Realises admin'ская модель «Скоро коротко сообщает о Событиях и отсылает к
    соответствующим Основным Постам». Refactored 2026-05-15 (Genius Simplification
    C): copy-paste imperative loop collapsed в declarative `Render_σ : List E → Output_σ`
    via skoro.SurfaceSkoroSpec + SKORO_TG_SPEC. Footer (consultations) composed
    AFTER digest body.
    """
    from skoro import render as render_skoro_digest_dispatch
    body = render_skoro_digest_dispatch(d, "telegram_channel")
    # Construct parts list для consultations footer (preserving original divider semantics)
    parts = body.split("\n") if body else ["СКОРО:"]
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
        parts.append(f"{host}{cons.get('link', '/init')}")
    return "\n".join(parts)


# ── P_bio: D → short bio ─────────────────────────────────────────────

def p_bio(d: dict[str, Any]) -> str:
    bio = d["bio"]
    lines = [bio["artist"]["text"]]
    lines.extend(f"{r};" for r in bio.get("roles", []))
    lines.extend(f"{s}." for s in bio.get("skills", []))
    lines.append(bio["inspire"].strip().splitlines()[0])
    lines.append(d["urls"].get("telegram_handle", "@olgaroset"))
    return "\n".join(lines)


# ── P_booking: (D, slots.json) → booking/index.html ──────────────────

def p_booking(d: dict[str, Any]) -> str:
    """Booking page. Uses _layout for head/footer; booking-specific CSS via extra_head.

    transport_url SoT (priority order, fail-loud if absent — no hardcode fallback):
      1. data.yaml.booking.transport_url
      2. <ROOT>/booking.json::transport_url   (legacy slots-bundle path)
      3. <ROOT>/engage.json::transport_url    (engage_transport.push_site path)
    Slots source: same files (booking.json or engage.json), `slots` key. Empty list OK.
    """
    import json as _json
    bio = d["bio"]
    cons = d["consultations"]
    # Slots-bundle file (engage_transport writes engage.json; legacy: booking.json).
    slots_data: dict[str, Any] = {"slots": [], "user": ""}
    for cand in (ROOT / "booking.json", ROOT / "engage.json"):
        if cand.exists():
            slots_data = _json.loads(cand.read_text())
            break
    slots_list = slots_data.get("slots", [])
    slots_json = _json.dumps(slots_list, ensure_ascii=False)
    desc_plain = cons["description"].strip().replace("\n", " ").replace("  ", " ")
    contact_email = cons.get("calendar_id", "o.g.rozet@gmail.com")
    no_slots = not slots_list
    # transport_url resolution required only когда we actually render the JS-driven
    # booking form (i.e., slots present). Empty-state placeholder doesn't need it.
    # Resolve through the ONE capability resolver (Inv-CRED-git-plain): data.yaml /
    # slots carry transport_url_ref (a secrets key name), never the inline capability;
    # the resolved URL is embedded into the PUBLISHED (public-by-design) booking form.
    import engage as _engage
    transport_url = (
        _engage.resolve_transport_url(d.get("booking"))
        or _engage.resolve_transport_url(slots_data)
    )
    if not no_slots and not transport_url:
        owner = (d.get("bio") or {}).get("canonical") or (d.get("bio") or {}).get("title") or "<unknown>"
        raise RuntimeError(
            f"booking transport_url required (data.yaml.booking.transport_url "
            f"or booking.json/engage.json::transport_url) for owner {owner!r}"
        )

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
.booking-empty{max-width:520px;margin:3rem auto 4rem;padding:clamp(2rem,1.5rem + 1.5vw,3.5rem) clamp(1.5rem,1rem + 1vw,2.5rem);text-align:center;border:1px solid var(--rule);border-radius:.5rem;background:var(--surface)}
.empty-eyebrow{text-transform:uppercase;letter-spacing:var(--tracking-caps);font-size:clamp(.95rem,.85rem + .4vw,1.15rem);font-weight:500;margin:0 0 1.6em;color:var(--ink)}
.empty-eyebrow .rule{display:block;width:2.5rem;height:1px;background:var(--rule);margin:1.2em auto 0}
.empty-hint{color:var(--muted);font-size:.95rem;margin:0 0 .8em;line-height:1.5}
.empty-contact{margin:0;font-size:.95rem;line-height:1.7}
.empty-contact a{color:var(--ink);border-bottom:1px solid var(--rule);text-decoration:none;padding-bottom:.05em;transition:border-color .15s}
.empty-contact a:hover{border-bottom-color:var(--ink)}
.empty-divider{color:var(--muted);margin:0 .5rem}
@media (prefers-reduced-motion:reduce){.bk-form{transition:none}.t{transition:none}.bk-input{transition:none}.bk-btn{transition:none}.bk-input.err{animation:none}.empty-contact a{transition:none}}
</style>"""

    # No-slots placeholder: substrate-chain returned no future free slots (per
    # provider.md::Inv-PROV-substrate-diversity). Server-rendered высококачественная
    # табличка, theme-agnostic via design tokens, NO JS-driven UI surfaces (no form,
    # no slot grid, no transport_url call — clean placeholder). Admin 2026-05-15:
    # «до реабилитации связи Бронирования с Календарем сгенерируй конгруэтную табличку».
    if no_slots:
        body = f"""<div class="booking" role="main">
<h2>Консультация</h2>
<p class="sub">{cons.get('duration_min', 40)} мин · {cons['price']} · онлайн</p>

<aside class="booking-empty" role="status" aria-live="polite">
  <p class="empty-eyebrow">пока времён нет<span class="rule" aria-hidden="true"></span></p>
  <p class="empty-hint">Напишите Ольге напрямую —<br>предложу время:</p>
  <p class="empty-contact">
    <a href="https://t.me/olgaroset" rel="noopener">@olgaroset</a>
    <span class="empty-divider" aria-hidden="true">·</span>
    <a href="mailto:{contact_email}">{contact_email}</a>
  </p>
</aside>

<p class="back"><a href="/">← назад</a></p>
</div>"""
        booking_label = bio.get("booking_page_label", "Записаться")
        return _layout(
            d,
            title=f"{booking_label} — {bio['title']}",
            description=f"{desc_plain} — {cons['price']}",
            body=body,
            canonical=f"{_canonical(d)}/{cons['link'].strip('/')}/",
            extra_head=booking_style,
            footer=False,
        )

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

    booking_label = bio.get("booking_page_label", "Записаться")
    return _layout(
        d,
        title=f"{booking_label} — {bio['title']}",
        description=f"{desc_plain} — {cons['price']}",
        body=body,
        canonical=f"{_canonical(d)}/{cons['link'].strip('/')}/",
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
    # Public booking path is DATA-DRIVEN from consultations.link — ONE source (admin «одна
    # ссылка — init», 2026-06-24): the page dir, the homepage CTA href (already cons['link'])
    # AND the canonical all follow it, so the URL is a data.yaml edit with zero code.
    booking_slug = (d["consultations"].get("link") or "/init").strip("/") or "init"
    booking_dir = ROOT / booking_slug
    if _booking_disabled(d):
        # admin 2026-05-15: «Никакой ссылки на Бронирование, пока не восстановим».
        # Remove the page entirely so an orphan can't be linked. Computed predicate.
        if booking_dir.is_dir():
            import shutil as _sh
            _sh.rmtree(booking_dir)
        print("booking: omitted (booking_disabled)")
    else:
        # mkdir ⇒ projection TOTAL over enable→disable→enable: the disabled branch rmtree's the
        # dir, so a re-enable (slots restored) hit FileNotFoundError — the «booking never returns»
        # root (Σ 2026-06-24, olgarozet.ru/booking 404 with 26 live slots).
        booking_dir.mkdir(parents=True, exist_ok=True)
        (booking_dir / "index.html").write_text(p_booking(d), encoding="utf-8")
        print(f"booking: {booking_slug}/index.html")
        # ОТСТАВКА АДРЕСА — АТРИБУТ ПЕРЕЕХАВШЕГО, а не новая вещь (тот же закон, что у статей:
        # `redirect_from` во frontmatter). Переименование booking → init оставило в мире
        # ОСИРОТЕВШУЮ /booking/, чей канон вёл в /init/, которого не было: у создания адреса
        # носитель есть (страница), у отставки — только отсутствие, а отсутствие неотличимо от
        # «никогда не было». Редирект даёт отставке НОСИТЕЛЬ (p_redirect — дериват, он и был
        # написан ровно для этого, но не имел ни одного вызывающего у страницы записи).
        for _old in (d["consultations"].get("redirect_from") or []):
            _old = str(_old).strip("/")
            if not _old or _old == booking_slug:
                continue
            _old_dir = ROOT / _old
            _old_dir.mkdir(parents=True, exist_ok=True)
            (_old_dir / "index.html").write_text(
                p_redirect(d, f"/{booking_slug}/", d["bio"].get("booking_page_label", "")),
                encoding="utf-8")
            print(f"booking: {_old}/ → /{booking_slug}/ (отставка адреса)")
    (ROOT / "telegram.txt").write_text(p_telegram(d), encoding="utf-8")
    print("telegram: telegram.txt")
    (ROOT / "bio.txt").write_text(p_bio(d), encoding="utf-8")
    print("bio: bio.txt")
