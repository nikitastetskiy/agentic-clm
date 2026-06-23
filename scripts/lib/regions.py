"""Region scoping for events & webinars.

Maps an event's location (or, failing that, its title) to a macro-region so the pipeline can keep only
in-scope items when a scope is configured (e.g. an EMEA/MENA-only deck).

Regions: EMEA (Europe, Middle East, Africa), MENA (Middle East & North Africa — a subset of EMEA),
APAC, AMERICAS. Middle-East / North-Africa places are tagged MENA, the rest of Europe/Africa EMEA, so a
scope of [EMEA, MENA] keeps Europe + Middle East + Africa.

`scope_events(events, regions)` filters the events.json structure in place-safe fashion and returns a
small report (kept/dropped counts + the dropped titles) for logging. Online webinars usually have no
location and are kept (globally accessible); a webinar whose title names a non-scope region is dropped.
Unknown locations are KEPT (never silently dropped) — extend PLACE_REGION below to scope a new city.
"""
from __future__ import annotations

import re

# Macro-region keyword for a place token (lowercased). Region words (emea/apac/...) included so a title
# like "APAC Vault webinar" is scoped even with no city.
PLACE_REGION: dict[str, str] = {
    # region words
    "emea": "EMEA", "mena": "MENA", "apac": "APAC", "anz": "APAC", "americas": "AMERICAS",
    "latam": "AMERICAS", "namer": "AMERICAS",
    # Europe (EMEA)
    "london": "EMEA", "uk": "EMEA", "united kingdom": "EMEA", "england": "EMEA", "britain": "EMEA",
    "scotland": "EMEA", "edinburgh": "EMEA", "manchester": "EMEA",
    "amsterdam": "EMEA", "netherlands": "EMEA", "the hague": "EMEA", "rotterdam": "EMEA", "utrecht": "EMEA",
    "zurich": "EMEA", "geneva": "EMEA", "basel": "EMEA", "switzerland": "EMEA",
    "paris": "EMEA", "lyon": "EMEA", "france": "EMEA",
    "berlin": "EMEA", "munich": "EMEA", "frankfurt": "EMEA", "hamburg": "EMEA", "cologne": "EMEA", "germany": "EMEA",
    "madrid": "EMEA", "barcelona": "EMEA", "spain": "EMEA",
    "milan": "EMEA", "rome": "EMEA", "italy": "EMEA",
    "dublin": "EMEA", "ireland": "EMEA",
    "stockholm": "EMEA", "sweden": "EMEA", "oslo": "EMEA", "norway": "EMEA", "copenhagen": "EMEA", "denmark": "EMEA",
    "helsinki": "EMEA", "finland": "EMEA", "brussels": "EMEA", "belgium": "EMEA", "luxembourg": "EMEA",
    "vienna": "EMEA", "austria": "EMEA", "lisbon": "EMEA", "porto": "EMEA", "portugal": "EMEA",
    "warsaw": "EMEA", "poland": "EMEA", "prague": "EMEA", "czechia": "EMEA", "czech republic": "EMEA",
    "athens": "EMEA", "greece": "EMEA", "budapest": "EMEA", "hungary": "EMEA", "bucharest": "EMEA", "romania": "EMEA",
    "zagreb": "EMEA", "croatia": "EMEA", "istanbul": "EMEA", "ankara": "EMEA", "turkey": "EMEA", "turkiye": "EMEA",
    # Africa (EMEA)
    "johannesburg": "EMEA", "cape town": "EMEA", "south africa": "EMEA", "nairobi": "EMEA", "kenya": "EMEA",
    "lagos": "EMEA", "nigeria": "EMEA", "accra": "EMEA", "ghana": "EMEA",
    # Middle East & North Africa (MENA)
    "dubai": "MENA", "abu dhabi": "MENA", "uae": "MENA", "united arab emirates": "MENA",
    "riyadh": "MENA", "jeddah": "MENA", "saudi arabia": "MENA", "saudi": "MENA",
    "doha": "MENA", "qatar": "MENA", "manama": "MENA", "bahrain": "MENA", "kuwait": "MENA", "muscat": "MENA", "oman": "MENA",
    "tel aviv": "MENA", "jerusalem": "MENA", "israel": "MENA", "amman": "MENA", "jordan": "MENA",
    "cairo": "MENA", "egypt": "MENA", "casablanca": "MENA", "rabat": "MENA", "morocco": "MENA",
    "tunis": "MENA", "tunisia": "MENA",
    # APAC
    "hanoi": "APAC", "ho chi minh": "APAC", "vietnam": "APAC", "jakarta": "APAC", "indonesia": "APAC",
    "singapore": "APAC", "kuala lumpur": "APAC", "malaysia": "APAC", "bangkok": "APAC", "thailand": "APAC",
    "manila": "APAC", "philippines": "APAC", "tokyo": "APAC", "osaka": "APAC", "japan": "APAC",
    "seoul": "APAC", "korea": "APAC", "beijing": "APAC", "shanghai": "APAC", "shenzhen": "APAC", "china": "APAC",
    "hong kong": "APAC", "taipei": "APAC", "taiwan": "APAC",
    "sydney": "APAC", "melbourne": "APAC", "canberra": "APAC", "australia": "APAC", "auckland": "APAC", "new zealand": "APAC",
    "mumbai": "APAC", "bangalore": "APAC", "bengaluru": "APAC", "new delhi": "APAC", "delhi": "APAC", "hyderabad": "APAC",
    "chennai": "APAC", "pune": "APAC", "india": "APAC",
    # Americas
    "washington dc": "AMERICAS", "washington": "AMERICAS", "new york": "AMERICAS", "nyc": "AMERICAS",
    "san francisco": "AMERICAS", "seattle": "AMERICAS", "austin": "AMERICAS", "chicago": "AMERICAS",
    "boston": "AMERICAS", "los angeles": "AMERICAS", "atlanta": "AMERICAS", "denver": "AMERICAS", "las vegas": "AMERICAS",
    "usa": "AMERICAS", "united states": "AMERICAS",
    "toronto": "AMERICAS", "vancouver": "AMERICAS", "montreal": "AMERICAS", "ottawa": "AMERICAS", "canada": "AMERICAS",
    "mexico city": "AMERICAS", "mexico": "AMERICAS", "guadalajara": "AMERICAS",
    "sao paulo": "AMERICAS", "rio de janeiro": "AMERICAS", "brazil": "AMERICAS",
    "buenos aires": "AMERICAS", "argentina": "AMERICAS", "bogota": "AMERICAS", "colombia": "AMERICAS",
    "santiago": "AMERICAS", "chile": "AMERICAS",
}
# bare, ambiguous tokens common in event titles ("AWS Summit DC")
_ALIASES = {"dc": "AMERICAS", "d c": "AMERICAS"}


