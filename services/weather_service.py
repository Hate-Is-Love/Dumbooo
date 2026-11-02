"""Current weather and today's/tomorrow's forecast from Open-Meteo (free, no API key).

* Geocoding: https://geocoding-api.open-meteo.com/v1/search?name=<city>
* Forecast:  https://api.open-meteo.com/v1/forecast (current conditions + 2 days, local time zone)

Only the city name and its coordinates are sent; nothing about the user. Results are
cached for 10 minutes per city. Errors become ``WeatherError`` with a plain message.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

log = logging.getLogger(__name__)

GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
CACHE_S = 600

# WMO weather interpretation codes (Open-Meteo documentation).
CODES = {
    0: "clear sky", 1: "mainly clear", 2: "partly cloudy", 3: "overcast", 45: "foggy", 48: "foggy with frost",
    51: "light drizzle", 53: "drizzle", 55: "heavy drizzle", 56: "freezing drizzle", 57: "freezing drizzle",
    61: "light rain", 63: "rain", 65: "heavy rain", 66: "freezing rain", 67: "heavy freezing rain",
    71: "light snow", 73: "snow", 75: "heavy snow", 77: "snow grains", 80: "light showers", 81: "showers",
    82: "violent showers", 85: "snow showers", 86: "heavy snow showers", 95: "a thunderstorm",
    96: "a thunderstorm with hail", 99: "a severe thunderstorm with hail",
}


class WeatherError(Exception):
    def __init__(self, message: str) -> None:
        self.user_message = message
        super().__init__(message)


@dataclass
class Place:
    name: str
    region: str
    country: str
    latitude: float
    longitude: float

    @property
    def label(self) -> str:
        return ", ".join(x for x in (self.name, self.region if self.region != self.name else "", self.country) if x)


@dataclass
class DayForecast:
    day: str
    low: float
    high: float
    rain_chance: int | None
    condition: str


@dataclass
class Weather:
    place: Place
    temperature: float
    feels_like: float
    humidity: int
    wind_kmh: float
    condition: str
    is_day: bool
    local_time: datetime
    days: list[DayForecast]


class WeatherService:
    name = "weather"

    def __init__(self, client=None, clock=time.monotonic) -> None:
        self._client = client
        self._clock = clock
        self._cache: dict[str, tuple[float, Weather]] = {}
        self._lock = threading.Lock()

    def _http(self):
        if self._client is None:
            import httpx

            self._client = httpx.Client(timeout=10.0, headers={"User-Agent": "DUMBOO desktop assistant"})
        return self._client

    def _get(self, url: str, params: dict) -> dict:
        try:
            response = self._http().get(url, params=params)
        except Exception as exc:
            log.warning("Weather request failed: %s", exc.__class__.__name__)
            raise WeatherError("I couldn't reach the weather service. Check your internet connection.") from exc
        if response.status_code != 200:
            raise WeatherError(f"The weather service answered with an error (HTTP {response.status_code}).")
        try:
            return response.json()
        except ValueError as exc:
            raise WeatherError("The weather service sent something I couldn't read.") from exc

    def find_place(self, city: str) -> Place:
        data = self._get(GEOCODE_URL, {"name": city.strip(), "count": 1, "language": "en", "format": "json"})
        results = data.get("results") or []
        if not results:
            raise WeatherError(f"I couldn't find a place called “{city}”.")
        r = results[0]
        return Place(r.get("name", city), r.get("admin1", ""), r.get("country", ""), float(r["latitude"]),
                     float(r["longitude"]))

    def current(self, city: str) -> Weather:
        key = " ".join(city.lower().split())
        with self._lock:
            hit = self._cache.get(key)
            if hit and self._clock() - hit[0] < CACHE_S:
                return hit[1]
        place = self.find_place(city)
        data = self._get(FORECAST_URL, {
            "latitude": place.latitude, "longitude": place.longitude, "timezone": "auto", "forecast_days": 2,
            "current": "temperature_2m,apparent_temperature,relative_humidity_2m,weather_code,wind_speed_10m,is_day",
            "daily": "temperature_2m_max,temperature_2m_min,precipitation_probability_max,weather_code",
        })
        try:
            cur, daily = data["current"], data["daily"]
            offset = timedelta(seconds=int(data.get("utc_offset_seconds", 0)))
            days = [DayForecast(daily["time"][i], float(daily["temperature_2m_min"][i]),
                                float(daily["temperature_2m_max"][i]),
                                (daily.get("precipitation_probability_max") or [None] * 2)[i],
                                CODES.get(int(daily["weather_code"][i]), "mixed weather"))
                    for i in range(min(2, len(daily["time"])))]
            weather = Weather(place, float(cur["temperature_2m"]), float(cur["apparent_temperature"]),
                              int(cur["relative_humidity_2m"]), float(cur["wind_speed_10m"]),
                              CODES.get(int(cur["weather_code"]), "mixed weather"), bool(cur.get("is_day", 1)),
                              datetime.now(timezone.utc) + offset, days)
        except (KeyError, TypeError, ValueError, IndexError) as exc:
            raise WeatherError("The weather service sent something I couldn't read.") from exc
        with self._lock:
            self._cache[key] = (self._clock(), weather)
        return weather

    def local_time(self, city: str) -> tuple[Place, datetime]:
        """The current time in another city (from the forecast's UTC offset)."""
        weather = self.current(city)
        return weather.place, weather.local_time


def describe_weather(w: Weather, when: str = "now") -> tuple[str, str]:
    """(text for the screen, short sentence to speak)."""
    t = round(w.temperature)
    place = w.place.name
    if when in ("today", "tomorrow"):
        d = w.days[0 if when == "today" else min(1, len(w.days) - 1)]
        rain = f", {d.rain_chance}% chance of rain" if d.rain_chance is not None else ""
        text = (f"**{place} {when}:** {d.condition}, {round(d.low)}–{round(d.high)} °C{rain}."
                + (f"\n\nRight now: {t} °C, {w.condition}." if when == "today" else ""))
        speech = f"{when.capitalize()} in {place}: {d.condition}, between {round(d.low)} and {round(d.high)} degrees"
        speech += f", with a {d.rain_chance} percent chance of rain." if d.rain_chance is not None else "."
        return text, speech
    today = w.days[0] if w.days else None
    text = (f"**{place} now:** {t} °C (feels like {round(w.feels_like)} °C), {w.condition}. "
            f"Humidity {w.humidity}%, wind {round(w.wind_kmh)} km/h.")
    if today:
        rain = f", {today.rain_chance}% chance of rain" if today.rain_chance is not None else ""
        text += f"\n\nToday: {round(today.low)}–{round(today.high)} °C{rain}."
    text += f"\n\n_{w.place.label} · Open-Meteo_"
    speech = f"It's {t} degrees in {place} right now, {w.condition}"
    if round(w.feels_like) != t:
        speech += f", feels like {round(w.feels_like)}"
    if today and today.rain_chance is not None and today.rain_chance >= 40:
        speech += f". There's a {today.rain_chance} percent chance of rain today"
    return text, speech + "."


def will_it_rain(w: Weather, when: str) -> tuple[str, str]:
    d = w.days[0 if when != "tomorrow" else min(1, len(w.days) - 1)]
    chance = d.rain_chance or 0
    answer = ("Yes, probably" if chance >= 60 else "Maybe" if chance >= 30 else "Probably not")
    text = f"{answer}: {chance}% chance of rain in {w.place.name} {when} ({d.condition})."
    if chance >= 40:
        text += " Take an umbrella. ☂"
    return text, text.replace(" ☂", "")
