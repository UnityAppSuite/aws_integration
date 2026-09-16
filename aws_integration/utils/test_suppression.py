# Copyright (c) 2026, Hybrowlabs Technologies and Contributors
# See license.txt
"""Unit tests for aws_integration/utils/suppression.py — email-governance-
engine Part 3 public API. Covers is_suppressed()/suppress()/unsuppress(),
the dual-write to Email Suppression Entry + core Email Unsubscribe, and
idempotency of a double-suppress on both doctypes.

These tests write real documents (Email Suppression Entry, Email
Unsubscribe) against the test DB — no frappe.sendmail/actual send is ever
exercised here (that belongs to test_email.py's sendmail() integration
tests)."""

import frappe
from frappe.tests.utils import FrappeTestCase

from aws_integration.utils.suppression import is_suppressed, suppress, unsuppress


class _SuppressionTestBase(FrappeTestCase):
    def tearDown(self):
        frappe.db.delete("Email Suppression Entry", {"recipient_email": ["like", "%@example.com"]})
        frappe.db.delete("Email Unsubscribe", {"email": ["like", "%@example.com"]})
        super().tearDown()


class TestIsSuppressed(_SuppressionTestBase):
    def test_falsy_email_returns_false(self):
        self.assertFalse(is_suppressed(None))
        self.assertFalse(is_suppressed(""))

    def test_not_suppressed_returns_false(self):
        self.assertFalse(is_suppressed("clean@example.com"))

    def test_globally_suppressed_returns_true_for_any_scope(self):
        suppress("global@example.com", reason="Manual", is_global=True)
        self.assertTrue(is_suppressed("global@example.com"))
        self.assertTrue(
            is_suppressed("global@example.com", reference_doctype="DocType", reference_name="User")
        )

    def test_scoped_suppression_only_true_for_that_scope(self):
        suppress(
            "scoped@example.com",
            reason="Unsubscribe",
            reference_doctype="DocType",
            reference_name="User",
        )
        self.assertTrue(
            is_suppressed("scoped@example.com", reference_doctype="DocType", reference_name="User")
        )
        self.assertFalse(
            is_suppressed("scoped@example.com", reference_doctype="DocType", reference_name="Role")
        )
        self.assertFalse(is_suppressed("scoped@example.com"))


class TestSuppressWritesBothDoctypes(_SuppressionTestBase):
    def test_falsy_email_is_a_no_op(self):
        suppress(None, reason="Manual")
        suppress("", reason="Manual")
        self.assertEqual(frappe.db.count("Email Suppression Entry"), 0)

    def test_global_suppress_writes_both_doctypes(self):
        suppress("bounce@example.com", reason="Hard Bounce", source="SES Feedback", is_global=True)

        entry = frappe.get_last_doc(
            "Email Suppression Entry", filters={"recipient_email": "bounce@example.com"}
        )
        self.assertEqual(entry.reason, "Hard Bounce")
        self.assertEqual(entry.source, "SES Feedback")
        self.assertEqual(entry.is_global, 1)
        self.assertIsNone(entry.reference_doctype)

        unsub = frappe.get_last_doc("Email Unsubscribe", filters={"email": "bounce@example.com"})
        self.assertEqual(unsub.global_unsubscribe, 1)

    def test_scoped_suppress_writes_both_doctypes_with_matching_scope(self):
        suppress(
            "complaint@example.com",
            reason="Complaint",
            reference_doctype="DocType",
            reference_name="System Settings",
        )

        entry = frappe.get_last_doc(
            "Email Suppression Entry", filters={"recipient_email": "complaint@example.com"}
        )
        self.assertEqual(entry.reference_doctype, "DocType")
        self.assertEqual(entry.reference_name, "System Settings")
        self.assertEqual(entry.is_global, 0)

        unsub = frappe.get_last_doc(
            "Email Unsubscribe", filters={"email": "complaint@example.com"}
        )
        self.assertEqual(unsub.reference_doctype, "DocType")
        self.assertEqual(unsub.reference_name, "System Settings")
        self.assertEqual(unsub.global_unsubscribe, 0)

    def test_scoped_suppress_without_reference_and_not_global_raises(self):
        with self.assertRaises(frappe.ValidationError):
            suppress("bad@example.com", reason="Manual")
        # Scoped to this test's own address — frappe.db.count() with no
        # filter would count every row in the whole (possibly non-empty,
        # pre-existing) table, not just what this test touched.
        self.assertEqual(
            frappe.db.count("Email Suppression Entry", {"recipient_email": "bad@example.com"}), 0
        )
        self.assertEqual(frappe.db.count("Email Unsubscribe", {"email": "bad@example.com"}), 0)

    def test_is_global_true_drops_any_reference_fields_passed(self):
        suppress(
            "globalref@example.com",
            reason="Manual",
            reference_doctype="DocType",
            reference_name="User",
            is_global=True,
        )
        entry = frappe.get_last_doc(
            "Email Suppression Entry", filters={"recipient_email": "globalref@example.com"}
        )
        self.assertIsNone(entry.reference_doctype)
        self.assertIsNone(entry.reference_name)
        self.assertEqual(entry.is_global, 1)


