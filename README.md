# NYC School Bus Electrification Planner (JS Native)

This is a JavaScript-native version of the NYC School Bus Electrification Planner, built with **React**, **Vite**, **Mapbox GL JS**, and **Turf.js**. It is optimized for deployment on GitHub Pages.

## Features
- **Interactive Map**: Visualize bus depots, EV charging stations, neighborhood boundaries, and flood risk zones.
- **Spatial Logic**: Uses Turf.js for client-side spatial filtering and depot generation within NTA boundaries.
- **Dynamic Routing**: Integrates with OSRM (Project OSRM) for generating sample 45-minute routes.
- **Premium UI**: Modern sidebar with Lucide-react icons and glassmorphism-inspired design.

## Setup

1. **Install Dependencies**:
   ```bash
   npm install
   ```

2. **Mapbox Token**:
   Create a `.env` file in the root directory and add your Mapbox token:
   ```env
   VITE_MAPBOX_TOKEN=your_mapbox_token_here
   ```

3. **Development**:
   ```bash
   npm run dev
   ```

4. **Production Build**:
   ```bash
   npm run build
   ```

## Deployment

The app is configured for GitHub Pages. 
1. The `vite.config.js` uses the base path `/st_esb_planner/`.
2. A GitHub Action is included in `.github/workflows/deploy.yml` to automatically build and deploy to the `gh-pages` branch on every push to `main` or `js-native`.

## Legacy Python Version
The original Streamlit application is preserved in `streamlit_app.py`.
