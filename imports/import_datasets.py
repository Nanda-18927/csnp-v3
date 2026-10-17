"""Build the South India intelligence database from the source workbooks.

Sources (kept in ``data/`` so the project is self-contained):

* ``India_Police_Stations_Full_Database.xlsx`` — 16,459 police stations for all
  of India (State, District, Police_Station_Name, Station_Code, Lat, Long).
* ``South_India_Criminal_Tracking_200.xlsx`` — 200 tracked criminals with the
  handling station, the officer in charge, crime type and current status.

The script does **not** need pandas/openpyxl: an .xlsx file is a zip of XML, so
a ~60-line reader (sharedStrings + sheet rows) is enough.

What it produces (``database/south_india.sql``):

* ``police_stations`` — every station in the eight southern states/UTs
* ``officers``       — the officers named in the criminal sheet
* ``criminals``      — the 200 tracked criminals, with coordinates resolved
                        from the station handling the case
* ``cases``          — one FIR per criminal, linked to station + officer
* ``criminal_links`` — the relationship graph: two criminals are linked when
                        they share a handling officer, a station, a city or a
                        crime type in the same state

Usage:
    python imports/import_datasets.py                 # regenerate the SQL
    python imports/import_datasets.py --apply         # …and load it into the DB
    python imports/import_datasets.py --stations "D:\\other\\file.xlsx"
"""
from __future__ import annotations

import argparse
import re
import sqlite3
import sys
import zipfile
from pathlib import Path
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
SQL_OUT = ROOT / "database" / "south_india.sql"
DB_PATH = ROOT / "backend" / "crimesight.db"

NS_MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
NS_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"

#: The southern states + union territories the platform covers.
SOUTH_STATES = (
    "TELANGANA",
    "ANDHRA PRADESH",
    "KARNATAKA",
    "KERALA",
    "TAMIL NADU",
    "PUDUCHERRY",
    "GOA",
    "LAKSHADWEEP",
)

#: Nicapitalisation for display: "ANDHRA PRADESH" -> "Andhra Pradesh".
_STATE_PRETTY = {
    "TELANGANA": "Telangana",
    "ANDHRA PRADESH": "Andhra Pradesh",
    "KARNATAKA": "Karnataka",
    "KERALA": "Kerala",
    "TAMIL NADU": "Tamil Nadu",
    "PUDUCHERRY": "Puducherry",
    "GOA": "Goa",
    "LAKSHADWEEP": "Lakshadweep",
}

CRITICAL = ("HOMICIDE", "KIDNAP", "SEXUAL", "ARSON", "TRAFFICK", "MURDER")
HIGH = ("ROBBERY", "ASSAULT", "NARCOTIC", "DRUG", "FIREARM", "EXTORTION",
        "COUNTERFEIT", "SMUGGLING", "CYBER", "FRAUD")


# ── minimal xlsx reader ────────────────────────────────────────────────────
def _col_index(ref: str) -> int:
    letters = re.match(r"([A-Z]+)", ref).group(1)
    n = 0
    for ch in letters:
        n = n * 26 + (ord(ch) - 64)
    return n - 1


def read_xlsx(path: Path) -> list[list[str]]:
    """Return the first worksheet as a list of rows (strings)."""
    ns = {"m": NS_MAIN}
    with zipfile.ZipFile(path) as z:
        shared: list[str] = []
        if "xl/sharedStrings.xml" in z.namelist():
            root = ET.fromstring(z.read("xl/sharedStrings.xml"))
            for si in root.findall("m:si", ns):
                shared.append("".join(t.text or "" for t in si.iter(f"{{{NS_MAIN}}}t")))
        wb = ET.fromstring(z.read("xl/workbook.xml"))
        first = next(iter(wb.iter(f"{{{NS_MAIN}}}sheet")))
        rid = first.get(f"{{{NS_REL}}}id")
        rels = ET.fromstring(z.read("xl/_rels/workbook.xml.rels"))
        target = next(r.get("Target") for r in rels if r.get("Id") == rid)
        sheet_path = "xl/" + target.lstrip("/").replace("xl/", "")

        rows: list[list[str]] = []
        root = ET.fromstring(z.read(sheet_path))
        for row in root.iter(f"{{{NS_MAIN}}}row"):
            cells: dict[int, str] = {}
            for c in row.findall("m:c", ns):
                v = c.find("m:v", ns)
                if v is None:
                    inline = c.find("m:is", ns)
                    val = ("".join(t.text or "" for t in inline.iter(f"{{{NS_MAIN}}}t"))
                           if inline is not None else "")
                elif c.get("t") == "s":
                    val = shared[int(v.text)]
                else:
                    val = v.text
                cells[_col_index(c.get("r"))] = (val or "").strip()
            if cells:
                width = max(cells) + 1
                rows.append([cells.get(i, "") for i in range(width)])
        return rows


