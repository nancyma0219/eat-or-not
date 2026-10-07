"""The tools the harness can run, and the JSON that describes them to the model.

Inspection data comes from NYC Open Data (Socrata), which is free and needs no API key:
- DOHMH Restaurant Inspection Results: https://data.cityofnewyork.us/d/43nn-pn8j
- DOHMH Rodent Inspection:             https://data.cityofnewyork.us/d/p937-wjvj
Ratings and prices come from Google Places API (New) and need GOOGLE_PLACES_API_KEY.
"""

import json
import math
import os
import re
import time
import unicodedata
from difflib import SequenceMatcher
from datetime import date, datetime, timedelta, timezone

import requests

RESTAURANTS_URL = "https://data.cityofnewyork.us/resource/43nn-pn8j.json"
RODENTS_URL = "https://data.cityofnewyork.us/resource/p937-wjvj.json"
PLACES_URL = "https://places.googleapis.com/v1/places:searchText"
PLACES_FIELDS = ",".join([
    "places.displayName", "places.formattedAddress", "places.location", "places.rating",
    "places.userRatingCount", "places.priceLevel", "places.priceRange", "places.googleMapsUri",
    "places.businessStatus", "places.currentOpeningHours",
])
PRICE_SIGNS = {
    "PRICE_LEVEL_FREE": "Free", "PRICE_LEVEL_INEXPENSIVE": "$", "PRICE_LEVEL_MODERATE": "$$",
    "PRICE_LEVEL_EXPENSIVE": "$$$", "PRICE_LEVEL_VERY_EXPENSIVE": "$$$$",
}

# Bayesian average for ratings (the IMDb Top 250 method): a 5.0 from 6 reviews should not beat
# a 4.7 from 2,000. Each rating is pulled toward a typical NYC restaurant rating, and the pull
# fades as reviews pile up.
PRIOR_RATING = 4.2   # roughly where a typical NYC restaurant sits on Google Maps
PRIOR_REVIEWS = 50   # reviews needed before a place's own rating counts as much as the prior

# Lower inspection scores are better. These are DOHMH's letter-grade cutoffs.
GRADE_MEANING = {
    "A": "A (0-13 points, best)",
    "B": "B (14-27 points)",
    "C": "C (28+ points, worst)",
    "N": "Not yet graded",
    "Z": "Grade pending",
    "P": "Grade pending (re-opened after closure)",
}
GRADE_RANK = {"A": 0, "B": 1, "C": 2}
BOROUGHS = ["Manhattan", "Brooklyn", "Queens", "Bronx", "Staten Island"]
SHOW_MAX = 8

# Violation codes about pests. Descriptions come from the data itself; codes are only for grouping.
RAT_CODES = {"04K"}            # evidence of rats
MOUSE_CODES = {"04L"}          # evidence of mice
INSECT_CODES = {"04M", "04N"}  # roaches, filth flies
HARBORAGE_CODES = {"08A"}      # conditions conducive to pests


# --- Helpers (not tools) ---


def _query(url: str, params: dict) -> list[dict]:
    """Run one Socrata query. Raises requests.RequestException on failure."""
    resp = requests.get(url, params=params, timeout=15)
    resp.raise_for_status()
    return resp.json()


def _squash(text: str) -> str:
    """Standardize a name for matching: accents removed, uppercase, letters and digits only.
    "Joe's Pizza" -> "JOESPIZZA", "Sai Tong" -> "SAITONG", "Crêperie" -> "CREPERIE"."""
    plain = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return re.sub(r"[^A-Z0-9]", "", plain.upper())


# The same standardization done inside the city's database (SoQL has replace()).
_SQUASHED_DBA = "upper(dba)"
for _ch in [" ", "''", "-", ".", "&", ",", "/", "!"]:
    _SQUASHED_DBA = f"replace({_SQUASHED_DBA}, '{_ch}', '')"


def _quote(text: str) -> str:
    """Escape a user string for a SoQL string literal."""
    return text.replace("'", "''").strip().upper()


def _day(timestamp: str | None) -> str | None:
    """'2024-12-19T00:00:00.000' -> '2024-12-19'"""
    return timestamp[:10] if timestamp else None


def _title(text: str) -> str:
    """'JOE'S PIZZA' -> "Joe's Pizza". str.title() would give "Joe'S" (it splits on apostrophes)."""
    return " ".join(w[:1].upper() + w[1:].lower() for w in text.split())


def _error(message: str) -> str:
    return json.dumps({"error": message})


def _bad_camis(camis) -> str | None:
    """Models sometimes pass a restaurant name instead of its ID. Catch that early."""
    if not str(camis).strip().isdigit():
        return _error(
            f"'{camis}' is not a restaurant ID. IDs are numbers like '50066109'. "
            "Call search_restaurants first and use the 'id' it returns."
        )
    return None


