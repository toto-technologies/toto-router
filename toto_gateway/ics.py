"""Minimal iCalendar (RFC 5545) VEVENT read/write — the calendar kind's ICS rung (P1).

We need exactly four fields off each event (SUMMARY / DTSTART / DTEND / UID), so this hand-parses
folded `KEY;PARAMS:VALUE` lines instead of taking a dependency.

# ponytail: hand VEVENT parse — add the `icalendar` dep if we ever need RRULE (recurrence),
# VTIMEZONE-correct conversion, attendees, or alarms. Today: single events, floating local time
# (any Z/offset is dropped — the same floating-local stance the frontend's parseLocal takes).

Times are emitted as the ISO strings the rest of the calendar kind speaks: an all-day DTSTART
(VALUE=DATE or a bare 8-digit date) becomes "YYYY-MM-DD" with all_day=True; a datetime becomes
"YYYY-MM-DDTHH:MM:SS". Parsing never raises on a malformed line — a bad VEVENT is skipped, not fatal
(a subscription feed is untrusted input).
"""

from __future__ import annotations


def _unfold(text: str) -> list[str]:
    """RFC 5545 line unfolding: a line beginning with a space or tab continues the previous one."""
    out: list[str] = []
    for raw in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if raw[:1] in (" ", "\t") and out:
            out[-1] += raw[1:]
        else:
            out.append(raw)
    return out


def _unescape(v: str) -> str:
    # RFC 5545 TEXT escaping. Backslash-n → newline; escaped comma/semicolon/backslash literalized.
    out, i = [], 0
    while i < len(v):
        c = v[i]
        if c == "\\" and i + 1 < len(v):
            nxt = v[i + 1]
            out.append({"n": "\n", "N": "\n"}.get(nxt, nxt))
            i += 2
        else:
            out.append(c)
            i += 1
    return "".join(out)


def _split_prop(line: str) -> tuple[str, dict[str, str], str] | None:
    """`NAME;PARAM=x;PARAM2=y:VALUE` → (NAME_upper, {param:value}, VALUE). None if no colon."""
    if ":" not in line:
        return None
    head, value = line.split(":", 1)
    parts = head.split(";")
    name = parts[0].strip().upper()
    params = {}
    for p in parts[1:]:
        if "=" in p:
            k, val = p.split("=", 1)
            params[k.strip().upper()] = val.strip()
    return name, params, value


def _to_iso(value: str, params: dict[str, str]) -> tuple[str, bool]:
    """A DTSTART/DTEND value → (iso_string, all_day). VALUE=DATE or a bare YYYYMMDD is all-day."""
    v = value.strip()
    is_date = params.get("VALUE", "").upper() == "DATE" or (len(v) == 8 and v.isdigit())
    if is_date:
        return f"{v[0:4]}-{v[4:6]}-{v[6:8]}", True
    v = v.rstrip("Z")  # floating local — drop the UTC marker (ponytail: no tz conversion)
    date, _, tm = v.partition("T")
    if len(date) < 8 or len(tm) < 4:
        return v, False  # unrecognized shape — hand it back untouched, never crash
    hh, mm, ss = tm[0:2], tm[2:4], (tm[4:6] or "00")
    return f"{date[0:4]}-{date[4:6]}-{date[6:8]}T{hh}:{mm}:{ss}", False


def parse_ics(text: str) -> list[dict]:
    """Parse VEVENTs into calendar-kind events: [{id?, title, start, end?, all_day}]. `source` is
    stamped by the caller (the subscription label). UID becomes `id` for stable upsert; a UID-less
    event still parses (id absent → caller can synthesize). Malformed events are skipped."""
    events: list[dict] = []
    cur: dict | None = None
    for line in _unfold(text or ""):
        s = line.strip()
        if s == "BEGIN:VEVENT":
            cur = {}
            continue
        if s == "END:VEVENT":
            if cur is not None and cur.get("start"):
                events.append(cur)
            cur = None
            continue
        if cur is None:
            continue
        prop = _split_prop(s)
        if prop is None:
            continue
        name, params, value = prop
        if name == "UID":
            cur["id"] = value.strip()
        elif name == "SUMMARY":
            cur["title"] = _unescape(value).strip()
        elif name == "DTSTART":
            iso, all_day = _to_iso(value, params)
            cur["start"] = iso
            cur["all_day"] = all_day
        elif name == "DTEND":
            iso, _ = _to_iso(value, params)
            cur["end"] = iso
    # Default a missing title so the grid never renders a blank chip.
    for e in events:
        e.setdefault("title", "(untitled)")
    return events


