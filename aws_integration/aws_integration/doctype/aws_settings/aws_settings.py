# Copyright (c) 2024, Hybrowlabs Technologies and contributors
# For license information, please see license.txt
import boto3
import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import validate_email_address


class AWSSettings(Document):

    def before_save(self):
        self.handle_email_flush()
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

    def get_ses_client(self):
        if not hasattr(self, "_ses_client"):
            self._ses_client = boto3.client(
                "sesv2",
                region_name=self.region,
                aws_access_key_id=self.aws_access_key_id,
                aws_secret_access_key=self.get_password("aws_secret_access_key"),
            )
        return self._ses_client

    def validate(self):
        if self.enable_aws:
            if not self.aws_access_key_id:
                frappe.throw(_("AWS Access Key ID is required when AWS is enabled"))
            if not self.aws_secret_access_key:
                frappe.throw(_("AWS Secret Access Key is required when AWS is enabled"))

        if self.enable_s3:
            if not self.enable_aws:
                frappe.throw(_("AWS must be enabled to use S3"))
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

        if self.enable_s3_backups:
            if not self.enable_s3 or not self.enable_aws:
                frappe.throw(_("AWS and S3 must be enabled to use S3 Backups"))
            if self.s3_backup_notify_email and not validate_email_address(self.s3_backup_notify_email):
                frappe.throw(_("Please enter a valid backup notification email address"))
            if self.s3_backup_retention_count and self.s3_backup_retention_count < 0:
                frappe.throw(_("Keep Last N Backups must be 0 or greater"))
            if self.s3_backup_retention_days and self.s3_backup_retention_days < 0:
                frappe.throw(_("Delete Backups Older Than (days) must be 0 or greater"))

        if self.source_email and not validate_email_address(self.source_email):
            frappe.throw(_("Please enter a valid source email address"))

    def _format_sender(self):
        name = (self.sender_name or "").replace("<", "").replace(">", "")
        name = name.replace("\n", "").replace("\r", "").replace("\x00", "")
        name = name.strip()
        if name:
            return f"{name} <{self.source_email}>"
        return self.source_email

    def send_email(
        self,
        destinations,
        subject,
        content=None,
        html=None,
        reply_tos=None,
    ):
        ses_client = self.get_ses_client()
        source = self._format_sender()

        send_args = {
            "FromEmailAddress": source,
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

        # Let SES API errors propagate to caller
        response = ses_client.send_email(**send_args)

        # Log in separate try/except — logging failure must not mask successful send
        try:
            message_id = response.get("MessageId")
            if message_id:
                self.add_ses_logs(subject, content or html, message_id, destinations)
        except Exception:
            frappe.log_error(title="SES Log Error", message=frappe.get_traceback())

        return response

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


    def handle_email_flush(self):
        methods = [
            ("frappe.email.queue.flush", not self.enable_aws),
            ("aws_integration.utils.email.flush_email_queue", self.enable_aws)
        ]
        
        for method, enable in methods:
            self.email_flush_handler(method, enable)
    
    def test_s3_connection(self):
        """Test S3 bucket connectivity."""
        from aws_integration.s3.client import S3Client
        client = S3Client()
        return client.test_connection()

    def email_flush_handler(self, method_name, enable=True):
        """
        Enable or disable the email flush job.
        """
        try:
            job = frappe.get_doc("Scheduled Job Type", {"method": method_name})
            job.stopped = not enable
            job.save()
        except frappe.DoesNotExistError:
            frappe.log_error(
                f"Failed to disable {method_name} job. Please disable it manually.",
                frappe.get_traceback(),
            )