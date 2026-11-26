"""Task 5 - uploads, OCR, cross-check, queue, retention.

OCR is exercised against generated invoice images, so the test proves values
come from the document rather than from the message. Every test freezes the
clock, uses a temporary upload folder, and closes leftover uploads/jobs.
"""

from __future__ import annotations

import io

import pytest
from PIL import Image, ImageDraw, ImageFont

from app import db, store
from app.core import clock, config, uploads


def _font(size: int = 34):
    try:
        return ImageFont.truetype("arial.ttf", size)
    except OSError:  # pragma: no cover - platform fallback
        return ImageFont.load_default()


def invoice_png(lines: list[str]) -> bytes:
    height = 80 + 60 * len(lines)
    image = Image.new("RGB", (1100, height), "white")
    draw = ImageDraw.Draw(image)
    font = _font()
    for index, line in enumerate(lines):
        draw.text((40, 40 + index * 60), line, fill="black", font=font)
    buffer = io.BytesIO()
    image.save(buffer, "PNG")
    return buffer.getvalue()


@pytest.fixture(autouse=True)
def upload_env(tmp_path):
    config.set_override("uploads.dir", str(tmp_path / "uploads"))
    config.set_override("uploads.force_queue", False)
    config.set_override("uploads.retention_hours", 24)
    db.execute("DELETE FROM uploads")
    db.execute("DELETE FROM jobs")
    clock.set_time("2026-11-23T11:00:00")
    yield


def upload(client, data: bytes, name: str = "invoice.png", message: str | None = None):
    form = {}
    if message is not None:
        form["message"] = message
    return client.post(
        "/uploads",
        files={"file": (name, data, "application/octet-stream")},
        data=form,
    )


def test_image_ocr_extracts_document_values(client):
    image = invoice_png(["Invoice INV-99887", "Date 21/11/2026", "Total Rs 1200"])
    response = upload(
        client, image, message="I was charged Rs 1200 for invoice INV-99887"
    )
    assert response.status_code == 200, response.text

    body = response.json()
    assert body["status"] == "processed"
    assert body["queued"] is False

    extracted = body["extracted"]
    assert "Invoice" in extracted["text"]
    assert extracted["fields"]["invoice_id"]["value"] == "INV-99887"
    assert extracted["fields"]["amount"]["value"] == 1200.0
    assert extracted["fields"]["date"]["value"] == "2026-11-21"
    assert extracted["cross_check"]["conflict"] is False

    matched = {item["field"] for item in extracted["cross_check"]["matches"]}
    assert {"invoice_id", "amount"} <= matched


def test_message_document_mismatch_is_reported_not_resolved(client):
    image = invoice_png(["Invoice INV-30001", "Total Rs 1200"])
    response = upload(client, image, message="You charged me Rs 5000 on INV-30001")
    assert response.status_code == 200

    extracted = response.json()["extracted"]
    check = extracted["cross_check"]
    assert check["conflict"] is True
    mismatch = next(item for item in check["mismatches"] if item["field"] == "amount")
    assert mismatch["document"] == 1200.0
    assert mismatch["message"] == 5000.0
    # The document value stands; no average, no overwrite.
    assert extracted["fields"]["amount"]["value"] == 1200.0


def test_values_missing_from_the_document_are_not_invented(client):
    image = invoice_png(["Invoice INV-40001", "Thank you for your payment"])
    response = upload(client, image, message="I paid Rs 700 for INV-40001")
    assert response.status_code == 200

    extracted = response.json()["extracted"]
    assert "amount" not in extracted["fields"]
    assert extracted["cross_check"]["only_in_message"] == ["amount"]
    assert extracted["cross_check"]["conflict"] is False


def test_pii_is_masked_before_storage(client):
    image = invoice_png(
        [
            "Invoice INV-50001",
            "Email me@example.com",
            "Phone 9876543210",
            "Total Rs 900",
        ]
    )
    response = upload(client, image)
    assert response.status_code == 200

    extracted = response.json()["extracted"]
    assert "me@example.com" not in extracted["text"]
    assert "9876543210" not in extracted["text"]
    assert "[EMAIL]" in extracted["text"]
    assert "[PHONE]" in extracted["text"]
    assert set(extracted["pii"]) >= {"EMAIL", "PHONE"}


