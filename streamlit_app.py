# streamlit_app.py
# NYC LAEP+ mock in Streamlit using Mapbox GL JS + OSRM routes.
# Features:
# - Renders map using streamlit.components.v1.html and mapbox-gl-js
# - Toggleable layers: points, transportation polylines (90‑minute OSRM routes), polygons (2020 NTAs)
# - Borough filters across all layers
# - **Mock points generated within NTA boundaries**
# - NTAs fetched from NYC Open Data with robust fallback
# - **Dynamic spinners and status updates**
# - **3 hardcoded routes for demo speed.**

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
import streamlit.components.v1 as components
from shapely.geometry import Point, shape  # <-- ADDED SHAPELY
from shapely.ops import unary_union       # <-- ADDED SHAPELY

# --- REMOVED FOLIUM ---
# import folium
# from streamlit_folium import st_folium

# =============================
# CONFIG (keys via st.secrets / env)
# =============================
# Set in .streamlit/secrets.toml:
# MAPBOX_TOKEN = "pk..."
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

# --- RE-ADDED HARD TOKEN CHECK: This method requires Mapbox ---
MAPBOX_TOKEN: Optional[str] = st.secrets.get("MAPBOX_TOKEN") or os.getenv("MAPBOX_TOKEN")
if not MAPBOX_TOKEN:
    ui_debug("MAPBOX_TOKEN missing from secrets and env")
    st.error("Missing MAPBOX_TOKEN. Add it to st.secrets or environment. This app requires it.")
    st.stop()

OSRM_BASE: str = st.secrets.get("OSRM_BASE", "https://router.project-osrm.org/route/v1/driving")

NYC_BBOX: Tuple[float, float, float, float] = (-74.25559, 40.49612, -73.70001, 40.91553)
DEFAULT_CENTER: Tuple[float, float] = (-73.95, 40.72) # (lon, lat)

# Use the local GeoJSON file
NTA2020_GEOJSON_PATH: str = "NYC_Neighborhood_Tabulation_Areas_2020_-2131974656277759428.geojson"
TARGET_ROUTE_SECONDS: int = 90 * 60  # 90 minutes
N_ROUTES_TO_FIND: int = 3 # Hardcoded number of routes (updated from 5)

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
    coordinates: List[Tuple[float, float]] # Note: [lon, lat] from OSRM
    duration_s: float
    distance_m: float
    status: str
    attempts: int

def rand_between(a: float, b: float) -> float:
    return random.random() * (b - a) + a


# --- UPDATED grid_points function ---
@st.cache_data(show_spinner=False) # Cache the expensive point generation
def grid_points_in_ntas(
    ntas_geojson: dict,
    cols: int = 18,
    rows: int = 12,
    bbox: Tuple[float, float, float, float] = NYC_BBOX
) -> List[Tuple[float, float, str]]:
    """Generates a grid of points that fall *within* NTA boundaries."""
    ui_debug("Generating points within NTA boundaries...")
    out = []
    
    try:
        # Create a list of (Shapely_Polygon, boro_name) tuples
        nta_polygons = []
        for feature in ntas_geojson.get("features", []):
            try:
                geom = shape(feature["geometry"])
                boro = feature["properties"].get("BoroName", "Unknown")
                nta_polygons.append((geom, boro))
            except Exception as e:
                ui_debug(f"Skipping invalid NTA feature: {e}")
        
        if not nta_polygons:
            ui_debug("No valid NTA polygons found to generate points.")
            return []

        # Create a single unioned shape for fast point-in-polygon checks
        union_shape = unary_union([p for p, b in nta_polygons])

        minx, miny, maxx, maxy = bbox
        dx = (maxx - minx) / (cols - 1)
        dy = (maxy - miny) / (rows - 1)

        for r in range(rows):
            for c in range(cols):
                jx = rand_between(-dx * 0.2, dx * 0.2)
                jy = rand_between(-dy * 0.2, dy * 0.2)
                lon = minx + c * dx + jx
                lat = miny + r * dy + jy
                
                point = Point(lon, lat)
                
                # Check if the point is within the combined NYC shape
                if point.within(union_shape):
                    # Find which NTA it's in to get the correct borough
                    found_boro = "Unknown"
                    for poly, boro in nta_polygons:
                        if point.within(poly):
                            found_boro = boro
                            break
                    out.append((lon, lat, found_boro))
                    
        ui_debug(f"Generated {len(out)} points inside NTAs.")
        return out

    except Exception as e:
        ui_debug(f"Error in grid_points_in_ntas: {e}")
        return [] # Return empty list on error

