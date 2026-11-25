"""Offline-friendly timezone lookup with fixed-offset fallbacks for common zones."""
from datetime import timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


_FIXED_OFFSETS = {
    "Asia/Kolkata": timezone(timedelta(hours=5, minutes=30), "IST"),
    "Asia/Calcutta": timezone(timedelta(hours=5, minutes=30), "IST"),
    "UTC": timezone.utc,
    "Etc/UTC": timezone.utc,
    "GMT": timezone.utc,
}


def case_zone(name: str):
    if name in _FIXED_OFFSETS:
        # Use the OS IANA database where available, otherwise this fixed zone is correct year-round.
        try:
            return ZoneInfo(name)
        except ZoneInfoNotFoundError:
            return _FIXED_OFFSETS[name]
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, TypeError) as exc:
        raise ValueError(f"Timezone data for {name!r} is unavailable; install the tzdata package.") from exc
