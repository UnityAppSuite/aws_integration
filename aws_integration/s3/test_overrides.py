from unittest.mock import patch

import frappe
from frappe.core.doctype.file.file import File as CoreFile
from frappe.tests.utils import FrappeTestCase

from aws_integration.s3 import S3_API_PREFIX, overrides


class TestFileOverrideChain(FrappeTestCase):
    """Frappe uses only the last override_doctype_class["File"] entry, so every app that
    overrides File has to build on the entry registered before it. One that extends core File
    directly silently drops the guards of every app registered earlier."""

    OWN = overrides.OWN_PATH
    OTHER = "drive_bucket.overrides.file.File"

    def base_when_registered(self, entries):
        # get_hooks("override_doctype_class") is the dict for that one hook key.
        with patch.object(frappe, "get_hooks", return_value={"File": entries}):
            return overrides._base_class()

    def test_alone_it_builds_on_core(self):
        self.assertIs(self.base_when_registered([self.OWN]), CoreFile)

    def test_registered_after_another_app_it_builds_on_that_app(self):
        with patch.object(frappe, "get_attr", side_effect=lambda path: path) as get_attr:
            base = self.base_when_registered([self.OTHER, self.OWN])

        self.assertEqual(base, self.OTHER)
        get_attr.assert_called_once_with(self.OTHER)

    def test_registered_before_another_app_it_does_not_build_on_it(self):
        # The other app comes last, so Frappe uses ITS class. Building on it here would make
        # the two classes each other's base.
        self.assertIs(self.base_when_registered([self.OWN, self.OTHER]), CoreFile)

    def test_with_three_apps_it_builds_on_the_one_right_before_it(self):
        with patch.object(frappe, "get_attr", side_effect=lambda path: path):
            base = self.base_when_registered(["a.First", "b.Second", self.OWN, "c.Last"])

        self.assertEqual(base, "b.Second")


class TestS3FileKeepsItsOwnBehaviour(FrappeTestCase):
    def file(self, file_url):
        return frappe.get_doc({"doctype": "File", "file_name": "a.pdf", "file_url": file_url})

    def test_an_s3_url_skips_the_disk_checks(self):
        doc = self.file(f"{S3_API_PREFIX}?key=k&file_name=a.pdf")

        self.assertIsInstance(doc, overrides.S3FileMixin)
        self.assertTrue(doc.exists_on_disk())
        self.assertIsNone(doc.validate_file_path())
        self.assertIsNone(doc.validate_file_url())
        self.assertIsNone(doc.validate_file_on_disk())

    def test_a_local_url_still_goes_through_core(self):
        doc = self.file("/files/does-not-exist.pdf")

        self.assertFalse(doc.exists_on_disk())
