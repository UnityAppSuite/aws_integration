from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase

from aws_integration.api import s3 as s3_api
from aws_integration.s3 import client as client_module
from aws_integration.s3 import form


class Settings(frappe._dict):
    """AWS Settings as the code reads it, without touching the real single."""

    def get_password(self, fieldname):
        return "secret"


def settings(enable_aws=1, enable_s3=1, **values):
    fields = dict(
        aws_access_key_id="key",
        s3_bucket_name="bucket",
        s3_bucket_region="us-east-1",
        s3_endpoint_url=None,
        s3_folder_prefix="site",
        s3_presigned_url_expiry=900,
    )
    fields.update(values)
    return Settings(enable_aws=enable_aws, enable_s3=enable_s3, **fields)


def make_user(label, roles):
    email = f"{label}-{frappe.generate_hash(length=6)}@example.com"
    frappe.get_doc(
        {
            "doctype": "User",
            "email": email,
            "first_name": label,
            "send_welcome_email": 0,
            "roles": [{"role": role} for role in roles],
        }
    ).insert(ignore_permissions=True)
    return email


class S3TestCase(FrappeTestCase):
    def setUp(self):
        commit = patch.object(frappe.db, "commit")
        commit.start()
        self.addCleanup(commit.stop)
        self.addCleanup(frappe.db.rollback)
        self.addCleanup(frappe.set_user, "Administrator")
        frappe.cache.delete_value(form.CONNECTION_CACHE_KEY)
        self.addCleanup(frappe.cache.delete_value, form.CONNECTION_CACHE_KEY)

    def use_settings(self, **kwargs):
        """Make AWS Settings read as `settings(**kwargs)`; every other document is untouched."""
        real = frappe.get_cached_doc
        fake = settings(**kwargs)

        def get_cached_doc(doctype, *args, **kw):
            return fake if doctype == "AWS Settings" else real(doctype, *args, **kw)

        patcher = patch.object(frappe, "get_cached_doc", side_effect=get_cached_doc)
        patcher.start()
        self.addCleanup(patcher.stop)
        return fake

    def make_file(self, *, on_s3=True, skipped=False, private=0):
        name = f"s3test-{frappe.generate_hash(length=8)}"
        doc = frappe.get_doc(
            {
                "doctype": "File",
                "name": name,
                "file_name": f"{name}.txt",
                "file_url": f"/files/{name}.txt",
                "is_private": private,
                "is_folder": 0,
                "content_hash": frappe.generate_hash(length=12),
                "is_on_s3": 1 if on_s3 else 0,
                "s3_key": f"keys/{name}.txt" if on_s3 else "",
                "s3_upload_skipped": 1 if skipped else 0,
            }
        )
        doc.db_insert()
        return doc

    def context(self, doc, user="Administrator"):
        frappe.set_user(user)
        loaded = frappe.get_doc("File", doc.name)
        loaded.set("__onload", frappe._dict())
        loaded.run_method("onload")
        frappe.set_user("Administrator")
        return loaded.get("__onload").get("s3")


class TestIsS3Enabled(S3TestCase):
    def test_s3_needs_both_aws_and_s3_switched_on(self):
        for aws, s3, expected in ((1, 1, True), (1, 0, False), (0, 1, False), (0, 0, False)):
            with self.subTest(enable_aws=aws, enable_s3=s3):
                self.use_settings(enable_aws=aws, enable_s3=s3)
                self.assertEqual(form.is_s3_enabled(), expected)


class TestFormContext(S3TestCase):
    def setUp(self):
        super().setUp()
        self.manager = make_user("manager", ["System Manager"])
        self.plain = make_user("plain", ["Blogger"])

    def test_the_form_is_told_whether_s3_is_switched_on_and_where_the_file_stands(self):
        for on_s3, skipped, state in ((True, False, "stored"), (False, True, "skipped"), (False, False, "pending")):
            with self.subTest(state=state):
                doc = self.make_file(on_s3=on_s3, skipped=skipped)
                self.use_settings(enable_s3=1)
                self.assertEqual(self.context(doc, self.plain), {"state": state, "enabled": True})

                self.use_settings(enable_s3=0)
                with patch.object(form, "s3_connection_works", return_value=True):
                    self.assertFalse(self.context(doc, self.plain)["enabled"])

    def test_connection_is_only_checked_for_a_system_manager_on_a_stored_file_while_s3_is_off(self):
        stored = self.make_file()
        pending = self.make_file(on_s3=False)
        cases = (
            ("manager, stored, off", stored, self.manager, 0, True),
            ("manager, stored, on", stored, self.manager, 1, False),
            ("manager, pending, off", pending, self.manager, 0, False),
            ("plain user, stored, off", stored, self.plain, 0, False),
        )
        for label, doc, user, enable_s3, expected_check in cases:
            with self.subTest(label):
                self.use_settings(enable_s3=enable_s3)
                with patch.object(form, "s3_connection_works", return_value=True) as check:
                    context = self.context(doc, user)

                self.assertEqual(check.called, expected_check)
                self.assertEqual("connected" in context, expected_check)

    def test_the_answer_reaches_the_form(self):
        doc = self.make_file()
        self.use_settings(enable_s3=0)

        for works in (True, False):
            with self.subTest(works=works), patch.object(form, "s3_connection_works", return_value=works):
                self.assertEqual(self.context(doc, self.manager)["connected"], works)

    def test_folders_get_no_context(self):
        folder = frappe.get_doc({"doctype": "File", "name": "s3test-folder", "file_name": "folder", "is_folder": 1})
        folder.db_insert()

        self.assertIsNone(self.context(folder))