def _norm(text: str) -> str:
    """'Tiruvananthapuram South PS' -> 'tiruvananthapuram south ps'."""
    return re.sub(r"\s+", " ", (text or "").strip().lower())


def _to_float(text: str):
    try:
        return float(text)
    except (TypeError, ValueError):
        return None


def severity_of(crime: str) -> str:
    up = (crime or "").upper()
    if any(k in up for k in CRITICAL):
        return "Critical"
    if any(k in up for k in HIGH):
        return "High"
    return "Medium"


def status_of(current: str):
    """Map 'Under Surveillance' -> ('Open', completion_pct)."""
    cur = (current or "").strip().lower()
    if cur.startswith("arrest") or "convict" in cur or "released" in cur:
        return "Closed", 100
    if "surveillance" in cur or "abscond" in cur:
        return "Open", 35 if "abscond" in cur else 60
    return "Under Investigation", 45


# ── build ──────────────────────────────────────────────────────────────────
#: Cities whose district was renamed in the station database. Small and
#: explicit on purpose - it documents the hand-matched pairs instead of hiding
#: them behind fuzzy matching.
CITY_ALIASES = {
    "bengaluru": ("bangalore",),
    "bangalore": ("bangalore",),
    "mangaluru": ("mangaluru city", "dakshina kannada"),
    "mysuru": ("mysuru",),
    "mysore": ("mysuru",),
    "shivamogga": ("shivamogga",),
    "shimoga": ("shivamogga",),
    "ballari": ("ballari",),
    "belagavi": ("belagavi",),
    "kochi": ("ernakulam",),
    "cochin": ("ernakulam",),
    "kozhikode": ("kozhikode",),
    "calicut": ("kozhikode",),
    "thiruvananthapuram": ("thiruvananthapuram",),
    "trivandrum": ("thiruvananthapuram",),
    "thrissur": ("thrissur",),
    "kollam": ("kollam",),
    "quilon": ("kollam",),
    "kannur": ("kannur",),
    "cannore": ("kannur",),
    "palakkad": ("palakkad",),
    "tiruchirappalli": ("tiruchirapalli", "trichirapalli", "trichy"),
    "trichy": ("tiruchirapalli", "trichirapalli", "trichy"),
    "karimnagar": ("karimnagar",),
    "mahbubnagar": ("mahaboobnagar", "mahabubnagar"),
    "tirupati": ("tirupathi",),
}

#: Noise words in district labels ("Thrissur City" == "Thrissur").
_DISTRICT_NOISE = (" city", " rural", " district", " commissionerate", " zone")


def _bare(name: str) -> str:
    n = _norm(name)
    for noise in _DISTRICT_NOISE:
        if n.endswith(noise):
            n = n[: -len(noise)].strip()
    return n


