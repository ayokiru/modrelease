"""Strip metadata from screenshots and videos before they go on a mod page."""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp"}
VIDEO_EXT = {".mp4", ".mov", ".mkv", ".webm", ".m4v"}


def clean_image(src: Path, dst: Path, quality: int = 95) -> None:
    """Rebuild the image from its pixels only: EXIF, GPS, XMP, ICC and text chunks are gone.
    JPEG and lossy WebP are encoded again, so they lose a little quality."""
    from PIL import Image
    with Image.open(src) as im:
        im.load()
        bare = Image.frombytes(im.mode, im.size, im.tobytes())
        if im.mode == "P":
            bare.putpalette(im.getpalette())
    fmt = {".jpg": "JPEG", ".jpeg": "JPEG", ".png": "PNG", ".webp": "WEBP"}[src.suffix.lower()]
    if fmt == "JPEG" and bare.mode not in ("RGB", "L"):
        bare = bare.convert("RGB")
    opts = {"quality": quality} if fmt in ("JPEG", "WEBP") else {"optimize": True}
    dst.parent.mkdir(parents=True, exist_ok=True)
    bare.save(dst, fmt, **opts)


def clean_video(src: Path, dst: Path, keep_audio: bool = True) -> None:
    """Copy the streams (no re-encode) without container tags, chapters, encoder strings or the x264 settings SEI."""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("ffmpeg is not on PATH; it is needed for videos")
    dst.parent.mkdir(parents=True, exist_ok=True)
    base = [ffmpeg, "-v", "error", "-y", "-i", str(src), "-map", "0:v"] + (["-map", "0:a?"] if keep_audio else []) + [
        "-c", "copy", "-map_metadata", "-1", "-map_chapters", "-1", "-fflags", "+bitexact"]
    # SEI type 6 holds x264's settings string; the filter only exists for H.264/H.265, so retry without it otherwise
    for extra in (["-bsf:v", "filter_units=remove_types=6"], []):
        r = subprocess.run(base + extra + [str(dst)], capture_output=True, text=True)
        if r.returncode == 0:
            return
    raise RuntimeError(f"ffmpeg failed on {src.name}: {r.stderr.strip()[-300:]}")


def clean(paths: list[Path], out_dir: Path, keep_audio: bool = True) -> list[tuple[Path, Path]]:
    done = []
    files = [f for p in paths for f in (sorted(p.rglob("*")) if p.is_dir() else [p]) if f.is_file()]
    for f in files:
        ext = f.suffix.lower()
        dst = out_dir / f.name
        if ext in IMAGE_EXT:
            clean_image(f, dst)
        elif ext in VIDEO_EXT:
            clean_video(f, dst, keep_audio)
        else:
            continue
        done.append((f, dst))
    return done
