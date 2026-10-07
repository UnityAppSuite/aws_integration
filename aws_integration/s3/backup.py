import json
import os
import re
import traceback

import frappe
from frappe import _
from frappe.utils import cint, now_datetime
from frappe.utils.backups import new_backup

from aws_integration.lazy_boto import get_boto3

# ── Whitelisted APIs ──

@frappe.whitelist()
def take_s3_backup():
	"""Whitelisted API: create a backup log and enqueue the job."""
	frappe.only_for("System Manager")
	return _enqueue_backup(triggered_by=frappe.session.user)


@frappe.whitelist()
def get_backup_download_url(log_name, file_field):
	"""Return a presigned download URL for one file in an S3 Backup Log."""
	frappe.only_for("System Manager")

	allowed_fields = {"db_file_url", "site_config_url", "files_archive_url", "private_archive_url"}
	if file_field not in allowed_fields:
		frappe.throw(_("Invalid file field. Must be one of: {0}").format(", ".join(sorted(allowed_fields))))

	log = frappe.get_doc("S3 Backup Log", log_name)
	s3_key = log.get(file_field)
	if not s3_key:
		frappe.throw(_("No S3 key found for field '{0}' on log {1}").format(file_field, log_name))

	settings = frappe.get_doc("AWS Settings")
	s3_client = _get_s3_client(settings)
	expiry = settings.s3_presigned_url_expiry or 900

	presigned_url = s3_client.generate_presigned_url(
		"get_object",
		Params={"Bucket": log.s3_bucket, "Key": s3_key},
		ExpiresIn=expiry,
	)

	return {"url": presigned_url}


@frappe.whitelist()
def cleanup_backup_local_files(log_name):
	"""Delete local backup files for a specific S3 Backup Log entry."""
	frappe.only_for("System Manager")

	log = frappe.get_doc("S3 Backup Log", log_name)
	if log.local_cleaned:
		frappe.throw(_("Local files already cleaned for {0}").format(log_name))

	if not log.local_backup_paths:
		frappe.throw(_("No local file paths stored for {0}").format(log_name))

	paths = json.loads(log.local_backup_paths)
	_cleanup_local_backups(log, list(paths.values()))

	return {"message": _("Local backup files deleted successfully.")}


@frappe.whitelist()
def upload_local_backups():
	"""Scan local backup directory, group by timestamp, and upload each set to S3."""
	frappe.only_for("System Manager")

	settings = frappe.get_cached_doc("AWS Settings")
	if not settings.enable_s3_backups:
		frappe.throw(_("S3 Backups are not enabled in AWS Settings"))

	backup_dir = os.path.join(frappe.get_site_path(), "private", "backups")
	if not os.path.isdir(backup_dir):
		frappe.throw(_("No local backup directory found"))

	# Group files by timestamp prefix: {YYYYMMDD_HHMMSS}-{sitename}-{type}
	groups = {}
	pattern = re.compile(r"^(\d{8}_\d{6})-(.+?)-(database\.sql\.gz|site_config_backup\.json|files\.tar|private-files\.tar)$")
	for fname in os.listdir(backup_dir):
		m = pattern.match(fname)
		if not m:
			continue
		ts = m.group(1)
		ftype = m.group(3)
		groups.setdefault(ts, {})[ftype] = os.path.join(backup_dir, fname)

	if not groups:
		frappe.throw(_("No backup files found in {0}").format(backup_dir))

	# Map local file type names to our internal keys
	type_key_map = {
		"database.sql.gz": "db",
		"site_config_backup.json": "conf",
		"files.tar": "files",
		"private-files.tar": "private",
	}

	# Batch check which logs already exist
	candidate_names = ["BKUP-" + ts.replace("_", "-") for ts in groups]
	existing_names = {
		r.name for r in frappe.get_all(
			"S3 Backup Log",
			filters={"name": ("in", candidate_names)},
			fields=["name"],
		)
	}

	new_groups = {ts: files for ts, files in groups.items()
		if "BKUP-" + ts.replace("_", "-") not in existing_names}
	if not new_groups:
		frappe.throw(_("All local backups have already been uploaded"))

	triggered_by = frappe.session.user
	queued = []
	for ts in sorted(new_groups):
		files = new_groups[ts]
		backup_paths = {}
		for ftype, path in files.items():
			key = type_key_map.get(ftype)
			if key:
				backup_paths[key] = path

		log_name = "BKUP-" + ts.replace("_", "-")
		log = frappe.get_doc({
			"doctype": "S3 Backup Log",
			"status": "Queued",
		})
		log.name = log_name
		log.flags.name_set = True
		log.local_backup_paths = json.dumps(backup_paths)
		log.insert(ignore_permissions=True)
		frappe.db.commit()

		backup_timeout = cint(settings.s3_backup_timeout) or 6000

		frappe.enqueue(
			_run_upload_existing,
			queue="long",
			timeout=backup_timeout,
			log_name=log_name,
			triggered_by=triggered_by,
		)
		queued.append(log_name)

	return {"queued": queued, "count": len(queued)}


