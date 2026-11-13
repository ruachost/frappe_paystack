from typing import Any, Optional

import frappe
from frappe.utils import flt

from frappe_paystack.frappe_paystack.doctype.paystack_refund_log.paystack_refund_log import (
    REFUNDABLE_LOG_STATUSES,
    get_total_refunded,
)
from frappe_paystack.utils import charge_currency, initiate_refund, record_failure, resolve_paystack_settings

PAYMENT_LOG_DOCTYPE = "Paystack Payment Log"
REFUND_LOG_DOCTYPE = "Paystack Refund Log"


def queue_payment_link_email(doc: Any) -> None:
    """RNIL patch: queue a payment-link email for a submitted invoice, when the gateway asks for it."""
    if getattr(doc, "is_pos", 0) or flt(doc.get("outstanding_amount")) <= 0:
        return

    if not frappe.db.get_value(
        "Paystack Gateway Setting", {"enabled": 1, "company": doc.company}, "auto_send_payment_link"
    ):
        return

    frappe.enqueue(
        "frappe_paystack.events.send_invoice_payment_link",
        queue="short",
        enqueue_after_commit=True,
        invoice=doc.name,
    )


def send_invoice_payment_link(invoice: str) -> None:
    """RNIL patch: create the Paystack link for an invoice and email it to the customer, once."""
    from frappe.core.doctype.communication.email import make
    from frappe.utils import fmt_money, formatdate

    from frappe_paystack.api import get_payable_amount
    from frappe_paystack.utils import customer_email, ensure_supported_currency
    from frappe_paystack.utils.payment_request import payment_request_checkout_url

    doc = frappe.get_doc("Sales Invoice", invoice)
    if doc.docstatus != 1 or doc.is_return or doc.get("is_pos"):
        return

    payable = get_payable_amount(doc)
    if payable <= 0:
        return

    # One email per invoice, however many times the job runs.
    if frappe.db.exists(
        "Communication",
        {"reference_doctype": "Sales Invoice", "reference_name": invoice, "subject": ["like", "Payment link for%"]},
    ):
        return

    recipient = doc.get("contact_email") or customer_email(doc.customer)
    if not recipient:
        doc.add_comment("Comment", "Paystack payment link not emailed: the customer has no email address.")
        return

    try:
        ensure_supported_currency(doc.currency)
        url = payment_request_checkout_url(doc, payable, recipient)
        amount = fmt_money(payable, currency=doc.currency)
        due = formatdate(doc.due_date) if doc.get("due_date") else ""
        body = (
            f"<p>Hello {frappe.utils.escape_html(doc.customer_name or doc.customer)},</p>"
            f"<p>Your invoice <b>{invoice}</b> for <b>{amount}</b> is ready."
            + (f" It is due on {due}." if due else "")
            + "</p>"
            f'<p><a href="{url}" style="display:inline-block;padding:10px 18px;background:#111;color:#fff;'
            f'text-decoration:none;border-radius:6px">Pay online</a></p>'
            f'<p>Or open this link: <a href="{url}">{url}</a></p>'
            "<p>Thank you.</p>"
        )
        make(
            doctype="Sales Invoice",
            name=invoice,
            content=body,
            subject=f"Payment link for {invoice}",
            recipients=recipient,
            send_email=True,
        )
    except Exception:
        frappe.log_error(title=f"Paystack payment-link email failed: {invoice}", message=frappe.get_traceback())


def sales_invoice_on_submit(doc: Any, method: Optional[str] = None) -> None:
    """
    Initiate a Paystack refund when a credit note is submitted.

    Applies to a return against an invoice paid via Paystack, when the gateway
    has auto_refund_on_credit_note enabled.
    """
    if not getattr(doc, "is_return", False):
        queue_payment_link_email(doc)
        return

    if not getattr(doc, "return_against", None):
        return

    settings = resolve_paystack_settings(getattr(doc, "company", None))
    if not settings or not settings.get("auto_refund_on_credit_note"):
        return

    # Doctype, document and company together identify the capture reversed.
    payment_log_name = frappe.db.get_value(
        PAYMENT_LOG_DOCTYPE,
        {
            "linked_doctype": doc.doctype,
            "linked_docname": doc.return_against,
            "company": doc.company,
            "status": ["in", REFUNDABLE_LOG_STATUSES],
        },
        "name",
    )
    if not payment_log_name:
        return

    payment_log = frappe.get_doc(PAYMENT_LOG_DOCTYPE, payment_log_name)
    amount_paid = flt(payment_log.amount_paid)

    if amount_paid <= 0:
        return

    # The request is capped at the balance earlier credit notes left.
    refundable = amount_paid - get_total_refunded(payment_log_name)
    if refundable <= 0:
        return

    # A credit note's grand_total is negative; the refund is its magnitude.
    credit_note_total = abs(flt(getattr(doc, "base_grand_total", 0) or 0))
    refund_amount = min(credit_note_total, refundable)

    if refund_amount <= 0:
        return

    transaction_id = payment_log.transaction_id
    currency = charge_currency(payment_log.company, payment_log.currency_paid)

    refund_log = frappe.get_doc(
        {
            "doctype": REFUND_LOG_DOCTYPE,
            "payment_log": payment_log_name,
            "company": doc.company,
            "linked_doctype": doc.doctype,
            "linked_docname": doc.name,
            "transaction_id": transaction_id,
            "refund_amount": refund_amount,
            "currency": currency,
            "status": "Pending",
            "refund_reason": f"Auto-refund from credit note {doc.name}",
        }
    )
    refund_log.flags.ignore_permissions = True

    try:
        refund_log.insert()
        result = initiate_refund(
            transaction_id=transaction_id,
            amount=refund_amount,
            currency=currency,
            company=doc.company,
            merchant_note=f"Credit note {doc.name}",
            refund_log=refund_log.name,
        )
        refund_log.db_set("status", "Processed", update_modified=True)
        refund_log.db_set("refund_reference", result.get("reference"), update_modified=False)
        refund_log.db_set("raw_response", frappe.as_json(result.get("raw")), update_modified=False)
        refund_log.reload()
        refund_log.run_method("on_update")
    except Exception:
        errors = record_failure(f"Paystack auto-refund failed for credit note {doc.name}")
        # The status lands only on a Refund Log whose insert survived.
        if refund_log.name and frappe.db.exists(REFUND_LOG_DOCTYPE, refund_log.name):
            refund_log.db_set("status", "Failed", update_modified=True)
            refund_log.db_set("errors", errors, update_modified=True)
