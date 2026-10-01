import frappe
from botocore.config import Config
from frappe.utils import cint

CONNECTION_CACHE_KEY = "aws_integration:s3_connection_ok"
CONNECTION_CACHE_SECONDS = 60
# A File form must open promptly even when S3 is unreachable.
CONNECTION_CHECK_CONFIG = Config(
    connect_timeout=3, read_timeout=3, retries={"max_attempts": 1}
)


def is_s3_enabled():
    """Whether new files are sent to S3 (AWS and S3 storage are both switched on)."""
    settings = frappe.get_cached_doc("AWS Settings")
    return bool(cint(settings.enable_aws) and cint(settings.enable_s3))


def is_system_manager(user=None):
    return "System Manager" in frappe.get_roles(user or frappe.session.user)


def s3_connection_works():
    """Whether the bucket can be reached with the saved credentials right now.

    AWS keeps no connection status the way a Drive account does, so this asks S3 with
    a short head_bucket call. It works while S3 is disabled (that is when it is
    needed) and the answer is cached for a minute so opening files does not repeat it.
    """
    cached = frappe.cache.get_value(CONNECTION_CACHE_KEY, expires=True)
    if cached is not None:
        return bool(cached)

    try:
        from aws_integration.s3.client import S3Client

        client = S3Client(require_enabled=False, config=CONNECTION_CHECK_CONFIG)
        works = bool(client.bucket) and bool(client.test_connection().get("success"))
    except Exception:
        works = False

    frappe.cache.set_value(CONNECTION_CACHE_KEY, 1 if works else 0, expires_in_sec=CONNECTION_CACHE_SECONDS)
    return works


def s3_state(doc):
    """Where a File stands with S3."""
    if cint(doc.get("is_on_s3")) and doc.get("s3_key"):
        return "stored"
    if cint(doc.get("s3_upload_skipped")):
        return "skipped"
    return "pending"


def add_s3_form_context(doc, method=None):
    """Tell the File form what S3 should show (File onload hook).

    enabled: whether S3 storage is switched on. When it is off the form shows nothing
    about S3, except to System Managers for a file that is already on S3 while the
    bucket can be reached.
    connected: only when S3 is off, only for a System Manager and only for a file that
    is on S3, the one case that needs it. Checking costs a network call, so it is not
    made otherwise.
    """
    del method
    if doc.is_folder:
        return

    state = s3_state(doc)
    enabled = is_s3_enabled()
    context = {"state": state, "enabled": enabled}
    if not enabled and state == "stored" and is_system_manager():
        context["connected"] = s3_connection_works()
    doc.set_onload("s3", context)