# ── Background jobs ──

def _run_upload_existing(log_name, triggered_by="Administrator"):
	"""Background job: upload pre-existing local backup files to S3."""
	log = frappe.get_doc("S3 Backup Log", log_name)
	try:
		log.status = "Uploading"
		log.started_at = now_datetime()
		log.save(ignore_permissions=True)
		frappe.db.commit()

		frappe.publish_realtime(
			"s3_backup_progress",
			{"status": "uploading", "log_name": log_name},
			user=triggered_by,
		)

		backup_paths = json.loads(log.local_backup_paths)

		settings = frappe.get_doc("AWS Settings")
		bucket = settings.s3_backup_bucket_name or settings.s3_bucket_name
		# Use the original backup timestamp from local_backup_paths
		ts = _extract_timestamp_from_paths(backup_paths)
		prefix = settings.s3_backup_folder_prefix or settings.s3_folder_prefix
		folder = f"{prefix}/backups/{ts}" if prefix else f"backups/{ts}"

		s3_client = _get_s3_client(settings)

		uploaded, total_size, db_size, files_size = _upload_paths_to_s3(
			s3_client, bucket, folder, backup_paths
		)

		log.reload()
		log.status = "Success"
		log.completed_at = now_datetime()
		_set_log_upload_results(log, bucket, folder, uploaded, total_size, db_size, files_size)
		log.save(ignore_permissions=True)
		frappe.db.commit()

		if settings.delete_local_after_upload:
			_cleanup_local_backups(log, list(backup_paths.values()))

		frappe.publish_realtime(
			"s3_backup_progress",
			{"status": "success", "log_name": log_name},
			user=triggered_by,
		)

	except Exception:
		tb = traceback.format_exc()
		_mark_failed(log, tb)

		frappe.publish_realtime(
			"s3_backup_progress",
			{"status": "failed", "log_name": log_name},
			user=triggered_by,
		)


def _enqueue_backup(triggered_by="Administrator"):
	"""Create an S3 Backup Log entry (Queued) and enqueue the actual backup job."""
	settings = frappe.get_cached_doc("AWS Settings")
	if not settings.enable_s3_backups:
		frappe.throw(_("S3 Backups are not enabled in AWS Settings"))

	log = frappe.get_doc({
		"doctype": "S3 Backup Log",
		"status": "Queued",
	})
	log.insert(ignore_permissions=True)
	frappe.db.commit()

	backup_timeout = cint(settings.s3_backup_timeout) or 6000

	frappe.enqueue(
		_run_backup,
		queue="long",
		timeout=backup_timeout,
		log_name=log.name,
		triggered_by=triggered_by,
		retry_count=0,
	)

	return {"log_name": log.name}


