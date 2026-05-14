"""Weather tools for the Weather Agent (CTR-0027, PRP-0017).

Provides geocoding and weather data via Open-Meteo APIs (free, no API key required).
Tools are registered as MAF function tools using the @tool decorator.
"""

from dataclasses import asdict
import json
import logging
from typing import Annotated

import httpx
from pydantic import Field

from app.weather.models import Coordinates, Location

logger = logging.getLogger(__name__)

# Open-Meteo API endpoints (free, no API key required)
GEOCODING_URL = "https://geocoding-api.open-meteo.com/v1/search"
WEATHER_URL = "https://api.open-meteo.com/v1/forecast"


def _geocode_city(city: str, country: str = "") -> Location:
    """Convert city name to coordinates using Open-Meteo geocoding API."""
    params = {"name": city, "count": "5", "language": "en", "format": "json"}
    with httpx.Client(timeout=10.0) as client:
        response = client.get(GEOCODING_URL, params=params)
        response.raise_for_status()
        data = response.json()

    if "results" not in data or not data["results"]:
        msg = f"City '{city}' not found. Please check the spelling or try a different city name."
        raise ValueError(msg)

    results = data["results"]
    location = results[0]
    if country:
        for result in results:
            if result.get("country_code", "").upper() == country.upper():
                location = result
                break

    return Location(
        name=location["name"],
        country=location.get("country", ""),
        region=location.get("admin1", ""),
        coordinates=Coordinates(
            latitude=location["latitude"],
            longitude=location["longitude"],
        ),
    )


def _fetch_current_weather(coords: Coordinates) -> dict:
    """Fetch current weather from Open-Meteo API."""
    params = {
        "latitude": str(coords.latitude),
        "longitude": str(coords.longitude),
        "current": [
            "temperature_2m",
            "relative_humidity_2m",
            "apparent_temperature",
            "weather_code",
            "wind_speed_10m",
            "wind_direction_10m",
            "precipitation",
        ],
        "timezone": "auto",
    }
    with httpx.Client(timeout=10.0) as client:
        response = client.get(WEATHER_URL, params=params)
        response.raise_for_status()
        return response.json()


def _fetch_weekly_forecast(coords: Coordinates) -> dict:
    """Fetch 7-day daily forecast from Open-Meteo API."""
    params = {
        "latitude": str(coords.latitude),
        "longitude": str(coords.longitude),
        "daily": [
            "temperature_2m_max",
            "temperature_2m_min",
            "weather_code",
            "precipitation_sum",
            "precipitation_probability_max",
        ],
        "timezone": "auto",
        "forecast_days": 7,
    }
    with httpx.Client(timeout=10.0) as client:
        response = client.get(WEATHER_URL, params=params)
        response.raise_for_status()
        return response.json()


def _get_weather_description(weather_code: int) -> str:
    """Convert WMO weather code to human-readable description."""
    weather_codes = {
        0: "Clear sky",
        1: "Mainly clear",
        2: "Partly cloudy",
        3: "Overcast",
        45: "Foggy",
        48: "Depositing rime fog",
        51: "Light drizzle",
        53: "Moderate drizzle",
        55: "Dense drizzle",
        56: "Light freezing drizzle",
        57: "Dense freezing drizzle",
        61: "Slight rain",
        63: "Moderate rain",
        65: "Heavy rain",
        66: "Light freezing rain",
        67: "Heavy freezing rain",
        71: "Slight snow fall",
        73: "Moderate snow fall",
        75: "Heavy snow fall",
        77: "Snow grains",
        80: "Slight rain showers",
        81: "Moderate rain showers",
        82: "Violent rain showers",
        85: "Slight snow showers",
        86: "Heavy snow showers",
        95: "Thunderstorm",
        96: "Thunderstorm with slight hail",
        99: "Thunderstorm with heavy hail",
    }
    return weather_codes.get(weather_code, "Unknown")


def get_coords_by_city(
    city: Annotated[str, Field(description="The city name (e.g., 'London', 'New York', 'Tokyo')")],
    country: Annotated[str, Field(description="Optional country code to disambiguate (e.g., 'US', 'UK', 'JP')")] = "",
) -> str:
    """Get latitude and longitude for a city. Use when you need coordinates for a place name."""
    try:
        location = _geocode_city(city, country)
        logger.info("Geocoded '%s' -> %s, %s (%s)", city, location.name, location.country, location.coordinates)
        return json.dumps(asdict(location))
    except ValueError as e:
        return str(e)
    except httpx.HTTPError as e:
        return f"Error fetching coordinates: {e}"


def get_current_weather_by_coords(
    latitude: Annotated[float, Field(description="Latitude (-90 to 90)")],
    longitude: Annotated[float, Field(description="Longitude (-180 to 180)")],
    location_name: Annotated[str, Field(description="Human-readable location name for display")] = "",
) -> str:
    """Get the current weather at a location given its latitude and longitude."""
    try:
        coords = Coordinates(latitude=latitude, longitude=longitude)
        data = _fetch_current_weather(coords)
        current = data["current"]
        units = data["current_units"]
        weather_desc = _get_weather_description(current["weather_code"])

        label = location_name or f"({coords.latitude:.2f}, {coords.longitude:.2f})"
        result = {
            "location": f"Current Weather at {label}",
            "conditions": weather_desc,
            "temperature": f"{current['temperature_2m']}{units['temperature_2m']}",
            "feelsLike": f"{current['apparent_temperature']}{units['apparent_temperature']}",
            "humidity": f"{current['relative_humidity_2m']}{units['relative_humidity_2m']}",
            "wind": (
                f"{current['wind_speed_10m']} {units['wind_speed_10m']} "
                f"from {current['wind_direction_10m']}{units['wind_direction_10m']}"
            ),
            "precipitation": f"{current['precipitation']} {units['precipitation']}",
            "dataTime": f"{current['time']} ({data['timezone']})",
        }
        logger.info("Weather fetched for %s: %s, %s", label, weather_desc, result["temperature"])
        return json.dumps(result)
    except httpx.HTTPError as e:
        return json.dumps({"error": f"Error fetching weather data: {e}"})


def get_weather_next_week(
    latitude: Annotated[float, Field(description="Latitude (-90 to 90)")],
    longitude: Annotated[float, Field(description="Longitude (-180 to 180)")],
    location_name: Annotated[str, Field(description="Human-readable location name for display")] = "",
) -> str:
    """Get the 7-day weather forecast at a location given its latitude and longitude."""
    try:
        coords = Coordinates(latitude=latitude, longitude=longitude)
        data = _fetch_weekly_forecast(coords)
        daily = data["daily"]
        units = data["daily_units"]

        label = location_name or f"({coords.latitude:.2f}, {coords.longitude:.2f})"
        days = [
            {
                "date": daily["time"][i],
                "conditions": _get_weather_description(daily["weather_code"][i]),
                "tempMax": f"{daily['temperature_2m_max'][i]}{units['temperature_2m_max']}",
                "tempMin": f"{daily['temperature_2m_min'][i]}{units['temperature_2m_min']}",
                "precipitation": f"{daily['precipitation_sum'][i]} {units['precipitation_sum']}",
                "precipitationProbability": f"{daily['precipitation_probability_max'][i]}{units['precipitation_probability_max']}",
            }
            for i in range(len(daily["time"]))
        ]

        result = {
            "location": f"Weekly Forecast at {label}",
            "timezone": data["timezone"],
            "days": days,
        }
        logger.info("Forecast fetched for %s: %d days", label, len(days))
        return json.dumps(result)
    except httpx.HTTPError as e:
        return json.dumps({"error": f"Error fetching forecast: {e}"})
