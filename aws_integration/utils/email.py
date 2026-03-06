import re
import time
import frappe
from frappe import _, cint, msgprint
from itertools import islice

from frappe.email.queue import (
    EMAIL_QUEUE_BATCH_FAILURE_THRESHOLD_COUNT,
    EMAIL_QUEUE_BATCH_FAILURE_THRESHOLD_PERCENT,
    get_queue,
)
from frappe.utils import now_datetime


class SESDestination:
    """Contains data about an email destination."""

    def __init__(self, tos, ccs=None, bccs=None):
        self.tos = tos
        self.ccs = ccs
        self.bccs = bccs

    def to_service_format(self):
        svc_format = {"ToAddresses": self.tos}
        if self.ccs:
            svc_format["CcAddresses"] = self.ccs
        if self.bccs:
            svc_format["BccAddresses"] = self.bccs
        return svc_format


def validate_email(email):
    """Regular expression to validate email addresses."""
    email_pattern = re.compile(r"[^@]+@[^@]+\.[^@]+")
    return bool(email_pattern.match(email))


def is_html(text):
    """Regular expression to check for HTML tags"""
    html_pattern = re.compile(r"<([a-zA-Z]+)[^>]*>(.*?)</\1>|<([a-zA-Z]+)[^>]*>")
    return bool(html_pattern.search(text))


def chunk(iterable, size):
    """Yield successive chunks of a specified size from an iterable."""
    iterator = iter(iterable)
    for first in iterator:
        yield [first, *islice(iterator, size - 1)]


def sendmail(subject, message, recepient, cc_recepient, bcc_recepient, reply_tos=None):
    email_sender = frappe.get_single("AWS Settings")
    if not email_sender.enable_bulk_ses_email:
        frappe.throw(_("Bulk SES Email is not enabled in AWS Settings"))
    destinations = SESDestination(tos=recepient, ccs=cc_recepient, bccs=bcc_recepient)

    email_params = {
        "destinations": destinations,
        "subject": subject,
        "reply_tos": reply_tos,
    }

    if is_html(message):
        email_params["html"] = message
    else:
        email_params["content"] = message

    return email_sender.send_email(**email_params)


def send_email_in_batches(data):
    """
    Structure of data:
    {
        "key_name": {
            "subject": "Subject",
            "content": "Content",
            "recepients": [],
            "cc_recepients": [],
            "bcc_recepients": [],
            reply_tos: []
        }
    }
    """
    email_batch_size = frappe.get_value(
        "AWS Settings", "AWS Settings", "email_batch_size"
    )

    for student_data in chunk(data.keys(), cint(email_batch_size)):
        for key in student_data:
            student = data[key]
            sendmail(
                student.get("subject"),
                student.get("content"),
                student.get("recepients"),
                student.get("cc_recepients"),
                student.get("bcc_recepients"),
            )
        time.sleep(1)


def flush_email_queue():
    """flush email queue, every time: called from scheduler.

    This should not be called outside of background jobs.
    """
    settings = frappe.get_cached_doc("AWS Settings")
    if not settings.enable_aws:
        return

    from frappe.email.doctype.email_queue.email_queue import EmailQueue

    # To avoid running jobs inside unit tests
    if frappe.are_emails_muted():
        msgprint(_("Emails are muted"))

    if cint(frappe.db.get_default("suspend_email_queue")) == 1:
        return
    
    email_batch_size = frappe.get_value(
        "AWS Settings", "AWS Settings", "email_batch_size"
    )

    email_queue_batch = get_queue(email_batch_size)
    if not email_queue_batch:
        return

    failed_email_queues = []
    for data in chunk(email_queue_batch, cint(email_batch_size)):
        for row in data:
            try:
                email_queue: EmailQueue = frappe.get_doc("Email Queue", row.name)
                email_queue.send()
            except Exception:
                frappe.get_doc("Email Queue", row.name).log_error()
                failed_email_queues.append(row.name)

                if (
                    len(failed_email_queues) / len(email_queue_batch)
                    > EMAIL_QUEUE_BATCH_FAILURE_THRESHOLD_PERCENT
                    and len(failed_email_queues) > EMAIL_QUEUE_BATCH_FAILURE_THRESHOLD_COUNT
                ):
                    frappe.throw(
                        _("Email Queue flushing aborted due to too many failures.")
                    )
        time.sleep(1)



def get_queue(email_batch_size=None):
    """
    Description:
    Get email queue from the database.
    email_batch_size is the number of emails to be sent in a batch per second.
    batch_per_minute is the number of emails to be sent in half a minute.
    batch_size is the number of emails to be sent in a batch.
    """
    batch_per_minute = cint(email_batch_size) * 30
    batch_size = batch_per_minute or cint(frappe.conf.email_queue_batch_size) or 500

    return frappe.db.sql(
		f"""select
			name, sender
		from
			`tabEmail Queue`
		where
			(status='Not Sent' or status='Partially Sent') and
			(send_after is null or send_after < %(now)s)
		order
			by priority desc, retry asc, creation asc
		limit {batch_size}""",
		{"now": now_datetime()},
		as_dict=True,
	)