def osrm_route(origin: Tuple[float, float], destination: Tuple[float, float], timeout_s: int = 12) -> Optional[RouteResult]:
    """Call OSRM server for a route."""
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
    """Heuristic search for a destination that yields a route close to target duration."""
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


@st.cache_data(show_spinner=False) # Spinner is handled manually
def load_nta_geojson() -> Tuple[dict, str]:
    """Load 2020 NTA GeoJSON from local file. Returns (geojson, status). Status in {loaded, fallback}."""
    try:
        ui_debug(f"NTA.load {NTA2020_GEOJSON_PATH}")
        # Open the local file instead of fetching a URL
        with open(NTA2020_GEOJSON_PATH, 'r', encoding='utf-8') as f:
            data = json.load(f)
        return data, "loaded"
    except FileNotFoundError:
        ui_debug(f"NTA.file_not_found {NTA2020_GEOJSON_PATH}")
        return NTA_FALLBACK, "fallback"
    except Exception as e:
        ui_debug(f"NTA.exception {e}")
        return NTA_FALLBACK, "fallback"


NTA_FALLBACK = {
    "type": "FeatureCollection",
    "features": [
        {
            "type": "Feature",
            "properties": {"ntacode": "MN17", "NTAName": "Midtown-Midtown South", "BoroName": "Manhattan"},
            "geometry": {"type": "Polygon", "coordinates": [[[-73.9985, 40.7636], [-73.9850, 40.7648], [-73.9733, 40.7563], [-73.9786, 40.7480], [-73.9918, 40.7471], [-73.9985, 40.7636]]]}
        },
        # ... other fallback features ...
        {
            "type": "Feature",
            "properties": {"ntacode": "BK09", "NTAName": "Williamsburg", "BoroName": "Brooklyn"},
            "geometry": {"type": "Polygon", "coordinates": [[[-73.9719, 40.7269], [-73.9490, 40.7269], [-73.9420, 40.7095], [-73.9645, 40.7095], [-73.9719, 40.7269]]]}
        },
        {
            "type": "Feature",
            "properties": {"ntacode": "QN01", "NTAName": "Astoria", "BoroName": "Queens"},
            "geometry": {"type": "Polygon", "coordinates": [[[-73.9437, 40.7893], [-73.9099, 40.7893], [-73.9099, 40.7687], [-73.9360, 40.7640], [-73.9437, 40.7893]]]}
        },
        {
            "type": "Feature",
            "properties": {"ntacode": "BX06", "NTAName": "Belmont", "BoroName": "Bronx"},
            "geometry": {"type": "Polygon", "coordinates": [[[-73.8922, 40.8620], [-73.8785, 40.8620], [-73.8785, 40.8503], [-73.8922, 40.8503], [-73.8922, 40.8620]]]}
        },
        {
            "type": "Feature",
            "properties": {"ntacode": "SI07", "NTAName": "New Springville", "BoroName": "Staten Island"},
            "geometry": {"type": "Polygon", "coordinates": [[[-74.1681, 40.5887], [-74.1378, 40.5887], [-74.1378, 40.5718], [-74.1681, 40.5718], [-74.1681, 40.5887]]]}
        }
    ]
}