def build(stations_xlsx: Path, criminals_xlsx: Path):
    print(f"[*] Reading {stations_xlsx.name}")
    st_rows = read_xlsx(stations_xlsx)
    stations = []
    for r in st_rows[1:]:
        if len(r) < 6:
            continue
        state = (r[0] or "").strip().upper()
        if state not in SOUTH_STATES:
            continue
        lat, lng = _to_float(r[4]), _to_float(r[5])
        code = (r[3] or "").strip()
        if not code or lat is None or lng is None:
            continue
        stations.append({
            "ps": code,
            "name": (r[2] or "").strip().title(),
            "district": (r[1] or "").strip().title(),
            "state": _STATE_PRETTY.get(state, state.title()),
            "lat": lat,
            "lng": lng,
            "sector": f"Sector-{state[:2].upper()}{r[3][-2:] if len(r[3]) >= 2 else '00'}",
        })
    print(f"    {len(stations):,} southern stations")

    # lookups used to attach criminals to a real station
    by_name = {}
    by_district = {}
    by_state: dict[str, list] = {}
    for s in stations:
        by_name.setdefault(_norm(s["name"]), s)
        by_district.setdefault((s["state"], _norm(s["district"])), []).append(s)
        by_state.setdefault(s["state"], []).append(s)

    def _resolve(station_name: str, city: str, state: str):
        """Find the police station that handles this record (see tiers)."""
        want = _norm(station_name)
        if want:
            hit = by_name.get(want)
            if hit:
                return hit
            for key, st in by_name.items():                 # A: name prefix
                if key.startswith(want[:14]) or want.startswith(key[:14]):
                    if st["state"] == state:
                        return st
        bare = _bare(city)
        for alias in CITY_ALIASES.get(bare, ()):             # B: alias table
            for (st_state, dist), rows2 in by_district.items():
                if st_state == state and _norm(alias) in dist:
                    return rows2[0]
        for (st_state, dist), rows2 in by_district.items():   # C: district prefix
            if st_state == state and dist and (dist.startswith(bare[:6]) or bare.startswith(dist[:6])):
                return rows2[0]
        pool = by_state.get(state) or []                     # D: any station
        return pool[0] if pool else None

    print(f"[*] Reading {criminals_xlsx.name}")
    cr_rows = read_xlsx(criminals_xlsx)
    header = [h.strip() for h in cr_rows[0]]
    idx = {h: i for i, h in enumerate(header)}
    need = ("Criminal_ID", "Criminal_Name", "Age", "Criminal_Place_City",
            "Criminal_Place_State", "Crime_Type", "Case_ID_Linked",
            "Station_Handling", "Handling_Police_Officer", "Current_Status")
    missing = [c for c in need if c not in idx]
    if missing:
        raise SystemExit(f"Missing expected columns: {missing}")

    criminals, officers, cases, links = [], [], [], []
    officer_ids: dict[str, str] = {}
    station_for_cr: dict[str, str] = {}
    used_fir: set[str] = set()
    matched = 0

    for n, r in enumerate(cr_rows[1:], start=1):
        def col(name, default=""):
            i = idx.get(name)
            return (r[i] if i is not None and i < len(r) else default) or default

        cid = col("Criminal_ID").strip() or f"CRIM-SI-{1000 + n}"
        name = col("Criminal_Name").strip().title()
        city = col("Criminal_Place_City").strip().title()
        state_raw = col("Criminal_Place_State").strip().upper()
        state = _STATE_PRETTY.get(state_raw, state_raw.title() or "Unknown")
        crime = col("Crime_Type").strip().title()
        station_name = col("Station_Handling").strip().title()
        officer_name = col("Handling_Police_Officer").strip().title()
        status_raw = col("Current_Status").strip()
        age = int(_to_float(col("Age")) or 0)
        fir = col("Case_ID_Linked").strip() or f"FIR-2026-{1000 + n}"
        if fir in used_fir:                      # guarantee UNIQUE(fir_number)
            fir = f"{fir}-{n}"
        used_fir.add(fir)

        # Resolve the station the case belongs to. Tiers, best first:
        #   A station name (or a prefix of it) from the sheet
        #   B curated city -> district alias
        #   C district label starts with the city name
        #   D any southern station in that state (last resort)
        station = _resolve(station_name, city, state)

        if station:
            matched += 1
            station_for_cr[cid] = station["ps"]

        # officer record
        oid = ""
        if officer_name:
            if officer_name not in officer_ids:
                oid = f"OFF-SI-{len(officer_ids) + 1:03d}"
                officer_ids[officer_name] = oid
                rank, _, squad = officer_name.partition(" ")
                officers.append({
                    "id": oid,
                    "name": officer_name,
                    "squad": f"{station['district']} Squad" if station else "Unassigned",
                    "rank": rank.title() if rank else "Inspector",
                })
            oid = officer_ids[officer_name]

        case_status, pct = status_of(status_raw)
        criminals.append({
            "id": cid,
            "name": name,
            "age": age,
            "gender": col("Gender").strip().title(),
            "address": f"{city}, {state}" if city else state,
            "city": city,
            "state": state,
            "photo_id": cid.split("-")[-1],
            "photo_url": col("Photo_URL").strip(),
            "alias": "",
            "crime": crime,
            "status": status_raw,
            "station": station["name"] if station else station_name,
            "officer": officer_name,
            "case_ref": fir,
            "lat": station["lat"] if station else None,
            "lng": station["lng"] if station else None,
        })

        if station:
            cases.append({
                "fir": fir,
                "ps": station["ps"],
                "officer": oid,
                "criminal": cid,
                "status": case_status,
                "severity": severity_of(crime),
                "crime": crime,
                "pct": pct,
                "handling": "Direct",
                "investigation": crime,
                "date": f"{re.sub(r'[^0-9]', '', fir)[:4] or '2026'}-"
                        f"{(n % 12) + 1:02d}-{(n % 27) + 1:02d}",
                "remarks": f"{status_raw}. Source: South India Criminal Tracking dataset.",
            })

    print(f"    {len(criminals)} criminals, {len(officers)} officers, "
          f"{len(cases)} FIRs ({matched} matched to a station)")

    # ── relationship graph ────────────────────────────────────────────────
    def link(a, b, kind, strength, label):
        if a == b:
            return
        links.append({"a": a, "b": b, "kind": kind, "strength": strength, "label": label})

    by_officer, by_station, by_city, by_crime = {}, {}, {}, {}
    for c in criminals:
        if c["officer"]:
            by_officer.setdefault(c["officer"], []).append(c["id"])
        if c["station"]:
            by_station.setdefault(c["station"], []).append(c["id"])
        if c["city"]:
            by_city.setdefault(c["city"], []).append(c["id"])
        by_crime.setdefault((c["crime"], c["state"]), []).append(c["id"])

    for group, kind, strength in ((by_officer, "officer", 3), (by_station, "station", 3),
                                  (by_city, "city", 2)):
        for key, ids in group.items():
            for i in range(len(ids)):
                for j in range(i + 1, len(ids)):
                    link(ids[i], ids[j], kind, strength, f"Shared {kind}: {key}")
    for (crime, state), ids in by_crime.items():
        if len(ids) > 8:            # keep the graph readable
            continue
        for i in range(len(ids)):
            for j in range(i + 1, len(ids)):
                link(ids[i], ids[j], "crime", 1, f"Same modus operandi: {crime} ({state})")

    # de-duplicate pairs, keep the strongest reason
    best: dict[tuple[str, str], dict] = {}
    for l in links:
        key = tuple(sorted((l["a"], l["b"])))
        prev = best.get(key)
        if prev is None or l["strength"] > prev["strength"]:
            best[key] = l
    links = list(best.values())
    print(f"    {len(links):,} criminal relations")

    return stations, officers, criminals, cases, links


