app_name = "aws_integration"
app_title = "AWS Integration"
app_publisher = "Hybrowlabs Technologies"
app_description = "AWS Integration for frappe"
app_email = "support@hybrowlabs.com"
app_license = "apache-2.0"

# Includes in <head>
# ------------------

# include js in doctype views
doctype_js = {
	"File": "public/js/file.js"
}

# DocType Class
# ---------------

override_doctype_class = {
	"File": "aws_integration.s3.overrides.S3File"
}

# Document Events
# ---------------

doc_events = {
	"File": {
		"onload": "aws_integration.s3.form.add_s3_form_context",
		"after_insert": "aws_integration.s3.handlers.on_file_upload",
		"on_trash": "aws_integration.s3.handlers.on_file_delete"
	}
}

# Scheduled Tasks
# ---------------

scheduler_events = {
	"all": [
		"aws_integration.utils.email.flush_email_queue"
	],
	"hourly": [
		"aws_integration.s3.scheduler.upload_pending_files"
	],
	"daily": [
		"aws_integration.s3.backup.take_backups_daily",
		"aws_integration.s3.backup.rotate_old_backups_daily"
	],
	"weekly_long": [
		"aws_integration.s3.backup.take_backups_weekly"
	],
	"monthly_long": [
		"aws_integration.s3.backup.take_backups_monthly"
	],
}

# Log Clearing
# ---------------

default_log_clearing_doctypes = {
	"S3 Backup Log": 90,
}

# Migrations
# ---------------

after_migrate = [
	"aws_integration.s3.setup.after_migrate"
]