# --- HTML/JS TEMPLATE FUNCTION (remains the same) ---
def get_mapbox_html(
    api_key: str,
    map_style: str,
    center_lon: float,
    center_lat: float,
    zoom: int,
    points_json: str,
    lines_json: str,
    polygons_json: str,
    colors: Dict[str, str]
) -> str:
    """Generates the HTML for the Mapbox GL JS map."""
    
    # Logic to add layers only if data is present
    polygon_layers_js = ""
    if polygons_json != 'null':
        polygon_layers_js = f"""
        map.addSource('nta-polygons', {{ 'type': 'geojson', 'data': {polygons_json} }});
        map.addLayer({{
            'id': 'nta-fill', 'type': 'fill', 'source': 'nta-polygons',
            'paint': {{ 'fill-color': '{colors["polygons"]}', 'fill-opacity': 0.2 }}
        }});
        map.addLayer({{
            'id': 'nta-line', 'type': 'line', 'source': 'nta-polygons',
            'paint': {{ 'line-color': '{colors["polygons"]}', 'line-width': 1 }}
        }});
        """
        
    line_layers_js = ""
    if lines_json != 'null':
        line_layers_js = f"""
        map.addSource('routes', {{ 'type': 'geojson', 'data': {lines_json} }});
        map.addLayer({{
            'id': 'routes-line', 'type': 'line', 'source': 'routes',
            'layout': {{ 'line-join': 'round', 'line-cap': 'round' }},
            'paint': {{ 'line-color': '{colors["lines"]}', 'line-width': 4 }}
        }});
        """
    
    point_layers_js = ""
    popup_js = ""
    if points_json != 'null':
        point_layers_js = f"""
        map.addSource('assets', {{ 'type': 'geojson', 'data': {points_json} }});
        map.addLayer({{
            'id': 'assets-points', 'type': 'circle', 'source': 'assets',
            'paint': {{ 
                'circle-radius': 5, 
                'circle-color': '{colors["points"]}',
                'circle-stroke-width': 1,
                'circle-stroke-color': '#FFFFFF'
            }}
        }});
        """
        # Add popup logic only if points exist
        popup_js = f"""
        const popup = new mapboxgl.Popup({{ closeButton: false, closeOnClick: false }});
        
        map.on('mouseenter', 'assets-points', (e) => {{
            map.getCanvas().style.cursor = 'pointer';
            const coordinates = e.features[0].geometry.coordinates.slice();
            const name = e.features[0].properties.name;
            
            popup.setLngLat(coordinates).setHTML(`<b>${{name}}</b>`).addTo(map);
        }});
        
        map.on('mouseleave', 'assets-points', () => {{
            map.getCanvas().style.cursor = '';
            popup.remove();
        }});
        """

    return f"""
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="utf-8"><title>Mapbox Map</title>
        <meta name="viewport" content="initial-scale=1,maximum-scale=1,user-scalable=no">
        <link href="https://api.mapbox.com/mapbox-gl-js/v3.0.0/mapbox-gl.css" rel="stylesheet">
        <script src="https://api.mapbox.com/mapbox-gl-js/v3.0.0/mapbox-gl.js"></script>
        <style>
            body {{ margin: 0; padding: 0; }} 
            #map {{ position: absolute; top: 0; bottom: 0; width: 100%; }}
            /* Popup style from your example */
            .mapboxgl-popup-content {{ 
                background-color: #333; color: white; font-family: "Helvetica Neue", Arial, sans-serif; 
                font-size: 13px; border-radius: 5px; padding: 10px; 
                box-shadow: 0 1px 2px rgba(0,0,0,0.1); 
            }}
            .mapboxgl-popup-tip {{ border-top-color: #333 !important; border-bottom-color: #333 !important; }}
        </style>
    </head>
    <body> 
    <div id="map"></div>
    <script>
        mapboxgl.accessToken = '{api_key}';
        const map = new mapboxgl.Map({{
            container: 'map',
            style: '{map_style}',
            center: [{center_lon}, {center_lat}],
            zoom: {zoom},
            pitch: 45,
            bearing: -17.6
        }});
        
        map.on('load', () => {{
            // Add layers here
            {polygon_layers_js}
            {line_layers_js}
            {point_layers_js}
        }});
        
        // Add popup interactivity
        {popup_js}

    </script>
    </body>
    </html>
    """

# =============================
# UI — SIDEBAR CONTROLS
# =============================
st.set_page_config(page_title="NYC LAEP+ Mock", layout="wide")
# Updated title to reflect the change
st.title("NYC LAEP+ Mock — Streamlit + Mapbox GL JS + OSRM 90‑min Routes")

with st.sidebar:
    st.header("Layers")
    show_points = st.checkbox("Points (Assets)", value=True)
    show_lines = st.checkbox("Transportation Routes (90‑min)", value=True)
    show_polygons = st.checkbox("NTAs 2020 (Polygons)", value=True)

    st.header("Filter: Borough")
    selected_boros = st.multiselect("Visible boroughs", options=BOROUGHS, default=BOROUGHS)

# Use a fixed seed for consistent route generation
random.seed(42)

# =============================
# DATA — POINTS, ROUTES, POLYGONS
# =============================

# --- UPDATED Data Loading with Spinners ---
with st.spinner("Loading NTA boundaries..."):
    ntas_geojson, nta_status = load_nta_geojson()

with st.spinner("Generating mock asset points..."):
    # Points - Pass the loaded geojson to the new function
    grid = grid_points_in_ntas(ntas_geojson)
    points_df = pd.DataFrame(grid, columns=["lon", "lat", "borough"])
    points_df["name"] = [f"Mock Site {i+1}" for i in range(len(points_df))]

