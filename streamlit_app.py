# streamlit_app.py
# NYC LAEP+ mock in Streamlit using Folium/Leaflet + OSRM routes.
# Features:
# - Toggleable layers: points, transportation polylines (90‑minute OSRM routes), polygons (2020 NTAs)
# - Borough filters across all layers
# - Dummy points grid covering NYC
# - NTAs fetched from NYC Open Data with robust fallback
# - Debugging logs, structured outputs, and smoke tests
# - 5 hardcoded routes for demo speed.

import json
import math
import os
import random
import time
import logging
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

import pandas as pd
import requests
import streamlit as st
import folium
from streamlit_folium import st_folium

# =============================
# CONFIG (keys via st.secrets / env)
# =============================
# Set in .streamlit/secrets.toml:
# OSRM_BASE = "https://your-osrm-host/route/v1/driving"  # optional, defaults to public demo
# DEBUG = true  # optional: echoes debug to sidebar

# Simple logger + UI-safe debug helper (avoid st.debug which is not an API)
logger = logging.getLogger("nyc_laep_mock")
if not logger.handlers:
    logging.basicConfig(level=logging.INFO)

def ui_debug(message: str) -> None:
    """Safe debugging helper.
    Logs to Python logger and, if DEBUG=true in secrets, mirrors to the sidebar.
    """
    try:
        logger.info(message)
        if bool(st.secrets.get("DEBUG", False)):
            st.sidebar.write(f"🔎 {message}")
    except Exception:
        # Never crash the app from debug output
        pass

OSRM_BASE: str = st.secrets.get("OSRM_BASE", "https://router.project-osrm.org/route/v1/driving")

NYC_BBOX: Tuple[float, float, float, float] = (-74.25559, 40.49612, -73.70001, 40.91553)
DEFAULT_CENTER: Tuple[float, float] = (-73.95, 40.72) # (lon, lat)

# Switched to a more reliable ArcGIS GeoJSON endpoint to load all NTAs
NTA2020_GEOJSON_URL: str = "https://services5.arcgis.com/GfwWNkhEL9T7a17V/arcgis/rest/services/NYC_NTA_2020/FeatureServer/0/query?where=1%3D1&outFields=*&f=geojson"
TARGET_ROUTE_SECONDS: int = 90 * 60  # 90 minutes
N_ROUTES_TO_FIND: int = 5 # Hardcoded number of routes

COLORS: Dict[str, str] = {
    "points": "#1f77b4",
    "lines": "#ff7f0e",
    "polygons": "#2ca02c",
}
BOROUGHS: List[str] = ["Manhattan", "Brooklyn", "Queens", "Bronx", "Staten Island"]

# =============================
# UTILITIES
# =============================

@dataclass
class RouteResult:
    coordinates: List[Tuple[float, float]]
    duration_s: float
    distance_m: float
    status: str
    attempts: int


def rand_between(a: float, b: float) -> float:
    return random.random() * (b - a) + a


def grid_points(cols: int = 18, rows: int = 12, bbox: Tuple[float, float, float, float] = NYC_BBOX) -> List[Tuple[float, float, str]]:
    """Return a list of (lon, lat, borough) covering NYC.
    - cols: int
    - rows: int
    - bbox: (minx, miny, maxx, maxy)
    """
    minx, miny, maxx, maxy = bbox
    dx = (maxx - minx) / (cols - 1)
    dy = (maxy - miny) / (rows - 1)
    out = []
    for r in range(rows):
        for c in range(cols):
            jx = rand_between(-dx * 0.2, dx * 0.2)
            jy = rand_between(-dy * 0.2, dy * 0.2)
            lon = minx + c * dx + jx
            lat = miny + r * dy + jy
            borough = BOROUGHS[(r + c) % len(BOROUGHS)]
            out.append((lon, lat, borough))
    return out