class TestSuppressIsIdempotent(_SuppressionTestBase):
    """Double-suppress is a legitimate real-world event (two SES complaint
    notifications for the same address) — must never raise."""

    def test_double_global_suppress_does_not_raise_and_leaves_one_row_each(self):
        suppress("dup@example.com", reason="Complaint", is_global=True)
        suppress("dup@example.com", reason="Complaint", is_global=True)  # must not raise

        self.assertEqual(
            frappe.db.count("Email Suppression Entry", {"recipient_email": "dup@example.com"}), 1
        )
        self.assertEqual(frappe.db.count("Email Unsubscribe", {"email": "dup@example.com"}), 1)

    def test_double_scoped_suppress_does_not_raise_and_leaves_one_row_each(self):
        suppress(
            "dupscoped@example.com",
            reason="Unsubscribe",
            reference_doctype="DocType",
            reference_name="File",
        )
        suppress(
            "dupscoped@example.com",
            reason="Unsubscribe",
            reference_doctype="DocType",
            reference_name="File",
        )  # must not raise

        self.assertEqual(
            frappe.db.count(
                "Email Suppression Entry", {"recipient_email": "dupscoped@example.com"}
            ),
            1,
        )
        self.assertEqual(
            frappe.db.count("Email Unsubscribe", {"email": "dupscoped@example.com"}), 1
        )

    def test_one_doctype_already_suppressed_other_not_still_succeeds(self):
        # Simulate a pre-existing core Email Unsubscribe row (e.g. from a
        # totally unrelated app calling core's own unsubscribe flow
        # directly) with no matching Email Suppression Entry yet. suppress()
        # must still create the missing Email Suppression Entry row and not
        # blow up on the Email Unsubscribe duplicate.
        frappe.get_doc(
            {
                "doctype": "Email Unsubscribe",
                "email": "partial@example.com",
                "global_unsubscribe": 1,
            }
        ).insert(ignore_permissions=True)

        suppress("partial@example.com", reason="Manual", is_global=True)

        self.assertEqual(
            frappe.db.count(
                "Email Suppression Entry", {"recipient_email": "partial@example.com"}
            ),
            1,
        )
        self.assertEqual(frappe.db.count("Email Unsubscribe", {"email": "partial@example.com"}), 1)