# Polygons (NTAs)
# Filter NTA by borough
filtered_features = []
for f in ntas_geojson.get("features", []):
    props = f.get("properties", {})
    # Updated logic to be more robust, catches BoroName from new source
    boro = props.get("boro_name") or props.get("BoroName") or props.get("borough")
    if not selected_boros or (boro in selected_boros):
        filtered_features.append(f)
nta_filtered = {"type": "FeatureCollection", "features": filtered_features}

# Routes — heuristic search with st.status
routes: List[RouteResult] = []
if show_lines and len(selected_boros) > 0:
    candidate_points = [tuple(row) for row in points_df[["lon", "lat", "borough"]].values if row[2] in selected_boros]
    random.shuffle(candidate_points)
    candidate_points = candidate_points[: max(N_ROUTES_TO_FIND * 2, N_ROUTES_TO_FIND + 2)]

    # --- UPDATED to use st.status ---
    with st.status(f"Searching for {N_ROUTES_TO_FIND} routes...", expanded=False) as status:
        for i, origin in enumerate(candidate_points[:N_ROUTES_TO_FIND]):
            status.update(label=f"Searching for route {i+1} of {N_ROUTES_TO_FIND}...")
            o = (origin[0], origin[1])
            res = find_route_near_duration(o, target_s=TARGET_ROUTE_SECONDS, attempts=7)
            if res:
                routes.append(res)
            time.sleep(0.2)  # be gentle on OSRM
        status.update(label=f"Found {len(routes)} routes.", state="complete")

# =============================
# MAP — Mapbox GL JS / components.html
# =============================
# --- MOVED UP, Subheader removed ---

# --- NEW: Prepare data for Mapbox GL JS ---
# Convert all data to GeoJSON strings, or 'null' if hidden
polygons_data_json = 'null'
if show_polygons and len(nta_filtered.get("features", [])):
    polygons_data_json = json.dumps(nta_filtered)

lines_data_json = 'null'
if show_lines and routes:
    line_features = []
    for r in routes:
        line_features.append({
            "type": "Feature",
            "geometry": {
                "type": "LineString",
                "coordinates": r.coordinates  # OSRM provides [lon, lat]
            },
            "properties": {
                "name": f"~{round(r.duration_s/60)} min route"
            }
        })
    lines_data_json = json.dumps({"type": "FeatureCollection", "features": line_features})

points_data_json = 'null'
if show_points:
    points_shown = points_df[points_df["borough"].isin(selected_boros)]
    point_features = []
    for _, row in points_shown.iterrows():
        point_features.append({
            "type": "Feature",
            "geometry": {
                "type": "Point",
                "coordinates": [row["lon"], row["lat"]]
            },
            "properties": {
                "name": row["name"]
            }
        })
    points_data_json = json.dumps({"type": "FeatureCollection", "features": point_features})

# --- NEW: Render map using components.html ---
map_html = get_mapbox_html(
    api_key=MAPBOX_TOKEN,
    map_style="mapbox://styles/mapbox/standard",
    center_lon=DEFAULT_CENTER[0],
    center_lat=DEFAULT_CENTER[1],
    zoom=10,
    points_json=points_data_json,
    lines_json=lines_data_json,
    polygons_json=polygons_data_json,
    colors=COLORS
)
# --- UPDATED height to 800px ---
components.html(map_html, height=800, scrolling=False)


# =============================
# DEBUG OUTPUTS
# --- MOVED DOWN ---
# =============================
st.subheader("Debug & Status")
status_table = pd.DataFrame([
    {"key": "nta_status", "value": f"{nta_status} ({len(nta_filtered.get('features', []))} features)"},
    {"key": "points_count", "value": f"{str(len(points_df))} (inside NTAs)"}, # Cast to string
    {"key": "routes_count", "value": f"{str(len(routes))} (target: {N_ROUTES_TO_FIND})"}, # Cast to string
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
- This app now uses `streamlit.components.v1.html` and `mapbox-gl-js` to render the map.
- `folium` and `streamlit-folium` are no longer used.
- All three layers (points, lines, polygons) are supported.
- App is set to find 3 routes for demo speed.
- OSRM_BASE can be overridden in `st.secrets` for a private OSRM server.
- Public OSRM is rate-limited; for reliability, run your own OSRM backend.
""")
