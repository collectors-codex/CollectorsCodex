"""
Collector's Cödex — weekly event scraper

For every venue in cc_venues (plus Art Basel and Eventbrite), this:
  1. asks Firecrawl to read the exhibitions page and return a clean list of events
  2. keeps only events in your six cities (New York, Miami, Los Angeles, Paris, Chicago, Mexico City)
  3. links each event to its venue, and looks up map coordinates
  4. saves everything to cc_events (updating existing rows, never duplicating)
  5. hides events whose end date has passed
  6. fails the run (so GitHub emails you) if too many sites return nothing

Needs three environment variables (GitHub secrets):
  FIRECRAWL_API_KEY, SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY
Optional:
  SOURCE_LIMIT  — only scrape the first N sources (for testing)
"""

import datetime as dt
import os
import sys
import time
import unicodedata
from urllib.parse import urljoin

import requests

FIRECRAWL_KEY = os.environ["FIRECRAWL_API_KEY"].strip()
SB_URL = os.environ["SUPABASE_URL"].strip().rstrip("/")
SB_KEY = os.environ["SUPABASE_SERVICE_ROLE_KEY"].strip()
SOURCE_LIMIT = int(os.environ.get("SOURCE_LIMIT") or 0)

TODAY = dt.date.today()
NOW_ISO = dt.datetime.now(dt.timezone.utc).isoformat()

# New-style Supabase secret keys (sb_secret_...) go in the apikey header only;
# old-style service_role keys (JWTs) also need the Authorization header.
SB_HEADERS = {"apikey": SB_KEY, "Content-Type": "application/json"}
if not SB_KEY.startswith("sb_"):
    SB_HEADERS["Authorization"] = f"Bearer {SB_KEY}"

# Sources that list events at many venues
AGGREGATORS = [
    {"name": "Art Basel", "url": "https://www.artbasel.com/events/browse"},
    {"name": "Art by Adrielle (Eventbrite)", "url": "https://www.eventbrite.com/o/adrielle-turner-121058837212"},
]

# Neighborhoods and nearby cities that count as one of the six directory cities
CITY_ALIASES = {
    "new york": "New York", "new york city": "New York", "nyc": "New York", "manhattan": "New York",
    "brooklyn": "New York", "queens": "New York", "bronx": "New York", "harlem": "New York",
    "miami": "Miami", "miami beach": "Miami", "north miami": "Miami", "coral gables": "Miami",
    "wynwood": "Miami", "little haiti": "Miami", "key biscayne": "Miami",
    "los angeles": "Los Angeles", "la": "Los Angeles", "west hollywood": "Los Angeles",
    "hollywood, ca": "Los Angeles", "santa monica": "Los Angeles", "culver city": "Los Angeles",
    "beverly hills": "Los Angeles", "downtown los angeles": "Los Angeles", "dtla": "Los Angeles",
    "paris": "Paris", "pantin": "Paris", "romainville": "Paris",
    "chicago": "Chicago",
    "mexico city": "Mexico City", "ciudad de mexico": "Mexico City", "cdmx": "Mexico City",
    "mexico, d.f.": "Mexico City", "ciudad de méxico": "Mexico City",
}
ALLOWED_TYPES = {"exhibition", "event", "fair"}

EVENT_SCHEMA = {
    "type": "object",
    "properties": {
        "exhibitions_page_url": {"type": "string"},
        "events": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "artists": {"type": "array", "items": {"type": "string"}},
                    "type": {"type": "string", "enum": ["exhibition", "event", "fair"]},
                    "venue": {"type": "string"},
                    "address": {"type": "string"},
                    "city": {"type": "string"},
                    "date_text": {"type": "string"},
                    "start_date": {"type": "string"},
                    "end_date": {"type": "string"},
                    "dates_approximate": {"type": "boolean"},
                    "url": {"type": "string"},
                    "ticket_url": {"type": "string"},
                    "image_url": {"type": "string"},
                    "description": {"type": "string"},
                },
                "required": ["title", "url"],
            },
        },
    },
    "required": ["events"],
}

EXTRACT_PROMPT = f"""Today is {TODAY.isoformat()}.
List every exhibition, fair and event on this page that is current or upcoming (skip past ones).
For each item give:
- title: the headline; if the page shows an artist name and a show title, combine them as "Artist: Title"
- artists: the artist names involved
- type: "exhibition" for shows, "fair" for art fairs, "event" for talks, tours, openings, parties, brunches
- venue: the venue name
- address: the street address if shown, otherwise empty
- city: the city it takes place in. If only a branch label is shown (e.g. "18th Street", "Wooster Street"), give the city that branch is in.
- date_text: the dates exactly as written on the page
- start_date and end_date as YYYY-MM-DD. Leave start_date empty for "Through ..." dates. For one-day events, use the same date for both.
  For vague dates like "Spring 2027" use the last day of that season and set dates_approximate to true. Leave both empty for "Ongoing".
- url: the full link to that item's own page
- ticket_url: a ticket, booking or RSVP link if one is shown, otherwise empty
- image_url: the item's image link if shown
- description: one short sentence about it, if the page gives one
Also give exhibitions_page_url: if this page is a homepage rather than an exhibitions listing, the full link to the site's current exhibitions page; otherwise empty."""


