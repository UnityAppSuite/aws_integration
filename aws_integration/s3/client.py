import mimetypes
import os

import boto3
import frappe
from botocore.exceptions import ClientError
from frappe import _


class S3Client:
    """Wrapper around boto3 S3 client using AWS Settings credentials."""

    def __init__(self):
        self.settings = frappe.get_cached_doc("AWS Settings")
        if not self.settings.enable_aws or not self.settings.enable_s3:
            frappe.throw(_("S3 is not enabled in AWS Settings"))

        access_key, secret, region, endpoint_url = self.settings.get_s3_credentials()
        client_kwargs = {
            "region_name": region,
            "aws_access_key_id": access_key,
            "aws_secret_access_key": secret,
        }
        if endpoint_url:
            client_kwargs["endpoint_url"] = endpoint_url

        self.client = boto3.client("s3", **client_kwargs)
        self.bucket = self.settings.s3_bucket_name
        self.prefix = self.settings.s3_folder_prefix or frappe.local.site

    def get_s3_key(self, file_doc):
        """Generate S3 key mirroring Frappe's folder structure.

        Uses the plain filename by default. Only appends a content_hash
        prefix when another file with the same name but different content
        already exists in S3 (collision).

        Strips the 'Home/' prefix from the folder since it's just Frappe's root.
        Files in 'Home' go directly under {visibility}/.

        Format: {private|public}/{folder}/{file_name}
        On collision: {private|public}/{folder}/{hash}_{file_name}

        Args:
            file_doc: Frappe File document instance

        Returns:
            str: S3 key path without prefix
        """
        visibility = "private" if file_doc.is_private else "public"

        folder_path = file_doc.folder or "Home"
        # Strip the "Home/" prefix; if folder is exactly "Home", use no subfolder
        if folder_path == "Home":
            folder_path = ""
        elif folder_path.startswith("Home/"):
            folder_path = folder_path[5:]  # remove "Home/"

        file_name = file_doc.file_name or os.path.basename(file_doc.file_url or "unknown")

        if folder_path:
            key = f"{visibility}/{folder_path}/{file_name}"
        else:
            key = f"{visibility}/{file_name}"

        # Check if another file with the same name, folder, and visibility
        # but different content already exists on S3. If so, prefix with
        # content_hash to avoid overwriting the S3 object.
        existing_hash = frappe.db.get_value(
            "File",
            {
                "file_name": file_doc.file_name,
                "folder": file_doc.folder or "Home",
                "is_private": file_doc.is_private,
                "is_on_s3": 1,
                "name": ["!=", file_doc.name],
            },
            "content_hash",
        )
        if existing_hash and existing_hash != file_doc.content_hash:
            hash_prefix = (file_doc.content_hash or frappe.generate_hash(file_doc.name, 6))[:6]
            if folder_path:
                key = f"{visibility}/{folder_path}/{hash_prefix}_{file_name}"
            else:
                key = f"{visibility}/{hash_prefix}_{file_name}"

        return key

    def get_full_s3_key(self, key):
        """Get full S3 key with prefix prepended.

        Args:
            key (str): Relative S3 key

        Returns:
            str: Full S3 key including prefix
        """
        return f"{self.prefix}/{key}" if self.prefix else key

    def upload_file(self, file_doc):
        """Upload a file to S3.

        Uses multipart upload for files larger than 5 MB. Returns the S3 key
        (without prefix) on success so it can be stored on the File document.

        Args:
            file_doc: Frappe File document instance

        Returns:
            str: S3 key (without prefix) for the uploaded file

        Raises:
            frappe.ValidationError: When the local file is not found on disk
        """
        file_path = file_doc.get_full_path()
        if not os.path.exists(file_path):
            frappe.throw(_("Local file not found: {0}").format(file_path))

        key = self.get_s3_key(file_doc)
        full_key = self.get_full_s3_key(key)

        content_type = mimetypes.guess_type(file_path)[0] or "application/octet-stream"

        extra_args = {
            "ContentType": content_type,
        }

        file_size = os.path.getsize(file_path)

        # Use multipart upload for files > 5 MB
        if file_size > 5 * 1024 * 1024:
            config = boto3.s3.transfer.TransferConfig(
                multipart_threshold=5 * 1024 * 1024,
                multipart_chunksize=5 * 1024 * 1024,
            )
            self.client.upload_file(
                file_path, self.bucket, full_key,
                ExtraArgs=extra_args, Config=config,
            )
        else:
            self.client.upload_file(
                file_path, self.bucket, full_key,
                ExtraArgs=extra_args,
            )

        return key

    def generate_presigned_url(self, key, expiry=None, file_name=None):
        """Generate a presigned URL for temporary file access.

        Args:
            key (str): S3 key (without prefix)
            expiry (int, optional): URL validity in seconds. Defaults to
                s3_presigned_url_expiry from AWS Settings (900 if not set).
            file_name (str, optional): When provided, sets
                ResponseContentDisposition so the browser shows the file
                inline with this name.

        Returns:
            str: Presigned URL for the S3 object
        """
        if expiry is None:
            expiry = self.settings.s3_presigned_url_expiry or 900

        full_key = self.get_full_s3_key(key)

        params = {
            "Bucket": self.bucket,
            "Key": full_key,
        }

        if file_name:
            from urllib.parse import quote

            ascii_name = file_name.encode("ascii", "replace").decode().replace('"', "'")
            encoded_name = quote(file_name, safe="")
            params["ResponseContentDisposition"] = (
                f"inline; filename=\"{ascii_name}\"; filename*=UTF-8''{encoded_name}"
            )

        return self.client.generate_presigned_url(
            "get_object",
            Params=params,
            ExpiresIn=expiry,
        )

    def download_file(self, key):
        """Download file content from S3.

        Args:
            key (str): S3 key (without prefix)

        Returns:
            bytes: Raw file content
        """
        full_key = self.get_full_s3_key(key)
        response = self.client.get_object(Bucket=self.bucket, Key=full_key)
        return response["Body"].read()

    def delete_file(self, key):
        """Delete a file from S3.

        Args:
            key (str): S3 key (without prefix)
        """
        full_key = self.get_full_s3_key(key)
        self.client.delete_object(Bucket=self.bucket, Key=full_key)

    def test_connection(self):
        """Test S3 connectivity by performing a head_bucket call.

        Returns:
            dict: {
                "success": True/False,
                "message": str
            }
        """
        try:
            self.client.head_bucket(Bucket=self.bucket)
            return {
                "success": True,
                "message": _("Successfully connected to S3 bucket: {0}").format(self.bucket),
            }
        except ClientError as e:
            error_code = e.response["Error"]["Code"]
            if error_code == "404":
                return {
                    "success": False,
                    "message": _("Bucket '{0}' does not exist").format(self.bucket),
                }
            elif error_code == "403":
                return {
                    "success": False,
                    "message": _("Access denied to bucket '{0}'. Check your credentials.").format(
                        self.bucket
                    ),
                }
            else:
                return {
                    "success": False,
                    "message": _("Error connecting to S3: {0}").format(str(e)),
                }
        except Exception as e:
            return {
                "success": False,
                "message": _("Error connecting to S3: {0}").format(str(e)),
            }

    def file_exists(self, key):
        """Check whether a file exists in S3.

        Args:
            key (str): S3 key (without prefix)

        Returns:
            bool: True if the object exists, False otherwise
        """
        full_key = self.get_full_s3_key(key)
        try:
            self.client.head_object(Bucket=self.bucket, Key=full_key)
            return True
        except ClientError:
            return False
