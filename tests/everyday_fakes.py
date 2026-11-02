"""Offline stand-ins for the weather service (Open-Meteo)."""

from __future__ import annotations

from types import SimpleNamespace

PLACES = {
    "pune": {"name": "Pune", "admin1": "Maharashtra", "country": "India", "latitude": 18.52, "longitude": 73.86,
             "offset": 19800, "temp": 28.4, "feels": 30.1, "code": 2, "rain": [10, 70]},
    "mumbai": {"name": "Mumbai", "admin1": "Maharashtra", "country": "India", "latitude": 19.07, "longitude": 72.88,
               "offset": 19800, "temp": 31.0, "feels": 36.0, "code": 61, "rain": [80, 40]},
    "london": {"name": "London", "admin1": "England", "country": "United Kingdom", "latitude": 51.5,
               "longitude": -0.12, "offset": 3600, "temp": 12.0, "feels": 10.0, "code": 3, "rain": [20, 5]},
}


class FakeWeatherHTTP:
    """Answers the two Open-Meteo endpoints from ``PLACES``; records every request."""

    def __init__(self, fail: bool = False) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.fail = fail

    def get(self, url: str, params: dict):
        self.calls.append((url, dict(params)))
        if self.fail:
            raise OSError("network down")
        if "geocoding" in url:
            place = PLACES.get(params["name"].lower())
            return _resp({"results": [place]} if place else {})
        place = next(p for p in PLACES.values() if p["latitude"] == params["latitude"])
        return _resp({
            "utc_offset_seconds": place["offset"],
            "current": {"temperature_2m": place["temp"], "apparent_temperature": place["feels"],
                        "relative_humidity_2m": 60, "weather_code": place["code"], "wind_speed_10m": 8.0, "is_day": 1},
            "daily": {"time": ["2026-10-22", "2026-10-23"], "temperature_2m_min": [22.0, 21.0],
                      "temperature_2m_max": [33.0, 31.0], "precipitation_probability_max": place["rain"],
                      "weather_code": [place["code"], 61]},
        })


def _resp(data: dict):
    return SimpleNamespace(status_code=200, json=lambda: data)
