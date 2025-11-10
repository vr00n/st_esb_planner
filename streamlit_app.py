# streamlit_app.py
# NYC LAEP+ mock in Streamlit using Mapbox GL JS + OSRM routes.
# Features:
# - Renders map using streamlit.components.v1.html and mapbox-gl-js
# - **FIXED: NameError crash by re-adding lines_data_json logic.**
# - **FIXED: Added robust geometry checks to EV station filter to reduce log spam.**
# - Sidebar restructured into 3 categories (Point, Polygon, Polyline)
# - EV Charging Stations now filter spatially and appear correctly.
# - EV Charging Station hover popup now shows detailed properties.
# - NTA layer style updated to transparent fill with black outline
# - Flood Risk layer uses graduated RED ramp based on 'FVI_storm_surge_2050s' (1-5)

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
from shapely.geometry import Point, shape
from shapely.ops import unary_union

# =============================
# CONFIG (keys via st.secrets / env)
# =============================
# Set in .streamlit/secrets.toml:
# MAPBOX_TOKEN = "pk..."
# OSRM_BASE = "https://your-osrm-host/route/v1/driving"  # optional, defaults to public demo
# DEBUG = true  # optional: echoes debug to sidebar

# Simple logger + UI-safe debug helper
logger = logging.getLogger("nyc_laep_mock")
if not logger.handlers:
    logging.basicConfig(level=logging.INFO)

def ui_debug(message: str) -> None:
    """Safe debugging helper."""
    try:
        logger.info(message)
        if bool(st.secrets.get("DEBUG", False)):
            st.sidebar.write(f"🔎 {message}")
    except Exception:
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
FVI_GEOJSON_PATH: str = "fvi.geojson"
EV_STATIONS_GEOJSON_PATH: str = "NYC_EV_Fleet_Station_Network_20251108.geojson" # <-- NEW
TARGET_ROUTE_SECONDS: int = 45 * 60  # 45 minutes
N_ROUTES_PER_BORO: int = 3

