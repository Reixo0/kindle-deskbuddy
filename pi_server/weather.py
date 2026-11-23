"""Lightweight weather fetcher backed by Open-Meteo (free, no API key required)."""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Optional

from .config import Settings

logger = logging.getLogger("deskbuddy.weather")


@dataclass(frozen=True)
class WeatherData:
    temp: float
    temp_min: float
    temp_max: float
    condition: str  # "clear" | "partly_cloudy" | "cloudy" | "fog" | "rain" | "snow" | "storm"
    label: str
    unit: str = "C"  # "C" or "F"


def wmo_code_to_condition(code: int) -> tuple[str, str]:
    """Map WMO weather interpretation code to (icon_kind, short_label)."""
    if code == 0:
        return "clear", "Sunny"
    if code in (1, 2):
        return "partly_cloudy", "Partly Cloudy"
    if code == 3:
        return "cloudy", "Cloudy"
    if code in (45, 48):
        return "fog", "Foggy"
    if code in (51, 53, 55, 56, 57, 61, 63, 65, 66, 67, 80, 81, 82):
        return "rain", "Rainy"
    if code in (71, 73, 75, 77, 85, 86):
        return "snow", "Snowy"
    if code in (95, 96, 99):
        return "storm", "Stormy"
    return "cloudy", "Cloudy"


class WeatherService:
    """Non-blocking cached weather service (refreshes every 15 minutes)."""

    def __init__(self, settings: Settings, demo_mode: bool = False) -> None:
        self._settings = settings
        self._demo = demo_mode
        self._lock = threading.Lock()
        self._data: Optional[WeatherData] = None
        self._last_fetch: float = 0.0
        self._inflight: bool = False
        self._coords: Optional[tuple[float, float]] = (
            (settings.weather_lat, settings.weather_lon)
            if settings.weather_lat is not None and settings.weather_lon is not None
            else None
        )

    def get_weather(self) -> Optional[WeatherData]:
        if self._demo:
            unit = self._settings.weather_units
            if unit == "F":
                return WeatherData(66.0, 54.0, 72.0, "partly_cloudy", "Partly Cloudy", "F")
            return WeatherData(19.0, 12.0, 22.0, "partly_cloudy", "Partly Cloudy", "C")

        now = time.monotonic()
        with self._lock:
            cached = self._data
            stale = (now - self._last_fetch) >= 900.0  # 15 minutes
            if not stale or self._inflight:
                return cached
            self._inflight = True

        thread = threading.Thread(target=self._fetch_worker, daemon=True)
        thread.start()
        return cached

    def _resolve_coords(self) -> Optional[tuple[float, float]]:
        if self._coords is not None:
            return self._coords
        try:
            import requests

            city = self._settings.weather_city
            if city:
                resp = requests.get(
                    "https://geocoding-api.open-meteo.com/v1/search",
                    params={"name": city, "count": 1, "language": "en", "format": "json"},
                    timeout=5,
                )
                if resp.status_code == 200:
                    results = (resp.json() or {}).get("results") or []
                    if results:
                        self._coords = (float(results[0]["latitude"]), float(results[0]["longitude"]))
                        return self._coords

            # Automatic IP geolocation fallback if no city/coords configured
            resp = requests.get("https://ipwho.is/", timeout=5)
            if resp.status_code == 200:
                payload = resp.json() or {}
                if payload.get("latitude") is not None and payload.get("longitude") is not None:
                    self._coords = (float(payload["latitude"]), float(payload["longitude"]))
                    return self._coords
        except Exception as exc:
            logger.debug("Weather geolocation failed: %s", exc)
        return None

    def _fetch_worker(self) -> None:
        try:
            import requests

            coords = self._resolve_coords()
            if not coords:
                return
            lat, lon = coords
            unit = self._settings.weather_units
            temp_unit = "fahrenheit" if unit == "F" else "celsius"
            resp = requests.get(
                "https://api.open-meteo.com/v1/forecast",
                params={
                    "latitude": lat,
                    "longitude": lon,
                    "current": "temperature_2m,weather_code",
                    "daily": "temperature_2m_max,temperature_2m_min",
                    "temperature_unit": temp_unit,
                    "timezone": "auto",
                    "forecast_days": 1,
                },
                timeout=6,
            )
            if resp.status_code != 200:
                return
            data = resp.json() or {}
            current = data.get("current") or {}
            daily = data.get("daily") or {}
            temp = current.get("temperature_2m")
            code = int(current.get("weather_code", 3))
            mins = daily.get("temperature_2m_min") or []
            maxs = daily.get("temperature_2m_max") or []
            if temp is None:
                return
            t_min = float(mins[0]) if mins else float(temp)
            t_max = float(maxs[0]) if maxs else float(temp)
            cond, label = wmo_code_to_condition(code)
            weather = WeatherData(
                temp=float(temp),
                temp_min=t_min,
                temp_max=t_max,
                condition=cond,
                label=label,
                unit=unit,
            )
            with self._lock:
                self._data = weather
                self._last_fetch = time.monotonic()
        except Exception as exc:
            logger.debug("Weather fetch failed: %s", exc)
        finally:
            with self._lock:
                self._inflight = False
                if self._last_fetch == 0.0:
                    # Retry in 60s instead of spinning on every poll if offline
                    self._last_fetch = time.monotonic() - 840.0
