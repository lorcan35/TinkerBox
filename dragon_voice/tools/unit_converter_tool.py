"""Unit converter tool: convert between common units."""

import logging

from dragon_voice.tools.base import Tool

logger = logging.getLogger(__name__)

# Conversion tables: each unit maps to a base unit and a conversion factor/function.
# For linear conversions: (base_unit, factor) where unit_value * factor = base_value.
# For temperature: handled separately.

# Base units: meter, kilogram, liter, m/s, celsius

LENGTH_TO_BASE = {
    "meters": 1.0,
    "meter": 1.0,
    "m": 1.0,
    "kilometers": 1000.0,
    "km": 1000.0,
    "centimeters": 0.01,
    "cm": 0.01,
    "millimeters": 0.001,
    "mm": 0.001,
    "miles": 1609.344,
    "mile": 1609.344,
    "mi": 1609.344,
    "feet": 0.3048,
    "foot": 0.3048,
    "ft": 0.3048,
    "inches": 0.0254,
    "inch": 0.0254,
    "in": 0.0254,
    "yards": 0.9144,
    "yard": 0.9144,
    "yd": 0.9144,
}

WEIGHT_TO_BASE = {
    "kilograms": 1.0,
    "kilogram": 1.0,
    "kg": 1.0,
    "grams": 0.001,
    "gram": 0.001,
    "g": 0.001,
    "milligrams": 0.000001,
    "mg": 0.000001,
    "pounds": 0.453592,
    "pound": 0.453592,
    "lbs": 0.453592,
    "lb": 0.453592,
    "ounces": 0.0283495,
    "ounce": 0.0283495,
    "oz": 0.0283495,
    "tons": 1000.0,
    "ton": 1000.0,
}

VOLUME_TO_BASE = {
    "liters": 1.0,
    "liter": 1.0,
    "l": 1.0,
    "milliliters": 0.001,
    "ml": 0.001,
    "gallons": 3.78541,
    "gallon": 3.78541,
    "gal": 3.78541,
    "cups": 0.236588,
    "cup": 0.236588,
    "pints": 0.473176,
    "pint": 0.473176,
    "pt": 0.473176,
    "quarts": 0.946353,
    "quart": 0.946353,
    "qt": 0.946353,
    "tablespoons": 0.0147868,
    "tbsp": 0.0147868,
    "teaspoons": 0.00492892,
    "tsp": 0.00492892,
}

SPEED_TO_BASE = {
    "m/s": 1.0,
    "kph": 1 / 3.6,
    "kmh": 1 / 3.6,
    "km/h": 1 / 3.6,
    "mph": 0.44704,
    "knots": 0.514444,
    "knot": 0.514444,
    "kn": 0.514444,
    "ft/s": 0.3048,
}

TEMP_UNITS = {"celsius", "fahrenheit", "kelvin", "c", "f", "k"}

# Group all linear tables for lookup
LINEAR_TABLES = {
    "length": LENGTH_TO_BASE,
    "weight": WEIGHT_TO_BASE,
    "volume": VOLUME_TO_BASE,
    "speed": SPEED_TO_BASE,
}


def _normalize_unit(unit: str) -> str:
    """Normalize unit name to lowercase, strip whitespace."""
    return unit.strip().lower()


def _find_table(unit: str) -> tuple[str, dict] | None:
    """Find which conversion table a unit belongs to."""
    for category, table in LINEAR_TABLES.items():
        if unit in table:
            return category, table
    return None


def _convert_temperature(value: float, from_unit: str, to_unit: str) -> float:
    """Convert between temperature units."""
    # Normalize aliases
    aliases = {"c": "celsius", "f": "fahrenheit", "k": "kelvin"}
    from_u = aliases.get(from_unit, from_unit)
    to_u = aliases.get(to_unit, to_unit)

    # Convert to Celsius first
    if from_u == "celsius":
        c = value
    elif from_u == "fahrenheit":
        c = (value - 32) * 5 / 9
    elif from_u == "kelvin":
        c = value - 273.15
    else:
        raise ValueError(f"Unknown temperature unit: {from_unit}")

    # Convert from Celsius to target
    if to_u == "celsius":
        return c
    elif to_u == "fahrenheit":
        return c * 9 / 5 + 32
    elif to_u == "kelvin":
        return c + 273.15
    else:
        raise ValueError(f"Unknown temperature unit: {to_unit}")


class UnitConverterTool(Tool):
    """Convert between common units (temperature, length, weight, volume, speed)."""

    @property
    def name(self) -> str:
        return "convert"

    @property
    def description(self) -> str:
        return "Convert between units (temperature, length, weight, volume, speed)"

    @property
    def parameters_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "value": {
                    "type": "number",
                    "description": "The value to convert",
                },
                "from": {
                    "type": "string",
                    "description": "Source unit (e.g., 'fahrenheit', 'kg', 'miles', 'liters', 'mph')",
                },
                "to": {
                    "type": "string",
                    "description": "Target unit (e.g., 'celsius', 'lbs', 'km', 'gallons', 'kph')",
                },
            },
            "required": ["value", "from", "to"],
        }

    async def execute(self, args: dict) -> dict:
        value = args.get("value")
        from_unit = _normalize_unit(args.get("from", ""))
        to_unit = _normalize_unit(args.get("to", ""))

        if value is None:
            return {"error": "'value' is required"}
        if not from_unit or not to_unit:
            return {"error": "'from' and 'to' units are required"}

        try:
            value = float(value)
        except (ValueError, TypeError):
            return {"error": f"Invalid value: {value}"}

        # Temperature (special case)
        if from_unit in TEMP_UNITS or to_unit in TEMP_UNITS:
            if from_unit not in TEMP_UNITS or to_unit not in TEMP_UNITS:
                return {"error": f"Cannot convert between temperature and non-temperature units"}
            try:
                result = _convert_temperature(value, from_unit, to_unit)
                result = round(result, 2)
                logger.info("Convert: %s %s -> %s %s", value, from_unit, result, to_unit)
                return {
                    "value": value,
                    "from": from_unit,
                    "to": to_unit,
                    "result": result,
                }
            except ValueError as e:
                return {"error": str(e)}

        # Linear conversions
        from_info = _find_table(from_unit)
        to_info = _find_table(to_unit)

        if from_info is None:
            return {"error": f"Unknown unit: '{from_unit}'"}
        if to_info is None:
            return {"error": f"Unknown unit: '{to_unit}'"}

        from_category, from_table = from_info
        to_category, to_table = to_info

        if from_category != to_category:
            return {"error": f"Cannot convert {from_category} to {to_category}"}

        # Convert: value -> base -> target
        base_value = value * from_table[from_unit]
        result = base_value / to_table[to_unit]
        result = round(result, 6)

        # Clean trailing zeros for display
        if result == int(result) and abs(result) < 1e12:
            display = str(int(result))
        else:
            display = f"{result:g}"

        logger.info("Convert: %s %s -> %s %s", value, from_unit, display, to_unit)
        return {
            "value": value,
            "from": from_unit,
            "to": to_unit,
            "result": result,
        }
