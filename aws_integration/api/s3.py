import mimetypes
from urllib.parse import unquote

import frappe
from frappe import _

from aws_integration.s3 import get_s3_file_url
from aws_integration.s3.handlers import _update_parent_attach_field


@frappe.whitelist(allow_guest=True)
def generate_file(key=None, file_name=None):
    """Generate a presigned S3 URL and redirect to it.

    This is the main file serving endpoint. When a file is uploaded to S3,
    its file_url is rewritten to:
    /api/method/aws_integration.api.s3.generate_file?key=...&file_name=...

    This method:
    1. Validates the key exists
    2. Checks file permissions (if the file is private)
    3. Generates a presigned URL
    4. Redirects the browser to the presigned URL

    Args:
        key (str): S3 key (relative, without prefix) e.g.
            "2026/02/17/Communication/HASH_filename.pdf"
        file_name (str): Display file name for Content-Disposition header.

    Raises:
        frappe.ValidationError: When key is not provided.
        frappe.DoesNotExistError: When no File document matches the key.
        frappe.PermissionError: When the requesting user lacks read access
            to the attached document.
    """
    if not key:
        frappe.throw(_("File key is required"), frappe.ValidationError)

    key = unquote(key)  # handle both raw and pre-encoded keys

    # Find the File document by s3_key
    file_name_doc = frappe.db.get_value(
        "File",
        {"s3_key": key},
        ["name", "is_private", "attached_to_doctype", "attached_to_name", "file_name"],
        as_dict=True,
    )

    if not file_name_doc:
        frappe.throw(_("File not found"), frappe.DoesNotExistError)

    # Check permissions for private files
    if file_name_doc.is_private:
        if frappe.session.user == "Guest":
            raise frappe.PermissionError(_("Please login to access this file"))

        if file_name_doc.attached_to_doctype and file_name_doc.attached_to_name:
            if not frappe.has_permission(
                file_name_doc.attached_to_doctype,
                "read",
                file_name_doc.attached_to_name,
            ):
                raise frappe.PermissionError(
                    _("You don't have permission to access this file")
                )
        else:
            # Private file not attached to any document — only the owner
            # and System Manager should be able to access it.
            file_owner = frappe.db.get_value("File", file_name_doc.name, "owner")
            if file_owner != frappe.session.user and "System Manager" not in frappe.get_roles():
                raise frappe.PermissionError(
                    _("You don't have permission to access this file")
                )

    # Generate presigned URL
    from aws_integration.s3.client import S3Client

    s3_client = S3Client()
    display_name = file_name or file_name_doc.file_name
    presigned_url = s3_client.generate_presigned_url(key, file_name=display_name)

    # Redirect to the presigned URL
    frappe.local.response["type"] = "redirect"
    frappe.local.response["location"] = presigned_url


@frappe.whitelist()
def get_s3_provider_regions(provider):
    """Return available regions for a given S3 provider."""
    from aws_integration.s3.providers import get_provider_regions
    return get_provider_regions(provider)


@frappe.whitelist()
def test_s3_connection():
    """Test S3 bucket connectivity.

    Requires the System Manager role. Instantiates an S3Client and delegates
    the connectivity check to its test_connection method.

    Returns:
        dict: {
            "success": bool,
            "message": str
        }

    Raises:
        frappe.PermissionError: When the user is not a System Manager.
    """
    frappe.only_for("System Manager")

    from aws_integration.s3.client import S3Client

    try:
        s3_client = S3Client()
        return s3_client.test_connection()
    except Exception as e:
        return {"success": False, "message": str(e)}


@frappe.whitelist()
def migrate_files_to_s3():
    """Start bulk migration of local files to S3.

    Validates that S3 is enabled, counts pending local files, then enqueues
    a long-running background job to perform the actual upload. Returns
    immediately with a count of files queued.

    Returns:
        str: Translated status message indicating files queued or none found.

    Raises:
        frappe.PermissionError: When the user is not a System Manager.
        frappe.ValidationError: When S3 is not enabled in AWS Settings.
    """
    frappe.only_for("System Manager")

    settings = frappe.get_cached_doc("AWS Settings")
    if not settings.enable_aws or not settings.enable_s3:
        frappe.throw(_("S3 is not enabled in AWS Settings"))

    # Count pending files, excluding exempt doctypes to match what the actual
    # bulk migration will process (avoids showing an inflated number to the user).
    exempt_doctypes = list({d.exempt_doctype for d in settings.exempt_doctypes})
    pending_filters = {
        "is_folder": 0,
        "file_url": ("like", "/%"),
        "is_on_s3": 0,
        "s3_upload_skipped": 0,
    }
    if exempt_doctypes:
        pending_filters["attached_to_doctype"] = ("not in", exempt_doctypes)

    count = frappe.db.count("File", pending_filters)

    if count == 0:
        return _("No local files to migrate. All files are already on S3.")

    # Enqueue background job
    frappe.enqueue(
        "aws_integration.s3.scheduler.bulk_migrate_files",
        queue="long",
        timeout=3600,
        now=False,
    )

    return _(
        "{0} files queued for migration to S3. This will run in the background."
    ).format(count)