def test_unsafe_files_are_rejected(client):
    executable = b"MZ" + b"\x90" * 100
    body = upload(client, executable, name="invoice.png").json()
    assert body["detail"]["reason"] == "unsafe_content"

    unsupported = upload(client, b"hello", name="notes.exe").json()
    assert unsupported["detail"]["reason"] == "unsupported_type"

    hostile_pdf = b"%PDF-1.7\n1 0 obj << /JavaScript (alert(1)) >> endobj\n"
    pdf = upload(client, hostile_pdf, name="invoice.pdf").json()
    assert pdf["detail"]["reason"] == "unsafe_pdf"

    mismatched = upload(client, b"%PDF-1.7\n", name="invoice.png").json()
    assert mismatched["detail"]["reason"] == "type_mismatch"


def test_oversized_upload_is_rejected(client):
    config.set_override("uploads.max_mb", 0.001)  # ~1 KB
    image = invoice_png(["Invoice INV-60001", "Total Rs 100"] + ["padding line"] * 30)
    body = upload(client, image).json()
    assert body["detail"]["reason"] == "too_large"


def test_slow_upload_is_queued_then_processed(client):
    config.set_override("uploads.force_queue", True)
    image = invoice_png(["Invoice INV-70001", "Total Rs 2500"])

    queued = upload(client, image, message="Invoice INV-70001 for Rs 2500").json()
    assert queued["queued"] is True
    assert queued["status"] == "queued"

    swept = client.post("/uploads/sweep").json()
    assert swept["jobs"]["processed"][0]["status"] == "processed"

    stored = client.get(f"/uploads/{queued['upload_id']}").json()
    assert stored["status"] == "processed"
    assert stored["extracted"]["fields"]["amount"]["value"] == 2500.0


def test_retention_deletes_file_and_text(client):
    config.set_override("uploads.retention_hours", 1)
    image = invoice_png(["Invoice INV-80001", "Total Rs 800"])
    body = upload(client, image).json()
    upload_id = body["upload_id"]

    path = store.get_upload(upload_id)["path"]
    from pathlib import Path

    assert Path(path).exists()

    clock.advance(hours=2)
    swept = client.post("/uploads/sweep").json()
    assert upload_id in swept["retention"]["deleted"]
    assert not Path(path).exists()

    gone = client.get(f"/uploads/{upload_id}").json()
    assert gone["status"] == "deleted"
    assert gone["extracted"] is None


def test_pdf_with_embedded_text_is_read(client):
    import pymupdf

    document = pymupdf.open()
    page = document.new_page()
    page.insert_text((72, 72), "Invoice INV-90001")
    page.insert_text((72, 110), "Total Rs 4500")
    pdf = document.tobytes()
    document.close()

    response = upload(client, pdf, name="invoice.pdf", message="Invoice INV-90001")
    assert response.status_code == 200

    extracted = response.json()["extracted"]
    assert extracted["pages"] == 1
    assert extracted["fields"]["invoice_id"]["value"] == "INV-90001"
    assert extracted["fields"]["amount"]["value"] == 4500.0
    assert extracted["engine"] in ("pdf_text", "pdf_text+tesseract")


def test_queued_job_retries_then_fails():
    row = store.add_upload(
        filename="missing.png",
        path="/does/not/exist.png",
        status="queued",
        delete_at="2026-12-01T00:00:00+05:30",
    )
    job_id = store.add_job(
        "upload.process", {"upload_id": row["id"]}, run_after=clock.now_iso()
    )

    first = uploads.process_jobs()
    assert first["processed"][0]["status"] == "retry"
    job = store.get_job(job_id) or {}
    assert job["attempts"] == 1
    assert job["run_after"].startswith("2026-11-23T11:05")

    clock.set_time("2026-11-23T11:05:00")
    uploads.process_jobs()
    clock.set_time("2026-11-23T11:20:00")
    uploads.process_jobs()
    clock.set_time("2026-11-23T11:50:00")
    uploads.process_jobs()

    job = store.get_job(job_id) or {}
    assert job["status"] == "failed"
    assert job["attempts"] == 4
    assert (store.get_upload(row["id"]) or {})["status"] == "failed"
