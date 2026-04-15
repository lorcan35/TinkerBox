"""Media package — binary file storage for Dragon server-side rendering.

Exports:
    MediaStore — filesystem-backed store (UUID IDs, age/size cleanup).
    MediaPipeline — (future) render + store pipeline.
"""

from dragon_voice.media.store import MediaStore

__all__ = ["MediaStore"]