class TestConnectionCheck(S3TestCase):
    def check(self, *, success=True, **kwargs):
        self.use_settings(enable_aws=0, enable_s3=0, **kwargs)
        s3 = MagicMock()
        s3.head_bucket.return_value = {} if success else None
        if not success:
            from botocore.exceptions import ClientError

            s3.head_bucket.side_effect = ClientError({"Error": {"Code": "403"}}, "HeadBucket")
        patcher = patch.object(client_module.boto3, "client", return_value=s3)
        self.boto = patcher.start()
        self.addCleanup(patcher.stop)
        return form.s3_connection_works(), s3

    def test_it_asks_the_bucket_even_though_s3_is_switched_off(self):
        works, s3 = self.check(success=True)

        self.assertTrue(works)
        s3.head_bucket.assert_called_once_with(Bucket="bucket")

    def test_a_bucket_that_refuses_is_not_connected(self):
        works, _ = self.check(success=False)

        self.assertFalse(works)

    def test_it_never_waits_long_for_an_unreachable_bucket(self):
        self.check(success=True)

        config = self.boto.call_args.kwargs["config"]
        self.assertLessEqual(config.connect_timeout, 5)
        self.assertLessEqual(config.read_timeout, 5)

    def test_credentials_that_cannot_build_a_client_count_as_not_connected(self):
        self.use_settings(enable_aws=0, enable_s3=0)
        with patch.object(client_module.boto3, "client", side_effect=ValueError("bad region")):
            self.assertFalse(form.s3_connection_works())

    def test_no_bucket_configured_is_not_connected_and_asks_nobody(self):
        self.use_settings(enable_aws=0, enable_s3=0, s3_bucket_name="")
        with patch.object(client_module.boto3, "client") as boto:
            self.assertFalse(form.s3_connection_works())

        boto.return_value.head_bucket.assert_not_called()

    def test_the_answer_is_remembered_so_opening_files_does_not_repeat_the_call(self):
        _, s3 = self.check(success=True)

        form.s3_connection_works()
        form.s3_connection_works()

        s3.head_bucket.assert_called_once()

    def test_a_failure_is_remembered_too(self):
        _, s3 = self.check(success=False)

        self.assertFalse(form.s3_connection_works())

        s3.head_bucket.assert_called_once()


class TestClientEnabledGate(S3TestCase):
    def test_a_client_is_refused_while_s3_is_off_unless_the_caller_opts_out(self):
        self.use_settings(enable_s3=0)
        with patch.object(client_module.boto3, "client"):
            with self.assertRaises(frappe.ValidationError):
                client_module.S3Client()

            self.assertEqual(client_module.S3Client(require_enabled=False).bucket, "bucket")

    def test_an_enabled_site_builds_a_client_as_before(self):
        self.use_settings(enable_s3=1)
        with patch.object(client_module.boto3, "client"):
            self.assertEqual(client_module.S3Client().bucket, "bucket")


class TestPreviewWhileS3IsOff(S3TestCase):
    """Turning S3 off stops new work. A System Manager can still open what is already stored."""

    def setUp(self):
        super().setUp()
        self.doc = self.make_file()
        self.use_settings(enable_s3=0)
        boto = MagicMock()
        boto.generate_presigned_url.return_value = "https://bucket.example/signed"
        patcher = patch.object(client_module.boto3, "client", return_value=boto)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_system_manager_gets_the_link(self):
        frappe.set_user(make_user("manager", ["System Manager"]))

        self.assertEqual(s3_api.get_file_preview(file_name=self.doc.name)["url"], "https://bucket.example/signed")

    def test_everyone_else_is_still_refused_while_s3_is_off(self):
        frappe.set_user(make_user("plain", ["Blogger"]))

        with self.assertRaises(frappe.ValidationError):
            s3_api.get_file_preview(file_name=self.doc.name)

    def test_while_s3_is_on_everyone_who_may_read_the_file_gets_the_link(self):
        self.use_settings(enable_s3=1)
        frappe.set_user(make_user("plain", ["Blogger"]))

        self.assertEqual(s3_api.get_file_preview(file_name=self.doc.name)["url"], "https://bucket.example/signed")
