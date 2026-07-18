"""Pipedream Connect client — the calendar-login pilot's external surface (flag-gated, read-only).

Verified against the live Connect API (pipedream.com/docs/connect, checked 2026-07-06):
  - OAuth:    POST https://api.pipedream.com/v1/oauth/token
              {grant_type: client_credentials, client_id, client_secret} -> {access_token, expires_in}
  - Token:    POST /v1/connect/{project}/tokens  (Bearer + x-pd-environment)
              {external_user_id} -> {token, connect_link_url, expires_at}
  - Accounts: GET  /v1/connect/{project}/accounts?external_user_id=&app=
              -> {data: [{id: "apn_...", app: {name_slug}, healthy}], page_info}
  - Proxy:    GET  /v1/connect/{project}/proxy/{base64url(target_url)}?external_user_id=&account_id=
              -> the upstream JSON response

`external_user_id` is ALWAYS the toto user_id — per-user isolation is Pipedream's, keyed on it. The
proxy is called with GET (not the POST the curl examples show) on purpose: a read must never be able
to turn into an upstream write. A GET to the proxy cannot become a POST to Google's /events (which
would INSERT an event) — the worst a mistaken GET does is 4xx, never a mutation. Read-only by
construction, which is exactly the pilot's posture.

Everything degrades to absent when the flag is off or creds are unset (see `enabled`); the callers
(endpoints + the sync tick) contain any raised error, so a Pipedream outage never breaks Toto.
"""

from __future__ import annotations

import base64
import datetime
import logging
import time
from urllib.parse import urlencode

log = logging.getLogger("toto_gateway.pipedream")

_API = "https://api.pipedream.com/v1"
GCAL_SLUG = "google_calendar"

# Metering estimate (pd-metering stamp): the Connect plan is ~10,000 credits / $99, and one proxy
# read burns ~1 credit, so ~$0.01 per sync pull. Their credit model is opaque, so this is an
# ESTIMATE logged per call and reconciled monthly against Pipedream's invoice.
# ponytail: flat per-call estimate; refine only if the monthly reconciliation drifts materially.
EST_USD_PER_CALL = 0.01


def enabled(settings) -> bool:
    """True iff the pilot is flag-on AND fully configured. Any missing piece → the whole surface is
    absent (endpoints 404, sync branch skipped), never a half-configured error."""
    return bool(settings.pipedream and settings.pipedream_client_id
                and settings.pipedream_client_secret and settings.pipedream_project_id)


