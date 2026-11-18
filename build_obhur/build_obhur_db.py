#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Wareef - Obhur Al-Janubiyah (Jeddah) street soil + climate database.

Same method as the Wareef Riyadh database (wareef.db):
  district boundary -> OSM streets -> a point every 10 m on each street
  -> SoilGrids 250 m cell under each point (13 properties x 5 depths to 100 cm,
     mean / Q05 / Q50 / Q95 / uncertainty)
  -> if the point's own cell is masked (built-up / sea), use the nearest valid
     cell by true ground distance and record that distance
  -> daily climate 2021-2025 from Open-Meteo (ERA5 archive).

Only the Python standard library is needed. Run:

    python build_obhur_db.py

Outputs (in ./obhur_output by default):
    obhur_south.db               SQLite database (same tables/views as wareef.db)
    obhur_points_soil.csv        one row per point, all 65 soil means
    obhur_streets.json           points grouped under each street name + soil cells
    obhur_climate_daily.csv      daily climate 2021-2025
    summary.txt                  counts (streets, points, cells ...)
    cache/                       raw downloads (OSM JSON, SoilGrids GeoTIFFs, climate JSON)
"""

import argparse
import concurrent.futures as cf
import csv
import datetime as dt
import hashlib
import json
import math
import os
import sqlite3
import ssl
import struct
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# --------------------------------------------------------------------------
# Settings
# --------------------------------------------------------------------------
DISTRICT_KEY = "obhur_south"
DISTRICT_ID = "jeddah_obhur_south"
DISTRICT_NAME_AR = "أبحر الجنوبية"
DISTRICT_NAME_EN = "Obhur Al Janubiyah"
POINT_PREFIX = "O"

# Names the district may carry in OSM / Nominatim
NAME_REGEX = ("أبحر الجنوبية|ابحر الجنوبية|أبحر الجنوبي|ابحر الجنوبي|"
              "Obhur Al[- ]?Janubiyah|Obhur Al[- ]?Janoubia|Obhur Al[- ]?Janubia|"
              "Abhur Al[- ]?Janubiyah|Abhur Al[- ]?Janubia|South Obhur|Southern Obhur|Obhur South")
JEDDAH_BBOX = (21.30, 39.00, 21.95, 39.35)   # south, west, north, east

HIGHWAYS = ["motorway", "motorway_link", "trunk", "trunk_link", "primary", "primary_link",
            "secondary", "secondary_link", "tertiary", "tertiary_link", "unclassified",
            "residential", "living_street", "service", "pedestrian"]

POINT_SPACING_M = 10.0
MIN_GAP_TO_VERTEX_M = 3.0      # no interpolated point closer than this to the next OSM vertex

OVERPASS_URLS = ["https://overpass-api.de/api/interpreter",
                 "https://overpass.kumi.systems/api/interpreter",
                 "https://overpass.private.coffee/api/interpreter"]
NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
WCS_BASE = "https://maps.isric.org/mapserv"
OPEN_METEO_URL = "https://archive-api.open-meteo.com/v1/archive"
USER_AGENT = "Wareef-urban-greening-research/1.0 (University of Jeddah student project)"

IGH_A = 6378137.0       # EPSG:152160 (+proj=igh +datum=WGS84), sphere radius = WGS84 a
CELL = 250.0            # SoilGrids native resolution (m)
NODATA = -32768

SOIL_PROPERTIES = [  # code, name_ar, name_en, unit, raw divisor
    ("bdod", "الكثافة الظاهرية", "Bulk density", "kg/dm³", 100.0),
    ("cec", "السعة التبادلية الكاتيونية", "Cation exchange capacity", "cmol(c)/kg", 10.0),
    ("cfvo", "الحصى والشظايا الخشنة", "Coarse fragments", "cm³/100cm³ (vol%)", 10.0),
    ("clay", "طين", "Clay", "%", 10.0),
    ("nitrogen", "نيتروجين", "Total nitrogen", "g/kg", 100.0),
    ("ocd", "كثافة الكربون العضوي", "Organic carbon density", "kg/m³", 10.0),
    ("phh2o", "الحموضة pH (ماء)", "pH (H2O)", "pH", 10.0),
    ("sand", "رمل", "Sand", "%", 10.0),
    ("silt", "طمي", "Silt", "%", 10.0),
    ("soc", "كربون عضوي", "Soil organic carbon", "g/kg", 10.0),
    ("wv0010", "ماء محتجز عند 10kPa", "Water content at 10 kPa", "10⁻² cm³/cm³ (vol%)", 10.0),
    ("wv0033", "ماء محتجز عند 33kPa (السعة الحقلية)", "Water content at 33 kPa (field capacity)", "10⁻² cm³/cm³ (vol%)", 10.0),
    ("wv1500", "ماء محتجز عند 1500kPa (نقطة الذبول)", "Water content at 1500 kPa (wilting point)", "10⁻² cm³/cm³ (vol%)", 10.0),
]
SOIL_DEPTHS = [("0-5cm", 0, 5), ("5-15cm", 5, 15), ("15-30cm", 15, 30),
               ("30-60cm", 30, 60), ("60-100cm", 60, 100)]
SOIL_STATS = [("mean", "mean"), ("Q0.05", "q05"), ("Q0.5", "q50"), ("Q0.95", "q95"),
              ("uncertainty", "uncertainty")]
UNCERTAINTY_DIVISOR = 10.0   # ISRIC publishes (Q95-Q05)/Q50 x 10

CLIMATE_START, CLIMATE_END = "2021-01-01", "2025-12-31"


def log(msg):
    print(f"[{dt.datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


# --------------------------------------------------------------------------
# HTTP with retries (replaceable for testing)
# --------------------------------------------------------------------------
def _ssl_context():
    # macOS python.org builds ship without root certificates; use certifi when available.
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


SSL_CTX = _ssl_context()
CERT_HELP = ("\nSSL certificate problem: Python cannot verify HTTPS certificates on this computer.\n"
             "Fix on Mac (run once, then run this script again):\n"
             "   /Applications/Python\\ 3*/Install\\ Certificates.command\n"
             "or:\n"
             "   python3 -m pip install --upgrade certifi\n")


def http_get(url, data=None, timeout=180, tries=5, accept_codes=(200,)):
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, data=data, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=timeout, context=SSL_CTX) as r:
                if r.status in accept_codes:
                    return r.read()
                last = RuntimeError(f"HTTP {r.status}")
        except urllib.error.HTTPError as e:
            last = e
            if e.code in (400, 404):
                raise
        except Exception as e:     # timeouts, resets
            if "CERTIFICATE_VERIFY_FAILED" in str(e):
                sys.exit(CERT_HELP)
            last = e
        time.sleep(min(60, 3 * 2 ** i))
    raise RuntimeError(f"Failed after {tries} tries: {url[:160]} ({last})")


def cached(cache_dir, name, fetch):
    path = os.path.join(cache_dir, name)
    if os.path.exists(path) and os.path.getsize(path) > 0:
        with open(path, "rb") as f:
            return f.read()
    data = fetch()
    tmp = path + ".part"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, path)
    return data


def overpass(query, cache_dir, name):
    def fetch():
        last = None
        for url in OVERPASS_URLS:
            try:
                return http_get(url, data=urllib.parse.urlencode({"data": query}).encode(), tries=3)
            except Exception as e:
                last = e
                log(f"  Overpass server failed ({url}), trying next...")
        raise RuntimeError(f"All Overpass servers failed: {last}")
    return json.loads(cached(cache_dir, name, fetch).decode("utf-8"))


# --------------------------------------------------------------------------
# Geometry helpers
# --------------------------------------------------------------------------
def haversine(lat1, lon1, lat2, lon2):
    """Great-circle distance in metres (sphere R = 6378137 m, as in the Riyadh build)."""
    r = 6378137.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(min(1.0, math.sqrt(h)))


def igh_lobe_lon0(lat, lon):
    # Interrupted Goode Homolosine lobes; Jeddah is in the northern 'Eurasia/Africa' lobe.
    if lat >= 0:
        return -100.0 if lon < -40.0 else 30.0
    if lon < -100.0:
        return -160.0
    if lon < -20.0:
        return -60.0
    if lon < 80.0:
        return 20.0
    return 140.0


def ll_to_igh(lat, lon):
    if abs(lat) > 40.7:
        raise ValueError("Only the sinusoidal zone of IGH (|lat| < 40.7) is implemented")
    l0 = math.radians(igh_lobe_lon0(lat, lon))
    ph, lam = math.radians(lat), math.radians(lon)
    return IGH_A * l0 + IGH_A * (lam - l0) * math.cos(ph), IGH_A * ph


def igh_to_ll(x, y, lon0_deg=30.0):
    ph = y / IGH_A
    l0 = math.radians(lon0_deg)
    lam = l0 + (x - IGH_A * l0) / (IGH_A * math.cos(ph))
    return math.degrees(ph), math.degrees(lam)


def point_in_ring(lat, lon, ring):          # ring: [[lon, lat], ...]
    inside = False
    n = len(ring)
    j = n - 1
    for i in range(n):
        xi, yi = ring[i]
        xj, yj = ring[j]
        if (yi > lat) != (yj > lat):
            x_cross = (xj - xi) * (lat - yi) / (yj - yi) + xi
            if lon < x_cross:
                inside = not inside
        j = i
    return inside


def point_in_geom(lat, lon, geom):
    polys = geom["coordinates"] if geom["type"] == "MultiPolygon" else [geom["coordinates"]]
    for poly in polys:
        if point_in_ring(lat, lon, poly[0]) and not any(point_in_ring(lat, lon, h) for h in poly[1:]):
            return True
    return False


def geom_rings(geom):
    polys = geom["coordinates"] if geom["type"] == "MultiPolygon" else [geom["coordinates"]]
    return [r for p in polys for r in p]


def geom_bbox(geom):
    pts = [c for r in geom_rings(geom) for c in r]
    lons = [p[0] for p in pts]
    lats = [p[1] for p in pts]
    return min(lats), min(lons), max(lats), max(lons)


def ring_area_m2(ring):
    lat0 = sum(p[1] for p in ring) / len(ring)
    k = 111320.0
    xs = [p[0] * k * math.cos(math.radians(lat0)) for p in ring]
    ys = [p[1] * 110574.0 for p in ring]
    return abs(sum(xs[i] * ys[i - 1] - xs[i - 1] * ys[i] for i in range(len(ring)))) / 2


def geom_area_m2(geom):
    polys = geom["coordinates"] if geom["type"] == "MultiPolygon" else [geom["coordinates"]]
    return sum(ring_area_m2(p[0]) - sum(ring_area_m2(h) for h in p[1:]) for p in polys)


def seg_len(a, b):
    return haversine(a[1], a[0], b[1], b[0])


def densify(part):
    """Points along a polyline: every OSM vertex + a point every 10 m from each vertex,
    skipping interpolated points within 3 m of the next vertex (Wareef method).
    Yields (kind, node_id, chainage, lat, lon)."""
    out = []
    chain = 0.0
    for i, (lon, lat, nid) in enumerate(part):
        if i > 0:
            a, b = part[i - 1], part[i]
            L = seg_len(a, b)
            k = 1
            while k * POINT_SPACING_M < L - MIN_GAP_TO_VERTEX_M:
                f = k * POINT_SPACING_M / L
                out.append(("interpolated", None, chain + k * POINT_SPACING_M,
                            a[1] + f * (b[1] - a[1]), a[0] + f * (b[0] - a[0])))
                k += 1
            chain += L
        kind = "osm_vertex" if nid is not None else "interpolated"
        out.append((kind, nid, chain, lat, lon))
    return out


# --------------------------------------------------------------------------
# Boundary
# --------------------------------------------------------------------------
def assemble_rings(ways):
    """Join way geometries (lists of [lon,lat]) into closed rings."""
    segs = [list(w) for w in ways if len(w) >= 2]
    rings = []
    while segs:
        ring = segs.pop(0)
        changed = True
        while ring[0] != ring[-1] and changed:
            changed = False
            for i, s in enumerate(segs):
                if s[0] == ring[-1]:
                    ring += s[1:]
                elif s[-1] == ring[-1]:
                    ring += s[::-1][1:]
                elif s[-1] == ring[0]:
                    ring = s[:-1] + ring
                elif s[0] == ring[0]:
                    ring = s[::-1][:-1] + ring
                else:
                    continue
                segs.pop(i)
                changed = True
                break
        if ring[0] == ring[-1] and len(ring) >= 4:
            rings.append(ring)
    return rings


def element_to_geom(el):
    if el["type"] == "way":
        ring = [[g["lon"], g["lat"]] for g in el.get("geometry", [])]
        if len(ring) >= 4 and ring[0] == ring[-1]:
            return {"type": "Polygon", "coordinates": [ring]}
        return None
    outers = assemble_rings([[[g["lon"], g["lat"]] for g in m.get("geometry", [])]
                             for m in el.get("members", []) if m["type"] == "way" and m.get("role") in ("outer", "")])
    inners = assemble_rings([[[g["lon"], g["lat"]] for g in m.get("geometry", [])]
                             for m in el.get("members", []) if m["type"] == "way" and m.get("role") == "inner"])
    if not outers:
        return None
    polys = [[o] for o in outers]
    for h in inners:
        for p in polys:
            if point_in_ring(h[0][1], h[0][0], p[0]):
                p.append(h)
                break
    if len(polys) == 1:
        return {"type": "Polygon", "coordinates": polys[0]}
    return {"type": "MultiPolygon", "coordinates": polys}


def find_boundary(args, cache_dir):
    if args.boundary:
        with open(args.boundary, encoding="utf-8") as f:
            gj = json.load(f)
        if gj.get("type") == "FeatureCollection":
            gj = gj["features"][0]
        if gj.get("type") == "Feature":
            gj = gj["geometry"]
        log(f"Boundary from file: {args.boundary}")
        return gj, "user_file", DISTRICT_NAME_EN

    s, w, n, e = JEDDAH_BBOX
    q = f"""[out:json][timeout:120];
