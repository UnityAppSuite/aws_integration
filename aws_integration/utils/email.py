import time
import frappe
from frappe import _, cint
from itertools import islice

from frappe.email.queue import (
    EMAIL_QUEUE_BATCH_FAILURE_THRESHOLD_COUNT,
    EMAIL_QUEUE_BATCH_FAILURE_THRESHOLD_PERCENT,
)
from frappe.utils import now_datetime, validate_email_address


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
    return bool(validate_email_address(email))


def is_html(text):
    """Regular expression to check for HTML tags"""
    import re
    html_pattern = re.compile(r"<([a-zA-Z]+)[^>]*>(.*?)</\1>|<([a-zA-Z]+)[^>]*>")
    return bool(html_pattern.search(text))


def chunk(iterable, size):
    """Yield successive chunks of a specified size from an iterable."""
    iterator = iter(iterable)
    for first in iterator:
        yield [first, *islice(iterator, size - 1)]


def sendmail(subject, message, recepient, cc_recepient, bcc_recepient, reply_tos=None):
    email_sender = frappe.get_single("AWS Settings")
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
            "reply_tos": []
        }
    }
    """
    email_batch_size = frappe.get_value(
        "AWS Settings", "AWS Settings", "email_batch_size"
    )
    rate_limiter = SESRateLimiter(rate_per_second=cint(email_batch_size) or 1)

    for key in data:
        if not rate_limiter.acquire(timeout=30):
            frappe.log_error(title="SES Rate Limit", message="Timed out waiting for rate limit slot")
            return
        entry = data[key]
        sendmail(
            entry.get("subject"),
            entry.get("content"),
            entry.get("recepients"),
            entry.get("cc_recepients"),
            entry.get("bcc_recepients"),
            entry.get("reply_tos"),
        )


class SESRateLimiter:
    """Fixed-window rate limiter using Redis for SES email sending.

    Uses per-second Redis keys with atomic INCR to track sends.
    When at the limit, waits until the next 1-second window opens.
    Safe across multiple RQ workers via Redis atomicity.
    """

    REDIS_KEY_PREFIX = "aws_ses_rate"

    def __init__(self, rate_per_second=None):
        self.rate = rate_per_second or cint(
            frappe.get_value("AWS Settings", "AWS Settings", "email_batch_size")
        ) or 14

    def acquire(self, timeout=30):
        """Block until a send slot is available.

        Returns True when acquired, False on timeout.
        """
        deadline = time.monotonic() + timeout

        while time.monotonic() < deadline:
            window = int(time.time())
            key = f"{self.REDIS_KEY_PREFIX}:{window}"

            # Atomic increment — safe across workers
            count = frappe.cache.incrby(key, 1)
            if count == 1:
                frappe.cache.expire(key, 3)

            if count <= self.rate:
                return True

            # Over limit — undo our increment, wait for next window
            frappe.cache.decrby(key, 1)
            sleep_time = 1.0 - (time.time() % 1)
            time.sleep(max(0.01, sleep_time))

        return False


def flush_email_queue():
    """flush email queue, every time: called from scheduler.

    This should not be called outside of background jobs.
    """
    from frappe.email.doctype.email_queue.email_queue import EmailQueue

    if frappe.are_emails_muted():
        return

    if cint(frappe.db.get_default("suspend_email_queue")) == 1:
        return

    email_batch_size = frappe.get_value(
        "AWS Settings", "AWS Settings", "email_batch_size"
    )

    email_queue_batch = get_queue(email_batch_size)
    if not email_queue_batch:
        return

    rate_limiter = SESRateLimiter(rate_per_second=cint(email_batch_size) or 1)
    failed_email_queues = []

    for row in email_queue_batch:
        if not rate_limiter.acquire(timeout=30):
            frappe.log_error(
                title="SES Rate Limit Timeout",
                message="Timed out waiting for SES rate limit slot. Stopping flush.",
            )
            break

        try:
            email_queue: EmailQueue = frappe.get_doc("Email Queue", row.name, for_update=True)
            email_queue.send()
        except Exception:
            frappe.get_doc("Email Queue", row.name).log_error()
            failed_email_queues.append(row.name)

            if (
                len(failed_email_queues) / len(email_queue_batch)
                > EMAIL_QUEUE_BATCH_FAILURE_THRESHOLD_PERCENT
                and len(failed_email_queues) > EMAIL_QUEUE_BATCH_FAILURE_THRESHOLD_COUNT
            ):
                frappe.log_error(
                    title="Email Queue Flush Aborted",
                    message="Too many failures in email queue batch",
                )
                break


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
        """SELECT name, sender
        FROM `tabEmail Queue`
        WHERE (status='Not Sent' OR status='Partially Sent')
            AND (send_after IS NULL OR send_after < %(now)s)
        ORDER BY priority DESC, retry ASC, creation ASC
        LIMIT %(batch_size)s""",
        {"now": now_datetime(), "batch_size": batch_size},
        as_dict=True,
    )
