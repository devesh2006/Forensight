"""Synthetic UFDR-like dataset generator: CASE PROJECT NIGHTFALL (fictional).
Writes mixed-format artifacts (CSV / JSON / XML). Narrative is embedded in noise,
not labelled. Deterministic via seed."""
import csv, json, random
from datetime import datetime, timedelta
from pathlib import Path
from xml.etree import ElementTree as ET

INCIDENT = datetime(2026, 10, 15, 22, 30)
PEOPLE = {  # id: (name, phone, device)
    "P1": ("Arjun Mehra", "+919810000001", "DEV-A"), "P2": ("Bhavna Rao", "+919810000002", "DEV-B"),
    "P3": ("Chirag Sood", "+919810000003", "DEV-C"), "P4": ("Divya Nair", "+919810000004", "DEV-D"),
    "P5": ("Esha Kapoor", "+919810000005", "DEV-E"), "P6": ("Farhan Ali", "+919810000006", "DEV-F"),
    "P7": ("Gita Verma", "+919810000007", "DEV-G"), "P8": ("Harsh Tandon", "+919810000008", "DEV-H"),
}
EXTRA = ["+919810000011", "+919810000012", "+919810000013", "+919810000014"]  # burner / unknown numbers
LOCS = {"Home-Arjun": (28.5672, 77.2100), "Office": (28.6139, 77.2090), "Connaught Place": (28.6315, 77.2167),
        "Old Fort Parking": (28.6118, 77.2437), "Market": (28.6562, 77.2410), "Airport T3": (28.5562, 77.1000)}
NOISE_TXT = ["Lunch tomorrow?", "Send the file", "Running late", "Call me", "Ok done", "Happy birthday!",
             "Did you see the match", "Bring the documents", "Traffic is terrible", "See you soon", "Thanks"]
APPS = ["WhatsApp", "SMS", "Telegram"]