# --- UPDATED COLORS for new layers and depot status ---
COLORS: Dict[str, str] = {
    "polygons": "#000000", # Black for NTA lines
    "lines": "#1f77b4",      # Blue for routes
    "ev_stations": "#17becf", # Cyan for EV stations
    # "flood_risk" is a ramp
    # Depot colors
    "depot_fast": "#2ca02c",  # Green
    "depot_medium": "#ff7f0e", # Orange
    "depot_slow": "#d62728",   # Red
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

@st.cache_data(show_spinner=False) # Cache the expensive point generation
def generate_depots_in_ntas(
    ntas_geojson: dict,
    cols: int = 18,
    rows: int = 12,
    bbox: Tuple[float, float, float, float] = NYC_BBOX
) -> List[Tuple[float, float, str, int, int, int, str]]:
    """
    Generates a grid of depots that fall *within* NTA boundaries.
    Returns: List of (lon, lat, borough, existing_kw, needed_kw, gap_kw, speed_category)
    """
    ui_debug("Generating depots within NTA boundaries...")
    out = []
    
    try:
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
                
                if point.within(union_shape):
                    found_boro = "Unknown"
                    for poly, boro in nta_polygons:
                        if point.within(poly):
                            found_boro = boro
                            break
                    
                    # --- Generate new depot data ---
                    existing_kw = random.randint(50, 500)
                    needed_kw = random.randint(existing_kw, 1000) # Needed is always >= existing
                    gap_kw = needed_kw - existing_kw
                    
                    if gap_kw < 250:
                        speed_category = "Fast"
                    elif gap_kw < 500:
                        speed_category = "Medium"
                    else:
                        speed_category = "Slow"
                    # ---------------------------------
                        
                    out.append((lon, lat, found_boro, existing_kw, needed_kw, gap_kw, speed_category))
                    
        ui_debug(f"Generated {len(out)} depots inside NTAs.")
        return out

    except Exception as e:
        ui_debug(f"Error in generate_depots_in_ntas: {e}")
        return []

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

    base_radius_deg = 0.1  # <-- REDUCED radius for shorter 45-min routes
    for attempt in range(1, attempts + 1):
        angle = rand_between(0, 2 * math.pi)
        radius = base_radius_deg * (attempt * 1.2) # Increase radius faster
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
        if abs(res.duration_s - target_s) <= target_s * 0.15: # Loosen tolerance slightly
            break
        if res.duration_s < target_s * 0.5:
            base_radius_deg *= 1.5 # Increase radius faster if routes are too short
    return best


@st.cache_data(show_spinner=False) # Spinner is handled manually
def load_nta_geojson() -> Tuple[dict, str]:
    """Load 2020 NTA GeoJSON from local file. Returns (geojson, status). Status in {loaded, fallback}."""
    try:
        ui_debug(f"NTA.load {NTA2020_GEOJSON_PATH}")
        with open(NTA2020_GEOJSON_PATH, 'r', encoding='utf-8') as f:
            data = json.load(f)
        return data, "loaded"
    except FileNotFoundError:
        ui_debug(f"NTA.file_not_found {NTA2020_GEOJSON_PATH}")
        return NTA_FALLBACK, "fallback"
    except Exception as e:
        ui_debug(f"NTA.exception {e}")
        return NTA_FALLBACK, "fallback"

# --- NEW Function to load FVI GeoJSON ---
@st.cache_data(show_spinner=False)
def load_fvi_geojson() -> Tuple[dict, str]:
    """Load FVI GeoJSON from local file. Returns (geojson, status)."""
    try:
        ui_debug(f"FVI.load {FVI_GEOJSON_PATH}")
        with open(FVI_GEOJSON_PATH, 'r', encoding='utf-8') as f:
            data = json.load(f)
        return data, "loaded"
    except FileNotFoundError:
        ui_debug(f"FVI.file_not_found {FVI_GEOJSON_PATH}")
        return {"type": "FeatureCollection", "features": []}, "fallback (not found)"
    except Exception as e:
        ui_debug(f"FVI.exception {e}")
        return {"type": "FeatureCollection", "features": []}, f"fallback (error: {e})"

# --- NEW Function to load EV Stations GeoJSON ---
@st.cache_data(show_spinner=False)
def load_ev_stations_geojson() -> Tuple[dict, str]:
    """Load EV Stations GeoJSON from local file. Returns (geojson, status)."""
    try:
        ui_debug(f"EV_Stations.load {EV_STATIONS_GEOJSON_PATH}")
        with open(EV_STATIONS_GEOJSON_PATH, 'r', encoding='utf-8') as f:
            data = json.load(f)
        return data, "loaded"
    except FileNotFoundError:
        ui_debug(f"EV_Stations.file_not_found {EV_STATIONS_GEOJSON_PATH}")
        return {"type": "FeatureCollection", "features": []}, "fallback (not found)"
    except Exception as e:
        ui_debug(f"EV_Stations.exception {e}")
        return {"type": "FeatureCollection", "features": []}, f"fallback (error: {e})"


NTA_FALLBACK = {
    "type": "FeatureCollection",
    "features": [
        {
            "type": "Feature",
            "properties": {"ntacode": "MN17", "NTAName": "Midtown-Midtown South", "BoroName": "Manhattan"},
            "geometry": {"type": "Polygon", "coordinates": [[[-73.9985, 40.7636], [-73.9850, 40.7648], [-73.9733, 40.7563], [-73.9786, 40.7480], [-73.9918, 40.7471], [-73.9985, 40.7636]]]}
        },
        # ... (other fallback features) ...
    ]
}

# --- UPDATED HTML/JS TEMPLATE FUNCTION ---
def get_mapbox_html(
    api_key: str,
    map_style: str,
    center_lon: float,
    center_lat: float,
    zoom: int,
    depots_json: str,         # <-- RENAMED
    ev_stations_json: str,  # <-- NEW
    lines_json: str,
    polygons_json: str,
    fvi_json: str,
    colors: Dict[str, str]
) -> str:
    """Generates the HTML for the Mapbox GL JS map."""
    
    # --- NTA Polygon Layer (with hover and new style) ---
    polygon_layers_js = ""
    nta_popup_js = ""
    if polygons_json != 'null':
        polygon_layers_js = f"""
        map.addSource('nta-polygons', {{ 'type': 'geojson', 'data': {polygons_json} }});
        map.addLayer({{
            'id': 'nta-fill', 'type': 'fill', 'source': 'nta-polygons',
            'paint': {{ 
                'fill-color': '{colors["polygons"]}', 
                'fill-opacity': 0.0  // <-- Transparent fill
            }}
        }});
        map.addLayer({{
            'id': 'nta-line', 'type': 'line', 'source': 'nta-polygons',
            'paint': {{ 
                'line-color': '{colors["polygons"]}', // <-- Black outline
                'line-width': 1 
            }}
        }});
        """
        nta_popup_js = f"""
        const ntaPopup = new mapboxgl.Popup({{ 
            closeButton: false, 
            closeOnClick: false,
            anchor: 'bottom-left'
        }});
        map.on('mouseenter', 'nta-fill', (e) => {{
            map.getCanvas().style.cursor = 'pointer';
            const ntaName = e.features[0].properties.NTAName;
            ntaPopup.setLngLat(e.lngLat).setHTML(`<b>${{ntaName}}</b>`).addTo(map);
        }});
        map.on('mouseleave', 'nta-fill', () => {{
            map.getCanvas().style.cursor = '';
            ntaPopup.remove();
        }});
        """

    # --- FVI Polygon Layer (Graduated Colors) ---
    fvi_layers_js = ""
    if fvi_json != 'null':
        fvi_layers_js = f"""
        map.addSource('fvi-polygons', {{ 'type': 'geojson', 'data': {fvi_json} }});
        map.addLayer({{
            'id': 'fvi-fill', 'type': 'fill', 'source': 'fvi-polygons',
            'paint': {{
                'fill-color': [
                    'interpolate',
                    ['linear'],
                    ['to-number', ['coalesce', ['get', 'FVI_storm_surge_2050s'], 0]],
                    0,  'rgba(0, 0, 0, 0)', // 0 = transparent
                    1,  '#fee5d9', // 1 = Lightest Red
                    2,  '#fcbba1',
                    3,  '#fc9272',
                    4,  '#fb6a4a',
                    5,  '#cb181d'  // 5 = Darkest Red
                ],
                'fill-opacity': 0.7 
            }}
        }}); 
        """
        
    # --- Route Line Layer ---
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
    
    # --- Depot Point Layer (Color, Size, and Hover) ---
    depot_layers_js = ""
    depot_popup_js = ""
    if depots_json != 'null':
        depot_layers_js = f"""
        map.addSource('assets', {{ 'type': 'geojson', 'data': {depots_json} }});
        map.addLayer({{
            'id': 'assets-points', 'type': 'circle', 'source': 'assets',
            'paint': {{ 
                'circle-radius': [
                    'interpolate', ['linear'], ['get', 'existing_capacity_kw'],
                    50, 3,  // At 50 kW, radius is 3px
                    500, 10 // At 500 kW, radius is 10px
                ],
                'circle-color': [
                    'match', ['get', 'electrification_speed'],
                    'Fast', '{colors["depot_fast"]}',
                    'Medium', '{colors["depot_medium"]}',
                    'Slow', '{colors["depot_slow"]}',
                    '#ccc' // Default
                ],
                'circle-stroke-width': 1,
                'circle-stroke-color': '#FFFFFF'
            }}
        }});
        """
        depot_popup_js = f"""
        const depotPopup = new mapboxgl.Popup({{ closeButton: false, closeOnClick: false }});
        map.on('mouseenter', 'assets-points', (e) => {{
            map.getCanvas().style.cursor = 'pointer';
            const coordinates = e.features[0].geometry.coordinates.slice();
            const props = e.features[0].properties;
            
            const popupHtml = `
                <b>${{props.name}}</b><br>
                <hr style='margin: 2px 0; border-color: #555;'>
                Electrification: <b>${{props.electrification_speed}}</b><br>
                Existing Capacity: <b>${{props.existing_capacity_kw}} kW</b><br>
                Needed Capacity: <b>${{props.needed_capacity_kw}} kW</b><br>
                Capacity Gap: <b>${{props.capacity_gap_kw}} kW</b>
            `;
            
            depotPopup.setLngLat(coordinates).setHTML(popupHtml).addTo(map);
        }});
        map.on('mouseleave', 'assets-points', () => {{
            map.getCanvas().style.cursor = '';
            depotPopup.remove();
        }});
        """

    # --- NEW EV Station Layer ---
    ev_station_layers_js = ""
    ev_station_popup_js = ""
    if ev_stations_json != 'null':
        ev_station_layers_js = f"""
        map.addSource('ev-stations', {{ 'type': 'geojson', 'data': {ev_stations_json} }});
        map.addLayer({{
            'id': 'ev-stations-points', 'type': 'circle', 'source': 'ev-stations',
            'paint': {{
                'circle-radius': 5,
                'circle-color': '{colors["ev_stations"]}',
                'circle-stroke-width': 1,
                'circle-stroke-color': '#FFFFFF'
            }}
        }});
        """
        # --- UPDATED EV Station Popup ---
        ev_station_popup_js = f"""
        const evPopup = new mapboxgl.Popup({{ closeButton: false, closeOnClick: false }});
        map.on('mouseenter', 'ev-stations-points', (e) => {{
            map.getCanvas().style.cursor = 'pointer';
            const coordinates = e.features[0].geometry.coordinates.slice();
            const props = e.features[0].properties;

            // Use '??' (nullish coalescing) to provide a fallback for missing data
            const stationName = props["STATION NAME"] ?? 'N/A';
            const agency = props["AGENCY"] ?? 'N/A';
            const chargerType = props["TYPE OF CHARGER"] ?? 'N/A';
            const numPlugs = props["NO. OF PLUGS"] ?? 'N/A';

            const popupHtml = `
                <b>${{stationName}}</b><br>
                <hr style='margin: 2px 0; border-color: #555;'>
                Agency: <b>${{agency}}</b><br>
                Charger Type: <b>${{chargerType}}</b><br>
                Plugs: <b>${{numPlugs}}</b>
            `;
            
            evPopup.setLngLat(coordinates).setHTML(popupHtml).addTo(map);
        }});
        map.on('mouseleave', 'ev-stations-points', () => {{
            map.getCanvas().style.cursor = '';
            evPopup.remove();
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
            // Add layers here, order matters for display
            {fvi_layers_js}
            {polygon_layers_js}
            {line_layers_js}
            {depot_layers_js}
            {ev_station_layers_js}
        }});
        
        // Add popup interactivity
        {depot_popup_js}
        {ev_station_popup_js}
        {nta_popup_js}

    </script>
    </body>
    </html>
    """

# =============================
# UI — SIDEBAR CONTROLS
# =============================
st.set_page_config(page_title="NYC LAEP+ Mock", layout="wide")
st.title("NYC School Bus Electrification Planner") 

with st.sidebar:
    # --- RESTRUCTURED SIDEBAR ---
    st.header("Point Layer")
    point_layer_selection = st.radio(
        "Select point data to display:",
        ("Bus Depots", "Existing Charging Stations", "None"),
        index=0,
        label_visibility="collapsed"
    )
    st.caption("Bus Depots show location and capacity gaps. Sized by existing capacity, colored by electrification speed.")

    st.header("Polygon Layers")
    show_polygons = st.checkbox("Neighborhood Boundaries (NTA)", value=True)
    show_flood_zones = st.checkbox("Flood Risk Zones (FVI)", value=True)
    
    st.header("Polyline Layer")
    show_lines = st.checkbox("45-min Routes", value=False)
    # --- END RESTRUCTURE ---

    st.header("Data Filters")
    selected_boros = st.multiselect("Filter by Borough", options=BOROUGHS, default=BOROUGHS)
    
    st.subheader("Depot Filters")
    selected_speeds = st.multiselect(
        "Filter by Electrification Speed", 
        options=["Fast", "Medium", "Slow"], 
        default=["Fast", "Medium", "Slow"]
    )

# Use a fixed seed for consistent route generation
random.seed(42)

# =============================
# DATA — LOADING AND FILTERING
# =============================

# --- Load NTA data first, as it's needed for spatial filtering ---
with st.spinner("Loading NTA boundaries..."):
    ntas_geojson, nta_status = load_nta_geojson()
    
    # *** NEW: Create spatial index of NTA polygons for filtering ***
    nta_polygons_with_boro = []
    if nta_status == 'loaded':
        for feature in ntas_geojson.get("features", []):
            try:
                # *** ADDED CHECK FOR GEOMETRY ***
                if not feature.get("geometry"):
                    # ui_debug("Skipping NTA feature with no geometry for spatial index.")
                    continue
                geom = shape(feature["geometry"])
                boro = feature["properties"].get("BoroName", "Unknown")
                nta_polygons_with_boro.append((geom, boro))
            except Exception as e:
                ui_debug(f"Skipping invalid NTA geom for spatial index: {e}")

with st.spinner("Loading Flood Risk zones..."):
    fvi_geojson, fvi_status = load_fvi_geojson()

with st.spinner("Loading EV Charging Stations..."):
    ev_stations_geojson, ev_stations_status = load_ev_stations_geojson()

with st.spinner("Generating mock bus depots..."):
    depot_data = generate_depots_in_ntas(ntas_geojson)
    # Create the full DataFrame
    points_df = pd.DataFrame(
        depot_data, 
        columns=["lon", "lat", "borough", "existing_capacity_kw", "needed_capacity_kw", "capacity_gap_kw", "electrification_speed"]
    )
    points_df["name"] = [f"School Bus Depot {i+1}" for i in range(len(points_df))]

# --- APPLY FILTERS ---
# 1. Filter Depots by Electrification Speed
if selected_speeds:
    points_df = points_df[points_df["electrification_speed"].isin(selected_speeds)]
else:
    # If no speeds are selected, show no depots
    points_df = pd.DataFrame(columns=points_df.columns) 

# 2. Filter NTA Polygons by Borough
filtered_nta_features = []
for f in ntas_geojson.get("features", []):
    props = f.get("properties", {})
    boro = props.get("boro_name") or props.get("BoroName") or props.get("borough")
    if not selected_boros or (boro in selected_boros):
        filtered_nta_features.append(f)
nta_filtered = {"type": "FeatureCollection", "features": filtered_nta_features}

# 3. Filter FVI Polygons (no filter, just use all)
fvi_filtered = fvi_geojson 

# 4. *** FIXED: Filter EV Stations by Borough (Spatially) ***
filtered_ev_station_features = []
if nta_status == 'loaded' and nta_polygons_with_boro:
    for f in ev_stations_geojson.get("features", []):
        try:
            # *** ADDED ROBUST GEOMETRY CHECKS TO FIX LOG SPAM ***
            if not f.get("geometry"):
                # ui_debug("Skipping EV station (no 'geometry' key).")
                continue
            
            geom_data = f.get("geometry")
            if not geom_data or not geom_data.get("coordinates"):
                # ui_debug("Skipping EV station (missing 'coordinates').")
                continue

            coords = geom_data.get("coordinates")
            if not isinstance(coords, list) or len(coords) < 2:
                # ui_debug("Skipping EV station (invalid 'coordinates' array).")
                continue
            
            # Check for non-numeric coordinates
            if not (isinstance(coords[0], (int, float)) and isinstance(coords[1], (int, float))):
                # ui_debug("Skipping EV station (non-numeric coordinates).")
                continue
            
            # Now safe to try shape()
            station_geom = shape(geom_data)
            if not station_geom or station_geom.is_empty:
                # ui_debug("Skipping EV station (empty/invalid geometry from shapely).")
                continue
            
            station_point = station_geom.centroid 
            # *** END ROBUST CHECKS ***
            
            found_boro = "Unknown"
            for poly, boro in nta_polygons_with_boro:
                if station_point.within(poly):
                    found_boro = boro
                    break
            
            # Now apply the borough filter
            if not selected_boros or (found_boro in selected_boros):
                filtered_ev_station_features.append(f)
        
        except Exception as e:
            # This will now catch *only* truly unexpected errors
            ui_debug(f"Skipping invalid EV station feature during spatial filter: {e}")
else:
    # Fallback if NTAs didn't load: use the old (likely failing) property filter
    ui_debug("NTA spatial index not available. Falling back to property filter for EV stations.")
    for f in ev_stations_geojson.get("features", []):
        props = f.get("properties", {})
        boro = props.get("boro") or props.get("BoroName") or props.get("borough")
        if not selected_boros or (boro in selected_boros):
            filtered_ev_station_features.append(f)
            
ev_stations_filtered = {"type": "FeatureCollection", "features": filtered_ev_station_features}


# --- UPDATED Route Generation (Per Borough) ---
routes: List[RouteResult] = []
if show_lines and len(selected_boros) > 0:
    # Routes generate from *depots*, regardless of which point layer is shown
    candidate_depots = points_df[points_df["borough"].isin(selected_boros)]
    
    with st.status(f"Searching for {N_ROUTES_PER_BORO} routes per borough...", expanded=False) as status:
        total_routes_found = 0
        for boro in selected_boros:
            status.update(label=f"Searching for routes in {boro}...")
            boro_depots = candidate_depots[candidate_depots["borough"] == boro]
            
            if boro_depots.empty:
                ui_debug(f"No depots in {boro} to start routes from.")
                continue

            origins = [tuple(row) for row in boro_depots[["lon", "lat"]].values]
            random.shuffle(origins)
            
            routes_found_in_boro = 0
            for origin in origins[:N_ROUTES_PER_BORO]:
                res = find_route_near_duration(origin, target_s=TARGET_ROUTE_SECONDS, attempts=7)
                if res:
                    routes.append(res)
                    routes_found_in_boro += 1
                time.sleep(0.1) 
            
            total_routes_found += routes_found_in_boro
            ui_debug(f"Found {routes_found_in_boro} routes for {boro}")

        status.update(label=f"Found {total_routes_found} routes total.", state="complete")

# =============================
# MAP — Mapbox GL JS / components.html
# =============================

# --- Prepare data for Mapbox GL JS ---
polygons_data_json = 'null'
if show_polygons and len(nta_filtered.get("features", [])):
    polygons_data_json = json.dumps(nta_filtered)

fvi_data_json = 'null'
if show_flood_zones and len(fvi_filtered.get("features", [])):
    fvi_data_json = json.dumps(fvi_filtered)

# *** THIS BLOCK IS THE FIX for NameError ***
lines_data_json = 'null'
if show_lines and routes:
    line_features = []
    for r in routes:
        line_features.append({
            "type": "Feature", "geometry": {"type": "LineString", "coordinates": r.coordinates},
            "properties": {"name": f"~{round(r.duration_s/60)} min route"}
        })
    lines_data_json = json.dumps({"type": "FeatureCollection", "features": line_features})
# *** END FIX ***

# --- Handle Point Layer Selection ---
depots_data_json = 'null'
ev_stations_data_json = 'null'

if point_layer_selection == "Bus Depots":
    # We use the already-filtered points_df
    point_features = []
    for _, row in points_df.iterrows():
        point_features.append({
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [row["lon"], row["lat"]]},
            "properties": {
                "name": row["name"],
                "borough": row["borough"],
                "existing_capacity_kw": row["existing_capacity_kw"],
                "needed_capacity_kw": row["needed_capacity_kw"],
                "capacity_gap_kw": row["capacity_gap_kw"],
                "electrification_speed": row["electrification_speed"]
            }
        })
    depots_data_json = json.dumps({"type": "FeatureCollection", "features": point_features})