def _restaurant_rows(camis: str) -> list[dict]:
    """Every inspection row (one per violation) for one restaurant, newest first."""
    return _query(RESTAURANTS_URL, {
        "camis": str(camis).strip(),
        "$order": "inspection_date DESC",
        "$limit": 1000,
    })


def _pest_kind(row: dict) -> str | None:
    code = row.get("violation_code", "")
    text = row.get("violation_description", "").lower()
    if code in RAT_CODES or " rats" in text:
        return "rats"
    if code in MOUSE_CODES or "mice" in text:
        return "mice"
    if code in INSECT_CODES or "roach" in text or "flies" in text:
        return "insects"
    if code in HARBORAGE_CODES:
        return "harborage"
    return None


# --- Tool 1: search ---


def search_restaurants(
    name: str | None = None,
    zipcode: str | None = None,
    cuisine: str | None = None,
    borough: str | None = None,
    sort_by: str = "best_grade",
) -> str:
    """Find NYC restaurants and their current inspection grade."""
    if not (name or zipcode):
        return _error("Give at least a restaurant name or a 5-digit zipcode to search.")
    if borough and borough not in BOROUGHS:
        return _error(f"borough must be one of {BOROUGHS}.")
    if sort_by not in ("best_grade", "most_recent"):
        return _error("sort_by must be 'best_grade' or 'most_recent'.")

    where = ["inspection_date > '1901-01-01'"]  # 1900-01-01 means 'not inspected yet'
    if name:
        if not _squash(name):
            return _error("The name has no letters or digits to search for.")
        # Standardized on both sides, so 'saitong', 'Sai-Tong' and "joes pizza" all match.
        where.append(f"{_SQUASHED_DBA} like '%{_squash(name)}%'")
    if zipcode:
        zips = [z.strip() for z in str(zipcode).replace(";", ",").split(",") if z.strip()]
        bad = [z for z in zips if not (z.isdigit() and len(z) == 5)]
        if bad or not zips:
            return _error(f"{bad or zipcode} is not a 5-digit NYC zipcode. Use e.g. '10025', "
                          "or several separated by commas like '10012,10013'.")
        where.append("zipcode in (" + ", ".join(f"'{z}'" for z in zips) + ")")
    if cuisine:
        where.append(f"upper(cuisine_description) like '%{_quote(cuisine)}%'")
    if borough:
        where.append(f"boro = '{borough}'")

    params = {
        "$select": "camis, dba, building, street, zipcode, boro, cuisine_description, "
                   "inspection_date, score, grade",
        "$where": " AND ".join(where),
        "$order": "inspection_date DESC",
        "$limit": 5000,
    }
    try:
        try:
            rows = _query(RESTAURANTS_URL, params)
        except requests.HTTPError:
            if not name:
                raise
            # Safety net: if the API ever rejects the standardized name, fall back to a plain match.
            params["$where"] = params["$where"].replace(
                f"{_SQUASHED_DBA} like '%{_squash(name)}%'", f"upper(dba) like '%{_quote(name)}%'")
            rows = _query(RESTAURANTS_URL, params)
    except requests.RequestException as e:
        return _error(f"NYC Open Data request failed ({type(e).__name__}). Try again in a moment.")

    # One row per violation -> one entry per restaurant, using its newest graded inspection.
    places: dict[str, dict] = {}
    for row in rows:
        place = places.setdefault(row["camis"], {
            "id": row["camis"],
            "name": _title(row.get("dba", "")),
            "address": " ".join(f"{row.get('building', '')} {_title(row.get('street', ''))}, "
                                f"{row.get('boro', '')} {row.get('zipcode', '')}".split()),
            "cuisine": row.get("cuisine_description"),
            "last_inspected": _day(row.get("inspection_date")),
            "latest_inspection_score": None,
            "grade": None,
            "grade_from": None,
            "score": None,
        })
        if place["latest_inspection_score"] is None and row.get("score"):
            place["latest_inspection_score"] = int(row["score"])
        if place["grade"] is None and row.get("grade"):
            place["grade"] = row["grade"]
            place["grade_from"] = _day(row.get("inspection_date"))
            place["score"] = int(row["score"]) if row.get("score") else None

    if not places:
        hints = []
        if name:
            hints.append("try a shorter or different spelling of the name (e.g. 'Koronet' not 'Koronet Pizza Inc')")
        if zipcode:
            hints.append("drop the zipcode filter")
        if cuisine:
            hints.append("drop the cuisine filter or use a broader word like 'Chinese' or 'Pizza'")
        return _error("No restaurants matched. You could " + ", or ".join(hints) + ".")

    results = list(places.values())

    # Exact name beats partial: "Lunar" should not list "Luna Rossa" (LUNAROSSA contains LUNAR).
    # Partial matches are only used when no restaurant has exactly the name asked for.
    partial_hidden = 0
    if name:
        exact = [p for p in results if _squash(p["name"]) == _squash(name)]
        if exact:
            partial_hidden = len(results) - len(exact)
            results = exact

    if sort_by == "best_grade":
        results.sort(key=lambda p: (GRADE_RANK.get(p["grade"], 3), p["score"] if p["score"] is not None else 99))
    else:
        results.sort(key=lambda p: p["last_inspected"] or "", reverse=True)

    for p in results:
        p["grade_meaning"] = GRADE_MEANING.get(p["grade"], "No grade on record")
        latest = p["latest_inspection_score"] or 0
        if latest >= 14 and p["grade_from"] and p["last_inspected"] > p["grade_from"]:
            p["heads_up"] = (f"Window grade {p['grade']} is from {p['grade_from']}. The newest inspection "
                             f"({p['last_inspected']}) found {latest} violation points, which is "
                             f"{'C' if latest >= 28 else 'B'}-level; the grade may drop after the re-inspection.")

    out = {
        "total_matches": len(results),
        "showing": min(SHOW_MAX, len(results)),
        "restaurants": results[:SHOW_MAX],
    }
    if partial_hidden:
        out["exact_name_match"] = True
        out["other_names_containing_it"] = partial_hidden
    if len(results) > SHOW_MAX:
        out["note"] = (f"Only {SHOW_MAX} of {len(results)} matches are shown. If the user means a specific "
                       "location, ask for the borough, neighborhood or street, then search again with "
                       "borough or zipcode.")
    return json.dumps(out)


