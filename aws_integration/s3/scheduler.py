import os

import frappe
from frappe.utils import cint, now_datetime

from aws_integration.s3 import get_s3_file_url
from aws_integration.s3.handlers import (
    _mark_dedup_file_as_s3,
    _publish_system_manager_realtime,
    _update_parent_attach_field,
)


def upload_pending_files():
    """Scheduler job: Upload local files to S3.

    Runs periodically based on the hourly scheduler event.
    Processes files in batches, skipping exempt doctypes.
    Deletes local files after upload if configured.

    Returns:
        None
    """
    settings = frappe.get_cached_doc("AWS Settings")
    if not settings.enable_aws or not settings.enable_s3:
        return

    batch_size = cint(settings.s3_batch_size) or 50

    # Build set of exempt doctypes for O(1) lookup
    exempt_doctypes = {d.exempt_doctype for d in settings.exempt_doctypes}

    # Query local files not yet uploaded to S3.
    # file_url starting with "/" means local; is_on_s3 = 0 means not yet migrated.
    filters = {
        "is_folder": 0,
        "file_url": ("like", "/%"),
        "is_on_s3": 0,
        "s3_upload_skipped": 0,
    }

    files = frappe.get_all(
        "File",
        filters=filters,
        fields=[
            "name",
            "file_name",
            "file_url",
            "is_private",
            "attached_to_doctype",
            "attached_to_name",
        ],
        limit_page_length=batch_size,
        order_by="creation asc",
    )

    if not files:
        return

    from aws_integration.s3.client import S3Client

    try:
        s3_client = S3Client()
    except Exception as e:
        frappe.log_error(
            title="S3 Upload Error",
            message=f"Failed to initialize S3 client: {str(e)}\n{frappe.get_traceback()}",
        )
        return

    uploaded_count = 0
    failed_files = []

    for file_data in files:
        # Skip files attached to exempt doctypes
        if file_data.attached_to_doctype and file_data.attached_to_doctype in exempt_doctypes:
            continue

        try:
            file_doc = frappe.get_doc("File", file_data.name)

            # Skip if already on S3 (double-check after fresh fetch)
            if file_doc.is_on_s3:
                continue

            # Skip if file_url already points to the S3 API route
            if file_doc.file_url and file_doc.file_url.startswith("/api/method/"):
                continue

            # Acquire a row-level lock to prevent a concurrent instant-upload
            # worker from uploading the same file at the same time.
            lock_result = frappe.db.sql(
                "SELECT name FROM `tabFile` WHERE name=%s AND is_on_s3=0 FOR UPDATE",
                file_doc.name,
            )
            if not lock_result:
                continue

            # Resolve the absolute path on disk
            file_path = file_doc.get_full_path()
            if not os.path.exists(file_path):
                frappe.db.set_value("File", file_data.name, "s3_upload_skipped", 1, update_modified=False)
                frappe.db.commit()
                continue

            # Content-hash dedup: if an identical file is already on S3,
            # reuse its key instead of uploading again.
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
                    uploaded_count += 1
                    continue

            # Upload to S3 and receive the object key
            s3_key = s3_client.upload_file(file_doc)

            # Persist S3 metadata on the File document without touching modified timestamp.
            # Keep file_url as the local path so the file can be served locally.
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

            # Commit BEFORE removing the local file so that a failed commit
            # never leaves the file permanently lost.
            frappe.db.commit()

            # Optionally remove the local copy after a successful upload.
            # Only now switch file_url to the S3 API route.
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

            uploaded_count += 1

        except Exception:
            failed_files.append(file_data.name)
            continue

    if failed_files:
        frappe.log_error(
            title="S3 Upload Errors",
            message=f"{uploaded_count} uploaded, {len(failed_files)} failed:\n" + "\n".join(failed_files),
        )


