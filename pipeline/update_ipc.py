#!/usr/bin/env python3
"""Build the inflation dataset used by the eupersonalfinance.eu and e-rahatarkus.ee
inflation calculators.

Three audiences, three indices, on purpose. Each page uses the measure its own
readers recognise, which matters more here than cross-border consistency:

  EA  Euro area, Eurostat HICP (prc_hicp_ainr), from 1997. The harmonised index is
      the only one comparable across member states, which is the point of a
      pan-European page.

  NL  Netherlands, CBS national CPI (StatLine 70936ned), from 1963. The figure Dutch
      media quote. It differs from the HICP by more than rounding: 2022 reads 10.0
      here and 11.6 in the HICP.

  EE  Estonia, Statistikaamet THI (tarbijahinnaindeks, tables IA001 and IA021). Same
      reasoning: Estonian news quotes the THI, not the harmonised THHI.

The current year is estimated from the months already published and flagged as
provisional. That step is deliberately non-fatal per country: a missing provisional
year is a small loss, a broken JSON would take a calculator down.
"""

import gzip
import json
import pathlib
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone

EA_START = 1997
NL_START = 1963
EE_START = 1990          # the table decides the real start; this only filters junk
OUT = pathlib.Path(__file__).resolve().parent.parent / "data" / "hicp-anual.json"
TIMEOUT = 180

GEOS = {"EA": "Euro area", "NL": "Netherlands", "EE": "Estonia"}

EUROSTAT_URLS = [
    "https://ec.europa.eu/eurostat/api/dissemination/sdmx/2.1/data/{ds}?format=TSV&compressed=true",
    "https://ec.europa.eu/eurostat/api/dissemination/sdmx/2.1/data/{ds}/?format=TSV&compressed=true",
    "https://ec.europa.eu/eurostat/api/dissemination/sdmx/2.1/data/{ds}?format=TSV",
    "https://ec.europa.eu/eurostat/api/dissemination/files/data/{ds}.tsv.gz",
]

CBS_URL = (
    "https://opendata.cbs.nl/ODataApi/OData/70936ned/TypedDataSet"
    "?$select=Perioden,JaarmutatieCPI_1"
)

# PxWeb. IA001 is the annual change of the CPI, IA021 the monthly change. The path to
# a table is discovered by walking the API tree rather than copied from the browser
# URL, which is not the same thing: guessing it returned 400 with no clue why.
STAT_EE_ROOT = "https://andmed.stat.ee/api/v1/et"
_px_paths = {}


def fetch(url: str, payload: dict = None) -> bytes:
    """GET, or POST when a payload is given (PxWeb needs POST to return data)."""
    data = None
    headers = {"User-Agent": "eupf-hicp-data"}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers)
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return resp.read()


# ----------------------------------------------------------------- Eurostat (EA)

def download_tsv(dataset: str) -> str:
    errors = []
    for template in EUROSTAT_URLS:
        url = template.format(ds=dataset)
        try:
            raw = fetch(url)
        except urllib.error.HTTPError as exc:
            body = ""
            try:
                body = exc.read().decode("utf-8", "replace")[:200]
            except Exception:
                pass
            errors.append(f"{url} -> HTTP {exc.code} {body}")
            continue
        except Exception as exc:
            errors.append(f"{url} -> {exc}")
            continue

        try:
            text = gzip.decompress(raw).decode("utf-8")
        except OSError:
            text = raw.decode("utf-8", "replace")

        if "\t" not in text.split("\n", 1)[0]:
            errors.append(f"{url} -> response is not a TSV")
            continue
        print(f"fetched {dataset} from {url}")
        return text

    raise RuntimeError("all Eurostat endpoints failed:\n  " + "\n  ".join(errors))


def parse_tsv(text: str, key: str) -> dict:
    """Return {period: value} for the row whose packed dimension key matches."""
    lines = text.strip().split("\n")
    periods = [p.strip() for p in lines[0].split("\t")[1:]]
    out = {}
    for line in lines[1:]:
        if "\t" not in line:
            continue
        rowkey, _, rest = line.partition("\t")
        if rowkey.strip() != key:
            continue
        for period, cell in zip(periods, rest.split("\t")):
            cell = cell.strip()
            if not cell or cell.startswith(":"):
                continue
            try:
                out[period] = float(cell.split(" ")[0])
            except ValueError:
                continue
    return out