(
  relation["name"~"{NAME_REGEX}",i]({s},{w},{n},{e});
  relation["name:ar"~"{NAME_REGEX}",i]({s},{w},{n},{e});
  relation["name:en"~"{NAME_REGEX}",i]({s},{w},{n},{e});
  way["name"~"{NAME_REGEX}",i]({s},{w},{n},{e});
  way["name:ar"~"{NAME_REGEX}",i]({s},{w},{n},{e});
  way["name:en"~"{NAME_REGEX}",i]({s},{w},{n},{e});
);
out tags geom;"""
    cands = []
    try:
        res = overpass(q, cache_dir, "osm_boundary_search.json")
        for el in res.get("elements", []):
            t = el.get("tags", {})
            if not (t.get("boundary") or t.get("place") or t.get("admin_level") or t.get("landuse") == "residential"):
                continue
            g = element_to_geom(el)
            if g:
                cands.append((geom_area_m2(g), g, f"osm_{el['type']}_{el['id']}", t))
    except Exception as ex:
        log(f"  Overpass boundary search failed: {ex}")

    if not cands:
        log("  No polygon in OSM, trying Nominatim...")
        for qq in ["أبحر الجنوبية جدة", "Obhur Al Janubiyah, Jeddah", "Al Obhur Al Janubiyah, Jeddah"]:
            url = NOMINATIM_URL + "?" + urllib.parse.urlencode(
                {"q": qq, "format": "jsonv2", "polygon_geojson": 1, "limit": 5, "countrycodes": "sa"})
            try:
                res = json.loads(cached(cache_dir, f"nominatim_{hashlib.md5(qq.encode()).hexdigest()[:8]}.json",
                                        lambda: http_get(url, tries=3)))
            except Exception as ex:
                log(f"  Nominatim failed: {ex}")
                continue
            for r in res:
                g = r.get("geojson")
                if g and g["type"] in ("Polygon", "MultiPolygon"):
                    cands.append((geom_area_m2(g), g, f"nominatim_{r.get('osm_type')}_{r.get('osm_id')}",
                                  {"name": r.get("display_name", "")}))
            if cands:
                break
            time.sleep(1.1)

    if not cands:
        sys.exit("\nCould not find the Obhur Al-Janubiyah boundary automatically.\n"
                 "Save the district polygon as GeoJSON (e.g. drawn on geojson.io) and run:\n"
                 "   python build_obhur_db.py --boundary obhur_south.geojson\n")
    for a, g, sid, t in sorted(cands, key=lambda c: -c[0]):
        log(f"  candidate: {sid}  {t.get('name', '')}  area={a/1e6:.2f} km²")
    area, geom, sid, tags = max(cands, key=lambda c: c[0])
    log(f"Boundary chosen: {sid} ({area/1e6:.2f} km²)")
    return geom, sid, tags.get("name:en") or DISTRICT_NAME_EN


# --------------------------------------------------------------------------
# Streets
# --------------------------------------------------------------------------
CLASS_AR = {"residential": "سكني", "living_street": "سكني", "service": "خدمة", "pedestrian": "مشاة"}


def class_ar(hw):
    return CLASS_AR.get(hw, "فرعي")


def fetch_ways(bbox, cache_dir):
    s, w, n, e = bbox
    pad = 0.002
    hw = "|".join(HIGHWAYS)
    q = f"""[out:json][timeout:300];
