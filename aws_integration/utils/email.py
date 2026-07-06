import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import frappe
from frappe import _, cint, msgprint

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


from botocore.exceptions import ConnectionError as BotoConnectionError
from botocore.exceptions import HTTPClientError

SES_THROTTLE_ERRORS = ("Throttling", "TooManyRequestsException", "LimitExceededException")
# transient transport failures (no AWS error code) — safe to retry a send
SES_RETRIABLE_EXCEPTIONS = (BotoConnectionError, HTTPClientError)
SES_SEND_ATTEMPTS = 3


def send_email_in_batches(data, max_workers=8):
    """Send one SES email per key, in parallel, throttled to
    AWS Settings.email_batch_size sends per second.

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

    Returns {key: error message} for sends that failed, or None when all
    succeeded (None preserves the old always-None return for callers that
    pass the result through API responses). A failed send never aborts the
    rest of the batch.

    Worker threads only call the (thread-safe) boto3 client — all frappe
    access (settings, password, SES logs) happens on the calling thread,
    since frappe.local is thread-local.
    """
    settings = frappe.get_single("AWS Settings")
    if not settings.enable_bulk_ses_email:
        frappe.throw(_("Bulk SES Email is not enabled in AWS Settings"))

    rate = cint(settings.email_batch_size) or 14  # sends per second
    source = f"{settings.sender_name} <{settings.source_email}>"
    client = settings.get_ses_client()

    jobs = {}  # key -> (send_args, destinations)
    failures = {}
    for key, item in data.items():
        recepients = [r for r in (item.get("recepients") or []) if r]
        if not recepients:
            failures[key] = "No recipient email address"
            continue
        destinations = SESDestination(
            tos=recepients,
            ccs=[c for c in (item.get("cc_recepients") or []) if c],
            bccs=[b for b in (item.get("bcc_recepients") or []) if b],
        )
        message = item.get("content") or ""
        send_args = {
            "FromEmailAddress": source,
            "Destination": destinations.to_service_format(),
            "Content": {
                "Simple": {
                    "Subject": {"Data": item.get("subject") or "", "Charset": "UTF-8"},
                    "Body": {
                        ("Html" if is_html(message) else "Text"): {
                            "Data": message,
                            "Charset": "UTF-8",
                        }
                    },
                }
            },
        }
        if item.get("reply_tos"):
            send_args["ReplyToAddresses"] = item["reply_tos"]
        jobs[key] = (send_args, destinations)

    results = {}  # key -> message id
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {}
        # even-spaced pacing: one submission every 1/rate seconds. Slot-based
        # (not anchored to batch start) so a worker stall re-anchors to "now"
        # and resumes at the configured rate instead of bursting to catch up.
        # sleep() here blocks only this background worker, which is the point.
        min_interval = 1.0 / rate
        next_slot = time.monotonic()
        for key, (send_args, _destinations) in jobs.items():
            delay = next_slot - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            next_slot = max(next_slot, time.monotonic()) + min_interval
            futures[pool.submit(_ses_send_with_retry, client, send_args)] = key

        for future in as_completed(futures):
            key = futures[future]
            try:
                response = future.result()
                results[key] = response.get("MessageId")
            except Exception as e:
                failures[key] = str(e)

    _add_ses_logs(settings, data, jobs, results)

    if failures:
        frappe.log_error(
            title="SES Batch Email Failures",
            message=frappe.as_json(failures),
        )
    return failures or None


def _ses_send_with_retry(client, send_args):
    """Runs in a worker thread: boto3 only, no frappe. Retries SES
    throttling errors and transient transport errors with backoff."""
    for attempt in range(SES_SEND_ATTEMPTS):
        try:
            return client.send_email(**send_args)
        except Exception as e:
            code = getattr(e, "response", {}).get("Error", {}).get("Code", "")
            retriable = code in SES_THROTTLE_ERRORS or isinstance(
                e, SES_RETRIABLE_EXCEPTIONS
            )
            if not retriable or attempt == SES_SEND_ATTEMPTS - 1:
                raise
            time.sleep(2**attempt)


def _add_ses_logs(settings, data, jobs, results):
    """Insert AWS SES Logs for successful sends (main thread)."""
    for key, message_id in results.items():
        try:
            item = data[key]
            destinations = jobs[key][1]
            ses_log = frappe.get_doc(
                {
                    "doctype": "AWS SES Logs",
                    "message_id": message_id,
                    "subject": item.get("subject"),
                    "message": item.get("content"),
                    "status": "Sent",
                    "from": settings.source_email,
                }
            )
            ses_log.recepients = ", ".join(destinations.tos or [])
            ses_log.cc_recepients = ", ".join(destinations.ccs or [])
            ses_log.bcc_recepients = ", ".join(destinations.bccs or [])
            ses_log.insert(ignore_permissions=True)
        except Exception:
            frappe.log_error(
                title="SES Log Insert Error", message=frappe.get_traceback()
            )


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
    rate = cint(email_batch_size) or 14  # sends per second

    email_queue_batch = get_queue(email_batch_size)
    if not email_queue_batch:
        return

    failed_email_queues = []
    # Even-spaced pacing: one send every 1/rate seconds. Slot-based so a
    # stall (slow SMTP, DB pause) re-anchors to "now" and resumes at the
    # configured rate instead of bursting to catch up; no trailing sleep
    # after the last email. Sends stay sequential: EmailQueue.send() needs
    # the frappe request context (frappe.local is thread-local), so unlike
    # the boto3-only SES batch sender this must NOT use worker threads.
    min_interval = 1.0 / rate
    next_slot = time.monotonic()
    for row in email_queue_batch:
        delay = next_slot - time.monotonic()
        if delay > 0:
            time.sleep(delay)
        next_slot = max(next_slot, time.monotonic()) + min_interval
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