# --- Tool 2: history ---


def get_inspection_history(camis: str, max_inspections: int = 5) -> str:
    """Recent inspections for one restaurant, with every violation cited."""
    if err := _bad_camis(camis):
        return err
    max_inspections = max(1, min(int(max_inspections or 5), 10))

    try:
        rows = _restaurant_rows(camis)
    except requests.RequestException as e:
        return _error(f"NYC Open Data request failed ({type(e).__name__}). Try again in a moment.")
    if not rows:
        return _error(f"No restaurant with ID '{camis}'. Call search_restaurants to find the right ID.")

    first = rows[0]
    inspections: dict[tuple, dict] = {}
    for row in rows:
        if not row.get("inspection_date", "").startswith("19"):
            key = (row["inspection_date"], row.get("inspection_type", ""))
            visit = inspections.setdefault(key, {
                "date": _day(row["inspection_date"]),
                "type": row.get("inspection_type"),
                "score": int(row["score"]) if row.get("score") else None,
                "grade": row.get("grade"),
                "outcome": row.get("action"),
                "violations": [],
            })
            if row.get("violation_code"):
                visit["violations"].append({
                    "code": row["violation_code"],
                    "critical": row.get("critical_flag") == "Critical",
                    "description": row.get("violation_description", "")[:200],
                })

    visits = list(inspections.values())
    all_violations = [v for visit in visits for v in visit["violations"]]

    return json.dumps({
        "restaurant": {
            "id": first["camis"],
            "name": _title(first.get("dba", "")),
            "address": f"{first.get('building', '')} {_title(first.get('street', ''))}, {first.get('boro', '')}",
            "cuisine": first.get("cuisine_description"),
        },
        "summary": {
            "inspections_on_record": len(visits),
            "critical_violations_total": sum(v["critical"] for v in all_violations),
            "times_closed_by_health_dept": sum("closed" in (v["outcome"] or "").lower() for v in visits),
            "closure_dates": [v["date"] for v in visits if "closed" in (v["outcome"] or "").lower()],
            "score_trend_newest_first": [v["score"] for v in visits if v["score"] is not None][:8],
            "note": "Score = violation points found by the inspector; each violation adds points by "
                    "severity, so lower is better (A 0-13, B 14-27, C 28+). An initial inspection with 14+ "
                    "points isn't graded; the restaurant is re-inspected and graded then.",
        },
        "recent_inspections": visits[:max_inspections],
    })


# --- Tool 3: rat risk (original) ---


