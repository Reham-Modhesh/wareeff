# WAREEF | وَريف
### Smart GIS Platform for Sustainable Urban Greening

WAREEF is a GIS-based platform designed to support sustainable urban planning and tree-planting decisions through spatial data, environmental analysis, and interactive visualization.

The platform helps identify streets with high planting priority, evaluate site suitability, recommend suitable tree species, and estimate the potential costs and water requirements of urban greening initiatives.

## Project Overview

WAREEF brings together geospatial and environmental data to support more informed urban greening decisions.

The platform focuses on answering four key questions:

- **Where should we plant?** Identify and prioritize streets and locations for urban greening.
- **What should we plant?** Explore suitable tree species based on site and environmental characteristics.
- **What will it cost?** Estimate planting costs and water requirements.
- **What could the outcome look like?** Visualize proposed planting scenarios through interactive maps and digital twin representations.

## Key Features

- **GIS-Based Exploration:** Interactive maps for exploring districts, streets, and spatial information.
- **Strategic Planning:** Street prioritization based on vegetation coverage, proximity to public facilities, commercial activity, and planting capacity.
- **Site Analysis:** Evaluation of soil and environmental characteristics.
- **Tree Recommendations:** Ranking of suitable tree species based on site characteristics.
- **Cost and Water Estimation:** Estimation of planting costs and water requirements.
- **Scenario Simulation:** Comparison of alternative planting scenarios.
- **Digital Twin Visualization:** Visual representation of proposed planting locations and expected changes over time.
- **Tree Library:** A catalog of tree species and their characteristics.

## Technology Stack

| Area | Technologies |
|---|---|
| Frontend | HTML, CSS, JavaScript |
| GIS & Mapping | Leaflet, OpenStreetMap, Esri Maps |
| Database | SQLite |
| Data Formats | JSON, GeoJSON |
| Environmental Data | SoilGrids, Open-Meteo |
| Geospatial Data | OpenStreetMap, Sentinel-2 |

## Project Structure

```text
wareeff/
│
├── frontend/
│   ├── index.html
│   ├── css/
│   │   └── style.css
│   └── js/
│       └── app.js
│
├── backend/
│   └── database/
│       └── wareef.db
│
├── data/
│   ├── wareef_compact.json
│   ├── tree_sites.json
│   └── README.md
│
└── README.md
```

### Directory Description

| Directory | Description |
|---|---|
| `frontend/` | Main application interface, styling, and JavaScript functionality. |
| `backend/database/` | SQLite database containing the project's structured data. |
| `data/` | Supporting JSON datasets and database documentation. |

**Note:** The `backend/database/` directory currently stores the SQLite database. A separate backend application or API is not currently included in this repository.

## Data Overview

The project database contains structured information covering:

- Districts and street networks.
- Street geometries and spatial points.
- Soil properties across multiple depths.
- Tree species and site suitability rules.
- Climate records.
- Strategic street-priority information.
- Points of interest, including schools and mosques.
- Data sources and metadata.

The supporting datasets are documented in `data/README.md`.

## Data Sources

WAREEF uses data from several sources, including:

- **SoilGrids (ISRIC):** Soil property estimates.
- **OpenStreetMap:** Street networks and geographic features.
- **Overture Maps:** Supporting geographic and points-of-interest data.
- **Microsoft Building Footprints:** Building geometry data.
- **Sentinel-2:** Satellite imagery and vegetation analysis.
- **Open-Meteo:** Weather and climate information.

Data availability and coverage vary by source and location.

## Important Data Limitations

- SoilGrids values are model-based estimates at approximately 250 m resolution, not on-site laboratory measurements.
- Some street and sidewalk dimensions are estimated and require field verification.
- Soil salinity measurements (EC) are not available in the current SoilGrids dataset.
- Climate data may not represent conditions at every individual street.
- Water availability and existing tree inventories are not fully represented in the current datasets.
- Estimated planting costs and environmental impacts depend on the assumptions and input data used.

These limitations should be considered when interpreting recommendations and planning real-world implementation.

## Getting Started

The current frontend is a static HTML, CSS, and JavaScript application.

To run it locally, use Python's built-in HTTP server:

```bash
cd frontend
python3 -m http.server 8000
```

Then open:

```text
http://localhost:8000
```

Keep the terminal session running while using the application.

The application relies on external services for certain map tiles, fonts, and environmental data, so an internet connection may be required for those features.

## Project Status

WAREEF currently includes:

- An interactive GIS-based frontend.
- Street exploration and strategic prioritization.
- Soil and environmental data integration.
- Tree species catalog and recommendation functionality.
- Cost and water estimation.
- Scenario comparison and digital twin visualization.
- A structured SQLite database and supporting datasets.

This repository organizes the existing WAREEF project files. It does not represent a separate backend implementation.

## Vision

WAREEF aims to support data-driven urban greening and sustainable city planning by helping decision-makers identify suitable planting locations, evaluate environmental conditions, and compare planting strategies before implementation.

**From identifying where to plant, to understanding what to plant and why.**

---

**WAREEF | وَريف**  
Smart GIS for Sustainable Urban Greening.