# S3 File Storage Integration

Offload Frappe/ERPNext file attachments to Amazon S3, reducing local disk usage while keeping seamless file access via presigned URLs.

## Features

- **Automatic S3 Upload** - Hourly scheduler uploads local files to S3 in configurable batches
- **Instant Upload** - Optional immediate upload on file attachment (via background job)
- **Presigned URL Access** - Files served through time-limited S3 presigned URLs (default: 15 min)
- **Folder Mirroring** - S3 keys mirror Frappe's folder structure (`Home/` prefix stripped)
- **DocType Exemption** - Exclude specific DocTypes from S3 upload (e.g., Item, Product)
- **Configurable Deletion** - Control whether deleting a file from ERP also deletes it from S3
- **Bulk Migration** - One-click migration of all existing local files to S3
- **Local Cleanup Tracking** - `local_deleted` field on File tracks whether the local copy was removed
- **Permission-Gated** - Private files require authentication and document-level permissions
- **Multipart Upload** - Files larger than 5 MB use S3 multipart upload
- **S3-Compatible Providers** - Works with Backblaze B2, Wasabi, MinIO via custom endpoint URL

## Setup

### 1. Enable S3 in AWS Settings

Navigate to **AWS Settings** and:

1. Check **Enable AWS** and provide your AWS credentials (Access Key, Secret Key, Region)
2. Check **Enable S3 File Storage**
3. Go to the **S3 Storage** tab and fill in:
   - **S3 Bucket Name** (required)
   - **S3 Bucket Region** (optional, falls back to general region)
   - **S3 Endpoint URL** (optional, for S3-compatible providers)
   - **S3 Folder Prefix** (optional, defaults to site name)
4. Click **S3 Files > Test S3 Connection** to verify

### 2. Configure Upload Behavior

In the **Upload Settings** section:

| Setting | Default | Description |
|---------|---------|-------------|
| Instant Upload to S3 | Off | Upload immediately when file is attached |
| Delete Local File After S3 Upload | On | Remove local copy after confirmed upload |
| Delete S3 File on ERP Deletion | On | Delete S3 object when File is trashed |
| Batch Size | 50 | Files processed per scheduler run, migration, and cleanup |
| Presigned URL Expiry | 900s | How long generated URLs remain valid |

### 3. Exempt DocTypes (Optional)

In the **Exempt DocTypes** section, add any DocTypes whose attachments should remain local (e.g., Item, Product).

### 4. Run Migration

```bash
bench --site <site> migrate
bench build --app aws_integration
bench --site <site> clear-cache
```

The `migrate` command creates custom fields on the File DocType: `s3_key`, `is_on_s3`, `s3_uploaded_at`, and `local_deleted`.

### 5. Migrate Existing Files

Click **S3 Files > Migrate All Files to S3** in AWS Settings to upload all existing local files. This runs as a background job with progress updates.

### 6. Clean Up Local Files

Click **S3 Files > Clean Up Local Files** to delete local copies of files already uploaded to S3. Each cleaned file gets `local_deleted=1`. Files that are already missing on disk are also marked as `local_deleted` to avoid re-checking.

## Custom Fields on File DocType

Added via `after_migrate` (not fixtures) in `aws_integration.s3.setup.after_migrate`:

| Field | Type | Label | Description |
|-------|------|-------|-------------|
| `s3_key` | Small Text (Read Only) | S3 Key | The object key in S3 |
| `is_on_s3` | Check (Read Only) | Uploaded to S3 | Whether the file has been uploaded to S3 |
| `s3_uploaded_at` | Datetime (Read Only) | S3 Upload Date | When the file was uploaded to S3 |
| `local_deleted` | Check (Read Only) | Local File Deleted | Whether the local copy was removed after upload |

All fields are in a collapsible **S3 Info** section on the File form.

## How It Works

### File Upload

Files are uploaded to S3 via two mechanisms:

1. **Instant** (if enabled): `File.after_insert` enqueues a background job on the `short` queue
2. **Scheduled**: Hourly scheduler processes remaining local files in batches

After upload, the File document's `file_url` is rewritten to:
```
/api/method/aws_integration.api.s3.generate_file?key={s3_key}&file_name={display_name}
```

When `delete_local_after_upload` is enabled, the local file is removed and `local_deleted` is set to `1`.

### File Access

