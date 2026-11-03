"""VirusTotal check of a release file (free API key: https://www.virustotal.com/gui/my-apikey)."""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

from .pack import sha256

API = "https://www.virustotal.com/api/v3"
GUI = "https://www.virustotal.com/gui/file/{}"
DIRECT_LIMIT = 32 * 1024 * 1024  # bigger files go through a one-time upload URL


def _call(url: str, key: str, data: bytes | None = None, headers: dict | None = None) -> dict | None:
    req = urllib.request.Request(url, data=data, headers={"x-apikey": key, "accept": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=300) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise RuntimeError(f"VirusTotal answered {e.code}: {e.read()[:200].decode('utf-8', 'replace')}") from None


def _upload(path: Path, key: str) -> str:
    url = f"{API}/files"
    if path.stat().st_size > DIRECT_LIMIT:
        url = _call(f"{API}/files/upload_url", key)["data"]
    boundary = uuid.uuid4().hex
    body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{path.name}\"\r\n"
            "Content-Type: application/octet-stream\r\n\r\n").encode() + path.read_bytes() + f"\r\n--{boundary}--\r\n".encode()
    res = _call(url, key, body, {"content-type": f"multipart/form-data; boundary={boundary}"})
    return res["data"]["id"]


def report(path: Path, key: str, upload: bool = False, wait: int = 600) -> dict:
    """-> {"sha256", "link", "known", "pending", "stats", "flagged": [(engine, verdict)]}. Uploads unknown files when asked."""
    digest = sha256(path)
    out = {"sha256": digest, "link": GUI.format(digest), "known": False, "pending": False, "stats": {}, "flagged": []}
    data = _call(f"{API}/files/{digest}", key)
    if data is None and upload:
        analysis = _upload(path, key)
        deadline = time.time() + wait
        while True:
            if _call(f"{API}/analyses/{analysis}", key)["data"]["attributes"]["status"] == "completed":
                break
            if time.time() >= deadline:
                out["pending"] = True  # uploaded; uploading again would only use up the quota
                return out
            time.sleep(15)  # the free API allows 4 requests a minute
        data = _call(f"{API}/files/{digest}", key)
    if data is None:
        return out
    attrs = data["data"]["attributes"]
    out["known"] = True
    out["stats"] = attrs.get("last_analysis_stats", {})
    out["flagged"] = sorted((engine, r.get("result") or r["category"])
                            for engine, r in attrs.get("last_analysis_results", {}).items()
                            if r.get("category") in ("malicious", "suspicious"))
    return out
