import os
from urllib.parse import parse_qs, urlparse

import frappe
from frappe.utils import now_datetime

from aws_integration.s3 import S3_API_PREFIX, get_s3_file_url


def _publish_system_manager_realtime(event, message):
    """Publish realtime events only to System Managers."""
    recipients = frappe.get_all("Has Role", filters={"role": "System Manager", "parenttype": "User"}, pluck="parent")
    for user in set(recipients or []):
        if user and user != "Guest":
            frappe.publish_realtime(event, message=message, user=user)


def on_file_upload(doc, method):
    """Upload file to S3 immediately after it's created in ERP.

    Enqueues a short background job so the user's request is not blocked.
    Only runs when 'Instant Upload to S3' is enabled in AWS Settings.
    """
    if getattr(doc.flags, "skip_s3_upload", False):
        return

    if doc.is_folder or doc.is_on_s3:
        return

    # Only act on local files
    if not doc.file_url or not doc.file_url.startswith("/"):
        return

    # Dedup-created docs copy the S3 API URL from the original file.
    # Mark them as already on S3 instead of trying to upload again.
    if doc.file_url.startswith(S3_API_PREFIX):
        _mark_dedup_file_as_s3(doc)
        return

    # Dedup-created docs may copy a local file_url from a file already on S3.
    # Detect via content_hash and mark as S3 without re-uploading.
    if doc.content_hash:
        existing_s3 = frappe.db.get_value(
            "File",
            {"content_hash": doc.content_hash, "is_on_s3": 1, "name": ["!=", doc.name]},
            ["s3_key", "s3_uploaded_at"],
            as_dict=True,
        )
        if existing_s3:
            _mark_dedup_file_as_s3(doc, s3_key=existing_s3.s3_key, uploaded_at=existing_s3.s3_uploaded_at)
            return

    if doc.file_url.startswith("/api/method/"):
        return

    try:
        settings = frappe.get_cached_doc("AWS Settings")
        if not settings.enable_aws or not settings.enable_s3:
            return
        if not settings.upload_to_s3_on_save:
            return

        # Check exempt doctypes
        exempt_doctypes = {d.exempt_doctype for d in settings.exempt_doctypes}
        if doc.attached_to_doctype and doc.attached_to_doctype in exempt_doctypes:
            return

        frappe.enqueue(
            "aws_integration.s3.handlers._upload_single_file",
            queue="short",
            timeout=300,
            file_name=doc.name,
        )
    except Exception:
        frappe.log_error(
            title="S3 Upload Enqueue Failed",
            message=f"Failed to enqueue S3 upload for {doc.name}: {frappe.get_traceback()}",
        )


def _upload_single_file(file_name):
    """Background job: Upload a single file to S3.

    Uses a SELECT FOR UPDATE database lock to prevent race conditions when
    both the instant-upload handler and the hourly scheduler attempt to
    upload the same file concurrently. The commit is issued before the local
    file is removed so that a commit failure never leaves the file in a
    permanently lost state.
    """
    from aws_integration.s3.client import S3Client

    file_doc = frappe.get_doc("File", file_name)

    # get_doc already does a fresh DB read — check directly
    if file_doc.is_on_s3:
        return

    # Acquire a row-level lock; if another worker already claimed this file
    # (i.e. set is_on_s3=1 between our check above and now), the result will
    # be empty and we bail out without uploading again.
    lock_result = frappe.db.sql(
        "SELECT name FROM `tabFile` WHERE name=%s AND is_on_s3=0 FOR UPDATE",
        file_name,
    )
    if not lock_result:
        return

    # Dedup-created docs may have an S3 API URL as file_url.
    # Mark them as on S3 and bail out instead of calling get_full_path().
    if file_doc.file_url and file_doc.file_url.startswith(S3_API_PREFIX):
        _mark_dedup_file_as_s3(file_doc)
        frappe.db.commit()
        return

    # Dedup check (also in on_file_upload — repeated here because another file
    # with the same content_hash may have been uploaded to S3 between the hook
    # enqueue and this background job executing).
    if file_doc.content_hash:
        existing_s3 = frappe.db.get_value(
            "File",
            {"content_hash": file_doc.content_hash, "is_on_s3": 1, "name": ["!=", file_doc.name]},
            ["s3_key", "s3_uploaded_at"],
            as_dict=True,
        )
        if existing_s3:
            _mark_dedup_file_as_s3(file_doc, s3_key=existing_s3.s3_key, uploaded_at=existing_s3.s3_uploaded_at)
            frappe.db.commit()
            return

    file_path = file_doc.get_full_path()
    if not os.path.exists(file_path):
        return

    settings = frappe.get_cached_doc("AWS Settings")
    s3_client = S3Client()
    s3_key = s3_client.upload_file(file_doc)

    frappe.db.set_value(
        "File",
        file_doc.name,
        {
            "s3_key": s3_key,
            "is_on_s3": 1,
            "s3_uploaded_at": now_datetime(),
        },
        update_modified=False,
    )

    # Commit BEFORE removing the local file so that a failed commit never
    # leaves the file permanently lost (is_on_s3 still 0 but file gone).
    frappe.db.commit()

    if settings.delete_local_after_upload and os.path.exists(file_path):
        old_url = file_doc.file_url
        file_doc._delete_file_on_disk()
        if not os.path.exists(file_path):
            s3_file_url = get_s3_file_url(s3_key, file_doc.file_name)
            frappe.db.set_value("File", file_doc.name, {
                "local_deleted": 1,
                "file_url": s3_file_url,
            }, update_modified=False)
            _update_parent_attach_field(file_doc, old_url, s3_file_url)
        frappe.db.commit()

    # Notify the browser so the File form auto-refreshes with the S3 indicator
    _publish_system_manager_realtime("s3_upload_complete", {"file_name": file_doc.name})


