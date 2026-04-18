"""Widget Platform v1 — skill-facing surfaces.

Skills call Tab5Surface methods to emit typed widget state to connected
devices. See ../../../TinkerTab/docs/WIDGETS.md for the full spec and
TinkerTab docs/protocol.md §17 for the on-wire message schemas.
"""
from .base import Tab5Surface
from .manager import SurfaceManager

__all__ = ["Tab5Surface", "SurfaceManager"]
