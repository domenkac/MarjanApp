from django.db import transaction

from .models import Invoice


@transaction.atomic
def nastavi_pdf_artifact(
    invoice: Invoice, *, rendered_pdf_path: str, checksum: str
) -> Invoice:
    svezi = Invoice.objects.select_for_update().get(pk=invoice.pk)
    if svezi.rendered_pdf_path or svezi.checksum:
        if (
            svezi.rendered_pdf_path == rendered_pdf_path
            and svezi.checksum == checksum
        ):
            return svezi
        raise ValueError("Ra?un ?e vsebuje PDF artifact.")

    svezi.rendered_pdf_path = rendered_pdf_path
    svezi.checksum = checksum
    svezi.save(update_fields=["rendered_pdf_path", "checksum"])
    return svezi