def bulk_migrate_files():
    """Coordinator: fan out file migration into parallel batch jobs."""
    settings = frappe.get_cached_doc("AWS Settings")
    if not settings.enable_aws or not settings.enable_s3:
        return

    # Prevent duplicate migrations from overlapping clicks
    lock_key = "s3_bulk_migration_running"
    if frappe.cache.get_value(lock_key):
        return
    frappe.cache.set_value(lock_key, 1, expires_in_sec=3600)

    exempt_doctypes = list({d.exempt_doctype for d in settings.exempt_doctypes})
    batch_size = cint(settings.s3_batch_size) or 50

    filters = _get_pending_filters(exempt_doctypes)
    pending_files = frappe.get_all(
        "File", filters=filters, fields=["name"], order_by="creation asc",
        limit_page_length=0,
    )

    if not pending_files:
        frappe.cache.delete_value(lock_key)
        return

    total = len(pending_files)
    chunks = []
    for i in range(0, total, batch_size):
        chunks.append([f.name for f in pending_files[i:i + batch_size]])

    migration_id = frappe.generate_hash(length=10)

    # Store progress in cache. Updates are protected by a Redis lock
    # in _update_migration_progress. TTL ensures cleanup on crash.
    frappe.cache.set_value(f"s3_migration:{migration_id}", {
        "total_batches": len(chunks),
        "completed_batches": 0,
        "uploaded": 0,
        "failed": 0,
    }, expires_in_sec=3600)

    for chunk in chunks:
        frappe.enqueue(
            _migrate_file_batch,
            queue="short",
            timeout=600,
            file_names=chunk,
            migration_id=migration_id,
            total=total,
        )


def _migrate_file_batch(file_names, migration_id, total):
    """Worker: upload a batch of files to S3."""
    settings = frappe.get_cached_doc("AWS Settings")
    from aws_integration.s3.client import S3Client

    try:
        s3_client = S3Client()
    except Exception as e:
        frappe.log_error(title="S3 Migration Error", message=f"Failed to init S3 client: {e}")
        _update_migration_progress(migration_id, 0, len(file_names), total)
        return

    uploaded = 0
    failed_files = []

    for file_name in file_names:
        try:
            file_doc = frappe.get_doc("File", file_name)

            lock_result = frappe.db.sql(
                "SELECT name FROM `tabFile` WHERE name=%s AND is_on_s3=0 FOR UPDATE",
                file_doc.name,
            )
            if not lock_result:
                continue

            file_path = file_doc.get_full_path()

            if not os.path.exists(file_path):
                frappe.db.set_value("File", file_name, "s3_upload_skipped", 1, update_modified=False)
                frappe.db.commit()
                failed_files.append(file_name)
                continue

            s3_key = s3_client.upload_file(file_doc)

            frappe.db.set_value("File", file_doc.name, {
                "s3_key": s3_key,
                "is_on_s3": 1,
                "s3_uploaded_at": now_datetime(),
            }, update_modified=False)
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

            uploaded += 1

        except Exception:
            failed_files.append(file_name)

    if failed_files:
        frappe.log_error(
            title="S3 Migration Errors",
            message=f"{uploaded} uploaded, {len(failed_files)} failed:\n" + "\n".join(failed_files),
        )

    _update_migration_progress(migration_id, uploaded, len(failed_files), total)


def _update_migration_progress(migration_id, batch_uploaded, batch_failed, total):
    """Update migration progress under a Redis lock for atomicity."""
    cache_key = f"s3_migration:{migration_id}"
    lock_key = f"s3_migration_lock:{migration_id}"

    with frappe.cache.lock(lock_key, timeout=5):
        progress = frappe.cache.get_value(cache_key) or {}

        progress["uploaded"] = progress.get("uploaded", 0) + batch_uploaded
        progress["failed"] = progress.get("failed", 0) + batch_failed
        progress["completed_batches"] = progress.get("completed_batches", 0) + 1

        frappe.cache.set_value(cache_key, progress, expires_in_sec=3600)

    _publish_system_manager_realtime("s3_migration_progress", {
        "uploaded": progress["uploaded"],
        "failed": progress["failed"],
        "total": total,
    })

    if progress["completed_batches"] >= progress.get("total_batches", 0):
        _publish_system_manager_realtime("s3_migration_complete", {
            "uploaded": progress["uploaded"],
            "failed": progress["failed"],
        })
        frappe.cache.delete_value(cache_key)
        frappe.cache.delete_value("s3_bulk_migration_running")


