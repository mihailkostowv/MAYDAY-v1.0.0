import json
import math
from urllib.parse import urlsplit


def coordinates(record):
    if not isinstance(record, dict):
        return None
    lat, lon = record.get("latitude"), record.get("longitude")
    if any(
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        for value in (lat, lon)
    ):
        return None
    return (lat, lon) if -90 <= lat <= 90 and -180 <= lon <= 180 else None


def source_url(value):
    try:
        url = urlsplit(value) if isinstance(value, str) else None
        return bool(url and url.scheme in ("http", "https") and url.hostname)
    except ValueError:
        return False


def arrival_bearing(event, route):
    if not isinstance(route, dict) or route.get("route_kind") != "reconstructed":
        return None
    origin = route.get("departure")
    start, end = coordinates(origin), coordinates(event)
    if (
        not start
        or not end
        or not source_url(route.get("source_url"))
        or not source_url(origin.get("source_url"))
    ):
        return None
    if abs(end[0]) >= 89.999999:
        return None
    lat1, lon1 = map(math.radians, start)
    lat2, lon2 = map(math.radians, end)
    separation = math.acos(
        max(
            -1,
            min(
                1,
                math.sin(lat1) * math.sin(lat2)
                + math.cos(lat1) * math.cos(lat2) * math.cos(lon2 - lon1),
            ),
        )
    )
    if separation < 1 / 6371 or math.pi - separation < 1e-6:
        return None
    y = math.sin(lon1 - lon2) * math.cos(lat1)
    x = math.cos(lat2) * math.sin(lat1) - math.sin(lat2) * math.cos(lat1) * math.cos(
        lon1 - lon2
    )
    return round((math.degrees(math.atan2(y, x)) + 180) % 360, 3) % 360


def preferred_routes(rows):
    if not rows:
        return []
    preferred = rows[0][0]
    routes = []
    for source, payload in rows:
        if source != preferred:
            continue
        try:
            route = json.loads(payload)
            if isinstance(route, dict):
                routes.append(route)
        except (ValueError, TypeError):
            continue
    return routes


def event_bearing(event, routes):
    return arrival_bearing(event, routes[0]) if len(routes) == 1 else None
