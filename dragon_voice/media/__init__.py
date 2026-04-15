"""Media package — binary file storage and rendering pipeline for Dragon.

Exports:
    MediaStore    — filesystem-backed store (UUID IDs, age/size cleanup).
    MediaPipeline — detect + render code blocks, tables, and image URLs from
                    LLM response text into media events for Tab5.
"""

from dragon_voice.media.store import MediaStore
from dragon_voice.media.pipeline import MediaPipeline

__all__ = ["MediaStore", "MediaPipeline"]
