#!/usr/bin/env python3
"""
update_events.py — Fetches Northern California live music events from the
Jambase API and writes them to events.js (loaded by index.html at runtime).

Covers the Bay Area, Sacramento area, Lake Tahoe basin, and Reno/Carson
City — which means querying both California and Nevada.

Usage:
    JAMBASE_API_KEY=your_key python scripts/update_events.py

Optional env vars:
    DAYS_AHEAD    How many days forward to fetch (default: 179)
    EVENTS_PATH   Output file path, relative to repo root (default: events.js)
"""

import json
import os
import sys
import time
from datetime import date, timedelta

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
JAMBASE_API_URL = "https://api.data.jambase.com/v3/events"
STATE_CODES = ["CA", "NV"]
DAYS_AHEAD = int(os.environ.get("DAYS_AHEAD", 179))
PER_PAGE = 50
EVENTS_PATH = os.environ.get("EVENTS_PATH", "events.js")

# ---------------------------------------------------------------------------
# NorCal city filter, grouped by region so each event can be tagged with
# the region it belongs to (drives the "Region" dropdown in the UI).
#
# The old Bay Area/Sacramento/Tahoe/Reno split is now nine regions: the
# Bay Area is broken into City, Peninsula, South Bay, East Bay, and North
# Bay, and Santa Cruz has been pulled out and expanded into its own
# Santa Cruz / Monterey Bay region, alongside Sacramento, Tahoe, and Reno.
# ---------------------------------------------------------------------------
CITIES_BY_REGION = {
    "City": {
        "San Francisco",
    },
    "Peninsula": {
        # San Mateo County
        "San Mateo", "Redwood City", "Daly City", "Burlingame", "Millbrae",
        "San Bruno", "South San Francisco", "Pacifica", "Half Moon Bay",
        "Foster City", "Belmont", "San Carlos", "Atherton", "Menlo Park",
        "Portola Valley", "Woodside", "Brisbane", "Colma",
    },
    "South Bay": {
        # Santa Clara County (Mountain View/Palo Alto/Los Altos grouped
        # here rather than Peninsula, matching common local usage)
        "San Jose", "Sunnyvale", "Santa Clara", "Mountain View", "Palo Alto",
        "Cupertino", "Campbell", "Los Gatos", "Los Altos", "Milpitas",
        "Gilroy", "Morgan Hill", "Saratoga", "Monte Sereno",
    },
    "East Bay": {
        # Alameda County
        "Oakland", "Berkeley", "Fremont", "Hayward", "Alameda", "Emeryville",
        "Albany", "Piedmont", "San Leandro", "Castro Valley", "Union City",
        "Newark", "Livermore", "Pleasanton", "Dublin",
        # Contra Costa County
        "Walnut Creek", "Concord", "Richmond", "Martinez", "Antioch", "Pittsburg",
        "Brentwood", "Oakley", "Pleasant Hill", "Danville", "San Ramon",
        "Hercules", "Pinole", "El Sobrante", "Orinda", "Moraga", "Lafayette",
        "El Cerrito",
    },
    "North Bay": {
        # Marin County
        "San Rafael", "Novato", "Mill Valley", "Sausalito", "Tiburon", "Larkspur",
        "Corte Madera", "San Anselmo", "Fairfax", "Belvedere", "Ross",
        # Solano County
        "Vallejo", "Benicia", "Fairfield", "Vacaville", "Dixon",
        # Sonoma County
        "Santa Rosa", "Petaluma", "Rohnert Park", "Cotati", "Windsor",
        "Healdsburg", "Sebastopol", "Cloverdale", "Sonoma", "Guerneville",
        # Napa County
        "Napa", "American Canyon", "St. Helena", "Calistoga", "Yountville",
    },
    "Santa Cruz / Monterey Bay": {
        # Santa Cruz County
        "Santa Cruz", "Scotts Valley", "Capitola", "Watsonville", "Aptos",
        # Monterey County
        "Monterey", "Pacific Grove", "Carmel-by-the-Sea", "Seaside", "Marina",
        "Del Rey Oaks", "Sand City", "Castroville", "Moss Landing", "Salinas",
    },
    "Sacramento": {
        "Sacramento", "Elk Grove", "Citrus Heights", "Folsom", "Rancho Cordova",
        "Roseville", "Rocklin", "Davis", "Woodland", "Carmichael", "Fair Oaks",
        "Orangevale", "West Sacramento", "Lincoln", "Auburn", "El Dorado Hills",
        "Placerville", "Loomis", "Galt",
    },
    "Tahoe": {
        "South Lake Tahoe", "Tahoe City", "Truckee", "Incline Village",
        "Stateline", "Kings Beach", "Tahoma", "Homewood", "Olympic Valley",
        "Squaw Valley", "Zephyr Cove", "Crystal Bay", "Tahoe Vista",
        "Carnelian Bay",
    },
    "Reno": {
        "Reno", "Sparks", "Carson City", "Minden", "Gardnerville", "Dayton",
        "Fernley", "Virginia City", "Verdi",
    },
}

