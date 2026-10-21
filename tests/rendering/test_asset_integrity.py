"""Issued images must be verified at the byte-to-data-URI boundary."""
# ruff: noqa: F811 -- Imported pytest fixtures are injected by name.

import base64
from dataclasses import replace
from hashlib import sha256
from io import BytesIO

import pytest
from django.core.files.storage import default_storage
from django.db import connection

from companies.models import NumberSequence
from invoicing.models import Invoice
from rendering.contexts import (
    PdfRenderNapaka,
    preveri_nespremenljiv_asset,
    pripravi_pdf_context,
)
from rendering.services import pripravi_html, render_pdf, stanje_koncnega_pdf
from tests.invoicing.test_numbering_io import PATHS, issue_action
from tests.invoicing.test_preflight import ready_company  # noqa: F401
from tests.rendering.test_contexts import (
    VALID_PNG_LOGO,
    VALID_PNG_NOV_ASSET,
    _mesecni_snapshot,
    izdani_racun,  # noqa: F401
)

ASSET_FIELDS = (
    "izdajatelj_logotip_slika_pot",
    "izdajatelj_podpisnik_slika_pot",
)


@pytest.mark.parametrize("field", ASSET_FIELDS)
def test_np_corruption_after_issuance_fails_closed_and_retry_keeps_number(
    izdani_racun, settings, tmp_path, field,
):
    settings.RENDERING_ROOT = tmp_path / "rendered"
    invoice = Invoice.objects.get(pk=izdani_racun.pk)
    path = tmp_path / getattr(invoice, field)
    original = path.read_bytes()
    sequences = list(NumberSequence.objects.values())
    invoices = list(Invoice.objects.values())
    # Valid image, wrong digest: existence and image decoding are insufficient.
    path.write_bytes(VALID_PNG_NOV_ASSET)
    with pytest.raises(PdfRenderNapaka) as error:
        render_pdf(invoice)
    assert error.value.koda == "ASSET_NEVELJAVEN"
    assert list(Invoice.objects.values()) == invoices
    assert list(NumberSequence.objects.values()) == sequences
    assert stanje_koncnega_pdf(invoice)["state"] == "PENDING"
    assert not list(settings.RENDERING_ROOT.rglob("*.pdf"))
    path.write_bytes(original)
    result = render_pdf(invoice)
    assert result.pdf_bytes.startswith(b"%PDF-")
    invoice.refresh_from_db()
    assert invoice.stevilka == "NP-00001"
    assert list(NumberSequence.objects.values()) == sequences


@pytest.mark.parametrize("field", ASSET_FIELDS)
def test_changed_storage_read_after_verification_is_rejected(
    izdani_racun, monkeypatch, field,
):
    context = pripravi_pdf_context(izdani_racun)
    target = getattr(context, field)
    original_open = default_storage.open
    reads = 0

    def changing_open(name, *args, **kwargs):
        nonlocal reads
        assert not connection.in_atomic_block
        if name == target:
            reads += 1
            if reads > 1:
                return BytesIO(VALID_PNG_NOV_ASSET)
        return original_open(name, *args, **kwargs)

    monkeypatch.setattr(default_storage, "open", changing_open)
    preveri_nespremenljiv_asset(target)
    with pytest.raises(PdfRenderNapaka) as error:
        pripravi_html(context)
    assert error.value.koda == "ASSET_NEVELJAVEN"


@pytest.mark.parametrize("path", [
    "logo.png", "nespremenljivi-asseti/ABC", "nespremenljivi-asseti/" + "a" * 63,
    "nespremenljivi-asseti/../logo.png", "https://example.invalid/logo.png",
])
def test_issued_html_rejects_non_content_addressed_paths(izdani_racun, path):
    context = replace(pripravi_pdf_context(izdani_racun), izdajatelj_logotip_slika_pot=path)
    with pytest.raises(PdfRenderNapaka) as error:
        pripravi_html(context)
    assert error.value.koda == "ASSET_NEVELJAVEN"


@pytest.mark.parametrize("payload", [b"not an image", b"\x89PNG\r\n\x1a\ntruncated"])
def test_matching_digest_does_not_authorize_invalid_image(izdani_racun, tmp_path, payload):
    path = "nespremenljivi-asseti/" + sha256(payload).hexdigest()
    (tmp_path / path).write_bytes(payload)
    context = replace(pripravi_pdf_context(izdani_racun), izdajatelj_logotip_slika_pot=path)
    with pytest.raises(PdfRenderNapaka) as error:
        pripravi_html(context)
    assert error.value.koda == "ASSET_NEVELJAVEN"


