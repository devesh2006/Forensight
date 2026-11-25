"""Extract explicitly recorded coordinates. No geocoding or interpolation."""

def point(coordinates):
    if not isinstance(coordinates, (list, tuple)) or len(coordinates) < 2:
        return None, None
    return coordinates[1], coordinates[0]


def geojson_records(document):
    if not isinstance(document, dict):
        raise ValueError("GeoJSON must contain an object")
    features = document.get("features") if document.get("type") == "FeatureCollection" else [document]
    if not isinstance(features, list):
        raise ValueError("Invalid GeoJSON feature collection")
    rows, errors = [], []
    for index, feature in enumerate(features, 1):
        geometry = feature.get("geometry", feature) if isinstance(feature, dict) else None
        if not isinstance(geometry, dict) or geometry.get("type") != "Point":
            errors.append({"record": index, "error": "Only GeoJSON Point geometry is mapped"})
            continue
        lat, lon = point(geometry.get("coordinates"))
        properties = feature.get("properties")
        row = dict(properties) if isinstance(properties, dict) else {}
        row.update(type="location", latitude=lat, longitude=lon)
        rows.append((row, f"feature {index}", feature))
    return rows, errors


def xml_geo_records(root, suffix):
    rows, errors = [], []
    strip = lambda tag: tag.rsplit("}", 1)[-1]
    for index, node in enumerate(root.iter(), 1):
        tag = strip(node.tag)
        if suffix == ".gpx" and tag in {"wpt", "trkpt", "rtept"}:
            children = {strip(child.tag): child.text for child in node}
            raw = {"attributes": dict(node.attrib), "fields": children}
            rows.append(({"type": "location", "latitude": node.get("lat"), "longitude": node.get("lon"),
                          "timestamp": children.get("time"), "title": children.get("name")},
                         f"{tag} at XML element {index}", raw))
        if suffix == ".kml" and tag == "Placemark":
            fields = {strip(child.tag): child.text for child in node}
            geometry = next((child for child in node if strip(child.tag) == "Point"), None)
            if geometry is None:
                errors.append({"record": index, "error": "Only KML Point placemarks are mapped"})
                continue
            coordinates = next((c.text for c in geometry if strip(c.tag) == "coordinates"), "") or ""
            lat, lon = point(coordinates.strip().split(","))
            timestamp = next((c.text for stamp in node if strip(stamp.tag) == "TimeStamp"
                              for c in stamp if strip(c.tag) == "when"), None)
            rows.append(({"type": "location", "latitude": lat, "longitude": lon,
                          "timestamp": timestamp, "title": fields.get("name"), "content":fields.get("description")},
                         f"Placemark at XML element {index}",
                         {"coordinates": coordinates, "timestamp": timestamp, "fields": fields}))
    return rows, errors
