# Copyright (c) 2026, Hybrowlabs Technologies and Contributors
# See license.txt
"""Unit tests for the Email Suppression Entry doctype controller —
email-governance-engine Part 3. Covers validate()'s mandatory-reference
guard (mirrors core Email Unsubscribe), the explicit-is_global dedup
existence query (mirrors core exactly, no DB-level unique index — see the
controller module docstring for why), and get_suppressed_emails()'s
single-query lookup semantics (scoped match OR global)."""

import frappe
from frappe.tests.utils import FrappeTestCase

from aws_integration.aws_integration.doctype.email_suppression_entry.email_suppression_entry import (
    get_suppressed_emails,
)


def _new_entry(**kwargs):
    defaults = {
        "doctype": "Email Suppression Entry",
        "recipient_email": "suppressed@example.com",
        "reason": "Manual",
    }
    defaults.update(kwargs)
    return frappe.get_doc(defaults)


class TestEmailSuppressionEntryValidation(FrappeTestCase):
    def tearDown(self):
        frappe.db.delete("Email Suppression Entry", {"recipient_email": ["like", "%@example.com"]})
        super().tearDown()

    def test_global_entry_does_not_require_reference(self):
        entry = _new_entry(is_global=1)
        entry.insert(ignore_permissions=True)
        self.assertTrue(entry.name)
        self.assertEqual(entry.added_by, frappe.session.user)

    def test_non_global_entry_requires_reference(self):
        entry = _new_entry(is_global=0)
        with self.assertRaises(frappe.MandatoryError):
            entry.insert(ignore_permissions=True)

    def test_non_global_entry_with_reference_succeeds(self):
        entry = _new_entry(
            is_global=0, reference_doctype="User", reference_name=frappe.session.user
        )
        entry.insert(ignore_permissions=True)
        self.assertTrue(entry.name)


class TestEmailSuppressionEntryDedup(FrappeTestCase):
    def tearDown(self):
        frappe.db.delete("Email Suppression Entry", {"recipient_email": ["like", "%@example.com"]})
        super().tearDown()

    def test_duplicate_global_entry_raises_duplicate_entry_error(self):
        _new_entry(is_global=1).insert(ignore_permissions=True)
        with self.assertRaises(frappe.DuplicateEntryError):
            _new_entry(is_global=1).insert(ignore_permissions=True)

    def test_duplicate_scoped_entry_raises_duplicate_entry_error(self):
        _new_entry(
            is_global=0, reference_doctype="User", reference_name=frappe.session.user
        ).insert(ignore_permissions=True)
        with self.assertRaises(frappe.DuplicateEntryError):
            _new_entry(
                is_global=0, reference_doctype="User", reference_name=frappe.session.user
            ).insert(ignore_permissions=True)

    def test_same_email_different_scope_is_not_a_duplicate(self):
        _new_entry(
            is_global=0, reference_doctype="User", reference_name=frappe.session.user
        ).insert(ignore_permissions=True)
        # A different reference_name is a different scope entirely — must
        # not collide with the entry above. frappe.session.user is
        # "Administrator" during tests, so "Guest" (also a real, always-
        # present User record) is used here as the genuinely different
        # scope.
        entry = _new_entry(is_global=0, reference_doctype="User", reference_name="Guest")
        entry.insert(ignore_permissions=True)
        self.assertTrue(entry.name)

    def test_global_and_scoped_entries_for_same_email_coexist(self):
        # is_global partitions the dedup space — a global entry and a
        # scoped entry for the same recipient_email are not duplicates of
        # each other.
        _new_entry(is_global=1).insert(ignore_permissions=True)
        entry = _new_entry(
            is_global=0, reference_doctype="User", reference_name=frappe.session.user
        )
        entry.insert(ignore_permissions=True)
        self.assertTrue(entry.name)

    def test_no_db_level_unique_index_multiple_global_rows_for_different_emails_ok(self):
        # Sanity check that inserting is not blocked by any DB-level
        # constraint for distinct emails — this doctype relies entirely on
        # the Python-side existence query, not a unique index.
        _new_entry(recipient_email="a@example.com", is_global=1).insert(ignore_permissions=True)
        _new_entry(recipient_email="b@example.com", is_global=1).insert(ignore_permissions=True)
        self.assertEqual(
            frappe.db.count(
                "Email Suppression Entry",
                {"recipient_email": ["in", ["a@example.com", "b@example.com"]]},
            ),
            2,
        )


class TestGetSuppressedEmails(FrappeTestCase):
    def tearDown(self):
        frappe.db.delete("Email Suppression Entry", {"recipient_email": ["like", "%@example.com"]})
        super().tearDown()

    def test_empty_addresses_returns_empty_without_query(self):
        self.assertEqual(get_suppressed_emails([]), [])
        self.assertEqual(get_suppressed_emails(None), [])

    def test_global_suppression_matches_regardless_of_scope(self):
        _new_entry(recipient_email="global@example.com", is_global=1).insert(
            ignore_permissions=True
        )
        result = get_suppressed_emails(
            ["global@example.com", "clean@example.com"],
            reference_doctype="Student",
            reference_name="STU-001",
        )
        self.assertIn("global@example.com", result)
        self.assertNotIn("clean@example.com", result)

    def test_scoped_suppression_matches_only_that_scope(self):
        _new_entry(
            recipient_email="scoped@example.com",
            is_global=0,
            reference_doctype="User",
            reference_name=frappe.session.user,
        ).insert(ignore_permissions=True)

        matching = get_suppressed_emails(
            ["scoped@example.com"], reference_doctype="User", reference_name=frappe.session.user
        )
        self.assertEqual(matching, ["scoped@example.com"])

        # frappe.session.user is "Administrator" during tests, so "Guest"
        # (also a real, always-present User record) is used here as the
        # genuinely different, non-matching scope.
        non_matching = get_suppressed_emails(
            ["scoped@example.com"], reference_doctype="User", reference_name="Guest"
        )
        self.assertEqual(non_matching, [])

        no_scope_given = get_suppressed_emails(["scoped@example.com"])
        self.assertEqual(no_scope_given, [])

    def test_not_suppressed_addresses_are_excluded(self):
        result = get_suppressed_emails(["never-suppressed@example.com"])
        self.assertEqual(result, [])

    def test_one_query_covers_the_whole_batch(self):
        # Functional proxy for "one query, not N+1": a batch of 3 addresses
        # with only 1 suppressed returns exactly that 1, in a single call.
        _new_entry(recipient_email="bad1@example.com", is_global=1).insert(
            ignore_permissions=True
        )
        result = get_suppressed_emails(
            ["bad1@example.com", "good1@example.com", "good2@example.com"]
        )
        self.assertEqual(result, ["bad1@example.com"])
