"""Weather data models (CTR-0027, PRP-0017)."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Coordinates:
    """Latitude and longitude for a location."""

    latitude: float
    longitude: float


@dataclass(frozen=True)
class Location:
    """Location name and coordinates."""

    name: str
    country: str
    region: str
    coordinates: Coordinates