# ── emit ───────────────────────────────────────────────────────────────────
def _q(v) -> str:
    if v is None:
        return "NULL"
    if isinstance(v, (int, float)):
        return str(v)
    return "'" + str(v).replace("'", "''") + "'"


def write_sql(stations, officers, criminals, cases, links, out: Path = SQL_OUT) -> Path:
    lines = [
        "-- ====================================================================",
        "-- CRIME SIGHT NOIR - SOUTH INDIA INTELLIGENCE DATA",
        "-- Generated by imports/import_datasets.py from:",
        "--   India_Police_Stations_Full_Database.xlsx",
        "--   South_India_Criminal_Tracking_200.xlsx",
        "-- Do not edit by hand - re-run the importer instead.",
        "-- ====================================================================",
        "",
    ]
    lines.append(f"INSERT OR IGNORE INTO police_stations (ps_code,name,district,state,lat,lng,sector) VALUES")
    lines.append(",\n".join(
        "  ({},{},{},{},{},{},{})".format(
            _q(s["ps"]), _q(s["name"]), _q(s["district"]), _q(s["state"]),
            s["lat"], s["lng"], _q(s["sector"]))
        for s in stations))
    lines.append(";")

    lines.append("\nINSERT OR IGNORE INTO officers (id,name,squad,rank) VALUES")
    lines.append(",\n".join(
        "  ({},{},{},{})".format(_q(o["id"]), _q(o["name"]), _q(o["squad"]), _q(o["rank"]))
        for o in officers))
    lines.append(";")

    lines.append("\nINSERT OR IGNORE INTO criminals "
                 "(id,name,age,gender,address,city,state,photo_id,photo_url,alias,"
                 "crime_type,status,station,officer,case_ref,lat,lng) VALUES")
    lines.append(",\n".join(
        "  ({},{},{},{},{},{},{},{},{},{},{},{},{},{},{},{},{})".format(
            _q(c["id"]), _q(c["name"]), c["age"], _q(c["gender"]), _q(c["address"]),
            _q(c["city"]), _q(c["state"]), _q(c["photo_id"]), _q(c["photo_url"]),
            _q(c["alias"]), _q(c["crime"]), _q(c["status"]), _q(c["station"]),
            _q(c["officer"]), _q(c["case_ref"]), c["lat"], c["lng"])
        for c in criminals))
    lines.append(";")

    lines.append("\nINSERT OR IGNORE INTO cases "
                 "(fir_number,station_id,officer_id,criminal_id,case_code,status,severity,"
                 "crime_type,completion_pct,handling,investigation_type,arrest_date,remarks) VALUES")
    lines.append(",\n".join(
        "  ({},(SELECT id FROM police_stations WHERE ps_code={}),{},{},{},{},{},{},{},{},{},{},{})".format(
            _q(c["fir"]), _q(c["ps"]), _q(c["officer"]), _q(c["criminal"]),
            _q(c["fir"]), _q(c["status"]), _q(c["severity"]), _q(c["crime"]),
            c["pct"], _q(c["handling"]), _q(c["investigation"]), _q(c["date"]),
            _q(c["remarks"]))
        for c in cases))
    lines.append(";")

    lines.append("\nINSERT OR IGNORE INTO criminal_links (a,b,kind,strength,label) VALUES")
    lines.append(",\n".join(
        "  ({},{},{},{},{})".format(_q(l["a"]), _q(l["b"]), _q(l["kind"]),
                                    l["strength"], _q(l["label"]))
        for l in links))
    lines.append(";")
    lines.append("")

    out.write_text("\n".join(lines), encoding="utf-8")
    return out


def apply(db_path: Path = DB_PATH) -> None:
    from backend.database import init_db  # noqa: E402  (needs sys.path)
    init_db()
    sql = SQL_OUT.read_text(encoding="utf-8")
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(sql)
        conn.commit()
        for t in ("police_stations", "officers", "criminals", "cases", "criminal_links"):
            n = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            print(f"    {t:18} {n:,}")
    finally:
        conn.close()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--stations", type=Path,
                    default=DATA_DIR / "India_Police_Stations_Full_Database.xlsx")
    ap.add_argument("--criminals", type=Path,
                    default=DATA_DIR / "South_India_Criminal_Tracking_200.xlsx")
    ap.add_argument("--apply", action="store_true",
                    help="load the generated SQL into backend/crimesight.db")
    args = ap.parse_args()

    sys.path.insert(0, str(ROOT))
    for p in (args.stations, args.criminals):
        if not p.is_file():
            raise SystemExit(f"Missing dataset: {p}")

    data = build(args.stations, args.criminals)
    out = write_sql(*data)
    print(f"[+] Wrote {out.relative_to(ROOT)} ({out.stat().st_size / 1024:.0f} KB)")

    if args.apply:
        print("[*] Applying to the database")
        apply()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())