def test_monthly_preview_rechecks_bytes_at_embedding(settings, tmp_path):
    from rendering.contexts import pripravi_mesecni_pdf_context

    settings.MEDIA_ROOT = tmp_path
    snapshot, candidate = _mesecni_snapshot(tmp_path)
    context = pripravi_mesecni_pdf_context(snapshot, candidate)
    (tmp_path / context.izdajatelj_logotip_slika_pot).write_bytes(VALID_PNG_NOV_ASSET)
    with pytest.raises(PdfRenderNapaka) as error:
        pripravi_html(context)
    assert error.value.koda == "ASSET_NEVELJAVEN"


def test_verified_bytes_are_embedded_without_second_read(izdani_racun, monkeypatch):
    context = pripravi_pdf_context(izdani_racun)
    original_open = default_storage.open
    reads = []

    def single_open(name, *args, **kwargs):
        assert name not in reads, "Embedding must use a single verified read per image"
        reads.append(name)
        return original_open(name, *args, **kwargs)

    monkeypatch.setattr(default_storage, "open", single_open)
    html = pripravi_html(context)
    assert base64.b64encode(VALID_PNG_LOGO).decode() in html
    assert len(reads) == 2


@pytest.mark.parametrize("field", ASSET_FIELDS)
def test_existing_pdf_uses_own_checksum_after_source_corruption(
    izdani_racun, settings, tmp_path, field,
):
    settings.RENDERING_ROOT = tmp_path / "rendered"
    result = render_pdf(izdani_racun)
    invoice = Invoice.objects.get(pk=izdani_racun.pk)
    (tmp_path / getattr(invoice, field)).write_bytes(b"corrupt")
    assert render_pdf(invoice) == result
    assert stanje_koncnega_pdf(invoice)["state"] == "READY"


@pytest.mark.parametrize("document", PATHS)
@pytest.mark.parametrize("field", ASSET_FIELDS)
def test_all_issued_types_reject_mutation_after_preflight_and_recover(
    ready_company, settings, monkeypatch, document, field,
):
    from companies import issuance_assets
    from invoicing import services, st_confirmation

    action = issue_action(ready_company, document)
    original_prepare = issuance_assets.prepare_issuance_assets
    repair = {}

    def prepare(*args, **kwargs):
        proof = original_prepare(*args, **kwargs)
        # Mutate after verification, before the short numbering transaction.
        key = "logo" if field == ASSET_FIELDS[0] else "signature"
        digest = proof.payload()[key][1]
        path = settings.MEDIA_ROOT / f"nespremenljivi-asseti/{digest}"
        repair[path] = path.read_bytes()
        path.write_bytes(VALID_PNG_NOV_ASSET)
        return proof

    with monkeypatch.context() as patch:
        patch.setattr(services, "prepare_issuance_assets", prepare)
        patch.setattr(st_confirmation, "prepare_issuance_assets", prepare)
        issued = action()
    before = list(Invoice.objects.order_by("pk").values())
    sequences = list(NumberSequence.objects.order_by("pk").values())
    with pytest.raises(PdfRenderNapaka) as error:
        render_pdf(issued)
    assert error.value.koda == "ASSET_NEVELJAVEN"
    assert list(Invoice.objects.order_by("pk").values()) == before
    assert list(NumberSequence.objects.order_by("pk").values()) == sequences
    assert stanje_koncnega_pdf(issued)["state"] == "PENDING"
    for path, content in repair.items():
        path.write_bytes(content)
    context = pripravi_pdf_context(issued)
    html = pripravi_html(context)
    result = render_pdf(issued)
    assert result.pdf_bytes.startswith(b"%PDF-")
    assert issued.stevilka in html
    assert issued.izdajatelj_naziv in html
    assert context.skupaj_z_ddv == issued.skupaj_z_ddv
    if issued.povezan_racun_id:
        assert issued.povezan_racun.stevilka in html
    issued.refresh_from_db()
    assert issued.stevilka == context.stevilka
    assert list(NumberSequence.objects.order_by("pk").values()) == sequences


@pytest.mark.parametrize("failure", ["after-html", "before-publication"])
def test_render_failure_after_verified_html_allows_same_invoice_retry(
    izdani_racun, settings, tmp_path, monkeypatch, failure,
):
    settings.RENDERING_ROOT = tmp_path / "rendered"
    sequences = list(NumberSequence.objects.values())

    def fail(*args, **kwargs):
        raise PdfRenderNapaka("PDF_GENERIRANJE_NI_USPELO", "Synthetic failure")

    with monkeypatch.context() as patch:
        target = ("rendering.services.HTML.write_pdf" if failure == "after-html"
                  else "rendering.services._objavi_atomsko")
        patch.setattr(target, fail)
        with pytest.raises(PdfRenderNapaka):
            render_pdf(izdani_racun)
    assert stanje_koncnega_pdf(izdani_racun)["state"] == "PENDING"
    assert not list(settings.RENDERING_ROOT.rglob("*.pdf"))
    assert render_pdf(izdani_racun).pdf_bytes.startswith(b"%PDF-")
    assert list(NumberSequence.objects.values()) == sequences