def _run_backup(log_name, retry_count=0, triggered_by="Administrator"):
	"""Background job: generate backup, upload to S3, update log."""
	log = frappe.get_doc("S3 Backup Log", log_name)
	try:
		log.status = "Generating"
		log.started_at = now_datetime()
		log.save(ignore_permissions=True)
		frappe.db.commit()

		frappe.publish_realtime(
			"s3_backup_progress",
			{"status": "generating", "log_name": log_name},
			user=triggered_by,
		)

		settings = frappe.get_doc("AWS Settings")
		include_files = settings.s3_backup_files
		bucket = settings.s3_backup_bucket_name or settings.s3_bucket_name
		timestamp = now_datetime().strftime("%Y%m%d_%H%M%S")
		prefix = settings.s3_backup_folder_prefix or settings.s3_folder_prefix
		folder = f"{prefix}/backups/{timestamp}" if prefix else f"backups/{timestamp}"
		notify_email = settings.s3_backup_notify_email
		notify_success = settings.s3_backup_notify_success

		odb = new_backup(
			ignore_files=not include_files,
			force=True,
		)

		backup_paths = {
			"db": odb.backup_path_db,
			"conf": odb.backup_path_conf,
		}
		if include_files:
			backup_paths["files"] = odb.backup_path_files
			backup_paths["private"] = odb.backup_path_private_files

		# Store local paths for optional manual cleanup later
		log.reload()
		log.local_backup_paths = json.dumps(
			{k: v for k, v in backup_paths.items() if v}
		)
		log.status = "Uploading"
		log.save(ignore_permissions=True)
		frappe.db.commit()

		frappe.publish_realtime(
			"s3_backup_progress",
			{"status": "uploading", "log_name": log_name},
			user=triggered_by,
		)

		s3_client = _get_s3_client(settings)

		uploaded, total_size, db_size, files_size = _upload_paths_to_s3(
			s3_client, bucket, folder, backup_paths
		)

		log.reload()
		log.status = "Success"
		log.completed_at = now_datetime()
		_set_log_upload_results(log, bucket, folder, uploaded, total_size, db_size, files_size)
		log.save(ignore_permissions=True)
		frappe.db.commit()

		if settings.delete_local_after_upload:
			_cleanup_local_backups(log, list(backup_paths.values()))

		frappe.publish_realtime(
			"s3_backup_progress",
			{"status": "success", "log_name": log_name},
			user=triggered_by,
		)

		if notify_email and notify_success:
			_send_notification(notify_email, log, success=True)

	except Exception:
		tb = traceback.format_exc()

		if retry_count < 2:
			retry_timeout = cint(
				frappe.db.get_single_value("AWS Settings", "s3_backup_timeout")
			) or 6000
			frappe.enqueue(
				_run_backup,
				queue="long",
				timeout=retry_timeout,
				log_name=log_name,
				triggered_by=triggered_by,
				retry_count=retry_count + 1,
			)
			return

		_mark_failed(log, tb)

		frappe.publish_realtime(
			"s3_backup_progress",
			{"status": "failed", "log_name": log_name},
			user=triggered_by,
		)

		settings = frappe.get_doc("AWS Settings")
		notify_email = settings.s3_backup_notify_email
		if notify_email:
			_send_notification(notify_email, log, success=False)

		return

	# Rotate old backups OUTSIDE the try/except so rotation errors
	# don't trigger backup retries
	try:
		_rotate_old_backups(s3_client, settings, exclude_log=log_name)
	except Exception:
		frappe.log_error(
			title="S3 Backup Rotation Error",
			message=frappe.get_traceback(),
		)


# ── Shared helpers ──

_S3_FILE_NAMES = {
	"db": "database.sql.gz",
	"conf": "site_config.json",
	"files": "files.tar",
	"private": "private-files.tar",
}


def _get_backup_dir():
	return os.path.realpath(os.path.join(frappe.get_site_path(), "private", "backups"))


def _safe_backup_path(path):
	"""Ensure a path is within the site's backup directory."""
	real = os.path.realpath(path)
	backup_dir = _get_backup_dir()
	if not real.startswith(backup_dir + os.sep):
		frappe.throw(_("Invalid backup path: {0}").format(path))
	return real


