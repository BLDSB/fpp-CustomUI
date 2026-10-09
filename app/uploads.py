"""Content checks for branding images (logo / background).

These files are served from the UI's own origin by Flask's static handler, so a
file that *claims* to be an image but carries script (an SVG with ``<script>``,
or HTML renamed ``.png``) would run with the admin session's privileges if it
were ever opened directly. The extension alone is not enough — check the bytes.
"""
import os
import re

ALLOWED_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg"}
MAX_IMAGE_BYTES = 8 * 1024 * 1024

_MAGIC = {
    ".png": (b"\x89PNG\r\n\x1a\n",),
    ".jpg": (b"\xff\xd8\xff",),
    ".jpeg": (b"\xff\xd8\xff",),
    ".gif": (b"GIF87a", b"GIF89a"),
}

# Anything that can execute or pull in active content from inside an SVG.
_SVG_FORBIDDEN = re.compile(
    r"<\s*script|<\s*foreignobject|<\s*iframe|<\s*embed|<\s*object|<!entity"
    r"|\son\w+\s*=|javascript\s*:|data\s*:\s*text/html",
    re.IGNORECASE,
)


def image_problem(filename, data):
    """Why ``data`` is not an acceptable image for ``filename``, or ``None``."""
    ext = os.path.splitext(filename)[1].lower()
    if ext not in ALLOWED_EXTS:
        return "Unsupported file type"
    if not data:
        return "The file is empty"
    if len(data) > MAX_IMAGE_BYTES:
        return "The image is too large (8 MB maximum)"

    if ext == ".svg":
        text = data.decode("utf-8", errors="ignore")
        if "<svg" not in text.lower():
            return "That file is not a valid SVG image"
        if _SVG_FORBIDDEN.search(text):
            return "SVG images with scripts or embedded content are not allowed"
    elif ext == ".webp":
        if not (data[:4] == b"RIFF" and data[8:12] == b"WEBP"):
            return "That file is not a valid WebP image"
    elif not data.startswith(_MAGIC[ext]):
        return f"That file is not a valid {ext.lstrip('.').upper()} image"
    return None