# ---------------------------------------------------------------- helpers

def norm(text):
    """Lowercase, strip accents and extra spaces."""
    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(c for c in text if not unicodedata.combining(c))
    return " ".join(text.lower().replace("’", "'").split())


def canonical_city(raw):
    """Map a city name onto one of the six directory cities, or None."""
    key = norm(raw).strip(" ,.")
    if not key:
        return None
    if key in CITY_ALIASES:
        return CITY_ALIASES[key]
    for alias, city in CITY_ALIASES.items():
        if len(alias) > 3 and alias in key:
            return city
    return None


def to_date(value):
    try:
        return dt.date.fromisoformat((value or "").strip()[:10])
    except ValueError:
        return None


def clean(value):
    value = (value or "").strip() if isinstance(value, str) else value
    return value or None


# ---------------------------------------------------------------- Firecrawl

def firecrawl_extract(url):
    body = {
        "url": url,
        "formats": ["json"],
        "jsonOptions": {"schema": EVENT_SCHEMA, "prompt": EXTRACT_PROMPT},
        "onlyMainContent": True,
        "timeout": 90000,
    }
    headers = {"Authorization": f"Bearer {FIRECRAWL_KEY}", "Content-Type": "application/json"}
    for attempt in range(3):
        resp = requests.post("https://api.firecrawl.dev/v1/scrape", headers=headers, json=body, timeout=180)
        if resp.status_code in (429, 500, 502, 503, 504) and attempt < 2:
            time.sleep(20 * (attempt + 1))
            continue
        resp.raise_for_status()
        payload = resp.json()
        if not payload.get("success"):
            raise RuntimeError(payload.get("error") or "Firecrawl returned no data")
        return payload.get("data", {}).get("json") or {}
    raise RuntimeError("Firecrawl kept failing")


# ---------------------------------------------------------------- Supabase

def sb(method, path, **kwargs):
    headers = {**SB_HEADERS, **kwargs.pop("headers", {})}
    resp = requests.request(method, f"{SB_URL}/rest/v1/{path}", headers=headers, timeout=60, **kwargs)
    if resp.status_code >= 400:
        raise RuntimeError(f"Supabase {method} {path} failed: {resp.status_code} {resp.text[:300]}")
    return resp.json() if resp.text.strip() else None


def sb_get_all(path, params):
    rows, start, size = [], 0, 1000
    while True:
        page = sb("GET", path, params=params, headers={"Range": f"{start}-{start + size - 1}"})
        rows.extend(page or [])
        if not page or len(page) < size:
            return rows
        start += size


def upsert_event(row):
    sb("POST", "cc_events", params={"on_conflict": "event_url"}, json=row,
       headers={"Prefer": "resolution=merge-duplicates,return=minimal"})


def update_venue(venue_id, fields):
    sb("PATCH", "cc_venues", params={"id": f"eq.{venue_id}"}, json=fields,
       headers={"Prefer": "return=minimal"})


# ---------------------------------------------------------------- geocoding (OpenStreetMap, free)

_geo_cache = {}
_geo_count = 0
GEO_MAX_PER_RUN = 400


def geocode(query):
    global _geo_count
    query = (query or "").strip(" ,")
    if not query or _geo_count >= GEO_MAX_PER_RUN:
        return None
    if query in _geo_cache:
        return _geo_cache[query]
    _geo_count += 1
    time.sleep(1.1)  # OpenStreetMap allows about one lookup per second
    try:
        resp = requests.get(
            "https://nominatim.openstreetmap.org/search",
            params={"q": query, "format": "json", "limit": 1},
            headers={"User-Agent": "CollectorsCodex/1.0 (artbyadrielle@gmail.com)"},
            timeout=30,
        )
        results = resp.json() if resp.ok else []
    except (requests.RequestException, ValueError):
        results = []
    coords = (float(results[0]["lat"]), float(results[0]["lon"])) if results else None
    _geo_cache[query] = coords
    return coords


# ---------------------------------------------------------------- main

def build_venue_matcher(venues):
    keyed = [(norm(v["name"]).replace("the ", ""), v) for v in venues]

    def match(name):
        key = norm(name).replace("the ", "")
        if len(key) < 4:
            return None
        for vkey, venue in keyed:
            if key == vkey or (len(vkey) > 5 and vkey in key) or (len(key) > 5 and key in vkey):
                return venue
        return None

    return match


