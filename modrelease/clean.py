"""Strip metadata from screenshots and videos before they go on a mod page."""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp"}
VIDEO_EXT = {".mp4", ".mov", ".mkv", ".webm", ".m4v"}


def clean_image(src: Path, dst: Path, quality: int = 95) -> None:
    """Rebuild the image from its pixels only: EXIF, GPS, XMP, ICC and text chunks are gone.
    The EXIF rotation is applied first, so phone photos stay upright. JPEG and lossy WebP are encoded again."""
    from PIL import Image, ImageOps
    with Image.open(src) as im:
        if getattr(im, "n_frames", 1) > 1:
            raise ValueError(f"{src.name} is animated; only still images are cleaned, convert it to a video first")
        im = ImageOps.exif_transpose(im)
        bare = Image.frombytes(im.mode, im.size, im.tobytes())
        if im.mode == "P":
            bare.putpalette(im.getpalette())
        transparency = im.info.get("transparency")
    fmt = {".jpg": "JPEG", ".jpeg": "JPEG", ".png": "PNG", ".webp": "WEBP"}[src.suffix.lower()]
    if fmt == "JPEG" and bare.mode not in ("RGB", "L"):
        bare = bare.convert("RGB")
        transparency = None
    opts = {"quality": quality} if fmt in ("JPEG", "WEBP") else {"optimize": True}
    if transparency is not None:
        opts["transparency"] = transparency
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(dst.stem + ".modrelease-tmp" + dst.suffix)
    bare.save(tmp, fmt, **opts)
    os.replace(tmp, dst)  # via a temp file: cleaning in place must not truncate the source


def _codec(src: Path) -> str:
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        raise RuntimeError("ffprobe is not on PATH; it comes with ffmpeg and is needed for videos")
    r = subprocess.run([ffprobe, "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=codec_name",
                        "-of", "csv=p=0", str(src)], capture_output=True, text=True)
    return r.stdout.strip()


def clean_video(src: Path, dst: Path, keep_audio: bool = True) -> None:
    """Copy the streams (no re-encode) without container tags, chapters, the muxer string or the encoder settings."""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("ffmpeg is not on PATH; it is needed for videos")
    codec = _codec(src)
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(dst.stem + ".modrelease-tmp" + dst.suffix)
    ts = dst.with_name(dst.stem + ".modrelease-tmp.ts")
    copy = ["-c", "copy", "-map_metadata", "-1", "-map_chapters", "-1", "-fflags", "+bitexact"]
    streams = ["-map", "0:v"] + (["-map", "0:a?"] if keep_audio else [])
    if codec == "hevc":
        # MP4/MKV keep x265's settings in the track header (hvcC) as well, which a stream filter can't reach.
        # Going through MPEG-TS puts them in the stream, where they are cut; the second run builds a clean header.
        # shortcut: type 39 is every prefix SEI, so HDR mastering data goes too; fine for mod trailers, revisit for HDR
        runs = [streams + copy + ["-bsf:v", "hevc_mp4toannexb,filter_units=remove_types=39", "-f", "mpegts", str(ts)],
                ["-map", "0"] + copy + [str(tmp)]]
        inputs = [src, ts]
    else:
        sei = ["-bsf:v", "filter_units=remove_types=6"] if codec == "h264" else []
        runs, inputs = [streams + copy + sei + [str(tmp)]], [src]
    try:
        for inp, args in zip(inputs, runs):
            r = subprocess.run([ffmpeg, "-v", "error", "-y", "-i", str(inp)] + args, capture_output=True, text=True)
            if r.returncode != 0:
                raise RuntimeError(f"ffmpeg failed on {src.name}: {r.stderr.strip()[-300:]}")
        os.replace(tmp, dst)
    finally:
        tmp.unlink(missing_ok=True)
        ts.unlink(missing_ok=True)


def clean(paths: list[Path], out_dir: Path, keep_audio: bool = True) -> list[tuple[Path, Path]]:
    """Cleaned copies in out_dir; files from a folder keep their place under it, so equal names don't collide."""
    jobs = []
    for p in paths:
        if p.is_dir():
            jobs += [(f, out_dir / f.relative_to(p)) for f in sorted(p.rglob("*")) if f.is_file()]
        elif p.is_file():
            jobs.append((p, out_dir / p.name))
        else:
            raise FileNotFoundError(p)
    jobs = [(f, d) for f, d in jobs if f.suffix.lower() in IMAGE_EXT | VIDEO_EXT]
    seen = {}
    for f, d in jobs:
        if d in seen:
            raise ValueError(f"{seen[d]} and {f} would both be written to {d}")
        seen[d] = f
    for f, d in jobs:
        if f.suffix.lower() in IMAGE_EXT:
            clean_image(f, d)
        else:
            clean_video(f, d, keep_audio)
    return jobs
