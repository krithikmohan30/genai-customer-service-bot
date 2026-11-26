"""Live check for step 7: OCR, cross-check, rejection, queue, retention."""

from __future__ import annotations

import io
import json
import tempfile
import urllib.error
import urllib.request
import uuid
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

BASE = "http://127.0.0.1:8139"
WORK = Path(tempfile.mkdtemp(prefix="upload-smoke-"))
UPLOADS = WORK / "uploads"


def call(method: str, path: str, body: dict | None = None) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(
        BASE + path,
        data=data,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.loads(response.read().decode())


def invoice_png(lines: list[str], name: str = "invoice.png") -> tuple[str, bytes]:
    height = 80 + 60 * len(lines)
    image = Image.new("RGB", (1100, height), "white")
    draw = ImageDraw.Draw(image)
    try:
        font = ImageFont.truetype("arial.ttf", 34)
    except OSError:
        font = ImageFont.load_default()
    for index, line in enumerate(lines):
        draw.text((40, 40 + index * 60), line, fill="black", font=font)
    buffer = io.BytesIO()
    image.save(buffer, "PNG")
    return name, buffer.getvalue()


def upload(path: str, payload: tuple[str, bytes], message: str | None = None):
    name, data = payload
    boundary = "----smoke" + uuid.uuid4().hex
    parts: list[bytes] = []
    if message is not None:
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="message"\r\n\r\n'
            f"{message}\r\n".encode()
        )
    parts.append(
        f'--{boundary}\r\nContent-Disposition: form-data; name="file"; '
        f'filename="{name}"\r\nContent-Type: application/octet-stream\r\n\r\n'.encode()
        + data
        + b"\r\n"
    )
    parts.append(f"--{boundary}--\r\n".encode())
    body = b"".join(parts)
    request = urllib.request.Request(
        BASE + path,
        data=body,
        method="POST",
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            return json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        return json.loads(exc.read().decode())


call("POST", "/admin/config", {"key": "uploads.dir", "value": str(UPLOADS)})
call("POST", "/admin/config", {"key": "uploads.force_queue", "value": False})
call("POST", "/admin/config", {"key": "uploads.retention_hours", "value": 24})
call("POST", "/admin/time", {"iso": "2026-11-23T11:00:00"})

print("--- matching invoice")
match = upload(
    "/uploads",
    invoice_png(["Invoice INV-99887", "Date 21/11/2026", "Total Rs 1200"]),
    message="I was charged Rs 1200 for invoice INV-99887",
)
fields = match["extracted"]["fields"]
print("status:", match["status"], "queued:", match["queued"])
print("fields:", {key: value["value"] for key, value in fields.items()})
print("cross-check:", match["extracted"]["cross_check"])

print("--- mismatching message")
mismatch = upload(
    "/uploads",
    invoice_png(["Invoice INV-30001", "Total Rs 1200"]),
    message="You charged me Rs 5000 on INV-30001",
)
print("mismatches:", mismatch["extracted"]["cross_check"]["mismatches"])
print("document amount kept:", mismatch["extracted"]["fields"]["amount"]["value"])

print("--- PII masking")
pii = upload(
    "/uploads",
    invoice_png(["Invoice INV-50001", "Email me@example.com", "Phone 9876543210"]),
)
print("masked text:", repr(pii["extracted"]["text"].strip()))
print("pii:", pii["extracted"]["pii"])

print("--- rejected files")
for label, payload in [
    ("executable", ("invoice.png", b"MZ" + b"\x90" * 100)),
    ("wrong type", ("notes.exe", b"hello")),
    (
        "hostile pdf",
        ("invoice.pdf", b"%PDF-1.7\n1 0 obj << /JavaScript (alert(1)) >> endobj\n"),
    ),
]:
    body = upload("/uploads", payload)
    print(label, "->", body["detail"]["reason"])

print("--- queued upload")
call("POST", "/admin/config", {"key": "uploads.force_queue", "value": True})
queued = upload("/uploads", invoice_png(["Invoice INV-70001", "Total Rs 2500"]))
print(
    "queued:",
    queued["queued"],
    queued["status"],
    "estimate:",
    queued["estimate_seconds"],
)
swept = call("POST", "/uploads/sweep")
print("jobs:", swept["jobs"]["processed"])
print("after sweep:", call("GET", f"/uploads/{queued['upload_id']}")["status"])

print("--- retention")
call("POST", "/admin/config", {"key": "uploads.force_queue", "value": False})
call("POST", "/admin/config", {"key": "uploads.retention_hours", "value": 1})
old = upload("/uploads", invoice_png(["Invoice INV-80001", "Total Rs 800"]))
stored_path = Path(call("GET", f"/uploads/{old['upload_id']}")["path"])
print("file exists:", stored_path.exists())
call("POST", "/admin/time", {"advance_minutes": 120})
retention = call("POST", "/uploads/sweep")
print("deleted:", retention["retention"]["deleted"])
print("file exists:", stored_path.exists())
gone = call("GET", f"/uploads/{old['upload_id']}")
print("tombstone:", gone["status"], gone["extracted"])

print("--- list")
items = call("GET", "/uploads?status=processed")["items"]
print("processed uploads:", [(item["id"], item["processing_ms"]) for item in items])