way["highway"~"^({hw})$"]({s-pad},{w-pad},{n+pad},{e+pad});
out body geom;"""
    res = overpass(q, cache_dir, "osm_highways.json")
    return [el for el in res.get("elements", []) if el["type"] == "way" and el.get("geometry")]


def name_unnamed(ways):
    """Descriptive names for ways with no name (Wareef method)."""
    named_nodes = {}
    for w in ways:
        if w["name"]:
            for nid in w["nodes"]:
                named_nodes.setdefault(nid, [])
                if w["name"] not in named_nodes[nid]:
                    named_nodes[nid].append(w["name"])
    node_to_ways = {}
    for i, w in enumerate(ways):
        for nid in w["nodes"]:
            node_to_ways.setdefault(nid, []).append(i)

    def names_at(nid):
        return named_nodes.get(nid, [])

    named_vertices = [(w["name"], lat, lon) for w in ways if w["name"] for (lon, lat) in w["coords"]]
    for i, w in enumerate(ways):
        if w["name"]:
            continue
        cls = class_ar(w["highway"])
        ends = []
        for nid in (w["nodes"][0], w["nodes"][-1]):
            for nm in names_at(nid):
                if nm not in ends:
                    ends.append(nm)
        if len(ends) < 2:
            for nid in w["nodes"][1:-1]:
                for nm in names_at(nid):
                    if nm not in ends:
                        ends.append(nm)
        if len(ends) >= 2:
            w["desc"] = f"{cls} بين {ends[0]} و{ends[1]}"
            continue
        if len(ends) == 1:
            w["desc"] = f"{cls} متفرّع من {ends[0]}"
            continue
        # follow connected unnamed ways up to 6 steps
        seen, frontier, found = {i}, [i], None
        for _ in range(6):
            nxt = []
            for wi in frontier:
                for nid in ways[wi]["nodes"]:
                    if names_at(nid):
                        found = names_at(nid)[0]
                        break
                    for wj in node_to_ways.get(nid, []):
                        if wj not in seen:
                            seen.add(wj)
                            nxt.append(wj)
                if found:
                    break
            if found or not nxt:
                break
            frontier = nxt
        if found:
            w["desc"] = f"{cls} متفرّع من {found}"
            continue
        mlon, mlat = w["coords"][len(w["coords"]) // 2]
        best = None
        for nm, la, lo in named_vertices:
            d = haversine(mlat, mlon, la, lo)
            if d <= 150 and (best is None or d < best[0]):
                best = (d, nm)
        w["desc"] = f"{cls} قرب {best[1]}" if best else f"{cls} بدون اسم"


# --------------------------------------------------------------------------
# GeoTIFF reader (pure Python: strips or tiles; none / deflate / LZW; predictor 1-2)
# --------------------------------------------------------------------------
def lzw_decode(data):
    out = bytearray()
    table = [bytes([i]) for i in range(256)] + [b"", b""]
    bitpos, nbits, prev = 0, 9, None
    total = len(data) * 8
    while bitpos + nbits <= total:
        byte = bitpos >> 3
        chunk = int.from_bytes(data[byte:byte + 4].ljust(4, b"\0"), "big")
        code = (chunk >> (32 - (bitpos & 7) - nbits)) & ((1 << nbits) - 1)
        bitpos += nbits
        if code == 256:
            table = table[:258]
            nbits, prev = 9, None
            continue
        if code == 257:
            break
        if prev is None:
            entry = table[code]
        elif code < len(table):
            entry = table[code]
            table.append(prev + entry[:1])
        else:
            entry = prev + prev[:1]
            table.append(entry)
        out += entry
        prev = entry
        if len(table) + 1 >= (1 << nbits) and nbits < 12:
            nbits += 1
    return bytes(out)


def read_geotiff(data):
    bo = data[:2]
    if bo == b"II":
        e = "<"
    elif bo == b"MM":
        e = ">"
    else:
        raise ValueError("not a TIFF (server returned: %r)" % data[:200])
    magic = struct.unpack(e + "H", data[2:4])[0]
    if magic != 42:
        raise ValueError("BigTIFF not supported")
    ifd = struct.unpack(e + "I", data[4:8])[0]
    n = struct.unpack(e + "H", data[ifd:ifd + 2])[0]
    tsz = {1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 6: 1, 7: 1, 8: 2, 9: 4, 10: 8, 11: 4, 12: 8, 16: 8}
    tfmt = {1: "B", 3: "H", 4: "I", 6: "b", 8: "h", 9: "i", 11: "f", 12: "d", 16: "Q"}
    tags = {}
    for k in range(n):
        off = ifd + 2 + 12 * k
        tag, typ, cnt = struct.unpack(e + "HHI", data[off:off + 8])
        size = tsz.get(typ, 1) * cnt
        voff = off + 8 if size <= 4 else struct.unpack(e + "I", data[off + 8:off + 12])[0]
        raw = data[voff:voff + size]
        if typ == 2:
            val = raw.rstrip(b"\0").decode("latin-1")
        elif typ in (5, 10):
            v = struct.unpack(e + ("I" if typ == 5 else "i") * (2 * cnt), raw)
            val = [v[i] / v[i + 1] for i in range(0, len(v), 2)]
        elif typ in tfmt:
            val = list(struct.unpack(e + tfmt[typ] * cnt, raw))
        else:
            val = raw
        tags[tag] = val
    W, H = tags[256][0], tags[257][0]
    bps = tags.get(258, [16])[0]
    comp = tags.get(259, [1])[0]
    pred = tags.get(317, [1])[0]
    sfmt = tags.get(339, [1])[0]
    spp = tags.get(277, [1])[0]
    if spp != 1:
        raise ValueError("only single-band rasters supported")
    code = {(16, 2): "h", (16, 1): "H", (32, 2): "i", (32, 1): "I", (32, 3): "f", (8, 1): "B", (8, 2): "b"}[(bps, sfmt)]
    bpp = bps // 8

    def decomp(b):
        if comp == 1:
            return b
        if comp in (8, 32946):
            return zlib.decompress(b)
        if comp == 5:
            return lzw_decode(b)
        raise ValueError(f"unsupported TIFF compression {comp}")

    def unpredict(buf, width, rows):
        if pred != 2:
            return list(struct.unpack(e + code * (len(buf) // bpp), buf))
        vals = list(struct.unpack(e + code * (len(buf) // bpp), buf))
        mask = (1 << bps) - 1
        for r in range(rows):
            base = r * width
            for c in range(1, width):
                v = (vals[base + c] + vals[base + c - 1]) & mask
                if code in "hib" and v >= 1 << (bps - 1):
                    v -= 1 << bps
                vals[base + c] = v
        return vals

    grid = [[None] * W for _ in range(H)]
    if 322 in tags:  # tiled
        tw, th = tags[322][0], tags[323][0]
        offs, cnts = tags[324], tags[325]
        across = (W + tw - 1) // tw
        for ti, (o, c) in enumerate(zip(offs, cnts)):
            vals = unpredict(decomp(data[o:o + c]), tw, th)
            r0, c0 = (ti // across) * th, (ti % across) * tw
            for r in range(th):
                if r0 + r >= H:
                    break
                for cc in range(tw):
                    if c0 + cc < W:
                        grid[r0 + r][c0 + cc] = vals[r * tw + cc]
    else:
        rps = tags.get(278, [H])[0]
        offs, cnts = tags[273], tags[279]
        row = 0
        for o, c in zip(offs, cnts):
            rows = min(rps, H - row)
            vals = unpredict(decomp(data[o:o + c]), W, rows)
            for r in range(rows):
                grid[row + r] = vals[r * W:(r + 1) * W]
            row += rows
    scale = tags.get(33550)
    tie = tags.get(33922)
    if not scale or not tie:
        raise ValueError("GeoTIFF georeferencing tags missing")
    px, py = scale[0], scale[1]
    origin_x = tie[3] - tie[0] * px
    origin_y = tie[4] + tie[1] * py
    nodata = tags.get(42113)
    nod = float(nodata) if isinstance(nodata, str) and nodata.strip() else NODATA
    return {"width": W, "height": H, "origin_x": origin_x, "origin_y": origin_y,
            "pixel_x": px, "pixel_y": py, "grid": grid, "nodata": nod}


# --------------------------------------------------------------------------
# SoilGrids
# --------------------------------------------------------------------------
def wcs_url(prop, cov, x0, x1, y0, y1):
    crs = "http://www.opengis.net/def/crs/EPSG/0/152160"
    return (f"{WCS_BASE}?map=/map/{prop}.map&SERVICE=WCS&VERSION=2.0.1&REQUEST=GetCoverage"
            f"&COVERAGEID={cov}&FORMAT=image/tiff&SUBSETTINGCRS={crs}&OUTPUTCRS={crs}"
            f"&SUBSET=X({x0:.0f},{x1:.0f})&SUBSET=Y({y0:.0f},{y1:.0f})")


def download_soil(extent, cache_dir, workers):
    x0, x1, y0, y1 = extent
    rdir = os.path.join(cache_dir, "soilgrids")
    os.makedirs(rdir, exist_ok=True)
    jobs = [(p[0], d[0], s[0]) for p in SOIL_PROPERTIES for d in SOIL_DEPTHS for s in SOIL_STATS]
    results, files = {}, []
    done = [0]

    def one(job):
        prop, depth, stat = job
        cov = f"{prop}_{depth}_{stat}"
        url = wcs_url(prop, cov, x0, x1, y0, y1)
        fname = cov.replace(".", "_") + ".tif"
        data = cached(rdir, fname, lambda: http_get(url, timeout=120, tries=6))
        if data[:2] not in (b"II", b"MM"):
            os.remove(os.path.join(rdir, fname))
            raise RuntimeError(f"{cov}: server did not return a GeoTIFF: {data[:200]!r}")
        r = read_geotiff(data)
        meta = {"coverage_id": cov, "url": url, "sha256": hashlib.sha256(data).hexdigest(),
                "bytes": len(data), "fetched_utc": dt.datetime.fromtimestamp(
                    os.path.getmtime(os.path.join(rdir, fname)), dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "width": r["width"], "height": r["height"], "origin_x": r["origin_x"],
                "origin_y": r["origin_y"], "pixel_size": r["pixel_x"]}
        return job, r, meta

    with cf.ThreadPoolExecutor(max_workers=workers) as ex:
        futs = [ex.submit(one, j) for j in jobs]
        for f in cf.as_completed(futs):
            job, r, meta = f.result()
            results[job] = r
            files.append(meta)
            done[0] += 1
            if done[0] % 25 == 0 or done[0] == len(jobs):
                log(f"  SoilGrids layers: {done[0]}/{len(jobs)}")
    ref = results[jobs[0]]
    for job, r in results.items():
        if (r["width"], r["height"], round(r["origin_x"]), round(r["origin_y"])) != \
           (ref["width"], ref["height"], round(ref["origin_x"]), round(ref["origin_y"])):
            raise RuntimeError(f"Grid mismatch in {job}")
    if abs(ref["pixel_x"] - CELL) > 0.01:
        raise RuntimeError(f"Unexpected pixel size {ref['pixel_x']}")
    return results, files, ref


# --------------------------------------------------------------------------
# Climate
# --------------------------------------------------------------------------
def download_climate(lat, lon, cache_dir):
    daily = ["temperature_2m_min", "temperature_2m_max", "temperature_2m_mean",
             "relative_humidity_2m_mean", "precipitation_sum", "et0_fao_evapotranspiration",
             "wind_speed_10m_max"]
    url = OPEN_METEO_URL + "?" + urllib.parse.urlencode({
        "latitude": f"{lat:.6f}", "longitude": f"{lon:.6f}", "start_date": CLIMATE_START,
        "end_date": CLIMATE_END, "daily": ",".join(daily), "timezone": "Asia/Riyadh",
        "wind_speed_unit": "kmh"})
    data = json.loads(cached(cache_dir, "open_meteo_daily.json", lambda: http_get(url, timeout=180)))
    d = data["daily"]
    rows = []
    for i, day in enumerate(d["time"]):
        tmax = d["temperature_2m_max"][i]
        rows.append((day, d["temperature_2m_min"][i], tmax, d["temperature_2m_mean"][i],
                     d["relative_humidity_2m_mean"][i], d["precipitation_sum"][i],
                     d["et0_fao_evapotranspiration"][i], d["wind_speed_10m_max"][i],
                     None if tmax is None else int(tmax >= 40.0)))
    return rows, url, data


# --------------------------------------------------------------------------
# Database
# --------------------------------------------------------------------------
SCHEMA = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE sources (source_id TEXT PRIMARY KEY, name TEXT NOT NULL, url TEXT, license TEXT, data_type TEXT, notes TEXT);
CREATE TABLE districts (district_id TEXT PRIMARY KEY, district_key TEXT NOT NULL UNIQUE, name_ar TEXT NOT NULL,
  name_en TEXT, boundary_geojson TEXT NOT NULL, source_id TEXT REFERENCES sources(source_id));
CREATE TABLE soil_depths (depth_code TEXT PRIMARY KEY, top_cm INTEGER NOT NULL, bottom_cm INTEGER NOT NULL, sort_order INTEGER NOT NULL);
CREATE TABLE soil_properties (property_code TEXT PRIMARY KEY, name_ar TEXT NOT NULL, name_en TEXT NOT NULL, unit TEXT NOT NULL, raw_divisor REAL NOT NULL);
CREATE TABLE soil_cells (cell_id TEXT PRIMARY KEY, igh_x REAL NOT NULL, igh_y REAL NOT NULL, lat REAL NOT NULL, lon REAL NOT NULL,
  is_valid INTEGER NOT NULL CHECK (is_valid IN (0,1)), source_id TEXT NOT NULL REFERENCES sources(source_id));
CREATE TABLE soil_values (cell_id TEXT NOT NULL REFERENCES soil_cells(cell_id), depth_code TEXT NOT NULL REFERENCES soil_depths(depth_code),
  property_code TEXT NOT NULL REFERENCES soil_properties(property_code), mean REAL, q05 REAL, q50 REAL, q95 REAL, uncertainty REAL,
  PRIMARY KEY (cell_id, depth_code, property_code)) WITHOUT ROWID;
CREATE TABLE soil_raster_files (district_key TEXT NOT NULL, coverage_id TEXT NOT NULL, url TEXT NOT NULL, sha256 TEXT NOT NULL,
  bytes INTEGER NOT NULL, fetched_utc TEXT, width INTEGER, height INTEGER, origin_x REAL, origin_y REAL, pixel_size REAL,
  PRIMARY KEY (district_key, coverage_id));
CREATE TABLE streets (street_id INTEGER PRIMARY KEY, district_id TEXT NOT NULL REFERENCES districts(district_id), name_ar TEXT, name_en TEXT,
  display_name TEXT NOT NULL, length_m REAL NOT NULL, point_count INTEGER NOT NULL, UNIQUE (district_id, display_name));
CREATE TABLE street_ways (osm_way_id INTEGER NOT NULL, street_id INTEGER NOT NULL REFERENCES streets(street_id), highway TEXT NOT NULL,
  length_m REAL NOT NULL, geometry_geojson TEXT NOT NULL, PRIMARY KEY (osm_way_id, street_id));
CREATE TABLE street_points (point_id TEXT PRIMARY KEY, street_id INTEGER NOT NULL REFERENCES streets(street_id), osm_way_id INTEGER NOT NULL,
  seq INTEGER NOT NULL, kind TEXT NOT NULL CHECK (kind IN ('osm_vertex','interpolated')), osm_node_id INTEGER, chainage_m REAL NOT NULL,
  lat REAL NOT NULL, lon REAL NOT NULL, own_cell_id TEXT NOT NULL, soil_cell_id TEXT NOT NULL REFERENCES soil_cells(cell_id),
  soil_status TEXT NOT NULL CHECK (soil_status IN ('point_cell_value','nearest_valid_cell')), soil_distance_m REAL NOT NULL);
CREATE TABLE climate_daily (site_id TEXT NOT NULL, date TEXT NOT NULL, temperature_min_c REAL, temperature_max_c REAL, temperature_mean_c REAL,
  relative_humidity_mean_pct REAL, precipitation_mm REAL, et0_mm REAL, wind_speed_10m_max_kmh REAL, heat_day_ge_40c INTEGER,
  source_id TEXT, evidence_type TEXT, PRIMARY KEY (site_id, date));
CREATE INDEX ix_points_street ON street_points(street_id);
CREATE INDEX ix_points_way ON street_points(osm_way_id, seq);
CREATE INDEX ix_points_soilcell ON street_points(soil_cell_id);
CREATE INDEX ix_points_latlon ON street_points(lat, lon);
CREATE INDEX ix_streets_name ON streets(name_ar);
CREATE INDEX ix_ways_street ON street_ways(street_id);
CREATE VIEW v_point_soil AS
SELECT p.point_id, s.district_id, s.display_name AS street_name, p.osm_way_id, p.seq, p.lat, p.lon,
       p.soil_status, p.soil_distance_m, p.soil_cell_id, v.depth_code, d.top_cm, d.bottom_cm, v.property_code,
       sp.name_ar AS property_ar, sp.unit, v.mean, v.q05, v.q50, v.q95, v.uncertainty
FROM street_points p JOIN streets s ON s.street_id = p.street_id JOIN soil_values v ON v.cell_id = p.soil_cell_id
JOIN soil_depths d ON d.depth_code = v.depth_code JOIN soil_properties sp ON sp.property_code = v.property_code;
CREATE VIEW v_street_summary AS
SELECT s.street_id, dd.name_ar AS district, s.display_name AS street_name, s.length_m, s.point_count,
       SUM(p.soil_status='point_cell_value') AS points_direct,
       ROUND(100.0*SUM(p.soil_status='point_cell_value')/COUNT(*),1) AS pct_direct,
       COUNT(DISTINCT p.soil_cell_id) AS soil_cells_used, ROUND(MAX(p.soil_distance_m),1) AS max_soil_distance_m
FROM streets s JOIN districts dd ON dd.district_id = s.district_id JOIN street_points p ON p.street_id = s.street_id
GROUP BY s.street_id;
CREATE VIEW v_climate_monthly AS
SELECT substr(date,1,7) AS month, ROUND(AVG(temperature_min_c),1) AS tmin_avg, ROUND(AVG(temperature_max_c),1) AS tmax_avg,
       ROUND(AVG(relative_humidity_mean_pct),1) AS rh_avg, ROUND(SUM(precipitation_mm),1) AS rain_mm,
       ROUND(SUM(et0_mm),1) AS et0_mm, SUM(heat_day_ge_40c) AS heat_days_ge_40c
FROM climate_daily GROUP BY 1;
"""


