#!/usr/bin/env python3
"""
sources/districts.py — "who represents this address?"

Two steps, both using free, key-less public services:

  1. Geocode the address to a lon/lat with NYC Planning Labs' GeoSearch
     (geosearch.planninglabs.nyc) — the same geocoder behind NYC's own maps.
  2. Point-in-polygon the coordinate against published district layers to get
     the City Council, NY State Senate, NY State Assembly, and U.S.
     Congressional districts that contain it (ArcGIS FeatureServer / Census
     TIGERweb — both queryable without a key).

The council-district layer is the one the app's district map already uses, so
it's proven. State/federal layers come from the Census Bureau's TIGERweb, which
is stable and public. Everything degrades to None on any failure — the UI shows
what it could resolve and links out for the rest.
"""

import time

try:
    import requests
except ImportError:
    requests = None

GEOSEARCH = ["https://geosearch.planninglabs.nyc/v2/search",
             "https://geosearch.planninglabs.nyc/v1/search"]

# Each entry: (label, url, list-of-candidate-fields-for-the-district-number)
DISTRICT_LAYERS = {
    "council": (
        "City Council",
        "https://services5.arcgis.com/GfwWNkhOj9bNBqoJ/arcgis/rest/services/"
        "NYC_City_Council_Districts/FeatureServer/0/query",
        ["CounDist", "coun_dist", "council_di", "COUNDIST"],
    ),
    "state_senate": (
        "NY State Senate",
        "https://tigerweb.geo.census.gov/arcgis/rest/services/TIGERweb/"
        "State_Legislative_Districts/MapServer/0/query",
        ["BASENAME", "NAME", "SLDUST"],
    ),
    "state_assembly": (
        "NY State Assembly",
        "https://tigerweb.geo.census.gov/arcgis/rest/services/TIGERweb/"
        "State_Legislative_Districts/MapServer/1/query",
        ["BASENAME", "NAME", "SLDLST"],
    ),
    "congress": (
        "U.S. House",
        "https://tigerweb.geo.census.gov/arcgis/rest/services/TIGERweb/"
        "Legislative/MapServer/0/query",
        ["BASENAME", "NAME", "CDST", "CD119"],
    ),
}


def _get(url, params, timeout=25):
    if not requests:
        return None
    last = None
    for attempt in range(3):
        try:
            r = requests.get(url, params=params, timeout=timeout)
            if r.status_code == 200:
                return r.json()
            time.sleep(min(2 ** attempt, 6))
        except requests.exceptions.RequestException as e:  # type: ignore
            last = e
            time.sleep(min(2 ** attempt, 6))
    return None


def geocode(address):
    """Return (lon, lat, label) for an address, or None."""
    if not address or not address.strip():
        return None
    for base in GEOSEARCH:
        data = _get(base, {"text": address.strip(), "size": 1})
        feats = (data or {}).get("features") or []
        if feats:
            f = feats[0]
            coords = (f.get("geometry") or {}).get("coordinates") or []
            if len(coords) == 2:
                label = (f.get("properties") or {}).get("label", address.strip())
                return (coords[0], coords[1], label)
    return None


def _district_at(layer_key, lon, lat):
    label, url, fields = DISTRICT_LAYERS[layer_key]
    params = {
        "geometry": f"{lon},{lat}", "geometryType": "esriGeometryPoint",
        "inSR": 4326, "spatialRel": "esriSpatialRelIntersects",
        "outFields": "*", "returnGeometry": "false", "f": "json",
    }
    data = _get(url, params)
    feats = (data or {}).get("features") or []
    if not feats:
        return None
    attrs = feats[0].get("attributes") or {}
    for fld in fields:
        val = attrs.get(fld)
        if val not in (None, ""):
            return _num(val)
    # last resort: any attribute that looks like a district number
    for v in attrs.values():
        n = _num(v)
        if n is not None:
            return n
    return None


def _num(v):
    """Pull an int district number out of '7', 7, or 'Congressional District 7'."""
    if v is None:
        return None
    s = str(v)
    digits = "".join(ch for ch in s if ch.isdigit())
    try:
        return int(digits) if digits else None
    except ValueError:
        return None


def lookup(address):
    """Full pipeline: address -> {label, lon, lat, districts:{...}}."""
    geo = geocode(address)
    if not geo:
        return {"ok": False, "reason": "Could not geocode that address. Try a fuller "
                "NYC address (number, street, borough)."}
    lon, lat, label = geo
    districts = {}
    for key in DISTRICT_LAYERS:
        try:
            districts[key] = _district_at(key, lon, lat)
        except Exception:
            districts[key] = None
    return {"ok": True, "label": label, "lon": lon, "lat": lat, "districts": districts}