def osrm_route(origin: Tuple[float, float], destination: Tuple[float, float], timeout_s: int = 12) -> Optional[RouteResult]:
    """Call OSRM server for a route.
    Parameters:
      origin: (lon, lat)
      destination: (lon, lat)
      timeout_s: int
    Returns: RouteResult or None on error
    """
    o_lon, o_lat = origin
    d_lon, d_lat = destination
    url = f"{OSRM_BASE}/{o_lon},{o_lat};{d_lon},{d_lat}?overview=full&annotations=false&geometries=geojson"
    try:
        r = requests.get(url, timeout=timeout_s)
        if r.status_code != 200:
            ui_debug(f"OSRM.non200 status={r.status_code} url={url}")
            return None
        data = r.json()
        if data.get("code") != "Ok" or not data.get("routes"):
            ui_debug(f"OSRM.badCode code={data.get('code')} url={url}")
            return None
        route = data["routes"][0]
        coords = route["geometry"]["coordinates"]  # [[lon, lat], ...]
        coords_t = [(float(x), float(y)) for x, y in coords]
        return RouteResult(
            coordinates=coords_t,
            duration_s=float(route.get("duration", 0.0)),
            distance_m=float(route.get("distance", 0.0)),
            status="ok",
            attempts=1,
        )
    except Exception as e:
        ui_debug(f"OSRM.exception {e}")
        return None


def find_route_near_duration(origin: Tuple[float, float], target_s: int = TARGET_ROUTE_SECONDS, attempts: int = 7) -> Optional[RouteResult]:
    """Heuristic search for a destination that yields a route close to target duration.
    We expand search radius and try several bearings. Not perfect but robust for demo.
    """
    minx, miny, maxx, maxy = NYC_BBOX
    best: Optional[RouteResult] = None

    base_radius_deg = 0.18  # ~20km at NYC lat
    for attempt in range(1, attempts + 1):
        angle = rand_between(0, 2 * math.pi)
        radius = base_radius_deg * attempt
        dest = (
            max(min(origin[0] + math.cos(angle) * radius, maxx), minx),
            max(min(origin[1] + math.sin(angle) * radius, maxy), miny),
        )
        res = osrm_route(origin, dest)
        if not res:
            continue
        res.attempts = attempt
        if best is None or abs(res.duration_s - target_s) < abs(best.duration_s - target_s):
            best = res
        if abs(res.duration_s - target_s) <= target_s * 0.1:
            break
        if res.duration_s < target_s * 0.5:
            base_radius_deg *= 1.4
    return best


@st.cache_data(show_spinner=False)
def load_nta_geojson() -> Tuple[dict, str]:
    """Fetch 2020 NTA GeoJSON. Returns (geojson, status). Status in {loaded, fallback}."""
    try:
        ui_debug(f"NTA.fetch {NTA2020_GEOJSON_URL}")
        r = requests.get(NTA2020_GEOJSON_URL, timeout=20)
        if r.status_code == 200:
            return r.json(), "loaded"
        ui_debug(f"NTA.fetch.failed status={r.status_code}")
        return NTA_FALLBACK, "fallback"
    except Exception as e:
        ui_debug(f"NTA.exception {e}")
        return NTA_FALLBACK, "fallback"