When a browser requests the file URL:
1. The `generate_file` API looks up the File document by `s3_key`
2. For private files, it checks permissions on the attached document
3. Generates a presigned S3 URL (default: 15 min expiry)
4. Returns an HTTP 302 redirect to the presigned URL
5. Browser loads the file directly from S3

Switching S3 off stops new uploads, migration and cleanup. It does not stop a System Manager opening a file that is already on S3 from its File form: `get_file_preview` builds its client without the enabled check for System Managers only (`S3Client(require_enabled=False)`), and everyone else is refused while S3 is off exactly as before. The `generate_file` link used inside documents still needs S3 to be enabled.

### Local Cleanup

The `cleanup_local_s3_files` function:
- Queries files where `is_on_s3=1` AND `local_deleted=0` (skips already-cleaned files)
- Validates file paths to prevent path traversal
- Removes local copies and sets `local_deleted=1`
- Marks already-missing files as `local_deleted=1` so they aren't re-checked
- Commits after each batch to prevent data loss on crash

### S3 Key Structure

```
{prefix}/{private|public}/{folder}/{hash}_{filename}
```

Examples:
```
unity/public/Baby Walnut Wakad-2026-2027/ZHUENX2M_photo.jpg
unity/private/Attachments/A3BKX9QP_salary_slip.pdf
unity/public/X7KMRT4P_logo.png
```

### Concurrency Safety

All upload paths (instant, scheduled, bulk migration) use `SELECT ... FOR UPDATE` row-level locks on the File table to prevent duplicate uploads when multiple workers process the same file concurrently. The S3 metadata is committed before local file deletion to prevent data loss.

## Frontend

### File Form (`public/js/file.js`)

- Green **"Stored on S3"** indicator pill when `is_on_s3=1`
- **"Stored on S3 (local deleted)"** indicator when both `is_on_s3=1` and `local_deleted=1`
- **"Upload to S3"** button for local files not yet on S3 (with confirmation dialog)
- **"Delete Local File"** button when `is_on_s3=1` and `local_deleted=0` (System Manager only, with confirmation dialog)
- Auto-refreshes when background S3 upload completes (via realtime events)

#### When S3 is disabled

With **Enable S3 File Storage** (or **Enable AWS**) off, the File form says nothing about S3 to anyone: no S3 fields, no indicator, no buttons. The one exception is a System Manager opening a file that is already on S3 while the bucket can be reached; that form keeps the S3 fields, the indicator and the buttons.

The server (`aws_integration.s3.form.add_s3_form_context`, a File `onload` hook) sends the form `enabled` and, only when S3 is off, only to a System Manager and only for a file that is on S3, `connected`. AWS keeps no connection status, so `connected` is a short `head_bucket` call (3 second timeouts, even with S3 off) whose answer is cached for a minute. Opening a file never waits on S3 for anyone else.

### AWS Settings (`aws_settings.js`)

Under **S3 Files** dropdown:
- **Test S3 Connection** — verify bucket access
- **Migrate All Files to S3** — enqueue bulk migration with progress bar
- **S3 Status** — dashboard showing file counts, sizes, migration progress
- **Clean Up Local Files** — delete local copies with progress bar

## Queue Usage

| Operation | Queue | Default Timeout |
|-----------|-------|-----------------|
| Instant file upload | `short` | 300s |
| Scheduled upload (`upload_pending_files`) | scheduler/default | — |
| Bulk migrate all files | `long` | 3600s |
| Clean up local files | `long` | 3600s |

Instant uploads and scheduled uploads are never blocked by long-running jobs on the `long` queue.

## File Structure

```
aws_integration/
  s3/
    client.py         # S3Client - boto3 wrapper
    scheduler.py      # Hourly upload + bulk migration + local cleanup
    handlers.py       # after_insert + on_trash hooks
    setup.py          # after_migrate: custom fields on File
    backup.py         # Backup logic: take_s3_backup, _run_backup, rotation, scheduler
  api/
    s3.py             # generate_file, test_s3_connection, migrate_files_to_s3, get_file_preview, etc.
  public/js/
    file.js           # File form: S3 indicator + Upload to S3 button
```

## API Reference