LOCAL_SOURCES = {"", "toto", "local", None}  # events the sync job must NEVER touch


def merge_events(existing: list[dict], source: str, fetched: list[dict],
                 *, max_events: int = 200) -> list[dict]:
    """Fold a freshly-fetched feed into an object's events. Every event NOT tagged with this
    `source` is kept verbatim (local `toto` events and OTHER subscriptions are untouched); this
    source's prior events are dropped and REPLACED by `fetched` — which is exactly a UID upsert
    plus removal of any UID that vanished from the feed, since we replace the whole source
    wholesale. Each incoming event is stamped source=<label> with a stable id (UID → id, else a
    source-scoped index). Clamped to `max_events` incoming so a runaway feed can't blow the 32KB
    payload. Deterministic (order preserved) so an unchanged feed produces an identical list."""
    kept = [e for e in (existing or []) if e.get("source") != source]
    merged = list(kept)
    for i, ev in enumerate(fetched[:max_events]):
        out = {k: v for k, v in ev.items() if k in ("title", "start", "end", "all_day")}
        out["source"] = source
        out["id"] = str(ev.get("id") or f"{source}-{i}")
        merged.append(out)
    return merged


def _escape(v: str) -> str:
    return (v or "").replace("\\", "\\\\").replace("\n", "\\n").replace(",", "\\,").replace(";", "\\;")


def _to_ics_dt(iso: str, all_day: bool) -> tuple[str, str]:
    """An event's ISO start/end → (param_suffix, ical_value). All-day → `;VALUE=DATE:YYYYMMDD`."""
    if all_day:
        return ";VALUE=DATE", iso.replace("-", "")[:8]
    date, _, tm = iso.partition("T")
    d = date.replace("-", "")
    t = (tm or "00:00:00").replace(":", "")[:6].ljust(6, "0")
    return "", f"{d}T{t}"


def to_ics(label: str, events: list[dict], *, calname: str = "") -> str:
    """Serialize local events as a VCALENDAR so Google/Apple/Outlook can subscribe to Toto. Only
    the fields the kind carries; PRODID names Toto. Stable per-event UID (falls back to an index)."""
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Toto//calendar//EN",
        "CALSCALE:GREGORIAN",
        f"X-WR-CALNAME:{_escape(calname or label or 'Toto calendar')}",
    ]
    for i, ev in enumerate(events or []):
        start = ev.get("start")
        if not start:
            continue
        all_day = bool(ev.get("all_day"))
        uid = str(ev.get("id") or f"toto-{i}")
        sp, sv = _to_ics_dt(start, all_day)
        lines += ["BEGIN:VEVENT", f"UID:{uid}", f"SUMMARY:{_escape(ev.get('title') or '')}",
                  f"DTSTART{sp}:{sv}"]
        if ev.get("end"):
            ep, evv = _to_ics_dt(ev["end"], all_day)
            lines.append(f"DTEND{ep}:{evv}")
        lines.append("END:VEVENT")
    lines.append("END:VCALENDAR")
    return "\r\n".join(lines) + "\r\n"


if __name__ == "__main__":  # self-check: python -m toto_gateway.ics
    sample = (
        "BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nUID:abc@x\r\nSUMMARY:Dentist\r\n"
        "DTSTART:20260707T150000\r\nDTEND:20260707T160000\r\nEND:VEVENT\r\n"
        "BEGIN:VEVENT\r\nUID:d2\r\nSUMMARY:Ship wave\r\nDTSTART;VALUE=DATE:20260708\r\n"
        "END:VEVENT\r\nEND:VCALENDAR\r\n"
    )
    evs = parse_ics(sample)
    assert len(evs) == 2, evs
    assert evs[0] == {"id": "abc@x", "title": "Dentist", "start": "2026-07-07T15:00:00",
                      "all_day": False, "end": "2026-07-07T16:00:00"}, evs[0]
    assert evs[1]["all_day"] is True and evs[1]["start"] == "2026-07-08", evs[1]
    # Round-trip: our export parses back to the same events.
    back = parse_ics(to_ics("Cal", evs))
    assert back[0]["start"] == "2026-07-07T15:00:00" and back[1]["all_day"] is True
    # Folded line + escaped comma.
    folded = "BEGIN:VEVENT\r\nUID:f\r\nSUMMARY:Lunch\\, then\r\n  more\r\nDTSTART:20260707\r\nEND:VEVENT"
    fe = parse_ics(folded)[0]
    assert fe["title"] == "Lunch, then more", fe
    assert fe["all_day"] is True
    print("ics self-check OK")