def cleanup_local_s3_files():
    """Background job: Delete local copies of files already uploaded to S3.

    Queries files where is_on_s3=1 and local_deleted=0, checks if a local
    copy still exists on disk, and deletes it. Publishes realtime progress events.
    """
    settings = frappe.get_cached_doc("AWS Settings")
    if not settings.enable_aws or not settings.enable_s3:
        return

    batch_size = cint(settings.s3_batch_size) or 50
    total_deleted = 0
    total_skipped = 0
    total_missing = 0
    skipped_files = []

    cleanup_filters = {"is_folder": 0, "is_on_s3": 1, "local_deleted": 0}
    total_pending = frappe.db.count("File", cleanup_filters)
    if not total_pending:
        return

    max_batches = (total_pending // batch_size) + 2

    for _ in range(max_batches):
        files = frappe.get_all(
            "File",
            filters=cleanup_filters,
            fields=["name", "file_name", "file_url", "is_private", "s3_key"],
            limit_page_length=batch_size,
            order_by="creation asc",
        )

        if not files:
            break

        for file_data in files:
            try:
                file_doc = frappe.get_doc("File", file_data.name)
                file_path = file_doc.get_full_path()
                existed = os.path.exists(file_path)

                if existed:
                    file_doc._delete_file_on_disk()

                if existed and os.path.exists(file_path):
                    # File still exists — shared via content_hash, skip
                    total_skipped += 1
                    continue

                if existed:
                    total_deleted += 1
                else:
                    total_missing += 1

                # Mark as locally deleted and switch file_url to S3 API route
                update_fields = {"local_deleted": 1}
                old_url = file_data.file_url
                if file_data.s3_key:
                    s3_file_url = get_s3_file_url(file_data.s3_key, file_data.file_name)
                    update_fields["file_url"] = s3_file_url
                frappe.db.set_value("File", file_data.name, update_fields, update_modified=False)
                if file_data.s3_key:
                    _update_parent_attach_field(file_doc, old_url, s3_file_url)

            except Exception:
                total_skipped += 1
                skipped_files.append(file_data.name)

        # Commit after each batch so progress is not lost on crash
        frappe.db.commit()

        _publish_system_manager_realtime(
            "s3_cleanup_progress",
            {
                "deleted": total_deleted,
                "missing": total_missing,
                "skipped": total_skipped,
                "total": total_pending,
            },
        )

        if len(files) < batch_size:
            break

    _publish_system_manager_realtime(
        "s3_cleanup_complete",
        {
            "deleted": total_deleted,
            "missing": total_missing,
            "skipped": total_skipped,
        },
    )

    if skipped_files:
        frappe.log_error(
            title="S3 Local Cleanup Errors",
            message=f"{total_deleted} deleted, {total_missing} already gone, {len(skipped_files)} failed:\n"
            + "\n".join(skipped_files),
        )


def adopt_orphaned_files():
    """Background job: Find files on disk with no File document, create File docs, and upload to S3.

    Walks public/files and private/files in streaming batches, cross-references
    the database, creates File documents for any orphans found, and uploads each
    to S3 following the same flow as regular file uploads (content-hash dedup,
    S3 upload, optional local deletion). If an S3 upload fails, the File doc is
    still committed so the hourly scheduler can retry.

    Uses a Redis lock to prevent duplicate runs from creating duplicate File docs.
    Resolves symlinks to prevent path traversal outside the site directory.
    """
    lock_key = "s3_adopt_orphans_running"
    if frappe.cache.get_value(lock_key):
        return
    frappe.cache.set_value(lock_key, 1, expires_in_sec=3600)

    site_path = frappe.get_site_path()
    adopted = 0
    errors = 0

    try:
        settings = frappe.get_cached_doc("AWS Settings")

        from aws_integration.s3.client import S3Client

        s3_client = S3Client()

        for base_dir, is_private in [("public/files", 0), ("private/files", 1)]:
            full_dir = os.path.join(site_path, base_dir)
            real_base = os.path.realpath(full_dir)
            if not os.path.isdir(real_base):
                continue

            # Stream disk files in batches to cap memory usage
            batch = {}
            for root, _dirs, files in os.walk(full_dir):
                for fname in files:
                    full_path = os.path.join(root, fname)

                    # Symlink boundary check — prevent traversal outside site dir
                    if not os.path.realpath(full_path).startswith(real_base + os.sep):
                        continue

                    rel_path = os.path.relpath(full_path, site_path)
                    if is_private:
                        file_url = "/" + rel_path
                    else:
                        file_url = "/" + rel_path.replace("public/", "", 1)
                    batch[file_url] = full_path

                    if len(batch) >= 1000:
                        a, e = _process_orphan_batch(batch, is_private, s3_client, settings)
                        adopted += a
                        errors += e
                        batch.clear()
                        _publish_system_manager_realtime(
                            "s3_orphan_progress",
                            {"adopted": adopted, "errors": errors},
                        )

            # Process remaining files in the last partial batch
            if batch:
                a, e = _process_orphan_batch(batch, is_private, s3_client, settings)
                adopted += a
                errors += e
    except Exception as e:
        frappe.log_error(
            title="Orphan Adoption Error",
            message=f"Failed during orphan adoption: {str(e)}",
        )
    finally:
        frappe.cache.delete_value(lock_key)
        frappe.db.commit()
        _publish_system_manager_realtime(
            "s3_orphan_complete",
            {"adopted": adopted, "errors": errors},
        )


def _process_orphan_batch(batch, is_private, s3_client, settings):
    """Create File documents for orphaned files and upload them to S3.

    Queries the database to find which file_urls already exist,
    then creates File documents for the rest and uploads each to S3
    following the same flow as _upload_single_file (content-hash dedup,
    S3 upload, optional local deletion). If the S3 upload fails for a
    file, the File doc is still committed so upload_pending_files can
    retry on the next scheduler run.

    Returns:
        tuple: (adopted_count, error_count)
    """
    if not batch:
        return 0, 0

    urls = list(batch.keys())
    results = frappe.db.sql(
        "SELECT file_url FROM `tabFile` WHERE file_url IN ({})".format(
            ", ".join(["%s"] * len(urls))
        ),
        tuple(urls),
    )
    existing = {r[0] for r in results}

    adopted = 0
    failed_urls = []

    for file_url, full_path in batch.items():
        if file_url in existing:
            continue

        # --- Step 1: Create File document ---
        try:
            file_doc = frappe.new_doc("File")
            file_doc.file_name = os.path.basename(full_path)
            file_doc.file_url = file_url
            file_doc.is_private = is_private
            file_doc.file_size = os.path.getsize(full_path)
            file_doc.folder = "Home"
            file_doc.flags.skip_s3_upload = True
            file_doc.insert(ignore_permissions=True)
            frappe.db.commit()
            adopted += 1
        except Exception:
            failed_urls.append(file_url)
            continue

        # --- Step 2: Upload to S3 (same flow as _upload_single_file) ---
        # On failure the File doc is already committed; the hourly
        # upload_pending_files scheduler will retry.
        try:
            # Content-hash dedup: reuse S3 key if identical file already on S3
            if file_doc.content_hash:
                existing_s3 = frappe.db.get_value(
                    "File",
                    {"content_hash": file_doc.content_hash, "is_on_s3": 1, "name": ["!=", file_doc.name]},
                    ["s3_key", "s3_uploaded_at"],
                    as_dict=True,
                )
                if existing_s3:
                    _mark_dedup_file_as_s3(
                        file_doc, s3_key=existing_s3.s3_key, uploaded_at=existing_s3.s3_uploaded_at
                    )
                    frappe.db.commit()
                    continue

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
            frappe.db.commit()

            if settings.delete_local_after_upload and os.path.exists(full_path):
                old_url = file_doc.file_url
                file_doc._delete_file_on_disk()
                if not os.path.exists(full_path):
                    s3_file_url = get_s3_file_url(s3_key, file_doc.file_name)
                    frappe.db.set_value("File", file_doc.name, {
                        "local_deleted": 1,
                        "file_url": s3_file_url,
                    }, update_modified=False)
                    _update_parent_attach_field(file_doc, old_url, s3_file_url)
                frappe.db.commit()
        except Exception:
            pass

    if failed_urls:
        frappe.log_error(
            title="Orphan File Adoption Errors",
            message=f"{len(failed_urls)} files failed:\n" + "\n".join(failed_urls),
        )

    frappe.db.commit()
    return adopted, len(failed_urls)


def _get_pending_filters(exempt_doctypes):
    """Build filters dict for querying local files not yet on S3."""
    filters = {
        "is_folder": 0,
        "file_url": ("like", "/%"),
        "is_on_s3": 0,
        "s3_upload_skipped": 0,
    }
    if exempt_doctypes:
        filters["attached_to_doctype"] = ("not in", exempt_doctypes)
    return filters