# Minimal fallback — one polygon per borough (keeps app working offline/CORS‑blocked)
NTA_FALLBACK = {
    "type": "FeatureCollection",
    "features": [
        {
            "type": "Feature",
            "properties": {"ntacode": "MN17", "ntaname": "Midtown-Midtown South", "boro_name": "Manhattan"},
            "geometry": {"type": "Polygon", "coordinates": [[[-73.9985, 40.7636], [-73.9850, 40.7648], [-73.9733, 40.7563], [-73.9786, 40.7480], [-73.9918, 40.7471], [-73.9985, 40.7636]]]}
        },
        {
            "type": "Feature",
            "properties": {"ntacode": "BK09", "ntaname": "Williamsburg", "boro_name": "Brooklyn"},
            "geometry": {"type": "Polygon", "coordinates": [[[-73.9719, 40.7269], [-73.9490, 40.7269], [-73.9420, 40.7095], [-73.9645, 40.7095], [-73.9719, 40.7269]]]}
        },
        {
            "type": "Feature",
            "properties": {"ntacode": "QN01", "ntaname": "Astoria", "boro_name": "Queens"},
            "geometry": {"type": "Polygon", "coordinates": [[[-73.9437, 40.7893], [-73.9099, 40.7893], [-73.9099, 40.7687], [-73.9360, 40.7640], [-73.9437, 40.7893]]]}
        },
        {
            "type": "Feature",
            "properties": {"ntacode": "BX06", "ntaname": "Belmont", "boro_name": "Bronx"},
            "geometry": {"type": "Polygon", "coordinates": [[[-73.8922, 40.8620], [-73.8785, 40.8620], [-73.8785, 40.8503], [-73.8922, 40.8503], [-73.8922, 40.8620]]]}
        },
        {
            "type": "Feature",
            "properties": {"ntacode": "SI07", "ntaname": "New Springville", "boro_name": "Staten Island"},
            "geometry": {"type": "Polygon", "coordinates": [[[-74.1681, 40.5887], [-74.1378, 40.5887], [-74.1378, 40.5718], [-74.1681, 40.5718], [-74.1681, 40.5887]]]}
        }
    ]
}

# =============================
# UI — SIDEBAR CONTROLS
# =============================
st.set_page_config(page_title="NYC LAEP+ Mock", layout="wide")
st.title("NYC LAEP+ Mock — Streamlit + Folium (Leaflet) + OSRM 90‑min Routes")

with st.sidebar:
    st.header("Layers")
    show_points = st.checkbox("Points (Assets)", value=True)
    show_lines = st.checkbox("Transportation Routes (90‑min)", value=True)
    show_polygons = st.checkbox("NTAs 2020 (Polygons)", value=True)

    st.header("Filter: Borough")
    selected_boros = st.multiselect("Visible boroughs", options=BOROUGHS, default=BOROUGHS)

    # --- REMOVED ROUTING CONTROLS ---
    # n_routes = st.slider("Number of demo routes", min_value=3, max_value=20, value=8, step=1)
    # seed = st.number_input("Random seed", value=42, step=1)
    # tolerance_min = st.slider("Duration tolerance (± minutes)", 5, 30, 10, step=5)

# Use a fixed seed for consistent route generation
random.seed(42)

# =============================
# DATA — POINTS, ROUTES, POLYGONS
# =============================
# Points
grid = grid_points()
points_df = pd.DataFrame(grid, columns=["lon", "lat", "borough"])
points_df["name"] = [f"Mock Site {i+1}" for i in range(len(points_df))]

# Polygons (NTAs)
ntas_geojson, nta_status = load_nta_geojson()

# Filter NTA by borough
filtered_features = []
for f in ntas_geojson.get("features", []):
    props = f.get("properties", {})
    # Updated logic to be more robust, catches BoroName from new source
    boro = props.get("boro_name") or props.get("BoroName") or props.get("borough")
    if not selected_boros or (boro in selected_boros):
        filtered_features.append(f)
nta_filtered = {"type": "FeatureCollection", "features": filtered_features}

# Routes — heuristic search for near‑90‑minute routes from random origins in selected boroughs
routes: List[RouteResult] = []
if show_lines and len(selected_boros) > 0:
    candidate_points = [tuple(row) for row in points_df[["lon", "lat", "borough"]].values if row[2] in selected_boros]
    random.shuffle(candidate_points)
    # Use hardcoded N_ROUTES_TO_FIND
    candidate_points = candidate_points[: max(N_ROUTES_TO_FIND * 2, N_ROUTES_TO_FIND + 2)]

    for origin in candidate_points[:N_ROUTES_TO_FIND]:
        o = (origin[0], origin[1])
        res = find_route_near_duration(o, target_s=TARGET_ROUTE_SECONDS, attempts=7)
        if res:
            routes.append(res)
        time.sleep(0.2)  # be gentle on OSRM