elif point_layer_selection == "Existing Charging Stations":
    # We use the already-filtered ev_stations_filtered
    ev_stations_data_json = json.dumps(ev_stations_filtered)


# --- Render map using components.html ---
map_html = get_mapbox_html(
    api_key=MAPBOX_TOKEN,
    map_style="mapbox://styles/mapbox/standard",
    center_lon=DEFAULT_CENTER[0],
    center_lat=DEFAULT_CENTER[1],
    zoom=10,
    depots_json=depots_data_json,
    ev_stations_json=ev_stations_data_json,
    lines_json=lines_data_json, # <-- This variable is now defined
    polygons_json=polygons_data_json,
    fvi_json=fvi_data_json, 
    colors=COLORS
)
components.html(map_html, height=800, scrolling=False)


# =============================
# DEBUG OUTPUTS
# =============================
st.subheader("Debug & Status")
status_table = pd.DataFrame([
    {"key": "nta_status", "value": f"{nta_status} ({len(nta_filtered.get('features', []))} features)"},
    {"key":"fvi_status", "value": f"{fvi_status} ({len(fvi_filtered.get('features', []))} features)"},
    {"key":"ev_station_status", "value": f"{ev_stations_status} ({len(ev_stations_filtered.get('features', []))} features shown)"}, # <-- NEW
    {"key": "depots_shown", "value": f"{str(len(points_df))} (after filters)"}, 
    {"key": "routes_found", "value": f"{str(len(routes))}"},
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
    st.caption(f"Route diagnostics (target = {TARGET_ROUTE_SECONDS/60} minutes)")
    st.dataframe(diag, use_container_width=True)

# =============================
# SMOKE TESTS
# =============================
with st.expander("Run smoke tests"):
    tests = []
    tests.append({"test": "Points present", "pass": len(points_df) > 0})
    tests.append({"test": "NTA features present", "pass": len(nta_filtered.get('features', [])) > 0})
    tests.append({"test": "NTA using fallback", "pass": nta_status != "fallback"})
    tests.append({"test": "FVI features present", "pass": len(fvi_filtered.get('features', [])) > 0})
    tests.append({"test": "FVI using fallback", "pass": not "fallback" in fvi_status})
    tests.append({"test": "EV Stations present", "pass": len(ev_stations_filtered.get('features', [])) > 0})
    tests.append({"test": "EV Stations using fallback", "pass": not "fallback" in ev_stations_status})
    
    if show_lines and routes:
        tests.append({"test": "Routes found", "pass": len(routes) > 0})
        
    st.table(pd.DataFrame(tests))

# =============================
# NOTES
# =============================
st.markdown("""
- This app now uses `streamlit.components.v1.html` and `mapbox-gl-js` to render the map.
- `folium` and `streamlit-folium` are no longer used.
- All layers are supported with a new sidebar structure.
- App is set to find 3 45-minute routes per selected borough.
- OSRM_BASE can be overridden in `st.secrets` for a private OSRM server.
- Public OSRM is rate-limited; for reliability, run your own OSRM backend.
""")