@frappe.whitelist()
def get_s3_status():
    """Return S3 upload statistics for the current site.

    Requires the System Manager role. Queries the File table to count
    files on S3, files pending upload, exempt files, and size totals.

    Returns:
        dict: {
            "on_s3": int,
            "pending": int,
            "exempt": int,
            "total_files": int,
            "s3_size": int,          # bytes
            "pending_size": int,     # bytes
            "last_uploaded_at": str or None,
            "recent_errors": int,
            "recent_skipped": int
        }
    """
    frappe.only_for("System Manager")

    settings = frappe.get_cached_doc("AWS Settings")
    exempt_doctypes = [d.exempt_doctype for d in settings.exempt_doctypes]

    # Files already on S3
    on_s3 = frappe.db.count("File", {"is_folder": 0, "is_on_s3": 1})

    # Total S3 file size (from file_size field)
    File = frappe.qb.DocType("File")
    s3_size = (
        frappe.qb.from_(File)
        .select(frappe.qb.functions.Coalesce(frappe.qb.functions.Sum(File.file_size), 0))
        .where(File.is_folder == 0)
        .where(File.is_on_s3 == 1)
    ).run()[0][0]

    # Pending local files (not on S3, local URL, not skipped)
    # Use frappe.qb for both count and size so NULL handling is consistent:
    # files with no attached_to_doctype (NULL) must be included.
    pending_base = (
        frappe.qb.from_(File)
        .where(File.is_folder == 0)
        .where(File.file_url.like("/%"))
        .where(File.is_on_s3 == 0)
        .where(File.s3_upload_skipped == 0)
    )
    if exempt_doctypes:
        pending_base = pending_base.where(
            (File.attached_to_doctype.isnull()) | (File.attached_to_doctype.notin(exempt_doctypes))
        )

    Fn = frappe.qb.functions
    result = pending_base.select(Fn.Count("*"), Fn.Coalesce(Fn.Sum(File.file_size), 0)).run()
    pending = result[0][0]
    pending_size = result[0][1]

    # Exempt files count (local files attached to exempt doctypes)
    exempt = 0
    if exempt_doctypes:
        exempt = frappe.db.count(
            "File",
            {
                "is_folder": 0,
                "file_url": ("like", "/%"),
                "is_on_s3": 0,
                "attached_to_doctype": ("in", exempt_doctypes),
            },
        )

    # Total non-folder files
    total_files = frappe.db.count("File", {"is_folder": 0})

    # Last upload timestamp
    last_uploaded_at = frappe.db.sql(
        "SELECT MAX(s3_uploaded_at) FROM `tabFile` WHERE is_on_s3=1"
    )[0][0]

    # Count files skipped during migration (file not found on disk)
    recent_skipped = frappe.db.count("File", {"is_folder": 0, "s3_upload_skipped": 1})

    # Recent S3 errors from Error Log (last 7 days)
    cutoff = frappe.utils.add_days(frappe.utils.nowdate(), -7)
    recent_errors = frappe.db.count("Error Log", {
        "creation": (">=", cutoff),
        "method": ("like", "%s3%"),
    })

    return {
        "on_s3": on_s3,
        "pending": pending,
        "exempt": exempt,
        "total_files": total_files,
        "s3_size": int(s3_size),
        "pending_size": int(pending_size),
        "last_uploaded_at": str(last_uploaded_at) if last_uploaded_at else None,
        "recent_errors": recent_errors,
        "recent_skipped": recent_skipped,
    }


@frappe.whitelist()
def cleanup_local_s3_files():
    """Delete local copies of files that are already on S3.

    Counts files where is_on_s3=1 and a local file still exists on disk,
    then enqueues a background job to delete them.

    Returns:
        str: Status message with count of files to clean up.
    """
    frappe.only_for("System Manager")

    settings = frappe.get_cached_doc("AWS Settings")
    if not settings.enable_aws or not settings.enable_s3:
        frappe.throw(_("S3 is not enabled in AWS Settings"))

    count = frappe.db.count("File", {"is_folder": 0, "is_on_s3": 1})

    if count == 0:
        return _("No files on S3 found. Nothing to clean up.")

    frappe.enqueue(
        "aws_integration.s3.scheduler.cleanup_local_s3_files",
        queue="long",
        timeout=3600,
        now=False,
    )

    return _(
        "{0} files on S3 will be checked. Local copies will be deleted in the background."
    ).format(count)


