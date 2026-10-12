"""Generate the synthetic sample data in data/.

Everything here is SYNTHETIC: no real household, phone number or plot is
described. Places (Bungoma County, Kanduyi Sub-County, its wards and markets)
are real; coordinates are approximate ward centroids. Dates are stored as
"days ago" so the data stays current whenever it is seeded.

Run: uv run python scripts/gen_data.py
"""

import csv
import random
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "data"
rng = random.Random(2026)

CLUSTERS = [
    # id, name, ward, lat, lon, nearest_market
    ("CL-KHA", "Khalaba cluster", "Khalaba", 0.5905, 34.5822, "Bungoma"),
    ("CL-MUS", "Musikoma cluster", "Musikoma", 0.5512, 34.5243, "Bungoma"),
    ("CL-TUU", "Tuuti cluster", "Marakaru/Tuuti", 0.6187, 34.5021, "Chwele"),
]

SURNAMES = ["Wafula", "Wekesa", "Nafula", "Simiyu", "Barasa", "Wanjala",
            "Khaemba", "Nekesa", "Masinde", "Makokha", "Wamalwa", "Namalwa"]
INITIALS = "ABCEFGJLMNPRSV"
CHANNELS = ["sms", "sms", "sms", "ussd", "ussd", "visit"]

# crop, variety, planting window offset (days ago), stage by days since planting
CROP_PLANS = [
    ("maize", "H520", (40, 55)),
    ("maize", "DH04", (35, 50)),
    ("beans", "KK8", (20, 35)),
    ("beans", "Rosecoco", (25, 40)),
    ("beans", "KK15", (0, 0)),  # not yet planted: waiting on rains
    ("sorghum", "Gadam", (30, 45)),
]


def stage(crop, days):
    if days == 0:
        return "not_planted"
    if crop == "maize":
        return "V6-V8 (knee high)" if days > 42 else "V3-V5 (early whorl)"
    if crop == "beans":
        return "flowering" if days > 30 else "vegetative"
    return "tillering"


def main():
    OUT.mkdir(exist_ok=True)
    with open(OUT / "clusters.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["id", "name", "ward", "sub_county", "county", "lat", "lon",
                    "agro_zone", "nearest_market"])
        for cid, name, ward, lat, lon, market in CLUSTERS:
            w.writerow([cid, name, ward, "Kanduyi", "Bungoma", lat, lon,
                        "LM1 (lower midland, sugarcane-maize zone)", market])

    households, plots = [], []
    hh_n = plot_n = 0
    for cid, *_ in CLUSTERS:
        for _ in range(10):
            hh_n += 1
            hid = f"HH-{hh_n:03d}"
            households.append([
                hid, cid,
                f"{rng.choice(INITIALS)}. {rng.choice(SURNAMES)}",
                f"+2547XXXXX{hh_n:03d}",
                rng.choice(CHANNELS),
                rng.choice(["Luhya (Bukusu)", "Luhya (Bukusu)", "Swahili", "English"]),
            ])
            for _ in range(rng.choice([1, 1, 2])):
                plot_n += 1
                crop, variety, (lo, hi) = rng.choice(CROP_PLANS)
                days = rng.randint(lo, hi) if hi else 0
                plots.append([
                    f"PLOT-{plot_n:03d}", hid, round(rng.uniform(0.25, 2.0), 2),
                    crop, variety, days if days else "",
                    stage(crop, days), rng.choice(["clay loam", "sandy loam", "loam"]),
                    rng.randint(3, 60),  # last officer visit, days ago
                    "",
                ])

    # Deliberate gaps the agent must notice instead of inventing advice:
    # HH-007 has no plot record at all; HH-024's plot record is very stale.
    plots = [p for p in plots if p[1] != "HH-007"]
    for p in plots:
        if p[1] == "HH-024":
            p[8] = 210
            p[9] = "Record not updated since the long rains; crop unconfirmed."

    with open(OUT / "households.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["id", "cluster_id", "head_name", "phone_masked", "channel", "language"])
        w.writerows(households)
    with open(OUT / "plots.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["id", "household_id", "area_acres", "crop", "variety",
                    "planted_days_ago", "stage", "soil", "last_visit_days_ago", "notes"])
        w.writerows(plots)

    # Retail prices, KES per kg. Shape follows KAMIS daily market reports;
    # values are synthetic. Beans jump in the last week at Bungoma.
    base = {"maize": 42, "beans": 118, "sorghum": 55}
    prices, n = [], 0
    for market in ["Bungoma", "Chwele"]:
        for crop, b in base.items():
            for days_ago in (28, 21, 14, 7, 1):
                n += 1
                p = b * rng.uniform(0.96, 1.04)
                if crop == "beans" and market == "Bungoma" and days_ago <= 7:
                    p = b * 1.31
                if market == "Chwele":
                    p *= 0.95
                prices.append([f"PRICE-{n:03d}", market, crop, "kg", round(p, 1),
                               days_ago, "synthetic (KAMIS-style daily retail)"])
    with open(OUT / "prices.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["id", "market", "crop", "unit", "price_kes", "days_ago", "source"])
        w.writerows(prices)

    with open(OUT / "pest_alerts.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["id", "ward", "pest", "crop", "severity", "reported_days_ago",
                    "source", "text"])
        w.writerow(["PEST-001", "Khalaba", "fall armyworm", "maize", "moderate", 2,
                    "ward farmers' WhatsApp group, relayed by officer",
                    "Windowpane feeding and frass in maize whorls on several farms "
                    "near Khalaba primary school. Roughly 1 in 5 plants affected."])
        w.writerow(["PEST-002", "Marakaru/Tuuti", "bean stem maggot", "beans", "low", 9,
                    "farmer phone call to sub-county office",
                    "Two farmers report yellowing bean seedlings, a few wilted."])


if __name__ == "__main__":
    main()
