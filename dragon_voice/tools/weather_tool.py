"""Weather tool: get current weather via Open-Meteo (free, no API key)."""

import logging

import aiohttp

from dragon_voice.tools.base import Tool

logger = logging.getLogger(__name__)

# WMO weather interpretation codes
# https://open-meteo.com/en/docs#weathervariables
WMO_CODES = {
    0: "Clear sky",
    1: "Mainly clear",
    2: "Partly cloudy",
    3: "Overcast",
    45: "Foggy",
    48: "Rime fog",
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
    71: "Slight snow",
    73: "Moderate snow",
    75: "Heavy snow",
    77: "Snow grains",
    80: "Slight showers",
    81: "Moderate showers",
    82: "Violent showers",
    85: "Slight snow showers",
    86: "Heavy snow showers",
    95: "Thunderstorm",
    96: "Thunderstorm with slight hail",
    99: "Thunderstorm with heavy hail",
}

GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"


class WeatherTool(Tool):
    """Get current weather for a location using Open-Meteo (free, no API key)."""

    @property
    def name(self) -> str:
        return "weather"

    @property
    def description(self) -> str:
        return "Get current weather for a location"

    @property
    def parameters_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "location": {
                    "type": "string",
                    "description": "City or place name (e.g., 'Dubai', 'Tokyo')",
                },
                "latitude": {
                    "type": "number",
                    "description": "Latitude (use with longitude instead of location)",
                },
                "longitude": {
                    "type": "number",
                    "description": "Longitude (use with latitude instead of location)",
                },
            },
        }

    async def execute(self, args: dict) -> dict:
        location = args.get("location", "")
        latitude = args.get("latitude")
        longitude = args.get("longitude")

        if not location and (latitude is None or longitude is None):
            return {"error": "Provide 'location' or both 'latitude' and 'longitude'"}

        try:
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=10)
            ) as session:
                # Step 1: Geocode location name to coordinates
                if location and (latitude is None or longitude is None):
                    geo_result = await self._geocode(session, location)
                    if "error" in geo_result:
                        return geo_result
                    latitude = geo_result["latitude"]
                    longitude = geo_result["longitude"]
                    resolved_name = geo_result["name"]
                else:
                    resolved_name = f"{latitude:.2f}, {longitude:.2f}"

                # Step 2: Fetch current weather
                weather = await self._fetch_weather(session, latitude, longitude)
                if "error" in weather:
                    return weather

                weather["location"] = resolved_name
                return weather

        except aiohttp.ClientError as e:
            logger.exception("Weather API request failed")
            return {"error": f"Weather API request failed: {e}"}
        except Exception as e:
            logger.exception("Weather tool error")
            return {"error": str(e)}

    async def _geocode(self, session: aiohttp.ClientSession, location: str) -> dict:
        """Geocode a location name to latitude/longitude."""
        async with session.get(
            GEOCODE_URL, params={"name": location, "count": 1, "language": "en"}
        ) as resp:
            if resp.status != 200:
                return {"error": f"Geocoding failed (HTTP {resp.status})"}
            data = await resp.json()

        results = data.get("results", [])
        if not results:
            return {"error": f"Location '{location}' not found"}

        top = results[0]
        country = top.get("country", "")
        name = top.get("name", location)
        display = f"{name}, {country}" if country else name

        return {
            "name": display,
            "latitude": top["latitude"],
            "longitude": top["longitude"],
        }

    async def _fetch_weather(
        self, session: aiohttp.ClientSession, lat: float, lon: float
    ) -> dict:
        """Fetch current weather from Open-Meteo."""
        params = {
            "latitude": lat,
            "longitude": lon,
            "current": "temperature_2m,relative_humidity_2m,weather_code,wind_speed_10m,apparent_temperature",
        }
        async with session.get(FORECAST_URL, params=params) as resp:
            if resp.status != 200:
                return {"error": f"Weather API failed (HTTP {resp.status})"}
            data = await resp.json()

        current = data.get("current", {})
        weather_code = current.get("weather_code", -1)
        condition = WMO_CODES.get(weather_code, f"Unknown ({weather_code})")

        return {
            "temperature_c": current.get("temperature_2m"),
            "feels_like_c": current.get("apparent_temperature"),
            "humidity": current.get("relative_humidity_2m"),
            "wind_kph": current.get("wind_speed_10m"),
            "condition": condition,
            "weather_code": weather_code,
        }