@frappe.whitelist()
def upload_single_file_to_s3(file_name):
    """Queue a single file for upload to S3.

    Args:
        file_name (str): File document name to upload.

    Returns:
        dict: {"success": bool, "message": str}
    """
    frappe.only_for("System Manager")

    file_doc = frappe.get_doc("File", file_name)
    if file_doc.is_on_s3:
        return {"success": False, "message": _("File is already on S3")}

    if not file_doc.file_url or not file_doc.file_url.startswith("/"):
        return {"success": False, "message": _("File is not a local file")}

    settings = frappe.get_cached_doc("AWS Settings")
    if not settings.enable_aws or not settings.enable_s3:
        return {"success": False, "message": _("S3 is not enabled in AWS Settings")}

    frappe.enqueue(
        "aws_integration.s3.handlers._upload_single_file",
        queue="short",
        timeout=300,
        file_name=file_name,
    )

    return {"success": True, "message": _("File queued for S3 upload")}


@frappe.whitelist()
def get_file_preview(file_name=None, file_url=None):
    """Generate a presigned URL for file preview.

    Resolves the File document by name or file_url, verifies the file is
    stored on S3, checks read permissions, and returns a presigned URL
    together with MIME type metadata.

    Args:
        file_name (str): File document name (primary lookup key).
        file_url (str): Original file URL (fallback lookup key).

    Returns:
        dict: {
            "url": str,           # Presigned S3 URL
            "content_type": str,  # MIME type of the file
            "file_name": str      # Original file name
        }

    Raises:
        frappe.ValidationError: When neither file_name nor file_url is provided,
            or when the file is not stored on S3.
        frappe.PermissionError: When the user lacks read access to the attached
            document.
    """
    if not file_name and not file_url:
        frappe.throw(_("Either file_name or file_url is required"))

    if file_name:
        file_doc = frappe.get_doc("File", file_name)
    else:
        file_doc = frappe.get_doc("File", {"file_url": file_url})

    if not file_doc.is_on_s3 or not file_doc.s3_key:
        frappe.throw(_("This file is not stored on S3"))

    # Check permissions
    if file_doc.is_private:
        if file_doc.attached_to_doctype and file_doc.attached_to_name:
            if not frappe.has_permission(
                file_doc.attached_to_doctype,
                "read",
                file_doc.attached_to_name,
            ):
                raise frappe.PermissionError

    from aws_integration.s3.client import S3Client

    s3_client = S3Client()
    presigned_url = s3_client.generate_presigned_url(
        file_doc.s3_key, file_name=file_doc.file_name
    )

    content_type = (
        mimetypes.guess_type(file_doc.file_name)[0] or "application/octet-stream"
    )

    return {
        "url": presigned_url,
        "content_type": content_type,
        "file_name": file_doc.file_name,
    }


@frappe.whitelist()
def delete_local_file(file_name):
    """Delete the local copy of a file that has already been uploaded to S3."""
    frappe.only_for("System Manager")

    file_doc = frappe.get_doc("File", file_name)
    if not file_doc.is_on_s3:
        frappe.throw(_("File is not on S3"))
    if file_doc.local_deleted:
        frappe.throw(_("Local file already deleted"))

    old_url = file_doc.file_url
    file_doc._delete_file_on_disk()

    s3_file_url = get_s3_file_url(file_doc.s3_key, file_doc.file_name)
    frappe.db.set_value("File", file_doc.name, {
        "local_deleted": 1,
        "file_url": s3_file_url,
    }, update_modified=False)
    _update_parent_attach_field(file_doc, old_url, s3_file_url)
    frappe.db.commit()

    return {"success": True}


@frappe.whitelist()
def adopt_orphaned_files():
    """Find files on disk with no File document, create File docs, and queue for S3 upload.

    Scans public/files and private/files for orphaned files (present on disk
    but not tracked by any File document). Creates File documents so the
    S3 scheduler can upload them on its next run.

    Returns:
        str: Status message.
    """
    frappe.only_for("System Manager")

    settings = frappe.get_cached_doc("AWS Settings")
    if not settings.enable_aws or not settings.enable_s3:
        frappe.throw(_("S3 is not enabled in AWS Settings"))

    lock_key = "s3_adopt_orphans_running"
    if frappe.cache.get_value(lock_key):
        return _("Orphan file adoption is already running. Please wait for it to complete.")

    frappe.enqueue(
        "aws_integration.s3.scheduler.adopt_orphaned_files",
        queue="long",
        timeout=3600,
        now=False,
    )

    return _(
        "Scanning for orphaned files in the background. "
        "You will be notified when the scan is complete."
    )