| Endpoint | Auth | Description |
|----------|------|-------------|
| `aws_integration.api.s3.generate_file` | Guest (permission-checked) | Presigned URL redirect for file access |
| `aws_integration.api.s3.get_file_preview` | Logged in | Returns presigned URL + metadata |
| `aws_integration.api.s3.test_s3_connection` | System Manager | Test S3 bucket connectivity |
| `aws_integration.api.s3.migrate_files_to_s3` | System Manager | Enqueue bulk migration job |
| `aws_integration.api.s3.cleanup_local_s3_files` | System Manager | Enqueue local file cleanup job |
| `aws_integration.api.s3.upload_single_file_to_s3` | System Manager | Upload a single file to S3 |
| `aws_integration.api.s3.delete_local_file` | System Manager | Delete local copy of a file already on S3 |
| `aws_integration.api.s3.get_s3_status` | System Manager | File counts and migration statistics |

---

# S3 Backups

Automated site backups (database, site config, public/private files) uploaded directly to S3 with scheduling, progress tracking, email notifications, and retention-based rotation.

## Features

- **On-Demand Backup** — "Take Backup Now" button in AWS Settings
- **Upload Existing Backups** — "Upload Local Backups" scans and uploads pre-existing local backup files
- **Scheduled Backups** — Daily, Weekly, or Monthly via Frappe scheduler
- **Realtime Progress** — Status pipeline in AWS Settings (Queued → Generating → Uploading → Done)
- **Backup Log** — `S3 Backup Log` DocType tracks every run with status, S3 keys, file sizes, and errors
- **Download from S3** — Presigned download URLs via download icons on backup log fields
- **Local Cleanup** — Local backup files deleted after upload; manual "Delete Local Files" button on log
- **Retention Policies** — Keep last N backups and/or delete backups older than N days from S3
- **Email Notifications** — Configurable email alerts on success and/or failure
- **Auto-Retry** — Failed backups retry up to 2 times automatically
- **Configurable Timeout** — `s3_backup_timeout` setting for large sites (default: 6000s / 100 min)
- **Auto Log Clearing** — Old log records purged after 90 days via Frappe's Log Settings

## Setup

### 1. Enable S3 Backups

In **AWS Settings**, ensure AWS and S3 are enabled, then:

1. Check **Enable S3 Backups**
2. Go to the **S3 Backups** tab

### 2. Configure Backup Settings

#### Backup Configuration

| Setting | Default | Description |
|---------|---------|-------------|
| Backup Frequency | Daily | `Daily`, `Weekly`, `Monthly`, or `None` (manual only) |
| Backup Files | On | Include public and private file tarballs in backup |
| Backup Job Timeout (seconds) | 6000 | Max time for the background job. Increase for large sites |

#### Backup Storage

| Setting | Default | Description |
|---------|---------|-------------|
| Backup Bucket Name | *(main bucket)* | Separate bucket for backups. Leave blank to use the main S3 bucket |
| Backup Folder Path | *(site prefix)* | S3 key prefix for backup files |

#### Retention

| Setting | Default | Description |
|---------|---------|-------------|
| Keep Last N Backups | 0 (disabled) | Keep only the N most recent successful backups on S3, delete older ones |
| Delete Backups Older Than (days) | 0 (disabled) | Delete backups older than N days from S3 |

Either or both retention policies can be active. Set to `0` to disable. Retention runs after every successful backup.

#### Notifications

| Setting | Default | Description |
|---------|---------|-------------|
| Notify Email | *(required)* | Email address to receive backup notifications |
| Send Email for Successful Backup | Off | Send notification even on success (failures always notify) |

### 3. Run Migration

```bash
bench --site <site> migrate
bench --site <site> clear-cache
```

This creates the `S3 Backup Log` DocType table.

## How It Works

### Backup Flow

1. **Trigger** — User clicks "Take Backup Now" or scheduler fires
2. **Queue** — An `S3 Backup Log` entry is created (status: `Queued`), background job enqueued on the `long` queue
3. **Generate** — Frappe's `new_backup()` creates the database dump (`.sql.gz`), site config (`.json`), and optionally file archives (`.tar`)
4. **Upload** — Each file is uploaded to S3 at `{folder}/{type}.{ext}`
5. **Record** — Log entry updated with S3 keys, file sizes, status `Success`
6. **Cleanup** — Local backup files deleted if `delete_local_after_upload` is enabled; `local_cleaned` set on log
7. **Rotate** — Old backups deleted from S3 based on retention settings
8. **Notify** — Email notification sent if configured

### Retry Behavior

If the backup or upload fails, the job retries up to 2 times automatically. After exhausting retries, the log is marked `Failed` with the traceback, and a failure notification is sent.

### Retention / Rotation

Runs after every successful backup. Two independent policies:

- **Count-based**: Queries all `Success` logs ordered by creation, deletes everything beyond position N
- **Age-based**: Queries `Success` logs where `completed_at` is older than the cutoff date

For each old log, all S3 objects (db, config, files, private files) are deleted via `delete_objects`, then the log record is removed. The current backup is always excluded from rotation.

### Queue Usage

| Operation | Queue | Timeout |
|-----------|-------|---------|
| Take backup / Upload existing | `long` | Configurable (`s3_backup_timeout`, default 6000s) |
| Retry on failure | `long` | Same configurable timeout |

Backup jobs on the `long` queue do **not** block instant file uploads (`short` queue) or scheduled file uploads (scheduler/default). Other `long` queue jobs (bulk migration, cleanup) will queue behind a running backup.

### Scheduler Events

| Event | Function | Fires |
|-------|----------|-------|
| `daily` | `take_backups_daily` | Every day |
| `weekly_long` | `take_backups_weekly` | Every week |
| `monthly_long` | `take_backups_monthly` | Every month |

Each checks `enable_s3_backups` and `s3_backup_frequency` before proceeding.

## S3 Backup Log

One record per backup run. Read-only from the UI (Administrator can delete). Deleting a log also removes the associated S3 objects via `on_trash`.

| Field | Type | Description |
|-------|------|-------------|
| Status | Select | `Queued` → `Generating` → `Uploading` → `Success` / `Failed` |
| Started At | Datetime | When the background job started |
| Completed At | Datetime | When the job finished (success or failure) |
| Database File | Data | S3 key of the `.sql.gz` file |
| Site Config File | Data | S3 key of the `.json` file |
| Files Archive | Data | S3 key of the public files `.tar` |
| Private Files Archive | Data | S3 key of the private files `.tar` |
| Database Size | Int | Size in bytes |
| Files Size | Int | Combined public + private archive size in bytes |
| Total Size | Int | Total bytes uploaded |
| S3 Bucket | Data | Bucket used for this backup |
| S3 Folder | Data | Folder prefix used |
| Local Files Cleaned | Check | Whether local files were deleted after upload |
| Local Backup Paths | Small Text (hidden) | JSON of local file paths for manual cleanup |
| Error | Long Text | Traceback on failure |

Naming: `BKUP-YYYYMMDD-HHMMSS` (with hex suffix on collision)

Status colors: Queued=Blue, Generating=Yellow, Uploading=Orange, Success=Green, Failed=Red

### Backup Log UI

- **Status pipeline** — Visual stage indicator (Queued → Generating → Uploading → Done) with animated pulse on active stage
- **Download icons** — Click the download icon on any file field to get a presigned S3 download URL
- **Size formatting** — File sizes displayed in human-readable format (KB, MB, GB)
- **Delete Local Files** button — Visible when `status=Success`, `s3_bucket` is set, and `local_cleaned=0`; deletes local backup files with confirmation dialog
- **On-trash S3 cleanup** — Deleting a backup log also deletes the corresponding S3 objects

## File Structure

```
aws_integration/
  s3/
    backup.py          # Backup logic: take_s3_backup, _run_backup, rotation, scheduler
    ...
  aws_integration/
    doctype/
      s3_backup_log/
        s3_backup_log.json   # DocType definition
        s3_backup_log.py     # Controller with autoname, on_trash S3 cleanup, clear_old_logs
        s3_backup_log.js     # Download icons, size formatting, delete local button
```

## API Reference

| Endpoint | Auth | Description |
|----------|------|-------------|
| `aws_integration.s3.backup.take_s3_backup` | System Manager | Queue a backup job, returns `{log_name}` |
| `aws_integration.s3.backup.get_backup_download_url` | System Manager | Presigned download URL for a backup file |
| `aws_integration.s3.backup.cleanup_backup_local_files` | System Manager | Delete local files for a specific backup log |
| `aws_integration.s3.backup.upload_local_backups` | System Manager | Scan and upload pre-existing local backups |

## Permissions

- **S3 Backup Log**: Administrator has read + delete. No other role has access.
- **Whitelisted APIs**: Restricted to System Manager via `frappe.only_for`
- Scheduler uses an internal `_enqueue_backup()` that bypasses the role guard

## Requirements

- `boto3` (already included in app dependencies)
- AWS IAM credentials with S3 permissions: `s3:PutObject`, `s3:GetObject`, `s3:DeleteObject`, `s3:HeadBucket`, `s3:HeadObject`
- S3 bucket with **no public access** (all access via presigned URLs)