def _upload_paths_to_s3(s3_client, bucket, folder, backup_paths):
	"""Upload a dict of {key: local_path} to S3. Returns (uploaded, total_size, db_size, files_size)."""
	uploaded = {}
	total_size = 0
	db_size = 0
	files_size = 0

	for key, path in backup_paths.items():
		if not path:
			continue
		safe_path = _safe_backup_path(path)
		if not os.path.exists(safe_path):
			continue
		file_name = _S3_FILE_NAMES.get(key, os.path.basename(safe_path))
		s3_key = f"{folder}/{file_name}"
		file_size = os.path.getsize(safe_path)

		s3_client.upload_file(safe_path, bucket, s3_key)
		uploaded[key] = s3_key
		total_size += file_size

		if key == "db":
			db_size = file_size
		elif key in ("files", "private"):
			files_size += file_size

	return uploaded, total_size, db_size, files_size


def _set_log_upload_results(log, bucket, folder, uploaded, total_size, db_size, files_size):
	"""Populate upload result fields on a log doc (does not save)."""
	log.s3_bucket = bucket
	log.s3_folder = folder
	log.db_file_url = uploaded.get("db", "")
	log.site_config_url = uploaded.get("conf", "")
	log.files_archive_url = uploaded.get("files", "")
	log.private_archive_url = uploaded.get("private", "")
	log.db_size = db_size
	log.files_size = files_size
	log.total_size = total_size


def _extract_timestamp_from_paths(backup_paths):
	"""Extract the YYYYMMDD_HHMMSS timestamp from local backup file paths."""
	pattern = re.compile(r"(\d{8}_\d{6})")
	for path in backup_paths.values():
		if path:
			m = pattern.search(os.path.basename(path))
			if m:
				return m.group(1)
	return now_datetime().strftime("%Y%m%d_%H%M%S")


def _get_s3_client(settings):
	"""Create a boto3 S3 client from AWS Settings credentials."""
	client_kwargs = {
		"region_name": settings.s3_bucket_region or settings.region,
		"aws_access_key_id": settings.aws_access_key_id,
		"aws_secret_access_key": settings.get_password("aws_secret_access_key"),
	}
	if settings.s3_endpoint_url:
		client_kwargs["endpoint_url"] = settings.s3_endpoint_url

	return get_boto3().client("s3", **client_kwargs)


def _cleanup_local_backups(log, paths):
	"""Delete local backup files after successful S3 upload."""
	for path in paths:
		if not path:
			continue
		safe_path = _safe_backup_path(path)
		if os.path.exists(safe_path):
			os.remove(safe_path)

	log.reload()
	log.local_cleaned = 1
	log.save(ignore_permissions=True)
	frappe.db.commit()


def _mark_failed(log, traceback_text):
	"""Set log to Failed status with traceback."""
	log.reload()
	log.status = "Failed"
	log.completed_at = now_datetime()
	log.error = traceback_text
	log.save(ignore_permissions=True)
	frappe.db.commit()

	frappe.log_error(title="S3 Backup Failed", message=traceback_text)


def _send_notification(email, log, success=True):
	"""Send backup notification email."""
	site = frappe.local.site

	if success:
		subject = _("S3 Backup Successful - {0}").format(site)
		message = _(
			"S3 backup completed successfully.<br><br>"
			"<b>Log:</b> {log_name}<br>"
			"<b>Bucket:</b> {bucket}<br>"
			"<b>Database:</b> {db_file}<br>"
			"<b>Total Size:</b> {total_size}"
		).format(
			log_name=log.name,
			bucket=log.s3_bucket,
			db_file=log.db_file_url,
			total_size=_fmt_size(log.total_size),
		)
	else:
		subject = _("S3 Backup Failed - {0}").format(site)
		message = _(
			"S3 backup failed.<br><br>"
			"<b>Log:</b> {log_name}<br>"
			"<b>Error:</b> Check the backup log for details."
		).format(log_name=log.name)

	try:
		frappe.sendmail(
			recipients=[email],
			subject=subject,
			message=message,
		)
	except Exception:
		frappe.log_error(title="S3 Backup Email Failed", message=frappe.get_traceback())