# Flat set of every covered city (used for the API-result filter) and a
# reverse city → region lookup (used to tag each event with its region).
NORCAL_CITIES: set = set()
CITY_REGION: dict = {}
for _region, _cities in CITIES_BY_REGION.items():
    NORCAL_CITIES |= _cities
    for _city in _cities:
        CITY_REGION[_city] = _region


def _slug(name: str) -> str:
    return name.strip().lower().replace(" ", "-")


def _genre_name(g) -> str:
    return (g if isinstance(g, str) else g.get("name", "")).strip()


# ---------------------------------------------------------------------------
# HTTP session with retry on transient errors
# ---------------------------------------------------------------------------
def _make_session() -> requests.Session:
    session = requests.Session()
    retry = Retry(
        total=4,
        backoff_factor=2,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET"],
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


# ---------------------------------------------------------------------------
# Fetch
# ---------------------------------------------------------------------------
def _fetch_state_events(
    api_key: str,
    state_code: str,
    date_from: str,
    date_to: str,
    session: requests.Session,
    seen_ids: set,
) -> list[dict]:
    """Fetch every page of events for a single state, skipping IDs already
    seen (so overlapping/duplicate results across states are deduped)."""
    state_events: list[dict] = []
    page = 1
    total_pages: int | None = None

    print(f"Fetching {state_code} events {date_from} → {date_to} …")

    while True:
        params = {
            "geoStateIso": f"US-{state_code}",
            "eventDateFrom": date_from,
            "eventDateTo": date_to,
            "perPage": PER_PAGE,
            "page": page,
        }
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Accept": "application/json",
            "User-Agent": "NorCalLiveMusicCalendar/1.0",
        }

        try:
            resp = session.get(JAMBASE_API_URL, params=params, headers=headers, timeout=30)
        except requests.RequestException as exc:
            print(f"  ✗ Network error on page {page}: {exc}", file=sys.stderr)
            sys.exit(1)

        print(f"  HTTP {resp.status_code}")

        if resp.status_code == 429:
            wait = int(resp.headers.get("Retry-After", 60))
            print(f"  Rate limited — waiting {wait}s before retry", file=sys.stderr)
            time.sleep(wait)
            continue

        if not resp.ok:
            print(f"  ✗ API error — response body: {resp.text[:500]}", file=sys.stderr)
            sys.exit(1)

        if not resp.text.strip():
            print("  ✗ API returned empty response body", file=sys.stderr)
            sys.exit(1)

        try:
            data = resp.json()
        except Exception as exc:
            print(f"  ✗ Could not parse JSON: {exc}", file=sys.stderr)
            print(f"  Raw response: {resp.text[:500]}", file=sys.stderr)
            sys.exit(1)

        if page == 1:
            print(f"  Top-level response keys: {list(data.keys())}")
            sample_events = (
                data["events"] if "events" in data else data.get("data", [])
            )
            if sample_events:
                first = sample_events[0]
                print(f"  First event keys: {list(first.keys())}")
                performers = first.get("performer", [])
                if performers:
                    print(f"  First performer keys: {list(performers[0].keys())}")
                    performer_genres = performers[0].get("genre", [])
                    print(f"  First performer genres: {performer_genres[:3]}")
                print(f"  First event (truncated): {json.dumps(first, default=str)[:600]}")

        if "events" in data:
            page_events = data["events"]
        elif "data" in data:
            page_events = data["data"]
        else:
            page_events = []

        if page == 1 and not page_events:
            print(f"  ⚠ No {state_code} events in response — check params or API plan", file=sys.stderr)
            print(f"  Full response: {json.dumps(data, default=str)[:1000]}", file=sys.stderr)
            break

        new_count = 0
        for ev in page_events:
            ev_id = ev.get("identifier", ev.get("id", ""))
            if ev_id not in seen_ids:
                seen_ids.add(ev_id)
                state_events.append(ev)
                new_count += 1

        if "pagination" in data:
            pagination = data["pagination"]
        elif "meta" in data:
            pagination = data["meta"]
        else:
            pagination = {}

        if total_pages is None:
            total_pages = (
                pagination.get("totalPages")
                or pagination.get("total_pages")
                or 1
            )

        print(f"  Page {page}/{total_pages} — {new_count} new events (running total: {len(state_events)})")

        if page >= total_pages or not page_events:
            break

        page += 1

    return state_events