def rat_risk_report(camis: str, radius_m: int = 150) -> str:
    """Score a restaurant's rat risk from its own pest violations plus city rodent inspections around it."""
    if err := _bad_camis(camis):
        return err
    radius_m = max(50, min(int(radius_m or 150), 500))
    today = date.today()

    try:
        rows = _restaurant_rows(camis)
        if not rows:
            return _error(f"No restaurant with ID '{camis}'. Call search_restaurants to find the right ID.")
        first = rows[0]
        if not first.get("latitude") or first.get("latitude") == "0":
            return _error("This restaurant has no map coordinates in the city data, so its "
                          "neighborhood can't be checked. Use get_inspection_history for its own pest violations.")
        lat, lon = first["latitude"], first["longitude"]

        street = _query(RODENTS_URL, {
            "$select": "result, count(*) AS n, max(inspection_date) AS latest",
            "$where": f"within_circle(location, {lat}, {lon}, {radius_m}) "
                      f"AND inspection_date >= '{today - timedelta(days=365)}' "
                      f"AND inspection_date <= '{today}'",
            "$group": "result",
        })
    except requests.RequestException as e:
        return _error(f"NYC Open Data request failed ({type(e).__name__}). Try again in a moment.")

    # Part 1: the restaurant's own pest violations in the last 3 years
    cutoff = str(today - timedelta(days=3 * 365))
    pest_hits = {"rats": [], "mice": [], "insects": [], "harborage": []}
    for row in rows:
        kind = _pest_kind(row)
        if kind and row.get("inspection_date", "") >= cutoff:
            pest_hits[kind].append(_day(row["inspection_date"]))

    # Part 2: city rodent inspections on nearby properties in the last 12 months
    results = {r["result"]: int(r["n"]) for r in street if r.get("result")}
    total = sum(results.values())
    rat_fails = sum(n for res, n in results.items() if "Rat Activity" in res)
    latest_rat = max((r.get("latest", "") for r in street if "Rat Activity" in r.get("result", "")), default=None)

    # Scoring: deliberately simple and explainable
    points, reasons = 0, []
    if pest_hits["rats"]:
        points += 4
        reasons.append(f"+4: inspectors found evidence of rats inside ({len(pest_hits['rats'])} citation(s), latest {max(pest_hits['rats'])})")
    if pest_hits["mice"]:
        points += 2
        reasons.append(f"+2: evidence of mice inside ({len(pest_hits['mice'])} citation(s), latest {max(pest_hits['mice'])})")
    if pest_hits["harborage"]:
        points += 1
        reasons.append(f"+1: cited for conditions that attract pests ({len(pest_hits['harborage'])} time(s))")
    if pest_hits["insects"]:
        points += 1
        reasons.append(f"+1: roaches or flies cited ({len(pest_hits['insects'])} time(s))")

    # Dense Manhattan blocks almost always have a few findings, so the bands are wide.
    street_points = 0 if rat_fails == 0 else 1 if rat_fails <= 10 else 2 if rat_fails <= 40 else 3
    if street_points:
        points += street_points
        reasons.append(f"+{street_points} (the block, not this shop): city inspectors found active rats at "
                       f"{rat_fails} inspections of other buildings within {radius_m} m in the past year")
    if total >= 10 and rat_fails / total > 0.3:
        points += 1
        reasons.append(f"+1 (the block): {round(100 * rat_fails / total)}% of rodent inspections on surrounding "
                       f"buildings found rats, a high share")
    if not reasons:
        reasons.append("No pest citations inside and no active-rat findings nearby.")

    level = "LOW" if points <= 2 else "MODERATE" if points <= 5 else "HIGH"
    inside_points = points - street_points - (1 if total >= 10 and rat_fails / total > 0.3 else 0)

    return json.dumps({
        "restaurant": {"id": first["camis"], "name": _title(first.get("dba", "")),
                       "address": f"{first.get('building', '')} {_title(first.get('street', ''))}, {first.get('boro', '')}"},
        "risk_level": level,
        "risk_points": points,
        "points_from_inside_the_restaurant": inside_points,
        "points_from_the_surrounding_block": points - inside_points,
        "scale": "0-2 LOW, 3-5 MODERATE, 6+ HIGH",
        "reasons": reasons,
        "inside_the_restaurant_last_3_years": {k: len(v) for k, v in pest_hits.items()},
        "neighborhood_last_12_months": {
            "radius_meters": radius_m,
            "rodent_inspections_nearby": total,
            "failed_for_active_rats": rat_fails,
            "most_recent_rat_finding": _day(latest_rat),
            "results_breakdown": results,
        },
        "caveat": "Heuristic built from public inspection records, not an official rating.",
    })


# --- Tool 4: worth it? (original) ---


GENERIC_WORDS = {"the", "and", "restaurant", "inc", "llc", "corp", "nyc", "ny", "new", "york"}