# =============================
# DEBUG OUTPUTS
# =============================
st.subheader("Debug & Status")
status_table = pd.DataFrame([
    {"key": "nta_status", "value": f"{nta_status} ({len(nta_filtered.get('features', []))} features)"},
    {"key": "points_count", "value": len(points_df)},
    {"key": "routes_count", "value": f"{len(routes)} (target: {N_ROUTES_TO_FIND})"},
    {"key": "osrm_base", "value": OSRM_BASE},
])
st.table(status_table)

if routes:
    diag = pd.DataFrame([
        {
            "duration_min": round(r.duration_s / 60.0, 1),
            "distance_km": round(r.distance_m / 1000.0, 1),
            "attempts": r.attempts,
            "status": r.status,
        }
        for r in routes
    ])
    st.caption("Route diagnostics (target = 90 minutes)")
    st.dataframe(diag, use_container_width=True)

# =============================
# MAP — Folium / Leaflet
# =============================
st.subheader("Map")

# Create Folium map
# Note: Folium uses (lat, lon) for location
m = folium.Map(location=[DEFAULT_CENTER[1], DEFAULT_CENTER[0]], zoom_start=10)

if show_polygons and len(nta_filtered.get("features", [])):
    # Style function for polygons
    def poly_style(feature):
        return {
            "fillColor": COLORS["polygons"],
            "color": COLORS["polygons"],
            "weight": 1,
            "fillOpacity": 0.2,
        }
    
    # Tooltip fields updated for new data source
    folium.GeoJson(
        nta_filtered,
        name="NTAs (Polygons)",
        style_function=poly_style,
        tooltip=folium.GeoJsonTooltip(fields=["NTAName"], aliases=["NTA Name:"])
    ).add_to(m)

if show_points:
    points_shown = points_df[points_df["borough"].isin(selected_boros)]
    
    # Group points into a FeatureGroup
    points_group = folium.FeatureGroup(name="Assets (Points)").add_to(m)
    
    for _, row in points_shown.iterrows():
        folium.CircleMarker(
            location=[row["lat"], row["lon"]],
            radius=5,
            color=COLORS["points"],
            fill=True,
            fill_color=COLORS["points"],
            fill_opacity=0.8,
            popup=row["name"]
        ).add_to(points_group)

if show_lines and routes:
    # Group lines into a FeatureGroup
    lines_group = folium.FeatureGroup(name="Routes (Lines)").add_to(m)

    for r in routes:
        # Folium PolyLine needs (lat, lon) coordinates
        coords_swapped = [(lat, lon) for lon, lat in r.coordinates]
        popup_text = f"~{round(r.duration_s/60)} min route"
        
        folium.PolyLine(
            coords_swapped,
            color=COLORS["lines"],
            weight=4,
            opacity=0.8,
            popup=popup_text
        ).add_to(lines_group)

# Add LayerControl to toggle layers
folium.LayerControl().add_to(m)

# Render the map in Streamlit
st_folium(m, height=600, use_container_width=True)

# =============================
# SMOKE TESTS
# =============================
with st.expander("Run smoke tests"):
    tests = []
    tests.append({"test": "Points present", "pass": len(points_df) > 0})
    tests.append({"test": "NTA features present", "pass": len(nta_filtered.get('features', [])) > 0})
    tests.append({"test": "NTA using fallback", "pass": nta_status != "fallback"})
    
    if show_lines:
        # Simplified test
        tests.append({"test": f"Routes found (target {N_ROUTES_TO_FIND})", "pass": len(routes) > 0})
        
    st.table(pd.DataFrame(tests))

# =============================
# NOTES
# =============================
st.markdown("""
- This app uses `folium` and `streamlit-folium` to render a Leaflet map.
- All three layers (points, lines, polygons) are now supported.
- NTA data source has been updated for better reliability.
- App is set to find 5 routes for demo speed.
- OSRM_BASE can be overridden in `st.secrets` for a private OSRM server.
- Public OSRM is rate-limited; for reliability, run your own OSRM backend.
- For true isochrones (90-min areas), consider Valhalla/OpenRouteService.
""")
