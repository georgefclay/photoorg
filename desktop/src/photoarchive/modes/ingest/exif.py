from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ExifData:
    taken_at: datetime | None    # naive local time from DateTimeOriginal
    camera: str | None
    gps_lat: float | None
    gps_lon: float | None
    orientation: int | None


def read_exif(path: Path) -> ExifData:
    try:
        import exifread
        with open(path, "rb") as f:
            tags = exifread.process_file(f, details=False, stop_tag="GPS GPSAltitude")
    except Exception as e:
        log.debug("exifread failed for %s: %s", path, e)
        return ExifData(None, None, None, None, None)

    taken_at = _parse_datetime(_tag(tags, "EXIF DateTimeOriginal")
                               or _tag(tags, "Image DateTime"))
    camera = _joined_camera(_tag(tags, "Image Make"), _tag(tags, "Image Model"))
    gps_lat, gps_lon = _parse_gps(tags)
    orientation = _parse_orientation(_tag(tags, "Image Orientation"))

    return ExifData(taken_at, camera, gps_lat, gps_lon, orientation)


def _tag(tags, name):
    v = tags.get(name)
    return str(v) if v is not None else None


def _parse_datetime(s: str | None) -> datetime | None:
    if not s:
        return None
    s = s.strip()
    for fmt in ("%Y:%m:%d %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y:%m:%d %H:%M:%S.%f"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def _joined_camera(make: str | None, model: str | None) -> str | None:
    m = " ".join(x for x in ((make or "").strip(), (model or "").strip()) if x)
    return m or None


def _parse_orientation(s: str | None) -> int | None:
    if not s:
        return None
    for token in s.split():
        try:
            n = int(token)
            if 1 <= n <= 8:
                return n
        except ValueError:
            continue
    lookup = {
        "Horizontal (normal)": 1, "Mirror horizontal": 2, "Rotate 180": 3,
        "Mirror vertical": 4, "Mirror horizontal and rotate 270 CW": 5,
        "Rotate 90 CW": 6, "Mirror horizontal and rotate 90 CW": 7,
        "Rotate 270 CW": 8,
    }
    return lookup.get(s)


def _parse_gps(tags) -> tuple[float | None, float | None]:
    def to_deg(rational_list, ref):
        try:
            parts = rational_list.values
            d = float(parts[0].num) / float(parts[0].den)
            m = float(parts[1].num) / float(parts[1].den)
            s = float(parts[2].num) / float(parts[2].den)
            val = d + m / 60 + s / 3600
            if ref in ("S", "W"):
                val = -val
            return val
        except Exception:
            return None

    lat_v = tags.get("GPS GPSLatitude")
    lat_ref = str(tags.get("GPS GPSLatitudeRef", ""))
    lon_v = tags.get("GPS GPSLongitude")
    lon_ref = str(tags.get("GPS GPSLongitudeRef", ""))
    lat = to_deg(lat_v, lat_ref) if lat_v else None
    lon = to_deg(lon_v, lon_ref) if lon_v else None
    return lat, lon