def _words(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", _squash_spaces(text).lower())) - GENERIC_WORDS


def _squash_spaces(text: str) -> str:
    return unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().replace("'", "")


def _name_match(city_name: str, google_name: str) -> float:
    """0-1 name similarity after standardizing both names.

    1. Containment: 'SAITONG' is inside 'SAITONGTHAI' -> 1.0
    2. Otherwise the share of the city name's words that Google's name also has.
    3. Strict fuzzy fallback (typos, 80%+ similar strings only).
    Generic words (restaurant, inc, nyc...) are dropped first so they can't create false matches.
    """
    def core(name: str) -> str:
        return _squash(" ".join(w for w in name.split() if w.lower() not in GENERIC_WORDS))

    a, b = core(city_name), core(google_name)
    if not a or not b:
        return 0.0
    if a in b or b in a:
        return 1.0
    words = len(_words(city_name) & _words(google_name)) / max(1, len(_words(city_name)))
    fuzzy = SequenceMatcher(None, a, b).ratio()
    return max(words, fuzzy if fuzzy >= 0.8 else 0.0)


def _google_search(body: dict, key: str) -> requests.Response:
    """POST to Places Text Search, retrying throttling (429) and temporary server errors (5xx).

    A comparison looks up several places within a second; Google may briefly refuse a burst.
    """
    for attempt in range(3):
        try:
            resp = requests.post(PLACES_URL, json=body, timeout=15, headers={
                "X-Goog-Api-Key": key, "X-Goog-FieldMask": PLACES_FIELDS})
        except (requests.ConnectionError, requests.Timeout):
            if attempt == 2:
                raise
        else:
            if resp.status_code not in (429, 500, 502, 503, 504) or attempt == 2:
                resp.raise_for_status()
                return resp
        time.sleep(1.0 * (attempt + 1))  # 1 s, then 2 s
    raise requests.RequestException("unreachable")


def _meters(lat1, lon1, lat2, lon2) -> float:
    """Haversine distance in meters."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = (math.sin((p2 - p1) / 2) ** 2
         + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2)
    return 6_371_000 * 2 * math.asin(math.sqrt(a))


def _hygiene(rows: list[dict], today: date) -> dict:
    """Hygiene band from the city record. Closures and grade C are dealbreakers."""
    graded = next((r for r in rows if r.get("grade") in GRADE_RANK), None)
    grade = graded["grade"] if graded else None
    closures = sorted({_day(r["inspection_date"]) for r in rows
                       if "closed" in r.get("action", "").lower() and r.get("inspection_date", "") > "1901"},
                      reverse=True)
    last_closed = closures[0] if closures else None
    days_since_closed = (today - date.fromisoformat(last_closed)).days if last_closed else None

    # The posted grade can be older than the latest inspection: a bad initial inspection
    # isn't graded until the re-inspection, so check the newest score too.
    latest = next((r for r in rows if r.get("score")), None)
    latest_score = int(latest["score"]) if latest else None
    newer_than_grade = bool(latest and graded and latest["inspection_date"] > graded["inspection_date"])

    reasons = [f"Posted grade {grade or 'none on record'}"
               + (f" (from {_day(graded['inspection_date'])})" if graded else "")]
    if newer_than_grade and latest_score >= 14:
        reasons.append(f"Latest inspection on {_day(latest['inspection_date'])} scored {latest_score} "
                       f"({'C' if latest_score >= 28 else 'B'}-range), waiting for re-inspection")
    if last_closed:
        reasons.append(f"Shut down by the Health Department on {last_closed}")
    if grade == "C" or (days_since_closed is not None and days_since_closed <= 365):
        band = "POOR"
    elif (grade == "B" or (days_since_closed is not None and days_since_closed <= 3 * 365)
          or (newer_than_grade and latest_score >= 28)):
        band = "FAIR"
    elif grade == "A":
        band = "GOOD"
    else:
        band = "UNKNOWN"
    return {"band": band, "grade": grade, "latest_score": latest_score, "last_closed": last_closed, "reasons": reasons}


def _taste(rating, count) -> dict:
    if rating is None or not count:
        return {"band": "UNKNOWN", "google_rating": rating, "reviews": count or 0}
    adjusted = (count * rating + PRIOR_REVIEWS * PRIOR_RATING) / (count + PRIOR_REVIEWS)
    band = ("EXCELLENT" if adjusted >= 4.5 else "GOOD" if adjusted >= 4.2
            else "OK" if adjusted >= 3.8 else "WEAK")
    return {
        "band": band,
        "google_rating": rating,
        "reviews": count,
        "adjusted_rating": round(adjusted, 2),
        "confidence": "low (under 50 reviews)" if count < 50 else "high",
    }


VERDICT_ORDER = ["GO", "GO IF SPLURGING", "TASTY BUT CHECK", "FINE", "NOT ENOUGH DATA", "SKIP"]


def _verdict(hyg: str, taste: str, price: str | None) -> tuple[str, str]:
    """Rules, not weights: cleanliness gates everything, then taste, then price."""
    cheap = price in ("Free", "$")  # $$ and up counts as a splurge
    if hyg == "POOR":
        return "SKIP", "Cleanliness is a dealbreaker here, no matter how good the reviews are."
    if taste == "WEAK":
        return "SKIP", "Reviews are weak, so there are better options nearby."
    if hyg == "GOOD" and taste in ("EXCELLENT", "GOOD"):
        if cheap or price is None:
            return "GO", "Clean record and well-loved" + (", at an easy price." if cheap else
                                                          " (no price info, so price wasn't judged).")
        return "GO IF SPLURGING", "Clean and well-loved, but priced for a special occasion."
    if hyg == "FAIR" and taste == "EXCELLENT":
        return "TASTY BUT CHECK", "People love it, but the inspection record has some marks. Read the violations first."
    if hyg == "UNKNOWN" or taste == "UNKNOWN":
        return "NOT ENOUGH DATA", "One side of the picture is missing, so judge from the parts that are there."
    return "FINE", "A reasonable option, nothing special on either side."


def worth_it_check(camis: str) -> str:
    """Hygiene record + Google rating + price, side by side, with a rule-based verdict."""
    if err := _bad_camis(camis):
        return err
    key = os.environ.get("GOOGLE_PLACES_API_KEY")
    today = date.today()

    try:
        rows = _restaurant_rows(camis)
    except requests.RequestException as e:
        return _error(f"NYC Open Data request failed ({type(e).__name__}). Try again in a moment.")
    if not rows:
        return _error(f"No restaurant with ID '{camis}'. Call search_restaurants to find the right ID.")

    first = rows[0]
    name = _title(first.get("dba", ""))
    address = f"{first.get('building', '')} {_title(first.get('street', ''))}, {first.get('boro', '')}"
    hyg = _hygiene(rows, today)
    out = {"restaurant": {"id": first["camis"], "name": name, "address": address}, "hygiene": hyg}

    # --- Google Places: find the same storefront ---
    match, problem = None, None
    if not key:
        problem = "Ratings unavailable: the server has no GOOGLE_PLACES_API_KEY set. Answer from hygiene alone."
    else:
        body = {"textQuery": f"{name} {address} New York", "pageSize": 5}
        lat, lon = first.get("latitude"), first.get("longitude")
        has_coords = bool(lat) and lat != "0"
        if has_coords:
            body["locationBias"] = {"circle": {"center": {"latitude": float(lat), "longitude": float(lon)}, "radius": 200.0}}
        try:
            resp = _google_search(body, key)
            candidates = resp.json().get("places", [])
        except requests.HTTPError as e:
            try:
                detail = e.response.json()["error"]["message"][:200]
            except Exception:
                detail = e.response.text[:200]
            candidates = []
            problem = (f"Google Places rejected the request (HTTP {e.response.status_code}: {detail}). "
                       "Likely the API key or 'Places API (New)' setup. Answer from hygiene alone.")
        except requests.RequestException as e:
            candidates, problem = [], f"Google Places request failed ({type(e).__name__}). Answer from hygiene alone."

        # Accept a candidate only if the name is similar and (when we know where the shop is) it's close.
        seen = []
        for place in candidates:
            gname = place.get("displayName", {}).get("text", "")
            similarity = _name_match(name, gname)
            loc = place.get("location", {})
            dist = (_meters(float(lat), float(lon), loc["latitude"], loc["longitude"])
                    if has_coords and loc else None)
            seen.append(f"{gname} (name match {similarity:.0%}"
                        + (f", {dist:.0f} m away)" if dist is not None else ")"))
            if similarity >= 0.6 and (dist is None or dist <= 150):
                match = place
                break
        if not match and not problem:
            problem = (f"Google returned {seen[:3] or 'no places'}; a match needs 60%+ name similarity "
                       "and under 150 m. "
                       "Couldn't confidently match this restaurant on Google Maps (names or locations differ). "
                       "Answer from hygiene alone and say ratings weren't found.")

    if match:
        price = PRICE_SIGNS.get(match.get("priceLevel"))
        rng = match.get("priceRange", {})
        if rng.get("startPrice"):
            lo, hi = rng["startPrice"].get("units"), rng.get("endPrice", {}).get("units")
            price_range = f"${lo}-{hi} per person" if hi else f"${lo}+ per person"
        else:
            price_range = None
        taste = _taste(match.get("rating"), match.get("userRatingCount"))
        out["taste"] = taste
        out["price"] = {"level": price or "unknown", "range": price_range}
        out["google_maps"] = match.get("googleMapsUri")
        hours = match.get("currentOpeningHours") or {}
        week = hours.get("weekdayDescriptions") or []
        if week:
            # Google lists Monday first; pick today's line by the New York weekday.
            try:
                from zoneinfo import ZoneInfo
                weekday = datetime.now(ZoneInfo("America/New_York")).weekday()
            except Exception:
                weekday = datetime.now(timezone(timedelta(hours=-4))).weekday()
            out["hours"] = {
                "open_now": hours.get("openNow"),
                "today": week[weekday] if weekday < len(week) else None,
                "this_week": week,
            }
        else:
            out["hours"] = {"open_now": None, "today": "Google has no opening hours for this place."}
        if match.get("businessStatus") not in (None, "OPERATIONAL"):
            out["warning"] = f"Google lists this place as {match['businessStatus']}."
    else:
        out["taste"] = {"band": "UNKNOWN"}
        out["price"] = {"level": "unknown", "range": None}
        out["ratings_note"] = problem

    # Unknown price must not count as expensive: only a real $-$$$$ level reaches the price rule.
    known_price = out["price"]["level"] if out["price"]["level"] in PRICE_SIGNS.values() else None
    verdict, why = _verdict(hyg["band"], out["taste"]["band"], known_price)
    out["verdict"] = verdict
    out["verdict_reason"] = why
    # Fixed ranking rule so comparisons are consistent, folded into ONE number (smaller = better),
    # because models compare single numbers far more reliably than lists:
    #   verdict rank x 10  +  (5 - adjusted rating)  +  violation points / 1000
    # e.g. GO with adj 4.46 and 9 pts -> 0.549; GO with adj 4.31 and 7 pts -> 0.697; FINE adj 3.83 -> 31.175
    adjusted = out["taste"].get("adjusted_rating")
    points = hyg.get("latest_score")
    out["rank_score"] = round(
        VERDICT_ORDER.index(verdict) * 10
        + (5 - adjusted if adjusted is not None else 5)
        + (points if points is not None else 99) / 1000,
        3,
    )
    out["method"] = ("Verdict uses rules, not a weighted score: hygiene POOR (grade C or closed in the last year) "
                     "means SKIP regardless of rating; ratings are Bayesian-adjusted so few-review places can't "
                     "top the list; price only decides between GO and GO IF SPLURGING.")
    return json.dumps(out)


# --- Tool 5: compare (ranking done in code, not by the model) ---


def compare_restaurants(camis_ids: list) -> str:
    """Run worth_it_check on several restaurants and return them already ranked."""
    if isinstance(camis_ids, str):
        camis_ids = [c for c in camis_ids.replace(";", ",").split(",")]
    ids = list(dict.fromkeys(str(c).strip() for c in camis_ids if str(c).strip()))
    if len(ids) < 2:
        return _error("Give at least 2 restaurant ids to compare (use worth_it_check for one).")
    if len(ids) > 5:
        return _error(f"Compare at most 5 restaurants at once; you gave {len(ids)}. Pick the top 5 from the search.")

    entries, failed = [], []
    for i, camis in enumerate(ids):
        if i:
            time.sleep(0.25)  # space out the Google lookups instead of a burst
        result = json.loads(worth_it_check(camis))
        if "error" in result:
            failed.append({"id": camis, "error": result["error"]})
            continue
        t, h, pr, hrs = result["taste"], result["hygiene"], result["price"], result.get("hours", {})
        entries.append({
            "name": result["restaurant"]["name"],
            "address": result["restaurant"]["address"],
            "id": camis,
            "verdict": result["verdict"],
            "verdict_reason": result["verdict_reason"],
            "hygiene": {"band": h["band"], "grade": h["grade"], "latest_violation_points": h.get("latest_score"),
                        "notes": h["reasons"][1:]},
            "google_rating": t.get("google_rating"),
            "reviews": t.get("reviews"),
            "adjusted_rating": t.get("adjusted_rating"),
            "price": pr.get("level"),
            "price_range": pr.get("range"),
            "open_now": hrs.get("open_now"),
            "hours_today": hrs.get("today"),
            "rank_score": result["rank_score"],
            **({"ratings_note": result["ratings_note"]} if result.get("ratings_note") else {}),
        })

    entries.sort(key=lambda e: e["rank_score"])
    for i, e in enumerate(entries, 1):
        e["rank"] = i
    return json.dumps({
        "ranking_rule": "Verdict first (GO > GO IF SPLURGING > TASTY BUT CHECK > FINE > NOT ENOUGH DATA > SKIP), "
                        "then higher review-adjusted rating, then fewer violation points. Already sorted: present "
                        "them in exactly this order.",
        "ranked": entries,
        **({"failed": failed} if failed else {}),
    })


# --- What the model sees: the "set notes" in the screenplay ---

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "search_restaurants",
            "description": (
                "Search NYC restaurants by name, zipcode, cuisine and/or borough. Returns up to 8 matches, each "
                "with its 'id' (needed by the other tools), address, cuisine, current letter grade and score. "
                "Use it first whenever the user names a restaurant or asks for places in an area. "
                "If several locations match a name, show them and ask the user which one they mean."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Part of the restaurant's name, case-insensitive, e.g. 'Koronet' or 'Shake Shack'. "
                                       "Shorter is better; leave out words like 'Inc' or 'Restaurant'.",
                    },
                    "zipcode": {
                        "type": "string",
                        "description": "One or more 5-digit NYC zipcodes, comma-separated. Convert neighborhoods "
                                       "yourself and include ALL their zipcodes in this one call, e.g. SoHo -> '10012,10013', "
                                       "Columbia/Morningside Heights -> '10025,10027', East Village -> '10003,10009'.",
                    },
                    "cuisine": {
                        "type": "string",
                        "description": "Cuisine keyword as the city records it, e.g. 'Chinese', 'Pizza', 'Japanese', "
                                       "'Mexican', 'Coffee/Tea', 'Bakery'. 'Dessert' matches both bakeries/desserts "
                                       "and frozen desserts. Use English.",
                    },
                    "borough": {
                        "type": "string",
                        "enum": BOROUGHS,
                        "description": "Limit to one borough. Use it when the user names a borough or a chain "
                                       "has many locations.",
                    },
                    "sort_by": {
                        "type": "string",
                        "enum": ["best_grade", "most_recent"],
                        "description": "'best_grade' (default) puts the cleanest places first: grade A, then lowest score. "
                                       "'most_recent' puts the most recently inspected first.",
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_inspection_history",
            "description": (
                "Get a restaurant's recent health inspections: date, score, grade, outcome (including closures) "
                "and every violation cited, marked critical or not, plus a summary of its record. "
                "Use it when the user asks whether a place is clean, safe, or what it was cited for."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "camis": {"type": "string", "description": "The restaurant 'id' from search_restaurants, e.g. '50066109'."},
                    "max_inspections": {
                        "type": "integer",
                        "description": "How many recent inspections to return in detail, 1-10. Default 5.",
                    },
                },
                "required": ["camis"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "rat_risk_report",
            "description": (
                "Estimate a restaurant's rat risk (LOW / MODERATE / HIGH) by combining its own pest violations "
                "from the last 3 years with NYC rodent inspections of buildings around it in the last 12 months. "
                "Returns the level, the points behind it and the evidence. Use it when the user asks about rats, "
                "mice, pests, or the neighborhood around a restaurant."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "camis": {"type": "string", "description": "The restaurant 'id' from search_restaurants, e.g. '50066109'."},
                    "radius_m": {
                        "type": "integer",
                        "description": "Neighborhood radius in meters, 50-500. Default 150 (about one city block).",
                    },
                },
                "required": ["camis"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "worth_it_check",
            "description": (
                "Decide if a restaurant is worth going to. Returns three things side by side: hygiene "
                "(grade, closures; band GOOD/FAIR/POOR), taste (Google rating, review count and a review-count-"
                "adjusted rating; band EXCELLENT/GOOD/OK/WEAK) and price ($ to $$$$, plus a per-person range when "
                "known), Google opening hours (open_now, today's hours, the week), then a rule-based verdict (GO, GO IF SPLURGING, TASTY BUT CHECK, FINE, SKIP, NOT ENOUGH "
                "DATA) with its reason. Use it when the user asks if a place is good, worth it, how it's rated, "
                "how expensive it is, or whether it's open now. For ONE restaurant; to compare or rank several, use compare_restaurants."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "camis": {"type": "string", "description": "The restaurant 'id' from search_restaurants, e.g. '50066109'."},
                },
                "required": ["camis"],
            },
        },
    },
]

TOOLS.append({
    "type": "function",
    "function": {
        "name": "compare_restaurants",
        "description": (
            "Compare 2-5 restaurants and get them back ALREADY RANKED best first (rank 1, 2, ...), each with "
            "verdict, hygiene, Google rating (raw and review-adjusted), price and today's hours. Use it for any "
            "'best X near Y', 'which is better', or 'compare' question, instead of calling worth_it_check one by one. "
            "Present the restaurants in exactly the returned order."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "camis_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "2-5 restaurant ids from search_restaurants, e.g. ['50084999', '40574872']. "
                                   "For 'best X' questions pass the top 4-5 search results.",
                },
            },
            "required": ["camis_ids"],
        },
    },
})

# What the harness runs: tool name -> Python function.
TOOL_MAP = {
    "compare_restaurants": compare_restaurants,
    "search_restaurants": search_restaurants,
    "get_inspection_history": get_inspection_history,
    "rat_risk_report": rat_risk_report,
    "worth_it_check": worth_it_check,
}


def run_tool(name: str, args: dict) -> str:
    """Run one tool call. Models invent tool names and arguments; never let that crash the loop."""
    if name not in TOOL_MAP:
        return json.dumps({"error": f"Unknown tool '{name}'. Available: {list(TOOL_MAP)}"})
    try:
        return TOOL_MAP[name](**args)
    except (TypeError, ValueError) as e:
        return json.dumps({"error": f"Bad arguments for {name}: {e}"})
    except Exception as e:  # unexpected data shape from the city API
        return json.dumps({"error": f"{name} failed unexpectedly ({type(e).__name__}). Try a different restaurant or query."})