class PipedreamClient:
    """Thin Connect REST client over an httpx.AsyncClient. One instance caches its OAuth access
    token for its own lifetime — make one per request (endpoints) or one per sync tick (the job
    reuses it across users, so the 1h token is fetched once per tick)."""

    def __init__(self, settings, http):
        self._s = settings
        self._http = http
        self._token = ""
        self._token_exp = 0.0

    @property
    def _proj(self) -> str:
        return self._s.pipedream_project_id

    @property
    def _env(self) -> str:
        return self._s.pipedream_environment or "development"

    async def _access_token(self) -> str:
        if self._token and time.time() < self._token_exp - 60:
            return self._token
        r = await self._http.post(f"{_API}/oauth/token", json={
            "grant_type": "client_credentials",
            "client_id": self._s.pipedream_client_id,
            "client_secret": self._s.pipedream_client_secret,
        })
        r.raise_for_status()
        d = r.json()
        self._token = d["access_token"]
        self._token_exp = time.time() + float(d.get("expires_in") or 3600)
        return self._token

    async def _auth_headers(self) -> dict:
        return {"Authorization": f"Bearer {await self._access_token()}",
                "x-pd-environment": self._env}

    async def create_connect_token(self, external_user_id: str, *, app: str = GCAL_SLUG,
                                   allowed_origins: list[str] | None = None) -> dict:
        """A short-lived Connect token + the hosted Connect Link URL the user opens (new tab) to
        authorize. We append ?app= so the Link lands straight on the Google Calendar consent."""
        body: dict = {"external_user_id": external_user_id}
        if allowed_origins:
            body["allowed_origins"] = allowed_origins
        r = await self._http.post(f"{_API}/connect/{self._proj}/tokens",
                                  json=body, headers=await self._auth_headers())
        r.raise_for_status()
        d = r.json()
        link = d.get("connect_link_url") or ""
        if link and app:
            link += ("&" if "?" in link else "?") + f"app={app}"
        return {"token": d.get("token"), "connect_link_url": link, "expires_at": d.get("expires_at")}

    async def list_accounts(self, external_user_id: str, *, app: str = GCAL_SLUG) -> list[dict]:
        """Connected accounts for this user (optionally filtered to one app). Empty list = the user
        hasn't authorized anything yet — the 'not connected' state, not an error."""
        r = await self._http.get(f"{_API}/connect/{self._proj}/accounts",
                                 params={"external_user_id": external_user_id, "app": app},
                                 headers=await self._auth_headers())
        r.raise_for_status()
        return r.json().get("data") or []

    async def _proxy_get(self, external_user_id: str, account_id: str, target_url: str) -> dict:
        enc = base64.urlsafe_b64encode(target_url.encode()).decode().rstrip("=")
        r = await self._http.get(
            f"{_API}/connect/{self._proj}/proxy/{enc}",
            params={"external_user_id": external_user_id, "account_id": account_id},
            headers=await self._auth_headers())
        r.raise_for_status()
        return r.json()

    async def calendar_events(self, external_user_id: str, account_id: str, *,
                              days: int = 30, max_results: int = 250) -> list[dict]:
        """Google Calendar events.list for the primary calendar over [now, now+days], expanded to
        single instances, mapped to the calendar kind's event shape (see `map_event`)."""
        now = datetime.datetime.now(datetime.timezone.utc)
        qs = urlencode({
            "timeMin": _z(now),
            "timeMax": _z(now + datetime.timedelta(days=days)),
            "singleEvents": "true", "orderBy": "startTime", "maxResults": max_results,
        })
        url = f"https://www.googleapis.com/calendar/v3/calendars/primary/events?{qs}"
        data = await self._proxy_get(external_user_id, account_id, url)
        out = []
        for it in (data.get("items") or []):
            ev = map_event(it)
            if ev:
                out.append(ev)
        return out


# On-demand calendar read (the external `calendar_events` tool surface): map the tool's coarse
# "range" arg to a day window for calendar_events(). "today" is a 1-day look-ahead (good enough —
# ponytail: not a real end-of-day boundary; add one only if "today" ever surfaces tomorrow's events
# confusingly). The REST endpoint + the companion handler share this map so the two agree.
RANGE_DAYS = {"today": 1, "week": 7}


async def fetch_user_calendar(client: "PipedreamClient", external_user_id: str, *,
                              days: int) -> dict:
    """Resolve THIS user's connected Google Calendar and pull events read-only — the one read path
    the REST endpoint and the companion tool share. external_user_id is always the toto user id.
    Returns {"connected": bool, "events": [...], "connect_link": str|None}: no account → connected
    False plus a freshly-minted Connect Link (best-effort) the caller relays so the user can
    authorize. Upstream/proxy errors propagate to the caller to contain (endpoint → 502, tool →
    graceful string)."""
    accts = await client.list_accounts(external_user_id)
    acct = next((a for a in accts
                 if (a.get("app") or {}).get("name_slug") == GCAL_SLUG and a.get("id")), None)
    if acct is None:
        try:
            tok = await client.create_connect_token(external_user_id)
            link = tok.get("connect_link_url") or None
        except Exception:  # a Link is a nicety, not the answer — never fail the read to mint one
            link = None
        return {"connected": False, "events": [], "connect_link": link}
    events = await client.calendar_events(external_user_id, acct["id"], days=days)
    return {"connected": True, "events": events, "connect_link": None}


def _fmt_event(e: dict) -> str:
    """One event → 'YYYY-MM-DD HH:MM–HH:MM: Title' (timed) or 'YYYY-MM-DD (all day): Title'."""
    title = e.get("title") or "(untitled)"
    start = e.get("start") or ""
    if e.get("all_day"):
        return f"{start[:10]} (all day): {title}"
    when = f"{start[:10]} {start[11:16]}".strip()
    end = e.get("end") or ""
    if end and end[11:16]:
        when += f"–{end[11:16]}"
    return f"{when}: {title}"