def make_row(ev, page_url, source_name, venue, city):
    url = clean(ev.get("url"))
    title = clean(ev.get("title"))
    if not url or not title:
        return None
    start, end = to_date(ev.get("start_date")), to_date(ev.get("end_date"))
    if start and end and end < start:      # sanity check: impossible dates
        start = None
    if end and end < TODAY:                # already over
        return None
    ev_type = ev.get("type") if ev.get("type") in ALLOWED_TYPES else "exhibition"
    ticket = clean(ev.get("ticket_url"))
    image = clean(ev.get("image_url"))
    artists = [a.strip() for a in (ev.get("artists") or []) if isinstance(a, str) and a.strip()]
    return {
        "event_url": urljoin(page_url, url),
        "source_name": source_name,
        "type": ev_type,
        "title": title[:300],
        "artists": artists or None,
        "venue": clean(ev.get("venue")) or (venue["name"] if venue else None),
        "venue_id": venue["id"] if venue else None,
        "address": clean(ev.get("address")),
        "city": city,
        "date_text": clean(ev.get("date_text")),
        "start_date": start.isoformat() if start else None,
        "end_date": end.isoformat() if end else None,
        "dates_approximate": bool(ev.get("dates_approximate")),
        "ticket_url": urljoin(page_url, ticket) if ticket else None,
        "image_url": urljoin(page_url, image) if image else None,
        "description": clean(ev.get("description")),
        "active": True,
        "last_checked": NOW_ISO,
    }


def add_coordinates(row, venue, have_coords):
    if row["event_url"] in have_coords:
        return  # already mapped on an earlier run; keep existing coordinates
    coords = None
    if row["address"]:
        coords = geocode(f'{row["address"]}, {row["city"]}')
    if not coords and venue and venue.get("latitude") is not None:
        coords = (venue["latitude"], venue["longitude"])
    if coords:
        row["latitude"], row["longitude"] = coords


def main():
    venues = sb_get_all("cc_venues", {"select": "*", "active": "eq.true", "order": "id"})
    match_venue = build_venue_matcher(venues)
    have_coords = {r["event_url"] for r in sb_get_all(
        "cc_events", {"select": "event_url", "latitude": "not.is.null"})}

    sources = [("venue", v) for v in venues if v.get("scrape_enabled", True)]
    sources += [("aggregator", a) for a in AGGREGATORS]
    if SOURCE_LIMIT:
        sources = sources[:SOURCE_LIMIT]

    summary, problems, saved_total = [], [], 0

    for kind, src in sources:
        name = src["name"]
        try:
            if kind == "venue":
                venue = src
                # Make sure the venue itself is on the map
                if venue.get("latitude") is None:
                    coords = geocode(f'{venue["name"]}, {venue["city"]}, {venue["country"]}')
                    if coords:
                        venue["latitude"], venue["longitude"] = coords
                        update_venue(venue["id"], {"latitude": coords[0], "longitude": coords[1]})

                page_url = venue.get("exhibitions_url") or venue["website"]
                data = firecrawl_extract(page_url)

                # First run for this venue: find and remember its exhibitions page
                found = clean(data.get("exhibitions_page_url"))
                if not venue.get("exhibitions_url") and found:
                    found = urljoin(page_url, found)
                    if found.rstrip("/") != page_url.rstrip("/"):
                        page_url = found
                        data = firecrawl_extract(page_url)
                        update_venue(venue["id"], {"exhibitions_url": page_url})

                rows = []
                for ev in data.get("events") or []:
                    raw_city = clean(ev.get("city"))
                    city = canonical_city(raw_city) if raw_city else venue["city"]
                    if not city:
                        continue  # e.g. a Hauser & Wirth show in London
                    row = make_row(ev, page_url, name, venue, city)
                    if row:
                        add_coordinates(row, venue, have_coords)
                        rows.append(row)
            else:
                page_url = src["url"]
                data = firecrawl_extract(page_url)
                rows = []
                for ev in data.get("events") or []:
                    city = canonical_city(ev.get("city") or "")
                    if not city:
                        continue  # outside the six directory cities
                    venue = match_venue(ev.get("venue") or "")
                    row = make_row(ev, page_url, name, venue, city)
                    if row:
                        add_coordinates(row, venue, have_coords)
                        rows.append(row)

            for row in rows:
                upsert_event(row)
            saved_total += len(rows)
            if kind == "venue":
                update_venue(src["id"], {"last_scraped": NOW_ISO, "last_event_count": len(rows), "last_error": None})
            summary.append(f"  {len(rows):>4}  {name}")
            if not rows:
                problems.append(f"{name}: no current events found")

        except Exception as exc:  # keep going with the other sites
            message = str(exc)[:500]
            summary.append(f"  FAIL  {name} — {message}")
            problems.append(f"{name}: {message}")
            if kind == "venue":
                try:
                    update_venue(src["id"], {"last_scraped": NOW_ISO, "last_error": message})
                except Exception:
                    pass

    # Hide events that have ended
    sb("PATCH", "cc_events", params={"end_date": f"lt.{TODAY.isoformat()}", "active": "eq.true"},
       json={"active": False}, headers={"Prefer": "return=minimal"})

    print(f"\nCollector's Cödex scrape — {TODAY.isoformat()}")
    print(f"Sources checked: {len(sources)}   Events saved: {saved_total}   Locations looked up: {_geo_count}\n")
    print("\n".join(summary))
    if problems:
        print("\nNeeds a look:")
        print("\n".join(f"  - {p}" for p in problems))

    # A few galleries between shows is normal; lots of empty sites means something broke
    if sources and len(problems) / len(sources) > 0.2:
        print("\nMore than 20% of sources returned nothing or failed — marking this run as failed so you get an email.")
        sys.exit(1)


if __name__ == "__main__":
    main()