def rotate_old_backups_daily():
	"""Scheduler job: rotate old S3 backups based on retention settings."""
	settings = frappe.get_cached_doc("AWS Settings")
	if not settings.enable_s3_backups:
		return
	if not settings.enable_aws or not settings.enable_s3:
		return

	s3_client = _get_s3_client(settings)

	try:
		_rotate_old_backups(s3_client, settings)
	except Exception:
		frappe.log_error(
			title="S3 Backup Rotation Error",
			message=frappe.get_traceback(),
		)


def _rotate_old_backups(s3_client, settings, exclude_log=None):
	"""Delete old backups from S3 based on retention settings.

	Always preserves the latest successful backup to prevent accidental
	deletion of all backups (e.g. when retention_days is very short).
	"""
	retention_count = settings.s3_backup_retention_count or 0
	retention_days = settings.s3_backup_retention_days or 0

	if not retention_count and not retention_days:
		return

	all_logs = frappe.get_all(
		"S3 Backup Log",
		filters={"status": "Success"},
		fields=["name"],
		order_by="creation desc",
	)

	if not all_logs:
		return

	# Always protect the latest successful backup
	latest_log = all_logs[0].name

	logs_to_delete = set()

	if retention_count > 0:
		if len(all_logs) > retention_count:
			for entry in all_logs[retention_count:]:
				logs_to_delete.add(entry.name)

	if retention_days > 0:
		from frappe.utils import add_days
		cutoff = add_days(now_datetime(), -retention_days)
		old_logs = frappe.get_all(
			"S3 Backup Log",
			filters={"status": "Success", "completed_at": ["<", cutoff]},
			fields=["name"],
		)
		for entry in old_logs:
			logs_to_delete.add(entry.name)

	# Never delete the latest backup or the explicitly excluded one
	logs_to_delete.discard(latest_log)
	if exclude_log:
		logs_to_delete.discard(exclude_log)

	if not logs_to_delete:
		return

	for log_name in logs_to_delete:
		try:
			_delete_backup_from_s3(s3_client, log_name)
		except Exception:
			frappe.log_error(
				title=f"S3 Backup Rotation Failed: {log_name}",
				message=frappe.get_traceback(),
			)


def _delete_backup_from_s3(s3_client, log_name):
	"""Delete all S3 objects for a backup log entry, then delete the log."""
	log = frappe.get_doc("S3 Backup Log", log_name)
	bucket = log.s3_bucket

	if not bucket:
		frappe.delete_doc("S3 Backup Log", log_name, ignore_permissions=True)
		return

	s3_keys = [
		log.db_file_url,
		log.site_config_url,
		log.files_archive_url,
		log.private_archive_url,
	]
	objects = [{"Key": key} for key in s3_keys if key]

	if objects:
		s3_client.delete_objects(
			Bucket=bucket,
			Delete={"Objects": objects, "Quiet": True},
		)

	frappe.delete_doc("S3 Backup Log", log_name, ignore_permissions=True)
	frappe.db.commit()


def _fmt_size(size_bytes):
	"""Format bytes into a human-readable string."""
	if not size_bytes:
		return "0 B"
	units = ["B", "KB", "MB", "GB", "TB"]
	i = 0
	size = float(size_bytes)
	while size >= 1024 and i < len(units) - 1:
		size /= 1024
		i += 1
	return f"{size:.1f} {units[i]}"


# ── Scheduler functions ──

def take_backups_daily():
	"""Called by scheduler daily event."""
	_take_backups_if("Daily")


def take_backups_weekly():
	"""Called by scheduler weekly event."""
	_take_backups_if("Weekly")


def take_backups_monthly():
	"""Called by scheduler monthly event."""
	_take_backups_if("Monthly")


def _take_backups_if(freq):
	"""Create a backup if the configured frequency matches."""
	settings = frappe.get_cached_doc("AWS Settings")
	if not settings.enable_s3_backups:
		return
	if not settings.enable_aws or not settings.enable_s3:
		return
	if settings.s3_backup_frequency != freq:
		return

	_enqueue_backup()