def euro_area() -> tuple:
    annual_raw = parse_tsv(download_tsv("prc_hicp_ainr"), "A,RCH_A_AVG,TOTAL,EA")
    annual = {y: round(v, 1) for y, v in annual_raw.items()
              if y.isdigit() and int(y) >= EA_START}
    if not annual:
        raise RuntimeError("no annual data found for EA")

    prov = {}
    try:
        year = str(datetime.now(timezone.utc).year)
        if year not in annual:
            monthly = parse_tsv(download_tsv("prc_hicp_minr"), "M,RCH_A,TOTAL,EA")
            months = [v for p, v in monthly.items() if p.startswith(year + "-")]
            if months:
                prov = {year: round(sum(months) / len(months), 1), "months": len(months)}
    except Exception as exc:
        print(f"EA provisional skipped: {exc}", file=sys.stderr)
    return annual, prov


# --------------------------------------------------------------------- CBS (NL)

def netherlands() -> tuple:
    rows = json.loads(fetch(CBS_URL).decode("utf-8")).get("value", [])
    if not rows:
        raise RuntimeError("CBS returned no rows")

    annual, monthly = {}, {}
    for row in rows:
        period = (row.get("Perioden") or "").strip()
        value = row.get("JaarmutatieCPI_1")
        if value is None or len(period) < 6:
            continue
        year, kind = period[:4], period[4:6]
        if kind == "JJ":
            if year.isdigit() and int(year) >= NL_START:
                annual[year] = round(float(value), 1)
        elif kind == "MM":
            monthly.setdefault(year, []).append(float(value))

    if not annual:
        raise RuntimeError("no annual CBS data found")

    prov = {}
    year = str(datetime.now(timezone.utc).year)
    if year not in annual and monthly.get(year):
        vals = monthly[year]
        prov = {year: round(sum(vals) / len(vals), 1), "months": len(vals)}
    print("fetched NL from CBS 70936ned")
    return annual, prov


# ----------------------------------------------------------- Statistikaamet (EE)

def px_children(url: str) -> list:
    try:
        data = json.loads(fetch(url).decode("utf-8"))
    except Exception as exc:
        print(f"  px: {url} -> {exc}", file=sys.stderr)
        return []
    return data if isinstance(data, list) else []


def px_find_table(table: str) -> str:
    """Walk the API tree until a table whose id starts with `table` turns up.

    Only branches that look like the economy/prices part of the tree are followed, so
    this costs three or four requests rather than a full crawl. The path is cached
    because both tables we need live in the same branch.
    """
    if table in _px_paths:
        return _px_paths[table]

    hints = ("majandus", "hinnad", "economy", "prices")
    frontier = []
    for db in px_children(STAT_EE_ROOT):
        dbid = db.get("dbid") or db.get("id")
        if dbid:
            frontier.append((f"{STAT_EE_ROOT}/{dbid}", 0))

    while frontier:
        url, depth = frontier.pop(0)
        if depth > 3:
            continue
        for item in px_children(url):
            item_id = item.get("id", "")
            child = f"{url}/{item_id}"
            if item.get("type") == "t":
                if item_id.upper().startswith(table.upper()):
                    print(f"px: found {table} at {child}")
                    _px_paths[table] = child
                    return child
            elif item.get("type") == "l":
                text = (item.get("text") or "").lower()
                if depth == 0 or any(h in item_id.lower() or h in text for h in hints):
                    frontier.append((child, depth + 1))

    raise RuntimeError(f"table {table} not found in the Statistikaamet API tree")


def px_fetch(table: str) -> dict:
    """PxWeb describes its own tables, so variable codes are discovered rather than
    hardcoded. Statistics agencies rename these, and a guess that silently selects
    the wrong series would be worse than a loud failure. The variables are printed
    so a future break can be diagnosed from the workflow log alone."""
    url = px_find_table(table)
    meta = json.loads(fetch(url).decode("utf-8"))
    described = [(v["code"], v.get("text", ""), len(v.get("values", [])))
                 for v in meta.get("variables", [])]
    print(f"{table} variables: {described}")
    payload = {
        "query": [{"code": v["code"], "selection": {"filter": "all", "values": ["*"]}}
                  for v in meta.get("variables", [])],
        "response": {"format": "json-stat2"},
    }
    return json.loads(fetch(url, payload).decode("utf-8"))


