import React, { useState, useEffect, useRef, useMemo } from 'react';
import mapboxgl from 'mapbox-gl';
import * as turf from '@turf/turf';
import { Layers, Map as MapIcon, Info, Loader2, Filter, Navigation } from 'lucide-react';

const BOROUGHS = ["Manhattan", "Brooklyn", "Queens", "Bronx", "Staten Island"];
const NYC_BBOX = [-74.25559, 40.49612, -73.70001, 40.91553];
const DEFAULT_CENTER = [-73.95, 40.72];
const TARGET_ROUTE_SECONDS = 45 * 60;
const N_ROUTES_PER_BORO = 3;

const COLORS = {
    polygons: "#000000",
    lines: "#1f77b4",
    ev_stations: "#17becf",
    depot_fast: "#2ca02c",
    depot_medium: "#ff7f0e",
    depot_slow: "#d62728",
};

// Set token from env or placeholder
// Note: VITE_ prefixed env variables are public in the build
mapboxgl.accessToken = import.meta.env.VITE_MAPBOX_TOKEN || 'YOUR_MAPBOX_TOKEN_HERE';

const App = () => {
    const mapContainer = useRef(null);
    const map = useRef(null);
    const [loading, setLoading] = useState(true);
    const [status, setStatus] = useState({
        nta: 'loading...',
        fvi: 'loading...',
        ev: 'loading...',
        depots: 0,
        routes: 0
    });

    // Filter State
    const [pointLayer, setPointLayer] = useState("Bus Depots");
    const [showPolygons, setShowPolygons] = useState(true);
    const [showFloodZones, setShowFloodZones] = useState(true);
    const [showLines, setShowLines] = useState(false);
    const [selectedBoros, setSelectedBoros] = useState(BOROUGHS);
    const [selectedSpeeds, setSelectedSpeeds] = useState(["Fast", "Medium", "Slow"]);

    // Data State
    const [geoData, setGeoData] = useState({
        nta: null,
        fvi: null,
        ev: null,
        depots: null,
        routes: null
    });

    // Fetch Data
    useEffect(() => {
        const fetchData = async () => {
            try {
                const [ntaRes, fviRes, evRes] = await Promise.all([
                    fetch('./data/NYC_Neighborhood_Tabulation_Areas_2020_-2131974656277759428.geojson').then(r => r.json()),
                    fetch('./data/fvi.geojson').then(r => r.json()),
                    fetch('./data/NYC_EV_Fleet_Station_Network_20251108.geojson').then(r => r.json())
                ]);

                const depots = generateDepots(ntaRes);

                setGeoData(prev => ({
                    ...prev,
                    nta: ntaRes,
                    fvi: fviRes,
                    ev: evRes,
                    depots: depots
                }));

                setStatus(s => ({
                    ...s,
                    nta: `${ntaRes.features.length} features`,
                    fvi: `${fviRes.features.length} features`,
                    ev: `${evRes.features.length} features`,
                    depots: depots.features.length
                }));

                setLoading(false);
            } catch (err) {
                console.error("Error loading GeoJSON data:", err);
                setLoading(false);
            }
        };

        fetchData();
    }, []);

    // Generate Routes when showLines changes
    useEffect(() => {
        if (showLines && geoData.depots && !geoData.routes) {
            generateRoutes();
        }
    }, [showLines, geoData.depots]);

    const generateDepots = (ntaGeojson) => {
        const cols = 18;
        const rows = 12;
        const [minx, miny, maxx, maxy] = NYC_BBOX;
        const dx = (maxx - minx) / (cols - 1);
        const dy = (maxy - miny) / (rows - 1);
        const features = [];

        // Pre-filter NTAs with valid geometry
        const validNtas = ntaGeojson.features.filter(f => f.geometry);

        for (let r = 0; r < rows; r++) {
            for (let c = 0; c < cols; c++) {
                const jx = (Math.random() - 0.5) * dx * 0.4;
                const jy = (Math.random() - 0.5) * dy * 0.4;
                const lon = minx + c * dx + jx;
                const lat = miny + r * dy + jy;
                const point = turf.point([lon, lat]);

                // Find which NTA this point is in
                let boro = null;
                for (const feature of validNtas) {
                    if (turf.booleanPointInPolygon(point, feature)) {
                        boro = feature.properties.BoroName || feature.properties.boro_name || "Unknown";
                        break;
                    }
                }

                if (boro) {
                    const existingKw = Math.floor(Math.random() * 450) + 50;
                    const neededKw = Math.floor(Math.random() * 500) + existingKw;
                    const gapKw = neededKw - existingKw;
                    let speed = "Slow";
                    if (gapKw < 250) speed = "Fast";
                    else if (gapKw < 500) speed = "Medium";

                    features.push(turf.point([lon, lat], {
                        id: features.length + 1,
                        name: `School Bus Depot ${features.length + 1}`,
                        borough: boro,
                        existing_capacity_kw: existingKw,
                        needed_capacity_kw: neededKw,
                        capacity_gap_kw: gapKw,
                        electrification_speed: speed
                    }));
                }
            }
        }
        return turf.featureCollection(features);
    };

    const generateRoutes = async () => {
        setLoading(true);
        const routes = [];
        const depotsByBoro = {};

        geoData.depots.features.forEach(f => {
            const b = f.properties.borough;
            if (!depotsByBoro[b]) depotsByBoro[b] = [];
            depotsByBoro[b].push(f);
        });

        for (const boro of BOROUGHS) {
            const boroDepots = depotsByBoro[boro];
            if (!boroDepots) continue;

            // Shuffle depots
            const shuffled = [...boroDepots].sort(() => 0.5 - Math.random());
            let count = 0;
            for (const depot of shuffled) {
                if (count >= N_ROUTES_PER_BORO) break;
                const route = await findRouteNearDuration(depot.geometry.coordinates);
                if (route) {
                    routes.push(route);
                    count++;
                }
                // Small delay to avoid hammering OSRM public API if using it
                await new Promise(r => setTimeout(r, 100));
            }
        }

        setGeoData(prev => ({ ...prev, routes: turf.featureCollection(routes) }));
        setStatus(s => ({ ...s, routes: routes.length }));
        setLoading(false);
    };

    const findRouteNearDuration = async (origin) => {
        const baseRadius = 0.05;
        let best = null;
        const [minx, miny, maxx, maxy] = NYC_BBOX;

        for (let i = 1; i <= 5; i++) {
            const angle = Math.random() * 2 * Math.PI;
            const radius = baseRadius * i * 1.5;
            const dest = [
                Math.max(minx, Math.min(maxx, origin[0] + Math.cos(angle) * radius)),
                Math.max(miny, Math.min(maxy, origin[1] + Math.sin(angle) * radius))
            ];

            const res = await fetchRoute(origin, dest);
            if (!res) continue;

            if (!best || Math.abs(res.properties.duration - TARGET_ROUTE_SECONDS) < Math.abs(best.properties.duration - TARGET_ROUTE_SECONDS)) {
                best = res;
            }

            if (Math.abs(res.properties.duration - TARGET_ROUTE_SECONDS) < TARGET_ROUTE_SECONDS * 0.2) break;
        }
        return best;
    };

    const fetchRoute = async (origin, dest) => {
        const url = `https://router.project-osrm.org/route/v1/driving/${origin[0]},${origin[1]};${dest[0]},${dest[1]}?overview=full&geometries=geojson`;
        try {
            const resp = await fetch(url);
            const data = await resp.json();
            if (data.code !== 'Ok' || !data.routes || data.routes.length === 0) return null;
            const route = data.routes[0];
            return turf.feature(route.geometry, {
                duration: route.duration,
                distance: route.distance,
                name: `~${Math.round(route.duration / 60)} min route`
            });
        } catch (e) {
            return null;
        }
    };

    // Filtered Data
    const filteredData = useMemo(() => {
        if (!geoData.nta) return {};

        const filteredNta = {
            ...geoData.nta,
            features: geoData.nta.features.filter(f => selectedBoros.includes(f.properties.BoroName || f.properties.boro_name))
        };

        let filteredDepots = { type: 'FeatureCollection', features: [] };
        if (geoData.depots && pointLayer === "Bus Depots") {
            filteredDepots = {
                ...geoData.depots,
                features: geoData.depots.features.filter(f =>
                    selectedBoros.includes(f.properties.borough) &&
                    selectedSpeeds.includes(f.properties.electrification_speed)
                )
            };
        }

        let filteredEv = { type: 'FeatureCollection', features: [] };
        if (geoData.ev && pointLayer === "Existing Charging Stations") {
            // Spatial filter for EV stations if needed, or property filter
            filteredEv = {
                ...geoData.ev,
                features: geoData.ev.features.filter(f => {
                    // This is a simplified check - in the real app we might want spatial check
                    // For now, let's assume 'borough' property exists or fallback
                    const b = f.properties.borough || f.properties.BoroName || "Unknown";
                    return selectedBoros.includes(b) || selectedBoros.length === BOROUGHS.length;
                })
            };
        }

        let filteredRoutes = { type: 'FeatureCollection', features: [] };
        if (geoData.routes) {
            // Filter routes based on origins? For now just show if toggle is on
            filteredRoutes = geoData.routes;
        }

        return {
            nta: filteredNta,
            fvi: geoData.fvi,
            depots: filteredDepots,
            ev: filteredEv,
            routes: filteredRoutes
        };
    }, [geoData, selectedBoros, selectedSpeeds, pointLayer]);

    // Init Map
    useEffect(() => {
        if (map.current) return;
        map.current = new mapboxgl.Map({
            container: mapContainer.current,
            style: 'mapbox://styles/mapbox/dark-v11',
            center: DEFAULT_CENTER,
            zoom: 10,
            pitch: 45,
            bearing: -17.6
        });

        map.current.on('load', () => {
            // Initial Sources
            map.current.addSource('nta', { type: 'geojson', data: turf.featureCollection([]) });
            map.current.addSource('fvi', { type: 'geojson', data: turf.featureCollection([]) });
            map.current.addSource('depots', { type: 'geojson', data: turf.featureCollection([]) });
            map.current.addSource('ev', { type: 'geojson', data: turf.featureCollection([]) });
            map.current.addSource('routes', { type: 'geojson', data: turf.featureCollection([]) });

            // Layers
            map.current.addLayer({
                id: 'fvi-layer',
                type: 'fill',
                source: 'fvi',
                paint: {
                    'fill-color': [
                        'interpolate', ['linear'], ['to-number', ['coalesce', ['get', 'FVI_storm_surge_2050s'], 0]],
                        0, 'rgba(0,0,0,0)',
                        1, '#fee5d9',
                        2, '#fcbba1',
                        3, '#fc9272',
                        4, '#fb6a4a',
                        5, '#cb181d'
                    ],
                    'fill-opacity': 0.6
                }
            });

            map.current.addLayer({
                id: 'nta-line',
                type: 'line',
                source: 'nta',
                paint: { 'line-color': COLORS.polygons, 'line-width': 1 }
            });

            map.current.addLayer({
                id: 'route-line',
                type: 'line',
                source: 'routes',
                paint: { 'line-color': COLORS.lines, 'line-width': 3 }
            });

            map.current.addLayer({
                id: 'depots-point',
                type: 'circle',
                source: 'depots',
                paint: {
                    'circle-radius': ['interpolate', ['linear'], ['get', 'existing_capacity_kw'], 50, 4, 500, 12],
                    'circle-color': [
                        'match', ['get', 'electrification_speed'],
                        'Fast', COLORS.depot_fast,
                        'Medium', COLORS.depot_medium,
                        'Slow', COLORS.depot_slow,
                        '#ccc'
                    ],
                    'circle-stroke-width': 1,
                    'circle-stroke-color': '#fff'
                }
            });

            map.current.addLayer({
                id: 'ev-point',
                type: 'circle',
                source: 'ev',
                paint: {
                    'circle-radius': 5,
                    'circle-color': COLORS.ev_stations,
                    'circle-stroke-width': 1,
                    'circle-stroke-color': '#fff'
                }
            });

            // Popups
            const popup = new mapboxgl.Popup({ closeButton: false, closeOnClick: false });

            const handleMouseEnter = (e, type) => {
                map.current.getCanvas().style.cursor = 'pointer';
                const props = e.features[0].properties;
                const coords = e.features[0].geometry.coordinates;

                let html = '';
                if (type === 'depot') {
                    html = `<b>${props.name}</b><br><hr>Speed: ${props.electrification_speed}<br>Gap: ${props.capacity_gap_kw} kW`;
                } else if (type === 'ev') {
                    html = `<b>${props['STATION NAME'] || 'EV Station'}</b><br><hr>Type: ${props['TYPE OF CHARGER']}<br>Plugs: ${props['NO. OF PLUGS']}`;
                } else if (type === 'nta') {
                    html = `<b>${props.NTAName}</b>`;
                }

                popup.setLngLat(coords).setHTML(html).addTo(map.current);
            };

            map.current.on('mouseenter', 'depots-point', (e) => handleMouseEnter(e, 'depot'));
            map.current.on('mouseenter', 'ev-point', (e) => handleMouseEnter(e, 'ev'));
            map.current.on('mouseenter', 'nta-line', (e) => handleMouseEnter(e, 'nta'));

            const handleMouseLeave = () => {
                map.current.getCanvas().style.cursor = '';
                popup.remove();
            };

            map.current.on('mouseleave', 'depots-point', handleMouseLeave);
            map.current.on('mouseleave', 'ev-point', handleMouseLeave);
            map.current.on('mouseleave', 'nta-line', handleMouseLeave);
        });
    }, []);

    // Update Map Sources
    useEffect(() => {
        if (!map.current || !map.current.isStyleLoaded()) return;

        map.current.getSource('nta').setData(showPolygons ? filteredData.nta : turf.featureCollection([]));
        map.current.getSource('fvi').setData(showFloodZones ? filteredData.fvi : turf.featureCollection([]));
        map.current.getSource('depots').setData(pointLayer === "Bus Depots" ? filteredData.depots : turf.featureCollection([]));
        map.current.getSource('ev').setData(pointLayer === "Existing Charging Stations" ? filteredData.ev : turf.featureCollection([]));
        map.current.getSource('routes').setData(showLines ? filteredData.routes : turf.featureCollection([]));
    }, [filteredData, showPolygons, showFloodZones, showLines, pointLayer]);

    const toggleBoro = (boro) => {
        setSelectedBoros(prev =>
            prev.includes(boro) ? prev.filter(b => b !== boro) : [...prev, boro]
        );
    };

    const toggleSpeed = (speed) => {
        setSelectedSpeeds(prev =>
            prev.includes(speed) ? prev.filter(s => s !== speed) : [...prev, speed]
        );
    };

    return (
        <>
            <div className="sidebar">
                <h1>NYC School Bus Electrification</h1>

                <div className="section">
                    <div className="section-title"><Layers size={14} style={{ marginRight: 8 }} /> Point Layer</div>
                    <div className="control-group">
                        {["Bus Depots", "Existing Charging Stations", "None"].map(option => (
                            <label key={option} className="checkbox-item">
                                <input
                                    type="radio"
                                    name="pointLayer"
                                    checked={pointLayer === option}
                                    onChange={() => setPointLayer(option)}
                                />
                                {option}
                            </label>
                        ))}
                    </div>
                </div>

                <div className="section">
                    <div className="section-title"><MapIcon size={14} style={{ marginRight: 8 }} /> Polygon Layers</div>
                    <div className="control-group">
                        <label className="checkbox-item">
                            <input type="checkbox" checked={showPolygons} onChange={() => setShowPolygons(!showPolygons)} />
                            NTAs (Neighborhoods)
                        </label>
                        <label className="checkbox-item">
                            <input type="checkbox" checked={showFloodZones} onChange={() => setShowFloodZones(!showFloodZones)} />
                            Flood Risk Zones (FVI)
                        </label>
                    </div>
                </div>

                <div className="section">
                    <div className="section-title"><Navigation size={14} style={{ marginRight: 8 }} /> Polyline Layer</div>
                    <div className="control-group">
                        <label className="checkbox-item">
                            <input type="checkbox" checked={showLines} onChange={() => setShowLines(!showLines)} />
                            Generate 45-min Routes
                        </label>
                    </div>
                </div>

                <div className="section">
                    <div className="section-title"><Filter size={14} style={{ marginRight: 8 }} /> Geographical Filter</div>
                    <div className="control-group" style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '5px' }}>
                        {BOROUGHS.map(boro => (
                            <label key={boro} className="checkbox-item" style={{ fontSize: '0.8rem' }}>
                                <input type="checkbox" checked={selectedBoros.includes(boro)} onChange={() => toggleBoro(boro)} />
                                {boro}
                            </label>
                        ))}
                    </div>
                </div>

                <div className="section">
                    <div className="section-title">Depot Filter (Speed)</div>
                    <div className="control-group">
                        {["Fast", "Medium", "Slow"].map(speed => (
                            <label key={speed} className="checkbox-item">
                                <input type="checkbox" checked={selectedSpeeds.includes(speed)} onChange={() => toggleSpeed(speed)} />
                                {speed}
                            </label>
                        ))}
                    </div>
                </div>

                <div className="debug-panel">
                    <Info size={12} style={{ marginRight: 4 }} /> Status
                    <div style={{ marginTop: 8, fontSize: '0.75rem' }}>
                        <div>NTAs: {status.nta}</div>
                        <div>Flood Zones: {status.fvi}</div>
                        <div>EV Stations: {status.ev}</div>
                        <div>Depots Generated: {status.depots}</div>
                        <div>Routes Generated: {status.routes}</div>
                    </div>
                </div>
            </div>

            <div className="map-container" ref={mapContainer}>
                {loading && (
                    <div className="loading-overlay">
                        <Loader2 className="animate-spin" style={{ marginRight: 8 }} />
                        Loading Spatial Infrastructure...
                    </div>
                )}
            </div>
        </>
    );
};

export default App;