def generate(out: Path, seed: int = 7):
    r = random.Random(seed); out.mkdir(parents=True, exist_ok=True)
    nums = [v[1] for v in PEOPLE.values()]
    msgs, calls, locs, web, appev = [], [], [], [], []
    base = datetime(2026, 10, 10)
    # --- background noise ---
    for _ in range(150):
        s, t = r.sample(nums, 2); ts = base + timedelta(minutes=r.randint(0, 6 * 24 * 60))
        msgs.append((ts, s, t, r.choice(NOISE_TXT), r.choice(APPS)))
    for _ in range(70):
        s, t = r.sample(nums, 2); ts = base + timedelta(minutes=r.randint(0, 6 * 24 * 60))
        calls.append((ts, s, t, r.randint(5, 600), r.choice(["outgoing", "incoming", "missed"])))
    # --- narrative (buried) ---
    P = {k: v[1] for k, v in PEOPLE.items()}; d = lambda h, m: datetime(2026, 10, 15, h, m)
    msgs += [(d(18, 5), P["P1"], EXTRA[0], "package ready", "Telegram"),
             (d(18, 9), EXTRA[0], P["P1"], "ok. cp. 9:30. old fort side after", "Telegram"),
             (d(19, 40), P["P1"], P["P2"], "Reach there by 9:30", "WhatsApp"),
             (d(20, 2), P["P2"], P["P3"], "tonight. you know the place", "WhatsApp"),
             (d(21, 36), P["P3"], P["P2"], "here. grey SUV near gate", "WhatsApp"),
             (d(22, 41), P["P2"], P["P1"], "done. delete this chat", "WhatsApp"),
             (d(22, 44), P["P1"], EXTRA[0], "all clear", "Telegram")]
    calls += [(d(21, 15), P["P2"], P["P3"], 94, "outgoing"), (d(21, 52), P["P1"], P["P2"], 31, "outgoing"),
              (d(22, 33), P["P3"], EXTRA[1], 12, "outgoing"), (d(22, 35), EXTRA[1], P["P3"], 48, "incoming"),
              (d(22, 38), P["P2"], P["P1"], 20, "missed")]
    # GPS: noise pings + narrative routes
    for k, (name, ph, dev) in PEOPLE.items():
        for i in range(10):
            ts = base + timedelta(hours=r.randint(0, 140)); la, lo = r.choice([LOCS["Home-Arjun"], LOCS["Office"], LOCS["Market"]])
            locs.append((ts, dev, la + r.gauss(0, .002), lo + r.gauss(0, .002), r.randint(5, 40), "GPS"))
    route = [("P2", d(20, 30), "Office"), ("P2", d(21, 32), "Connaught Place"), ("P3", d(21, 34), "Connaught Place"),
             ("P2", d(22, 28), "Old Fort Parking"), ("P3", d(22, 29), "Old Fort Parking"), ("P1", d(22, 31), "Old Fort Parking"),
             ("P1", d(23, 20), "Home-Arjun"), ("P3", d(23, 50), "Airport T3")]
    for pid, ts, loc in route:
        la, lo = LOCS[loc]; locs.append((ts, PEOPLE[pid][2], la + r.gauss(0, .0003), lo + r.gauss(0, .0003), 8, "GPS"))
    for pid in ("P2", "P3"):  # dense travel pings
        for i in range(8):
            ts = d(21, 40) + timedelta(minutes=i * 6); la, lo = LOCS["Connaught Place"]; la2, lo2 = LOCS["Old Fort Parking"]
            f = i / 7; locs.append((ts, PEOPLE[pid][2], la + (la2 - la) * f, lo + (lo2 - lo) * f, 12, "GPS"))
    web += [(d(19, 10), PEOPLE["P1"][2], "https://maps.example/Old+Fort+parking", "Old Fort parking map"),
            (d(20, 45), PEOPLE["P2"][2], "https://flights.example/DEL-DXB", "Flights Delhi to Dubai"),
            (d(22, 50), PEOPLE["P3"][2], "https://news.example/incident", "Local news")]
    for i in range(20):
        web.append((base + timedelta(hours=r.randint(0, 140)), r.choice(list(PEOPLE.values()))[2], f"https://site{i}.example", f"Page {i}"))
    appev += [(d(22, 52), "DEV-B", "com.whatsapp", "chat_deleted"), (d(22, 55), "DEV-A", "org.telegram", "chat_cleared")]
    fmt = "%Y-%m-%dT%H:%M:%S"
    with open(out / "messages.csv", "w", newline="") as f:
        w = csv.writer(f); w.writerow(["timestamp", "sender", "receiver", "text", "app"])
        for m in sorted(msgs): w.writerow([m[0].strftime(fmt), *m[1:]])
    (out / "calls.json").write_text(json.dumps([{"time": c[0].strftime(fmt), "caller": c[1], "receiver": c[2], "duration": c[3], "type": c[4]} for c in sorted(calls)], indent=1))
    (out / "locations.json").write_text(json.dumps([{"time": l[0].strftime(fmt), "device": l[1], "lat": round(l[2], 6), "lon": round(l[3], 6), "accuracy": l[4], "source": l[5]} for l in sorted(locs)], indent=1))
    root = ET.Element("report")
    for k, (n, ph, dev) in PEOPLE.items(): ET.SubElement(root, "contact", name=n, number=ph, device=dev, pid=k)
    for e in EXTRA: ET.SubElement(root, "contact", name="Unknown", number=e, device="", pid="")
    for b in web: ET.SubElement(root, "browser", time=b[0].strftime(fmt), device=b[1], url=b[2], title=b[3])
    for a in appev: ET.SubElement(root, "appevent", time=a[0].strftime(fmt), device=a[1], package=a[2], activity=a[3])
    ET.ElementTree(root).write(out / "report.xml")
    return {"messages": len(msgs), "calls": len(calls), "locations": len(locs), "browser": len(web)}
