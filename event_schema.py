"""event_schema.py — single typed shape for an Event entity in data.yaml.

ONE schema → graph-clean rendering across:
  • site_generator.p_event_landing  (HTML projection)
  • broadcast_html.update_landing   (per-fqdn deploy)
  • site_preview                    (live render)
  • schema.org JSON-LD              (SEO markup)

Anti-pattern eliminated: dozens of `.get(…) or {}` chains scattered across
render code, each silently degrading on missing fields. Replaced with a
single `validate(ev) → EventModel | InvalidEvent` call at render entry.

Implementation: pydantic-v2 if available; else dataclass + custom validate.
Both produce the same `EventModel` shape — call sites are pydantic-agnostic.
Every render path imports `validate(ev)` and uses the validated model;
fail-fast with a clear error rather than render a half-built page.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


class InvalidEvent(ValueError):
    """Raised by validate() when an event dict cannot be projected.

    Carries event id (best-effort) and a human-readable reason that
    site_preview surfaces as an HTTP 500 body.
    """

    def __init__(self, event_id: str, reason: str):
        self.event_id = event_id
        self.reason = reason
        super().__init__(f"event {event_id!r}: {reason}")


# ── Section variants ─────────────────────────────────────────────────

@dataclass
class SectionPair:
    label: str
    text: str


@dataclass
class Section:
    title: str
    # Inv-SEMANTIC-WHITESPACE: intro/text accept str OR list[str]. List preserves
    # admin's `\n\n` paragraph breaks (md source) — renderer iterates and emits
    # one <p> per element. Single string still works (back-compat).
    intro: "str | list[str]" = ""
    text: "str | list[str]" = ""
    pairs: list[SectionPair] = field(default_factory=list)
    items: list[str] = field(default_factory=list)


@dataclass
class OpenQuestion:
    to: list[str]   # always normalized to list (single-string `to` lifted)
    q: str


@dataclass
class Signup:
    title: str = "Записаться"
    note: str = ""


@dataclass
class AboutOrganizer:
    # Inv-SEMANTIC-WHITESPACE: text accepts str OR list[str] (paragraphs).
    text: "str | list[str]" = ""
    link_text: str = ""
    link_url: str = ""


@dataclass
class Contact:
    """Direct-contact block, rendered after signup. Lapidary, public-side."""
    prompt: str = ""        # «Остался вопрос?»
    text: str = ""          # «Напишите Ольге — ответит лично.»
    email: str = ""         # mailto: target


# ── Event model ──────────────────────────────────────────────────────

@dataclass
class EventModel:
    """Validated event entity.

    Required: id, broadcast (list[str], may be empty for graph-only events).
    Required for landing render: lead, sections (non-empty).
    Anything beyond shape goes through as `extra` for back-compat.
    """
    id: str
    broadcast: list[str]
    title: str = ""
    date: str = ""
    t_key: str = ""
    # Inv-SEMANTIC-WHITESPACE: str OR list[str] of paragraphs.
    lead: "str | list[str]" = ""
    web_addresses: list[str] = field(default_factory=list)
    co_organizers: list[str] = field(default_factory=list)
    organizers: list[str] = field(default_factory=list)
    locations: list[str] = field(default_factory=list)
    audience: list[str] = field(default_factory=list)
    format: list[str] = field(default_factory=list)
    status: str = "PLANNING"
    sections: list[Section] = field(default_factory=list)
    open_questions: list[OpenQuestion] = field(default_factory=list)
    # `internal_questions` carry organizer-facing gaps (admin/Lumen surface);
    # explicitly NOT rendered on public traveler-facing landing
    # (Inv-CONTENT-AUDIENCE: surface tracks audience, not data presence).
    internal_questions: list[OpenQuestion] = field(default_factory=list)
    signup: Signup | None = None
    contact: "Contact | None" = None
    about_organizer: AboutOrganizer | None = None
    pricing: dict = field(default_factory=dict)
    cohort: dict | None = None
    duration: str = ""
    concept: str = ""
    days: list[dict] = field(default_factory=list)
    extra: dict = field(default_factory=dict)

    @property
    def renders_landing(self) -> bool:
        """True iff the event has the minimum shape to render an essay landing.

        Exactly one decision point — referenced by site_generator and
        broadcast_html so the schema, not the renderer, gates the surface.
        """
        return bool(self.lead and self.sections)


def _norm_str(v: Any) -> str:
    return str(v).strip() if v is not None else ""


def _norm_list_str(v: Any) -> list[str]:
    if v is None:
        return []
    if isinstance(v, str):
        return [v]
    return [str(x) for x in v if x is not None]


def _norm_paras(v: Any) -> "str | list[str]":
    """Normalize a prose field that may carry paragraph structure.

    Inv-SEMANTIC-WHITESPACE: list[str] preserves admin's blank-line breaks;
    single str passes through as-is. None → "". Empty list → "". Single-item
    list collapses to its element (avoids spurious `[x]` wrapping).
    """
    if v is None:
        return ""
    if isinstance(v, list):
        paras = [str(x).strip() for x in v if x is not None and str(x).strip()]
        if not paras:
            return ""
        return paras[0] if len(paras) == 1 else paras
    return str(v).strip()


def _validate_section(raw: Any, ev_id: str, idx: int) -> Section:
    if not isinstance(raw, dict):
        raise InvalidEvent(ev_id, f"sections[{idx}] must be a mapping, got {type(raw).__name__}")
    title = _norm_str(raw.get("title"))
    if not title:
        raise InvalidEvent(ev_id, f"sections[{idx}] missing title")
    sec = Section(title=title)
    sec.intro = _norm_paras(raw.get("intro"))
    sec.text = _norm_paras(raw.get("text"))
    items = raw.get("items") or []
    if items and not isinstance(items, list):
        raise InvalidEvent(ev_id, f"sections[{idx}].items must be a list")
    sec.items = [_norm_str(x) for x in items]
    pairs_raw = raw.get("pairs") or []
    if pairs_raw and not isinstance(pairs_raw, list):
        raise InvalidEvent(ev_id, f"sections[{idx}].pairs must be a list")
    for j, p in enumerate(pairs_raw):
        if not isinstance(p, dict):
            raise InvalidEvent(ev_id, f"sections[{idx}].pairs[{j}] must be a mapping")
        label = _norm_str(p.get("label"))
        text = _norm_str(p.get("text"))
        if not (label and text):
            raise InvalidEvent(ev_id, f"sections[{idx}].pairs[{j}] needs label+text")
        sec.pairs.append(SectionPair(label=label, text=text))
    # at least one of {text, items, pairs} or intro must be present (else section is empty)
    if not (sec.intro or sec.text or sec.items or sec.pairs):
        raise InvalidEvent(ev_id, f"sections[{idx}] {title!r} has no content "
                                  "(intro/text/items/pairs all empty)")
    return sec


def _validate_oq(raw: Any, ev_id: str, idx: int) -> OpenQuestion:
    if not isinstance(raw, dict):
        raise InvalidEvent(ev_id, f"open_questions[{idx}] must be a mapping")
    to = raw.get("to")
    if to is None:
        raise InvalidEvent(ev_id, f"open_questions[{idx}].to required")
    to_list = _norm_list_str(to)
    q = _norm_str(raw.get("q"))
    if not q:
        raise InvalidEvent(ev_id, f"open_questions[{idx}].q empty")
    return OpenQuestion(to=to_list, q=q)


def validate(ev: dict) -> EventModel:
    """Coerce a raw event dict (from data.yaml) into a typed EventModel.

    Fail-fast: raises InvalidEvent on shape problems with a clear `reason`.
    Soft fields (cohort/pricing/days/etc) pass through unchanged for back-compat
    with renderers that have not yet been migrated.
    """
    if not isinstance(ev, dict):
        raise InvalidEvent("?", f"event must be a mapping, got {type(ev).__name__}")
    ev_id = _norm_str(ev.get("id"))
    if not ev_id:
        raise InvalidEvent("?", "missing id")

    broadcast = ev.get("broadcast") or []
    if not isinstance(broadcast, list):
        raise InvalidEvent(ev_id, "broadcast must be a list")
    broadcast = [_norm_str(x) for x in broadcast if _norm_str(x)]

    m = EventModel(id=ev_id, broadcast=broadcast)
    m.title = _norm_str(ev.get("title"))
    m.date = _norm_str(ev.get("date"))
    m.t_key = _norm_str(ev.get("t_key"))
    m.lead = _norm_paras(ev.get("lead"))
    m.web_addresses = _norm_list_str(ev.get("web_addresses"))
    m.co_organizers = _norm_list_str(ev.get("co_organizers"))
    m.organizers = _norm_list_str(ev.get("organizers"))
    m.locations = _norm_list_str(ev.get("locations"))
    m.audience = _norm_list_str(ev.get("audience"))
    m.format = _norm_list_str(ev.get("format"))
    m.status = _norm_str(ev.get("status")) or "PLANNING"
    m.duration = _norm_str(ev.get("duration"))
    m.concept = _norm_str(ev.get("concept"))

    sections_raw = ev.get("sections") or []
    if sections_raw and not isinstance(sections_raw, list):
        raise InvalidEvent(ev_id, "sections must be a list")
    m.sections = [_validate_section(s, ev_id, i) for i, s in enumerate(sections_raw)]

    oq_raw = ev.get("open_questions") or []
    if oq_raw and not isinstance(oq_raw, list):
        raise InvalidEvent(ev_id, "open_questions must be a list")
    m.open_questions = [_validate_oq(q, ev_id, i) for i, q in enumerate(oq_raw)]

    iq_raw = ev.get("internal_questions") or []
    if iq_raw and not isinstance(iq_raw, list):
        raise InvalidEvent(ev_id, "internal_questions must be a list")
    m.internal_questions = [_validate_oq(q, ev_id, i) for i, q in enumerate(iq_raw)]

    contact = ev.get("contact")
    if isinstance(contact, dict):
        m.contact = Contact(
            prompt=_norm_str(contact.get("prompt")),
            text=_norm_str(contact.get("text")),
            email=_norm_str(contact.get("email")),
        )
    elif contact not in (None, False):
        raise InvalidEvent(ev_id, "contact must be a mapping or null")

    signup = ev.get("signup")
    if isinstance(signup, dict):
        m.signup = Signup(
            title=_norm_str(signup.get("title")) or "Записаться",
            note=_norm_str(signup.get("note")),
        )
    elif signup not in (None, False):
        raise InvalidEvent(ev_id, f"signup must be a mapping or null, got {type(signup).__name__}")

    about = ev.get("about_organizer")
    if isinstance(about, dict):
        m.about_organizer = AboutOrganizer(
            text=_norm_paras(about.get("text")),
            link_text=_norm_str(about.get("link_text")),
            link_url=_norm_str(about.get("link_url")),
        )
    elif about not in (None, False):
        raise InvalidEvent(ev_id, f"about_organizer must be a mapping or null")

    pricing = ev.get("pricing")
    if pricing is None or pricing is False:
        m.pricing = {}
    elif isinstance(pricing, dict):
        m.pricing = pricing
    else:
        raise InvalidEvent(ev_id, "pricing must be a mapping or null")

    cohort = ev.get("cohort")
    if cohort is None or cohort is False:
        m.cohort = None
    elif isinstance(cohort, dict):
        m.cohort = cohort
    else:
        raise InvalidEvent(ev_id, "cohort must be a mapping or null")

    days = ev.get("days") or []
    if days and not isinstance(days, list):
        raise InvalidEvent(ev_id, "days must be a list")
    m.days = list(days)

    # Landing-render gate: if broadcast surface includes 'site' or web_addresses
    # is non-empty, the event will be rendered as a landing — must have lead+sections.
    will_render_landing = ("site" in broadcast) or bool(m.web_addresses)
    if will_render_landing and not m.renders_landing:
        raise InvalidEvent(
            ev_id,
            "broadcasts to site / has web_addresses but lacks lead+sections "
            "(modern landing schema). Add `lead:` (single sentence, frame-setter) "
            "and at least one entry in `sections:`.",
        )

    return m


__all__ = ["EventModel", "Section", "SectionPair", "OpenQuestion", "Signup",
           "Contact", "AboutOrganizer", "InvalidEvent", "validate"]
