# Copyright (c) 2024, Hybrowlabs Technologies and contributors
# For license information, please see license.txt
import boto3
import frappe
from frappe import _
from frappe.model.document import Document
from aws_integration.utils import validate_email


class AWSSettings(Document):

    def before_save(self):
        self.handle_email_flush()
        self.handle_backup_scheduler()
        self._warn_on_s3_disable()

    def _warn_on_s3_disable(self):
        """Warn the user when S3 is being disabled while files still exist on S3.

        Uses get_doc_before_save() to detect a transition from enabled to
        disabled. Emits an orange msgprint so the user can make an informed
        decision — it does NOT block the save.
        """
        old = self.get_doc_before_save()
        if old and old.enable_s3 and not self.enable_s3:
            count = frappe.db.count("File", {"is_on_s3": 1})
            if count:
                frappe.msgprint(
                    _("{0} files are stored on S3. Disabling S3 will make them inaccessible until S3 is re-enabled.").format(count),
                    indicator="orange",
                    title=_("Warning"),
                )

    def get_s3_credentials(self):
        """Return (access_key, secret, region, endpoint_url) for S3."""
        return (
            self.s3_access_key_id,
            self.get_password("s3_secret_access_key"),
            self.s3_region or self.region,
            self.s3_endpoint_url,
        )

    def get_ses_client(self):
        aws_secret_access_key = self.get_password("aws_secret_access_key")
        return boto3.client(
            "sesv2",
            region_name=self.region,
            aws_access_key_id=self.aws_access_key_id,
            aws_secret_access_key=aws_secret_access_key,
        )

    def validate(self):
        if self.enable_s3:
            if not self.enable_aws:
                frappe.throw(_("AWS must be enabled to use S3"))
            if not self.s3_access_key_id:
                frappe.throw(_("S3 Access Key ID is required when S3 is enabled"))
            if not self.s3_secret_access_key:
                frappe.throw(_("S3 Secret Access Key is required when S3 is enabled"))
            if not self.s3_bucket_name:
                frappe.throw(_("S3 Bucket Name is required when S3 is enabled"))
            if self.s3_endpoint_url:
                if not self.s3_endpoint_url.startswith(("https://", "http://")):
                    self.s3_endpoint_url = f"https://{self.s3_endpoint_url}"

            if self.s3_presigned_url_expiry and (
                self.s3_presigned_url_expiry < 1 or self.s3_presigned_url_expiry > 604800
            ):
                frappe.throw(
                    _("Presigned URL Expiry must be between 1 and 604800 seconds (7 days)")
                )

        if self.enable_bulk_ses_email:
            if not self.enable_aws:
                frappe.throw(_("AWS must be enabled to use Bulk SES Email"))
            if not self.aws_access_key_id:
                frappe.throw(_("AWS Access Key ID is required for SES Email"))
            if not self.aws_secret_access_key:
                frappe.throw(_("AWS Secret Access Key is required for SES Email"))
            if not self.source_email:
                frappe.throw(_("Source Email is required when Bulk SES Email is enabled"))

        if self.enable_s3_backups:
            if not self.enable_aws:
                frappe.throw(_("AWS must be enabled to use S3 Backups"))
            if not self.enable_s3:
                frappe.throw(_("S3 must be enabled to use S3 Backups"))
            if self.s3_backup_notify_email and not validate_email(self.s3_backup_notify_email):
                frappe.throw(_("Please enter a valid backup notification email address"))
            if self.s3_backup_retention_count and self.s3_backup_retention_count < 0:
                frappe.throw(_("Keep Last N Backups must be 0 or greater"))
            if self.s3_backup_retention_days and self.s3_backup_retention_days < 0:
                frappe.throw(_("Delete Backups Older Than (days) must be 0 or greater"))

        if self.source_email and not validate_email(self.source_email):
            frappe.throw("Please enter valid email address")

    def send_email(
        self,
        destinations,
        subject,
        content=None,
        html=None,
        reply_tos=None,
    ):
        """
        Sends emails in batches with a rate limit of 25 per second.

        :param destinations: List of destinations (objects with `to_service_format` method).
        :param subject: Email subject.
        :param text: Plain text body (optional).
        :param html: HTML body (optional).
        :param reply_tos: List of reply-to addresses (optional).
        :param batch_size: Number of recipients per batch (default: 25).
        :return: List of message IDs or None for failed attempts.
        """
        if not self.enable_bulk_ses_email:
            frappe.throw(_("Bulk SES Email is not enabled in AWS Settings"))
        self.source = f"{self.sender_name} <{self.source_email}>"
        self.ses_client = self.get_ses_client()
        send_args = {
            "FromEmailAddress": self.source,
            "Destination": destinations.to_service_format(),
            "Content": {
                "Simple": {
                    "Subject": {"Data": subject, "Charset": "UTF-8"},
                    "Body": {},
                }
            },
        }

        if content:
            send_args["Content"]["Simple"]["Body"]["Text"] = {
                "Data": content,
                "Charset": "UTF-8",
            }

        if html:
            send_args["Content"]["Simple"]["Body"]["Html"] = {
                "Data": html,
                "Charset": "UTF-8",
            }

        if reply_tos:
            send_args["ReplyToAddresses"] = reply_tos

        try:
            response = self.ses_client.send_email(**send_args)
            message_id = response.get("MessageId")
            if not message_id:
                frappe.throw(_("Failed to send email. Please try again."))
            self.add_ses_logs(subject, content or html, message_id, destinations)
            return response
        except Exception as e:
            frappe.log_error(
                _("Failed to send email: {error}").format(error=str(e)),
                frappe.get_traceback(),
            )

    def add_ses_logs(self, subject, message, message_id, destinations):
        """Add SES logs after sending email."""
        ses_log = frappe.get_doc(
            {
                "doctype": "AWS SES Logs",
                "message_id": message_id,
                "subject": subject,
                "message": message,
                "status": "Sent",
                "from": self.source_email,
            }
        )
        ses_log.recepients = ", ".join(destinations.tos or [])
        ses_log.cc_recepients = ", ".join(destinations.ccs or [])
        ses_log.bcc_recepients = ", ".join(destinations.bccs or [])
        ses_log.insert(ignore_permissions=True)


    def handle_backup_scheduler(self):
        """Toggle backup scheduled jobs based on enable_s3_backups setting.

        When our S3 backups are enabled:
          - Stop Frappe's built-in S3 backup jobs (avoid duplicate backups)
          - Enable our own backup jobs
        When disabled:
          - Re-enable Frappe's built-in S3 backup jobs
          - Stop our own backup jobs
        """
        methods = [
            ("frappe.integrations.doctype.s3_backup_settings.s3_backup_settings.take_backups_daily", not self.enable_s3_backups),
            ("frappe.integrations.doctype.s3_backup_settings.s3_backup_settings.take_backups_weekly", not self.enable_s3_backups),
            ("frappe.integrations.doctype.s3_backup_settings.s3_backup_settings.take_backups_monthly", not self.enable_s3_backups),
            ("aws_integration.s3.backup.take_backups_daily", self.enable_s3_backups),
            ("aws_integration.s3.backup.take_backups_weekly", self.enable_s3_backups),
            ("aws_integration.s3.backup.take_backups_monthly", self.enable_s3_backups),
            ("aws_integration.s3.backup.rotate_old_backups_daily", self.enable_s3_backups),
        ]
        for method, enable in methods:
            self._toggle_scheduled_job(method, enable)

    def handle_email_flush(self):
        methods = [
            ("frappe.email.queue.flush", not self.enable_aws),
            ("aws_integration.utils.email.flush_email_queue", self.enable_aws),
        ]
        for method, enable in methods:
            self._toggle_scheduled_job(method, enable)

    def test_s3_connection(self):
        """Test S3 bucket connectivity."""
        from aws_integration.s3.client import S3Client
        client = S3Client()
        return client.test_connection()

    def _toggle_scheduled_job(self, method_name, enable=True):
        """Enable or disable a Scheduled Job Type by its method path."""
        try:
            job = frappe.get_doc("Scheduled Job Type", {"method": method_name})
            job.stopped = not enable
            job.save()
        except frappe.DoesNotExistError:
            frappe.log_error(
                f"Scheduled Job Type for {method_name} not found. Please toggle it manually.",
                frappe.get_traceback(),
            )