@pytest.mark.parametrize("damage", ["missing", "inaccessible", "oversized", "pixels", "gif", "symlink"])
def test_asset_safety_limits_and_storage_errors(settings, tmp_path, monkeypatch, damage):
    from PIL import Image

    from core.validators import NAJVECJA_SLIKA
    from rendering.contexts import preberi_pdf_sliko

    settings.MEDIA_ROOT = tmp_path / "media"
    folder = settings.MEDIA_ROOT / "nespremenljivi-asseti"
    folder.mkdir(parents=True)
    payload = VALID_PNG_LOGO
    if damage == "oversized":
        payload += b"x" * NAJVECJA_SLIKA
    if damage in {"pixels", "gif"}:
        buffer = BytesIO()
        Image.new("RGB", (5000, 4001) if damage == "pixels" else (1, 1)).save(
            buffer, format="GIF" if damage == "gif" else "PNG",
        )
        payload = buffer.getvalue()
    name = "nespremenljivi-asseti/" + sha256(payload).hexdigest()
    path = settings.MEDIA_ROOT / name
    if damage == "symlink":
        external = tmp_path / "external.png"
        external.write_bytes(payload)
        try:
            path.symlink_to(external)
        except OSError as error:
            pytest.skip(f"Symlink creation unavailable: {error.winerror}")
    elif damage != "missing":
        path.write_bytes(payload)
    if damage == "inaccessible":
        def denied(*args, **kwargs):
            raise PermissionError("Synthetic permission failure")
        monkeypatch.setattr(default_storage, "open", denied)
    with pytest.raises(PdfRenderNapaka) as error:
        preberi_pdf_sliko(name)
    assert error.value.koda == ("ASSET_MANJKA" if damage in {"missing", "inaccessible"}
                                else "ASSET_NEVELJAVEN")


def test_monthly_final_retry_ignores_changed_sources(ready_company, settings):
    from rendering.services import renderiraj_koncne_pdfje_mesecnega_paketa

    issued = issue_action(ready_company, "monthly")()
    first = renderiraj_koncne_pdfje_mesecnega_paketa(ready_company["cycle"])[0]
    assert first["ok"]
    for field in ASSET_FIELDS:
        (settings.MEDIA_ROOT / getattr(issued, field)).write_bytes(b"corrupt")
    second = renderiraj_koncne_pdfje_mesecnega_paketa(ready_company["cycle"])[0]
    assert second["ok"] and first["artifact"] == second["artifact"]


def test_jpeg_is_accepted_by_content_without_extension(settings, tmp_path):
    from PIL import Image

    from rendering.contexts import preberi_pdf_sliko

    settings.MEDIA_ROOT = tmp_path
    buffer = BytesIO()
    Image.new("RGB", (2, 2)).save(buffer, format="JPEG")
    payload = buffer.getvalue()
    name = "nespremenljivi-asseti/" + sha256(payload).hexdigest()
    (tmp_path / "nespremenljivi-asseti").mkdir()
    (tmp_path / name).write_bytes(payload)
    assert preberi_pdf_sliko(name) == (payload, "image/jpeg")


def test_storage_path_escape_is_rejected_before_open(settings, tmp_path, monkeypatch):
    from rendering.contexts import preberi_pdf_sliko

    settings.MEDIA_ROOT = tmp_path / "media"
    settings.MEDIA_ROOT.mkdir()
    monkeypatch.setattr(default_storage, "path", lambda _: str(tmp_path / "outside.png"))
    monkeypatch.setattr(default_storage, "open", lambda *a, **k: pytest.fail("Escaped path opened"))
    with pytest.raises(PdfRenderNapaka) as error:
        preberi_pdf_sliko("nespremenljivi-asseti/" + sha256(VALID_PNG_LOGO).hexdigest())
    assert error.value.koda == "ASSET_NEVELJAVEN"


def test_concurrent_real_pdf_renders_share_one_verified_artifact(
    izdani_racun, settings, tmp_path, monkeypatch,
):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from django.db import connections

    from rendering import services

    settings.RENDERING_ROOT = tmp_path / "rendered"
    original_generate = services._generiraj_pdf_bytes
    barrier = Barrier(2)

    def generate(context):
        assert not connection.in_atomic_block
        barrier.wait(timeout=20)
        return original_generate(context)

    def render():
        try:
            return render_pdf(izdani_racun)
        finally:
            connections.close_all()

    monkeypatch.setattr(services, "_generiraj_pdf_bytes", generate)
    with ThreadPoolExecutor(max_workers=2) as executor:
        jobs = [executor.submit(render) for _ in range(2)]
        results = [job.result(timeout=30) for job in jobs]
    assert results[0] == results[1]
    assert len(list(settings.RENDERING_ROOT.rglob("*.pdf"))) == 1
    assert stanje_koncnega_pdf(izdani_racun)["state"] == "READY"