def jsonstat_rows(js: dict):
    """Yield ({dimension: (code, label)}, value) for a json-stat2 response.

    Values arrive in a flat array indexed by a single offset, so the offset is
    unpacked using the dimension sizes, in the order given by id.
    """
    dim_ids = js["id"]
    sizes = js["size"]

    labels = []
    for dim in dim_ids:
        cat = js["dimension"][dim]["category"]
        index = cat["index"]
        if isinstance(index, dict):
            ordered = [None] * len(index)
            for code, pos in index.items():
                ordered[pos] = code
        else:
            ordered = list(index)
        label_map = cat.get("label") or {}
        labels.append([(code, label_map.get(code, code)) for code in ordered])

    strides = [1] * len(sizes)
    for i in range(len(sizes) - 2, -1, -1):
        strides[i] = strides[i + 1] * sizes[i + 1]

    values = js["value"]
    pairs = values.items() if isinstance(values, dict) else enumerate(values)
    for flat, value in pairs:
        if value is None:
            continue
        rest = int(flat)
        key = {}
        for pos, dim in enumerate(dim_ids):
            idx, rest = divmod(rest, strides[pos])
            key[dim] = labels[pos][idx]
        yield key, float(value)


def pick_dimension(js: dict, needle: str):
    for dim in js["id"]:
        label = str(js["dimension"][dim].get("label") or dim).lower()
        if needle in label:
            return dim
    return None


def estonia() -> tuple:
    js = px_fetch("IA001")
    time_dim = pick_dimension(js, "aasta") or js["id"][-1]

    annual = {}
    for key, value in jsonstat_rows(js):
        code = key[time_dim][0]
        if code.isdigit() and int(code) >= EE_START:
            annual[code] = round(value, 1)
    if not annual:
        raise RuntimeError("no annual data found for EE")

    prov = {}
    try:
        year = str(datetime.now(timezone.utc).year)
        if year not in annual:
            jm = px_fetch("IA021")
            time_dim_m = pick_dimension(jm, "aasta") or jm["id"][0]
            ind_dim = pick_dimension(jm, "naitaja") or pick_dimension(jm, "n\u00e4itaja")
            months = []
            for key, value in jsonstat_rows(jm):
                if key[time_dim_m][0] != year:
                    continue
                # Two indicators share this table: change on the same month of the
                # previous year, and change on the previous month. Only the first is
                # comparable with the annual figure.
                if ind_dim and "eelmise aasta" not in str(key[ind_dim][1]).lower():
                    continue
                months.append(value)
            if months:
                prov = {year: round(sum(months) / len(months), 1), "months": len(months)}
    except Exception as exc:
        print(f"EE provisional skipped: {exc}", file=sys.stderr)

    print("fetched EE from Statistikaamet IA001")
    return annual, prov


# ------------------------------------------------------------------------- main

def main() -> int:
    series, provisional = {}, {}

    for geo, fn in (("EA", euro_area), ("NL", netherlands), ("EE", estonia)):
        try:
            series[geo], prov = fn()
            if prov:
                provisional[geo] = prov
        except Exception as exc:
            print(f"{geo} fetch failed: {exc}", file=sys.stderr)
            return 1

    payload = {
        "indicator": "Annual average rate of change of consumer prices (%)",
        "sources": {
            "EA": "Eurostat, HICP, prc_hicp_ainr and prc_hicp_minr, from 1997",
            "NL": "CBS StatLine 70936ned, national CPI (jaarmutatie), from 1963",
            "EE": "Statistikaamet IA001 and IA021, national CPI (tarbijahinnaindeks)",
        },
        "note": (
            "Each geography uses the index its own readers recognise rather than a "
            "single harmonised one. EA is the euro area with changing composition "
            "(HICP). NL and EE use their national CPI, which differs materially from "
            "the HICP (NL 2022: 10.0 vs 11.6). Provisional values are the mean of the "
            "months published so far in the current year."
        ),
        "geos": GEOS,
        "updated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "series": series,
        "provisional": provisional,
    }

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    for geo in GEOS:
        years = sorted(series[geo])
        print(f"{geo}: {len(years)} years, {years[0]} to {years[-1]}, "
              f"latest {series[geo][years[-1]]}%")
    print(f"provisional: {provisional or 'none'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