def _mark_dedup_file_as_s3(doc, s3_key=None, uploaded_at=None):
    """Mark a dedup-created File doc as already on S3.

    When Frappe's content_hash dedup copies a file_url to a new File doc,
    the doc has is_on_s3=0 and no s3_key. This sets the correct S3 fields
    so the file shows up as "Stored on S3".

    Args:
        doc: The File document to mark.
        s3_key: S3 object key. If not provided, extracted from file_url (backward compat).
        uploaded_at: Original upload timestamp. If not provided, looked up from DB.
    """
    if not s3_key and doc.file_url and doc.file_url.startswith(S3_API_PREFIX):
        # Backward compat: extract from URL for existing files
        parsed = urlparse(doc.file_url)
        params = parse_qs(parsed.query)
        s3_key = params.get("key", [None])[0]

    if not s3_key:
        return

    if not uploaded_at:
        # Re-validate that a file with this s3_key still exists on S3.
        # Guards against TOCTOU: the source file may have been deleted
        # between the content_hash lookup and this point.
        uploaded_at = frappe.db.get_value(
            "File", {"s3_key": s3_key, "is_on_s3": 1}, "s3_uploaded_at"
        )
        if not uploaded_at:
            return

    frappe.db.set_value(
        "File",
        doc.name,
        {
            "is_on_s3": 1,
            "s3_key": s3_key,
            "s3_uploaded_at": uploaded_at,
        },
        update_modified=False,
    )


def _update_parent_attach_field(file_doc, old_url, new_url):
    """Update Attach/Attach Image fields on parent document when file_url changes.

    When a file is uploaded to S3 and the local copy is deleted, the File doc's
    file_url changes from a local path to the S3 API route. But if the file was
    attached via an Attach field, that field on the parent document still holds
    the old local URL. This function finds and updates the stale reference.
    """
    if not file_doc.attached_to_doctype or not file_doc.attached_to_name:
        return

    if not old_url or not new_url or old_url == new_url:
        return

    try:
        meta = frappe.get_meta(file_doc.attached_to_doctype)
    except Exception:
        return

    attach_fields = [
        df.fieldname for df in meta.fields
        if df.fieldtype in ("Attach", "Attach Image")
    ]

    if not attach_fields:
        return

    values = frappe.db.get_value(
        file_doc.attached_to_doctype,
        file_doc.attached_to_name,
        attach_fields,
        as_dict=True,
    )

    if not values:
        return

    for fieldname in attach_fields:
        if values.get(fieldname) == old_url:
            frappe.db.set_value(
                file_doc.attached_to_doctype,
                file_doc.attached_to_name,
                fieldname,
                new_url,
                update_modified=False,
            )
            frappe.clear_document_cache(file_doc.attached_to_doctype, file_doc.attached_to_name)
            break


def on_file_delete(doc, method):
    """Handle File deletion - also delete the object from S3 if it was uploaded.

    Called via the doc_events hook on File.on_trash. If AWS or S3 integration
    is disabled, or the file was never uploaded, this is a no-op.

    Args:
        doc: The File document being deleted.
        method (str): The hook method name that triggered this handler
            (always "on_trash" in normal usage).

    Returns:
        None

    Raises:
        Does not raise. Errors are captured with frappe.log_error so that a
        failed S3 deletion does not block the local file deletion.
    """
    if not doc.is_on_s3 or not doc.s3_key:
        return

    try:
        settings = frappe.get_cached_doc("AWS Settings")
        if not settings.enable_aws or not settings.enable_s3:
            return

        if not settings.delete_s3_on_trash:
            return

        # Don't delete the S3 object if other File docs still reference it
        # (e.g. dedup-created copies sharing the same s3_key)
        other_refs = frappe.db.count(
            "File", {"s3_key": doc.s3_key, "is_on_s3": 1, "name": ["!=", doc.name]}
        )
        if other_refs:
            return

        from aws_integration.s3.client import S3Client

        s3_client = S3Client()
        s3_client.delete_file(doc.s3_key)

    except Exception as e:
        frappe.log_error(
            title="S3 Delete Error",
            message=f"Failed to delete {doc.s3_key} from S3: {str(e)}\n{frappe.get_traceback()}",
        )
