"""Image thumbnail helper.

Lists render instantly from a ≤24 KB data-URI thumb; full bytes always travel
through /api/upload + /api/files/{id}, never inside the WebSocket.
"""

from __future__ import annotations

import base64
import io
import logging

logger = logging.getLogger(__name__)

THUMB_MAX_BYTES = 24 * 1024
THUMB_SIZE = (320, 320)


def make_thumb(data: bytes) -> str | None:
    """Build a ≤24 KB JPEG data-URI thumbnail, or None when not an image."""
    try:
        from PIL import Image
    except ImportError:
        return None
    try:
        img = Image.open(io.BytesIO(data))
        img.thumbnail(THUMB_SIZE)
        if img.mode in ("RGBA", "LA", "P"):
            bg = Image.new("RGB", img.size, (255, 255, 255))
            bg.paste(img, mask=img.split()[-1] if img.mode in ("RGBA", "LA") else None)
            img = bg
        elif img.mode != "RGB":
            img = img.convert("RGB")
        for quality in (70, 55, 40):
            buf = io.BytesIO()
            img.save(buf, "JPEG", quality=quality, optimize=True)
            if buf.tell() <= THUMB_MAX_BYTES:
                b64 = base64.b64encode(buf.getvalue()).decode("ascii")
                return f"data:image/jpeg;base64,{b64}"
            img.thumbnail((img.width // 2 or 1, img.height // 2 or 1))
    except Exception as exc:
        logger.debug("Thumbnail failed: %s", exc)
    return None
