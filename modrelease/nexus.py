"""New version of a file on Nexus Mods through the v3 API: the same steps as Nexus's own GitHub action
(github.com/Nexus-Mods/upload-action). The file must already exist on the mod page; this adds a version to it."""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path

from . import __version__

API = os.environ.get("NEXUSMODS_API_BASE", "https://api.nexusmods.com/v3")
CATEGORIES = ("main", "optional", "miscellaneous")


def _send(method: str, url: str, body: bytes | None = None, headers: dict | None = None) -> tuple[dict, bytes]:
    """-> (response headers, body). The only function that touches the network."""
    req = urllib.request.Request(url, data=body, method=method,
                                 headers={"User-Agent": f"modrelease/{__version__}", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=600) as r:
            return dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Nexus answered {e.code} to {method} {url.split('?')[0]}: "
                           f"{e.read()[:300].decode('utf-8', 'replace')}") from None


def _api(method: str, path: str, key: str, payload: dict | None = None) -> dict:
    body = json.dumps(payload).encode() if payload is not None else None
    _, data = _send(method, API + path, body, {"apikey": key, "Content-Type": "application/json"})
    return json.loads(data) if data else {}


def upload(path: Path, key: str, file_id: str, version: str, *, name: str | None = None, description: str | None = None,
           category: str = "main", archive_old: bool = False, update_mod_version: bool = False,
           mod_id: str | None = None, changelog: str | None = None, log=print) -> str:
    """-> id of the new file version."""
    if category not in CATEGORIES:
        raise ValueError(f"category must be one of {', '.join(CATEGORIES)}")
    if changelog and not mod_id:
        raise ValueError("a changelog needs the mod id (--mod-id)")
    size = path.stat().st_size
    up = _api("POST", "/uploads/multipart", key, {"filename": path.name, "size_bytes": str(size)})["data"]
    urls, part = up["part_presigned_urls"], int(up["part_size_bytes"])
    parts = []
    with open(path, "rb") as f:
        for n, url in enumerate(urls, 1):
            chunk = f.read(part)
            log(f"  part {n}/{len(urls)} ({len(chunk) // 1024} KB)")
            headers, _ = _send("PUT", url, chunk, {"Content-Type": "application/octet-stream"})
            etag = next((v for k, v in headers.items() if k.lower() == "etag"), None)
            if not etag:
                raise RuntimeError(f"no ETag for part {n}")
            parts.append(f"  <Part>\n    <PartNumber>{n}</PartNumber>\n    <ETag>{etag.strip(chr(34))}</ETag>\n  </Part>")
    xml = "<CompleteMultipartUpload>\n" + "\n".join(parts) + "\n</CompleteMultipartUpload>"
    _send("POST", up["complete_presigned_url"], xml.encode(), {"Content-Type": "application/xml"})
    _api("POST", f"/uploads/{up['id']}/finalise", key)
    delay = 2.0
    for _ in range(60):  # Nexus scans the file before it can be attached
        state = _api("GET", f"/uploads/{up['id']}", key)["data"]["state"]
        if state == "available":
            break
        log(f"  Nexus is processing the upload ({state})")
        time.sleep(delay)
        delay = min(delay * 1.5, 30)
    else:
        raise RuntimeError(f"upload {up['id']} was still processing after 60 checks; it may show up on Nexus later")
    body = {"upload_id": up["id"], "name": name or path.name, "version": version, "file_category": category,
            "archive_existing_file": archive_old, "update_mod_version": update_mod_version}
    if description:
        body["description"] = description
    res = _api("POST", f"/mod-files/{file_id}/versions", key, body)
    if changelog:
        _api("POST", f"/mods/{mod_id}/changelogs", key, {"version": version, "changelog": changelog})
    return str(res["data"]["version"]["id"])