def calendar_receipt(result: dict, *, range_label: str, cap: int = 20) -> str:
    """A concise, model- and human-facing receipt from fetch_user_calendar's result: a connect
    prompt when not connected, a clear-day line when empty, else up to `cap` 'time — title' lines."""
    if not result.get("connected"):
        link = result.get("connect_link")
        msg = ('No Google Calendar connected yet. Connect it from the calendar card\'s '
               '"Connect Google Calendar" button')
        return msg + (f", or open {link}" if link else "") + ", then ask again."
    events = result.get("events") or []
    if not events:
        return f"Your Google Calendar is clear for {range_label}."
    shown = events[:cap]
    lines = [f"Your Google Calendar — {range_label} ({len(events)} event(s)):"]
    lines += ["- " + _fmt_event(e) for e in shown]
    if len(events) > cap:
        lines.append(f"…and {len(events) - cap} more.")
    return "\n".join(lines)


def _z(dt: datetime.datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")


def _norm(v: str, all_day: bool) -> str:
    """Google start/end -> the calendar kind's ISO string. All-day -> 'YYYY-MM-DD'; a timed
    'dateTime' (which carries an offset or Z) -> floating-local 'YYYY-MM-DDTHH:MM:SS'.
    # ponytail: drop the offset like ics._to_iso — the kind stores floating local. Add tz mapping
    # only if events ever land at the wrong hour across zones."""
    if all_day:
        return v[:10]
    return v[:19]  # trims any timezone designator, keeping wall-clock time


def map_event(item: dict) -> dict | None:
    """A Google Calendar event resource -> {id:"gcal:"+id, title, start, end?, all_day, source}.
    Cancelled instances and start-less items are dropped (return None)."""
    if item.get("status") == "cancelled":
        return None
    start = item.get("start") or {}
    end = item.get("end") or {}
    all_day = "date" in start and "dateTime" not in start
    s = start.get("dateTime") or start.get("date")
    if not s:
        return None
    ev = {"id": "gcal:" + str(item.get("id") or ""), "title": item.get("summary") or "(untitled)",
          "start": _norm(s, all_day), "all_day": all_day, "source": "google"}
    e = end.get("dateTime") or end.get("date")
    if e:
        ev["end"] = _norm(e, all_day)
    return ev


if __name__ == "__main__":  # self-check: python -m toto_gateway.pipedream
    timed = map_event({"id": "abc", "status": "confirmed", "summary": "Sync",
                       "start": {"dateTime": "2026-07-07T15:00:00-07:00"},
                       "end": {"dateTime": "2026-07-07T16:00:00-07:00"}})
    assert timed == {"id": "gcal:abc", "title": "Sync", "start": "2026-07-07T15:00:00",
                     "all_day": False, "source": "google", "end": "2026-07-07T16:00:00"}, timed
    allday = map_event({"id": "d2", "summary": "Trip", "start": {"date": "2026-07-08"},
                        "end": {"date": "2026-07-09"}})
    assert allday["all_day"] is True and allday["start"] == "2026-07-08", allday
    assert map_event({"status": "cancelled", "id": "x", "start": {"date": "2026-07-08"}}) is None
    assert map_event({"id": "n", "summary": "no start"}) is None

    # calendar_events receipt formatting (pure — no I/O)
    assert _fmt_event({"title": "Sync", "start": "2026-07-07T15:00:00",
                       "end": "2026-07-07T16:00:00"}) == "2026-07-07 15:00–16:00: Sync"
    assert _fmt_event({"title": "Trip", "start": "2026-07-08", "all_day": True}) \
        == "2026-07-08 (all day): Trip"
    assert "Connect Google Calendar" in calendar_receipt(
        {"connected": False, "connect_link": None}, range_label="today")
    assert "open https://x" in calendar_receipt(
        {"connected": False, "connect_link": "https://x"}, range_label="today")
    assert calendar_receipt({"connected": True, "events": []}, range_label="today") \
        == "Your Google Calendar is clear for today."
    r = calendar_receipt({"connected": True, "events": [
        {"title": "A", "start": "2026-07-07T09:00:00"}]}, range_label="this week")
    assert "1 event(s)" in r and "09:00: A" in r

    class _S:  # enabled() gate
        pipedream = True
        pipedream_client_id = pipedream_client_secret = pipedream_project_id = ""
    assert enabled(_S()) is False
    _S.pipedream_client_id = _S.pipedream_client_secret = _S.pipedream_project_id = "x"
    assert enabled(_S()) is True
    _S.pipedream = False
    assert enabled(_S()) is False
    print("pipedream self-check OK")