def fetch_all_events(api_key: str) -> list[dict]:
    today = date.today()
    date_from = today.strftime("%Y-%m-%d")
    date_to = (today + timedelta(days=DAYS_AHEAD)).strftime("%Y-%m-%d")

    all_events: list[dict] = []
    seen_ids: set[str] = set()
    session = _make_session()

    for state_code in STATE_CODES:
        state_events = _fetch_state_events(
            api_key, state_code, date_from, date_to, session, seen_ids
        )
        all_events.extend(state_events)

    if not all_events:
        print("  ✗ No events returned for any state — check params or API plan", file=sys.stderr)
        sys.exit(1)

    return all_events


# ---------------------------------------------------------------------------
# Map
# ---------------------------------------------------------------------------
def _extract_genres(raw: dict) -> str:
    seen: dict[str, None] = {}
    for g in raw.get("genre", []):
        name = _genre_name(g)
        if name:
            seen[_slug(name)] = None
    for performer in raw.get("performer", []):
        for g in performer.get("genre", []):
            name = _genre_name(g)
            if name:
                seen[_slug(name)] = None
    return ",".join(seen.keys())


def _best_ticket_url(offers: list[dict]) -> str:
    ticket_keywords = {"ticket", "tickets", "buy", "purchase", "get tickets"}
    for offer in offers:
        name = offer.get("name", "").lower()
        if any(kw in name for kw in ticket_keywords):
            return offer.get("url", "")
    return offers[0].get("url", "") if offers else ""


def map_event(raw: dict) -> dict:
    identifier = raw.get("identifier", "")
    event_id = identifier.rsplit(":", 1)[-1] if identifier else str(raw.get("id", ""))

    location = raw.get("location", {})
    address = location.get("address", {})
    geo = location.get("geo", {})

    performers = raw.get("performer", [])
    headliner = performers[0].get("name", "") if performers else ""
    artists_str = " | ".join(p.get("name", "") for p in performers if p.get("name"))

    genres_str = _extract_genres(raw)
    tickets_url = _best_ticket_url(raw.get("offers", []))
    city = address.get("addressLocality", "")

    return {
        "id": event_id,
        "name": raw.get("name", ""),
        "date": raw.get("startDate", ""),
        "venue": location.get("name", ""),
        "city": city,
        "region": CITY_REGION.get(city, ""),
        "lat": geo.get("latitude", 0),
        "lng": geo.get("longitude", 0),
        "headliner": headliner,
        "artists": artists_str,
        "genres": genres_str,
        "url": raw.get("url", ""),
        "tickets": tickets_url,
    }


# ---------------------------------------------------------------------------
# Write events.js
# ---------------------------------------------------------------------------
def write_events_js(events: list[dict], path: str = EVENTS_PATH) -> None:
    events_json = json.dumps(events, separators=(",", ":"), ensure_ascii=False)
    today_str = date.today().isoformat()
    content = (
        f"// Auto-generated by scripts/update_events.py — do not edit manually.\n"
        f"// Last updated: {today_str}\n"
        f"const ALL_EVENTS = {events_json};\n"
    )
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(content)
    print(f"  ✓ Wrote {len(events)} events to {path} ({len(content):,} bytes)")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> None:
    api_key = os.environ.get("JAMBASE_API_KEY", "").strip()
    if not api_key:
        print("✗ JAMBASE_API_KEY is not set.", file=sys.stderr)
        sys.exit(1)

    print(f"API key present: {api_key[:8]}…")

    # 1. Fetch all CA + NV events
    raw_events = fetch_all_events(api_key)
    print(f"\nTotal raw events fetched ({'/'.join(STATE_CODES)}): {len(raw_events)}")

    # 2. Drop cancelled/postponed
    raw_events = [
        e for e in raw_events
        if e.get("eventStatus") not in ("EventCancelled", "EventPostponed")
    ]
    print(f"After filtering cancelled/postponed: {len(raw_events)}")

    # 3. Filter to NorCal cities only
    def is_norcal(ev: dict) -> bool:
        city = (
            ev.get("location", {})
            .get("address", {})
            .get("addressLocality", "")
            .strip()
        )
        return city in NORCAL_CITIES

    raw_events = [e for e in raw_events if is_norcal(e)]
    print(f"After filtering to NorCal cities: {len(raw_events)}")

    # 4. Map to schema
    events = [map_event(e) for e in raw_events]

    # 5. Log genre coverage
    with_genres = sum(1 for e in events if e["genres"])
    print(f"Events with genres: {with_genres}/{len(events)} ({100*with_genres//max(len(events),1)}%)")
    if with_genres == 0:
        print(
            "  ⚠ Warning: no genres found. Check that Jambase is returning\n"
            "    `genre` arrays on performer objects.",
            file=sys.stderr,
        )

    # 6. Sort chronologically
    events.sort(key=lambda e: (e["date"], e["name"]))

    # 7. Write events.js
    print(f"\nWriting {EVENTS_PATH} …")
    write_events_js(events)

    print("\nDone ✓")


if __name__ == "__main__":
    main()