def _norm(s: str) -> str:
    s = (s or "").lower()
    s = s.replace("ã", "a").replace("ü", "u").replace("ö", "o").replace("é", "e")
    s = re.sub(r"[.,]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _match(text: str) -> str | None:
    n = _norm(text)
    if not n:
        return None
    if n in PLACE_REGION:
        return PLACE_REGION[n]
    if n in _ALIASES:
        return _ALIASES[n]
    for key in sorted(PLACE_REGION, key=len, reverse=True):  # longest (most specific) first
        if re.search(rf"\b{re.escape(key)}\b", n):
            return PLACE_REGION[key]
    for key, reg in _ALIASES.items():
        if re.search(rf"\b{re.escape(key)}\b", n):
            return reg
    return None


def region_for(location: str) -> str | None:
    """Region for an explicit location string, or None if unknown."""
    return _match(location)


def event_region(event: dict) -> str | None:
    """Best-effort region for an event dict: its `location`, else a place named in its `title`."""
    return _match(event.get("location", "")) or _match(event.get("title", ""))


def in_scope(event: dict, regions) -> bool:
    """True if the event is in scope. `regions` empty/None = no filter. Unknown region = kept."""
    if not regions:
        return True
    reg = event_region(event)
    return reg is None or reg in regions


def scope_events(events: dict, regions) -> dict:
    """Filter an events.json dict (featured + every webinar bucket) to `regions`. Returns a report
    {regions, kept, dropped:[{title,region,bucket}]}. No-op when `regions` is empty/None."""
    report = {"regions": list(regions or []), "kept": 0, "dropped": []}
    if not regions:
        report["kept"] = len(events.get("featured", [])) + sum(len(v) for v in events.get("webinars", {}).values())
        return report

    kept_featured = []
    for ev in events.get("featured", []):
        if in_scope(ev, regions):
            kept_featured.append(ev)
        else:
            report["dropped"].append({"title": ev.get("title", "")[:50], "region": event_region(ev), "bucket": "featured"})
    events["featured"] = kept_featured

    for bucket, items in list(events.get("webinars", {}).items()):
        kept = []
        for ev in items:
            if in_scope(ev, regions):
                kept.append(ev)
            else:
                report["dropped"].append({"title": ev.get("title", "")[:50], "region": event_region(ev), "bucket": f"webinar/{bucket}"})
        events["webinars"][bucket] = kept

    report["kept"] = len(events["featured"]) + sum(len(v) for v in events.get("webinars", {}).values())
    if "counts" in events:
        events["counts"] = {
            "terraform_webinars": len(events["webinars"].get("terraform", [])),
            "vault_webinars": len(events["webinars"].get("vault", [])),
            "other_webinars": len(events["webinars"].get("other", [])),
            "featured": len(events["featured"]),
        }
    return report
