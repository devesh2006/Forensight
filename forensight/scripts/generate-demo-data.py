"""One-time export of the local PROJECT NIGHTFALL synthetic case to a static JS bundle."""
import csv
import hashlib
import json
import re
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path

root = Path(__file__).resolve().parents[1]
case = root / 'data' / 'nightfall'
people_root = ET.parse(case / 'report.xml').getroot()
contacts = {x.get('number'): x.get('name') for x in people_root.findall('contact')}
devices = {x.get('device'): x.get('name') for x in people_root.findall('contact') if x.get('device')}

def person(number):
    name = contacts.get(number, number or 'Unknown')
    return f'Unknown ({number})' if name == 'Unknown' else name

def make(kind, time, actor, target, summary, source, metadata, lat=None, lon=None):
    payload = [kind, time, actor, target, summary, lat, lon]
    digest = hashlib.sha256(json.dumps(payload, separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()
    return {'type': kind, 'time': time, 'actor': actor, 'target': target, 'summary': summary,
            'source': source, 'metadata': metadata, 'lat': lat, 'lon': lon, 'sha256': digest}

rows = []
for i, x in enumerate(people_root.findall('browser'), 1):
    actor = devices.get(x.get('device'), x.get('device', 'Unknown'))
    rows.append(make('browser', x.get('time'), actor, '', x.get('title', x.get('url', 'Browser activity')),
                     f'report.xml:browser[{i}]', dict(x.attrib)))
for i, x in enumerate(people_root.findall('appevent'), 1):
    actor = devices.get(x.get('device'), x.get('device', 'Unknown'))
    summary = f"{x.get('activity')} · {x.get('package')}"
    rows.append(make('app', x.get('time'), actor, '', summary, f'report.xml:appevent[{i}]', dict(x.attrib)))
with (case / 'messages.csv').open(encoding='utf-8', newline='') as f:
    for i, x in enumerate(csv.DictReader(f), 1):
        rows.append(make('message', x['timestamp'], person(x['sender']), person(x['receiver']), x['text'],
                         f'messages.csv:row[{i}]', x))
for kind, filename, source_key in [('call', 'calls.json', 'calls.json'), ('location', 'locations.json', 'locations.json')]:
    for i, x in enumerate(json.loads((case / filename).read_text(encoding='utf-8')), 1):
        if kind == 'call':
            actor, target = person(x.get('caller')), person(x.get('receiver'))
            summary = f"{x.get('type', 'call')} call · {x.get('duration', 0)} sec"
            rows.append(make(kind, x['time'], actor, target, summary, f'{source_key}:row[{i}]', x))
        else:
            actor = devices.get(x.get('device'), x.get('device', 'Unknown'))
            summary = f"GPS fix · ±{x.get('accuracy', '?')} m · {x.get('source', 'GPS')}"
            rows.append(make(kind, x['time'], actor, '', summary, f'{source_key}:row[{i}]', x, x.get('lat'), x.get('lon')))
for i, row in enumerate(rows, 1):
    row['id'] = f'E-{i:04d}'
    row['metadata']['artifact_id'] = row['id']
contact_rows = [{'name': person(x.get('number')), 'number': x.get('number'), 'device': x.get('device') or None}
                for x in people_root.findall('contact')]
bundle = {'case': 'PROJECT NIGHTFALL', 'case_id': 'NF-2026-001', 'incident': '2026-10-15T22:30:00',
          'incident_location': {'lat': 28.6118, 'lon': 77.2437, 'name': 'Old Fort Parking, New Delhi'},
          'people': sorted({x['name'] for x in contact_rows}), 'contacts': contact_rows, 'events': rows}
(root / 'frontend' / 'demo-data.js').write_text('window.NIGHTFALL_DATA = ' + json.dumps(bundle, ensure_ascii=False, separators=(',', ':')) + ';\n', encoding='utf-8')
print(f'Exported {len(rows)} NIGHTFALL artifacts to frontend/demo-data.js')