def wide_view_sql():
    cols = []
    for d in SOIL_DEPTHS:
        for p in ("phh2o", "sand", "silt", "clay", "soc", "cfvo", "bdod", "cec", "wv0033", "wv1500"):
            cols.append(f"  MAX(CASE WHEN v.depth_code='{d[0]}' AND v.property_code='{p}' THEN v.mean END) AS "
                        f"{p}_{d[1]}_{d[2]}")
    return ("CREATE VIEW v_point_soil_wide AS\nSELECT p.point_id, s.district_id, s.display_name AS street_name, "
            "p.lat, p.lon, p.soil_status, p.soil_distance_m,\n" + ",\n".join(cols) +
            "\nFROM street_points p JOIN streets s ON s.street_id = p.street_id "
            "JOIN soil_values v ON v.cell_id = p.soil_cell_id\nGROUP BY p.point_id;")


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------
def main(argv=None):
    ap = argparse.ArgumentParser(description="Build the Wareef soil + climate database for Obhur Al-Janubiyah, Jeddah")
    ap.add_argument("--out-dir", default="obhur_output")
    ap.add_argument("--boundary", help="GeoJSON polygon of the district (skips automatic lookup)")
    ap.add_argument("--pad-m", type=float, default=2500, help="extra SoilGrids margin around the district (m)")
    ap.add_argument("--workers", type=int, default=4, help="parallel SoilGrids downloads")
    args = ap.parse_args(argv)

    out = args.out_dir
    cache = os.path.join(out, "cache")
    os.makedirs(cache, exist_ok=True)
    t0 = time.time()

    # 1. boundary
    log("1/6 District boundary")
    geom, boundary_src, name_en = find_boundary(args, cache)
    bbox = geom_bbox(geom)
    rings = geom_rings(geom)

    # 2. streets
    log("2/6 Streets from OpenStreetMap")
    raw = fetch_ways(bbox, cache)
    ways = []
    for el in raw:
        coords = [[g["lon"], g["lat"]] for g in el["geometry"]]
        nodes = el.get("nodes") or [None] * len(coords)
        # Wareef method: densify the whole OSM way, keep only the points inside the district
        # (seq renumbered from 1, chainage measured from the start of the full way)
        full = densify([(c[0], c[1], n) for c, n in zip(coords, nodes)])
        inside = [p for p in full if point_in_geom(p[3], p[4], geom)]
        t = el.get("tags", {})
        name_ar = (t.get("name:ar") or t.get("name") or "").strip() or None
        name_en = (t.get("name:en") or "").strip() or None
        ways.append({"id": el["id"], "highway": t.get("highway", ""), "name": name_ar or name_en,
                     "name_ar": name_ar, "name_en": name_en, "nodes": nodes, "coords": coords,
                     "length_m": round(full[-1][2], 1),
                     "points": [[i + 1, k, nid, round(ch, 1), round(la, 7), round(lo, 7)]
                                for i, (k, nid, ch, la, lo) in enumerate(inside)]})
    name_unnamed(ways)                       # names from all nearby streets, inside or just outside
    ways = [w for w in ways if w["points"]]  # keep only streets with points inside the district
    if not ways:
        sys.exit("No streets found inside the boundary - check the boundary.")
    log(f"  {len(ways)} OSM ways inside the district "
        f"({sum(1 for w in ways if w['name'])} named, {sum(1 for w in ways if not w['name'])} unnamed)")

    # group into streets: same real name -> one street; descriptive names numbered when repeated
    desc_count = {}
    for w in ways:
        if not w["name"]:
            desc_count[w["desc"]] = desc_count.get(w["desc"], 0) + 1
    desc_seen = {}
    streets = {}
    for w in sorted(ways, key=lambda w: w["id"]):
        if w["name"]:
            key = w["name"]
        else:
            if desc_count[w["desc"]] > 1:
                desc_seen[w["desc"]] = desc_seen.get(w["desc"], 0) + 1
                key = f"{w['desc']} ({desc_seen[w['desc']]})"
            else:
                key = w["desc"]
        st = streets.setdefault(key, {"name_ar": w["name_ar"], "name_en": w["name_en"], "ways": []})
        st["ways"].append(w)

    # 3. points every 10 m (generated above, per way)
    log("3/6 Points every 10 m along each street")
    points = [p for w in ways for p in w["points"]]
    log(f"  {len(points)} points")

    # 4. soil
    log("4/6 SoilGrids (13 properties x 5 depths x 5 statistics = 325 layers)")
    xs, ys = [], []
    for r in rings:
        for lon, lat in r:
            x, y = ll_to_igh(lat, lon)
            xs.append(x)
            ys.append(y)
    pad = args.pad_m
    x0 = math.floor((min(xs) - pad) / CELL) * CELL
    x1 = math.ceil((max(xs) + pad) / CELL) * CELL
    y0 = math.floor((min(ys) - pad) / CELL) * CELL
    y1 = math.ceil((max(ys) + pad) / CELL) * CELL
    layers, raster_files, ref = download_soil((x0, x1, y0, y1), cache, args.workers)
    W, H, ox, oy = ref["width"], ref["height"], ref["origin_x"], ref["origin_y"]
    lon0 = igh_lobe_lon0(bbox[0], bbox[1])

    cells = {}   # (row, col) -> dict
    zero_cells = [0]   # cells with data but impossible values (e.g. sand+silt+clay = 0) -> treated as masked
    for r in range(H):
        for c in range(W):
            cx = ox + (c + 0.5) * CELL
            cy = oy - (r + 0.5) * CELL
            lat, lon = igh_to_ll(cx, cy, lon0)
            vals = {}
            complete = True
            for p in SOIL_PROPERTIES:
                for d in SOIL_DEPTHS:
                    row = []
                    for stat, col in SOIL_STATS:
                        v = layers[(p[0], d[0], stat)]["grid"][r][c]
                        if v is None or v == NODATA or v == ref["nodata"]:
                            row.append(None)
                        else:
                            div = UNCERTAINTY_DIVISOR if stat == "uncertainty" else p[4]
                            row.append(round(v / div, 4))
                    if row[0] is None:
                        complete = False
                    vals[(d[0], p[0])] = row
            bad_texture = False
            if complete:
                for d in SOIL_DEPTHS:
                    tex = sum(vals[(d[0], p)][0] for p in ("sand", "silt", "clay"))
                    if not (90.0 <= tex <= 110.0) or vals[(d[0], "bdod")][0] <= 0 or vals[(d[0], "phh2o")][0] <= 0:
                        bad_texture = True
                if bad_texture:
                    complete = False
                    zero_cells[0] += 1
            cells[(r, c)] = {"id": f"IGH_X{cx:.3f}_Y{cy:.3f}", "x": cx, "y": cy, "lat": round(lat, 7),
                             "lon": round(lon, 7), "valid": complete, "vals": vals}
    valid = {k for k, v in cells.items() if v["valid"]}
    if not valid:
        sys.exit("SoilGrids returned no valid cells in this area.")
    log(f"  {len(cells)} cells, {len(valid)} valid, {len(cells) - len(valid)} masked (built-up / sea)"
        f"{f', of which {zero_cells[0]} had zero/impossible values' if zero_cells[0] else ''}")

    # link each point to its own cell, or the nearest valid cell by ground distance
    max_r = max(W, H)
    for pt in points:
        lat, lon = pt[4], pt[5]
        x, y = ll_to_igh(lat, lon)
        c = int((x - ox) // CELL)
        r = int((oy - y) // CELL)
        if not (0 <= r < H and 0 <= c < W):
            raise RuntimeError(f"Point outside soil extent: {lat},{lon} - increase --pad-m")
        own = cells[(r, c)]
        if (r, c) in valid:
            pt += [own["id"], own["id"], "point_cell_value", 0.0]
            continue
        best = None
        for ring in range(1, max_r + 1):
            if best is not None and (ring - 1) * CELL * 0.8 > best[0]:
                break
            for rr in range(r - ring, r + ring + 1):
                for cc in range(c - ring, c + ring + 1):
                    if max(abs(rr - r), abs(cc - c)) != ring or (rr, cc) not in valid:
                        continue
                    cl = cells[(rr, cc)]
                    dd = haversine(lat, lon, cl["lat"], cl["lon"])
                    if best is None or dd < best[0]:
                        best = (dd, cl["id"])
        if best is None:
            raise RuntimeError("No valid soil cell found - increase --pad-m")
        pt += [own["id"], best[1], "nearest_valid_cell", round(best[0], 1)]
    edge = [p for p in points if p[9] > args.pad_m * 0.8]
    if edge:
        log(f"  WARNING: {len(edge)} points have their nearest valid cell more than {args.pad_m*0.8:.0f} m away; "
            f"consider re-running with a larger --pad-m")

    # 5. climate
    log("5/6 Climate 2021-2025 (Open-Meteo)")
    clat, clon = sum(p[4] for p in points) / len(points), sum(p[5] for p in points) / len(points)
    if not point_in_geom(clat, clon, geom):
        clat, clon = points[len(points) // 2][4], points[len(points) // 2][5]
    climate, climate_url, climate_raw = download_climate(clat, clon, cache)
    log(f"  {len(climate)} days at {clat:.4f}, {clon:.4f} (grid cell {climate_raw.get('latitude')}, "
        f"{climate_raw.get('longitude')})")

    # 6. database
    log("6/6 Writing database and exports")
    db_path = os.path.join(out, "obhur_south.db")
    if os.path.exists(db_path):
        os.remove(db_path)
    con = sqlite3.connect(db_path)
    con.executescript(SCHEMA)
    con.executescript(wide_view_sql())
    now = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    site_id = f"obhur_south_climate_reference_{clat:.6f}_{clon:.6f}"
    con.executemany("INSERT INTO meta VALUES (?,?)", [
        ("name", "Wareef Jeddah - Obhur Al-Janubiyah street soil and climate database"),
        ("built_utc", now), ("schema_version", "1"),
        ("crs", "WGS84 lat/lon for points; SoilGrids IGH (EPSG:152160) for cell ids"),
        ("point_spacing_m", "10"), ("soil_depth_max_cm", "100"),
        ("boundary_source", boundary_src), ("soil_pad_m", str(args.pad_m)),
        ("climate_site", f"{clat:.6f},{clon:.6f}"),
        ("method", "OSM highway ways touching the district; a point at every OSM vertex and every 10 m between "
                   "vertices, keeping the points inside the district; each point takes its own SoilGrids 250 m cell, or the nearest valid cell by ground "
                   "distance when its own cell is masked (built-up / sea)."),
    ])
    con.executemany("INSERT INTO sources VALUES (?,?,?,?,?,?)", [
        ("soilgrids", "ISRIC SoilGrids 2.0 (WCS maps.isric.org)", "https://maps.isric.org/", "CC-BY 4.0",
         "modelled 250 m predictions", "Not field measurements; built-up areas and sea are masked (no data)."),
        ("osm", "OpenStreetMap (Overpass API)", "https://www.openstreetmap.org/", "ODbL 1.0", "mapped vector",
         "Street centre-lines; points every 10 m along each way."),
        ("boundary", f"District boundary ({boundary_src})", "https://www.openstreetmap.org/", "ODbL 1.0",
         "mapped administrative geometry", None),
        ("open_meteo", "Open-Meteo Historical Weather API (ERA5 / ERA5-Land reanalysis)", climate_url,
         "CC-BY 4.0", "modelled reanalysis", "Daily values for one reference point in the district."),
    ])
    con.execute("INSERT INTO districts VALUES (?,?,?,?,?,?)",
                (DISTRICT_ID, DISTRICT_KEY, DISTRICT_NAME_AR, name_en, json.dumps(geom), "boundary"))
    con.executemany("INSERT INTO soil_depths VALUES (?,?,?,?)",
                    [(d[0], d[1], d[2], i) for i, d in enumerate(SOIL_DEPTHS)])
    con.executemany("INSERT INTO soil_properties VALUES (?,?,?,?,?)", SOIL_PROPERTIES)
    con.executemany("INSERT INTO soil_cells VALUES (?,?,?,?,?,?,?)",
                    [(v["id"], v["x"], v["y"], v["lat"], v["lon"], int(v["valid"]), "soilgrids")
                     for v in cells.values()])
    con.executemany("INSERT INTO soil_values VALUES (?,?,?,?,?,?,?,?)",
                    [(v["id"], d, p, *row) for v in cells.values() for (d, p), row in v["vals"].items()])
    con.executemany("INSERT INTO soil_raster_files VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    [(DISTRICT_KEY, m["coverage_id"], m["url"], m["sha256"], m["bytes"], m["fetched_utc"],
                      m["width"], m["height"], m["origin_x"], m["origin_y"], m["pixel_size"])
                     for m in sorted(raster_files, key=lambda m: m["coverage_id"])])

    street_rows, way_rows, point_rows = [], [], []
    for sid, (key, st) in enumerate(sorted(streets.items(), key=lambda kv: kv[0]), start=1):
        st["id"] = sid
        length = round(sum(w["length_m"] for w in st["ways"]), 1)
        npts = sum(len(w["points"]) for w in st["ways"])
        street_rows.append((sid, DISTRICT_ID, st["name_ar"], st["name_en"], key, length, npts))
        for w in st["ways"]:
            g = {"type": "LineString", "coordinates": [[round(x, 7), round(y, 7)] for x, y in w["coords"]]}
            way_rows.append((w["id"], sid, w["highway"], w["length_m"], json.dumps(g)))
            for p in w["points"]:
                point_rows.append((f"{POINT_PREFIX}-W{w['id']}-P{p[0]:03d}", sid, w["id"], *p))
    con.executemany("INSERT INTO streets VALUES (?,?,?,?,?,?,?)", street_rows)
    con.executemany("INSERT INTO street_ways VALUES (?,?,?,?,?)", way_rows)
    con.executemany("INSERT INTO street_points VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", point_rows)
    con.executemany("INSERT INTO climate_daily VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    [(site_id, *r, "open_meteo", "modelled_reanalysis_ERA5_ERA5Land_daily") for r in climate])
    con.commit()

    # integrity checks
    fk = con.execute("PRAGMA foreign_key_check").fetchall()
    bad_link = con.execute("SELECT COUNT(*) FROM street_points p JOIN soil_cells c ON c.cell_id=p.soil_cell_id "
                           "WHERE c.is_valid=0").fetchone()[0]
    texture = con.execute("""SELECT MIN(t), MAX(t) FROM (SELECT SUM(mean) t FROM soil_values
                             WHERE property_code IN ('sand','silt','clay') AND mean IS NOT NULL
                             AND cell_id IN (SELECT DISTINCT soil_cell_id FROM street_points)
                             GROUP BY cell_id, depth_code HAVING COUNT(*)=3)""").fetchone()
    con.execute("ANALYZE")
    con.commit()

    # exports
    prop_codes = [p[0] for p in SOIL_PROPERTIES]
    cell_by_id = {v["id"]: v for v in cells.values()}
    with open(os.path.join(out, "obhur_points_soil.csv"), "w", newline="", encoding="utf-8-sig") as f:
        wr = csv.writer(f)
        wr.writerow(["street_name", "point_id", "lat", "lon", "soil_status", "soil_distance_m", "soil_cell_id"] +
                    [f"{p}_{d[1]}_{d[2]}" for d in SOIL_DEPTHS for p in prop_codes])
        names = {r[0]: r[4] for r in street_rows}
        for pr in point_rows:
            cl = cell_by_id[pr[10]]
            wr.writerow([names[pr[1]], pr[0], pr[7], pr[8], pr[11], pr[12], pr[10]] +
                        [cl["vals"][(d[0], p)][0] for d in SOIL_DEPTHS for p in prop_codes])

    used = sorted({pr[10] for pr in point_rows})
    streets_json = {
        "district": {"name_ar": DISTRICT_NAME_AR, "name_en": name_en},
        "units": {p[0]: p[3] for p in SOIL_PROPERTIES},
        "depths": [d[0] for d in SOIL_DEPTHS],
        "streets": [],
        "soil_cells": {cid: {"lat": cell_by_id[cid]["lat"], "lon": cell_by_id[cid]["lon"],
                             "mean": {d[0]: {p: cell_by_id[cid]["vals"][(d[0], p)][0] for p in prop_codes}
                                      for d in SOIL_DEPTHS}} for cid in used},
    }
    pts_by_street = {}
    for pr in point_rows:
        pts_by_street.setdefault(pr[1], []).append(
            {"id": pr[0], "lat": pr[7], "lon": pr[8], "soil_cell": pr[10], "status": pr[11],
             "distance_m": pr[12]})
    for r in street_rows:
        streets_json["streets"].append({"street_id": r[0], "name": r[4], "length_m": r[5],
                                        "point_count": r[6], "points": pts_by_street.get(r[0], [])})
    with open(os.path.join(out, "obhur_streets.json"), "w", encoding="utf-8") as f:
        json.dump(streets_json, f, ensure_ascii=False, separators=(",", ":"))

    with open(os.path.join(out, "obhur_climate_daily.csv"), "w", newline="", encoding="utf-8-sig") as f:
        wr = csv.writer(f)
        wr.writerow(["date", "temperature_min_c", "temperature_max_c", "temperature_mean_c",
                     "relative_humidity_mean_pct", "precipitation_mm", "et0_mm", "wind_speed_10m_max_kmh",
                     "heat_day_ge_40c"])
        wr.writerows(climate)

    n_direct = sum(1 for p in point_rows if p[11] == "point_cell_value")
    max_d = max(p[12] for p in point_rows)
    named = sum(1 for k, s in streets.items() if s["name_ar"] or s["name_en"])
    tmax = [c[2] for c in climate if c[2] is not None]
    summary = [
        "Wareef - Obhur Al-Janubiyah (أبحر الجنوبية) database",
        f"built: {now}",
        f"boundary: {boundary_src}  area: {geom_area_m2(geom)/1e6:.2f} km2",
        "",
        f"STREETS: {len(street_rows)}   ({named} with an OSM name, {len(street_rows)-named} named descriptively)",
        f"OSM ways: {len(way_rows)}",
        "descriptive names: " + ", ".join(f"{k} {sum(1 for w in ways if not w['name'] and w['desc'].split(' ')[1] == k)}"
                                         for k in ("بين", "متفرّع", "قرب", "بدون")),
        f"POINTS: {len(point_rows)}   (every 10 m)",
        f"total street length: {sum(r[5] for r in street_rows)/1000:.1f} km",
        "",
        f"soil cells downloaded: {len(cells)}  valid: {len(valid)}  masked: {len(cells)-len(valid)}",
        f"points with soil from their own cell: {n_direct} ({100*n_direct/len(point_rows):.1f}%)",
        f"points using nearest valid cell: {len(point_rows)-n_direct}  (max distance {max_d:.0f} m)",
        f"distinct soil cells used by points: {len(used)}",
        f"sand+silt+clay in cells used by points: {texture[0]:.1f} - {texture[1]:.1f} %",
        f"cells with zero/impossible values (treated as masked): {zero_cells[0]}",
        "",
        f"climate days: {len(climate)}  ({CLIMATE_START} .. {CLIMATE_END})",
        f"  max temperature: {max(tmax):.1f} C, days >= 40 C: {sum(c[8] or 0 for c in climate)}",
        "",
        f"checks: foreign keys {'OK' if not fk else fk[:5]}, points on masked cells: {bad_link}",
        f"time: {time.time()-t0:.0f} s",
    ]
    with open(os.path.join(out, "summary.txt"), "w", encoding="utf-8") as f:
        f.write("\n".join(summary) + "\n")
    print("\n" + "=" * 60 + "\n" + "\n".join(summary) + "\n" + "=" * 60)
    print(f"\nFiles are in: {os.path.abspath(out)}")
    con.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