class TestSuppressPartialWriteAtomicity(_SuppressionTestBase):
    """Disposer-found, deterministic (not theoretical-race) partial-write
    hazard: core's Email Unsubscribe.on_update() unconditionally does
    ``frappe.get_doc(self.reference_doctype, self.reference_name)`` for
    every non-global insert. If that raises (most directly:
    ``frappe.DoesNotExistError`` when the referenced document is gone),
    it was previously uncaught and left the already-committed Email
    Suppression Entry row behind with no Email Unsubscribe counterpart.
    suppress() must now roll back the first write too and re-raise,
    regardless of how the caller then handles the exception.

    NOTE on reproducing this deterministically: a reference_name that
    simply *never existed* is actually caught earlier than the disposer's
    report suggests — core's own ``Document._validate_links()`` runs
    during the FIRST write's (Email Suppression Entry's) own ``insert()``
    and raises ``frappe.LinkValidationError`` before the second write is
    even attempted, so no row is ever committed for that specific input.
    ``test_never_existed_reference_name_is_rejected_before_any_write``
    below documents and locks in that behavior.

    The disposer's exact "first write commits, second write's on_update
    blows up" shape requires the referenced document to have EXISTED at
    validation time and be gone by the time on_update() dereferences it
    again — precisely what ``_validate_links()`` itself is vulnerable to,
    since it checks existence via ``frappe.db.get_value(..., cache=True)``
    (a request-local value cache). ``test_stale_link_cache_partial_write_is_rolled_back``
    reproduces exactly that: it warms the same value cache
    ``_validate_links()`` reads, then deletes the referenced document with
    a raw SQL delete (bypassing ``Document.delete()``'s cache
    invalidation) so the cache is stale — both inserts' own link
    validation then passes on the stale cached answer, Email Suppression
    Entry commits, and Email Unsubscribe's on_update() does a real,
    uncached ``frappe.get_doc()`` fetch and correctly raises
    ``frappe.DoesNotExistError`` — the disposer's scenario, reproduced
    exactly rather than approximated."""

    def test_never_existed_reference_name_is_rejected_before_any_write(self):
        # Belt-and-suspenders regression: a reference_name that never
        # existed at all must not leave any row in either doctype, whether
        # that's core's own pre-insert link validation catching it (as it
        # does today) or suppress()'s savepoint rollback (if core's
        # behavior ever changes). Either way: zero rows, exception
        # propagates.
        with self.assertRaises(frappe.ValidationError):
            suppress(
                "orphan@example.com",
                reason="Manual",
                reference_doctype="ToDo",
                reference_name="TODO-DOES-NOT-EXIST-99999",
            )

        self.assertEqual(
            frappe.db.count("Email Suppression Entry", {"recipient_email": "orphan@example.com"}),
            0,
        )
        self.assertEqual(
            frappe.db.count("Email Unsubscribe", {"email": "orphan@example.com"}), 0
        )

    def test_stale_link_cache_partial_write_is_rolled_back(self):
        ref = frappe.get_doc(
            {"doctype": "ToDo", "description": "suppression atomicity test — to be deleted"}
        ).insert(ignore_permissions=True)
        ref_name = ref.name

        # Warm the exact request-local value cache that
        # Document._validate_links()/get_invalid_links() reads
        # (frappe.db.get_value(doctype, docname, "name", cache=True)),
        # then delete the row with a raw SQL DELETE — bypassing
        # doc.delete()'s own cache invalidation — so that cached "yes,
        # this exists" answer goes stale while the document is actually
        # gone.
        frappe.db.get_value("ToDo", ref_name, "name", cache=True)
        frappe.db.sql("delete from `tabToDo` where name=%s", ref_name)

        with self.assertRaises(frappe.DoesNotExistError):
            suppress(
                "orphan-race@example.com",
                reason="Manual",
                reference_doctype="ToDo",
                reference_name=ref_name,
            )

        self.assertEqual(
            frappe.db.count(
                "Email Suppression Entry", {"recipient_email": "orphan-race@example.com"}
            ),
            0,
            "Email Suppression Entry row must not survive when the paired "
            "Email Unsubscribe write fails — partial write left uncommitted.",
        )
        self.assertEqual(
            frappe.db.count("Email Unsubscribe", {"email": "orphan-race@example.com"}), 0
        )

    def test_stale_link_cache_partial_write_rolled_back_even_if_caller_swallows_it(self):
        ref = frappe.get_doc(
            {"doctype": "ToDo", "description": "suppression atomicity test — to be deleted"}
        ).insert(ignore_permissions=True)
        ref_name = ref.name

        frappe.db.get_value("ToDo", ref_name, "name", cache=True)
        frappe.db.sql("delete from `tabToDo` where name=%s", ref_name)

        try:
            suppress(
                "orphan-race2@example.com",
                reason="Manual",
                reference_doctype="ToDo",
                reference_name=ref_name,
            )
        except frappe.DoesNotExistError:
            pass  # caller swallows it, as a webhook handler realistically might

        self.assertEqual(
            frappe.db.count(
                "Email Suppression Entry", {"recipient_email": "orphan-race2@example.com"}
            ),
            0,
        )
        self.assertEqual(
            frappe.db.count("Email Unsubscribe", {"email": "orphan-race2@example.com"}), 0
        )

    def test_successful_suppress_still_commits_normally_after_savepoint_change(self):
        # Regression guard: the savepoint wrapping must not break the
        # ordinary, fully-successful path.
        suppress(
            "normal@example.com",
            reason="Manual",
            reference_doctype="DocType",
            reference_name="User",
        )
        self.assertEqual(
            frappe.db.count("Email Suppression Entry", {"recipient_email": "normal@example.com"}),
            1,
        )
        self.assertEqual(
            frappe.db.count("Email Unsubscribe", {"email": "normal@example.com"}), 1
        )


class TestUnsuppress(_SuppressionTestBase):
    def test_falsy_email_is_a_no_op(self):
        unsuppress(None)  # must not raise

    def test_unsuppress_global_removes_both_doctypes(self):
        suppress("undo@example.com", reason="Manual", is_global=True)
        self.assertTrue(is_suppressed("undo@example.com"))

        unsuppress("undo@example.com")

        self.assertFalse(is_suppressed("undo@example.com"))
        self.assertEqual(frappe.db.count("Email Suppression Entry", {"recipient_email": "undo@example.com"}), 0)
        self.assertEqual(frappe.db.count("Email Unsubscribe", {"email": "undo@example.com"}), 0)

    def test_unsuppress_scoped_removes_only_matching_scope(self):
        suppress(
            "multi@example.com",
            reason="Unsubscribe",
            reference_doctype="DocType",
            reference_name="User",
        )
        suppress(
            "multi@example.com",
            reason="Unsubscribe",
            reference_doctype="DocType",
            reference_name="Role",
        )

        unsuppress("multi@example.com", reference_doctype="DocType", reference_name="User")

        self.assertFalse(
            is_suppressed("multi@example.com", reference_doctype="DocType", reference_name="User")
        )
        self.assertTrue(
            is_suppressed("multi@example.com", reference_doctype="DocType", reference_name="Role")
        )

    def test_unsuppress_nonexistent_entry_is_a_no_op(self):
        unsuppress("never-suppressed@example.com")  # must not raise
        unsuppress(
            "never-suppressed@example.com", reference_doctype="DocType", reference_name="User"
        )  # must not raise
