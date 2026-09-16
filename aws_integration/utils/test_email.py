# Copyright (c) 2026, Hybrowlabs Technologies and Contributors
# See license.txt
"""Unit tests for aws_integration/utils/email.py — transport consolidation
(Part 1): sendmail()/send_email_in_batches() now enqueue through Frappe
core's frappe.sendmail() instead of the old raw-boto3 SES path.

frappe.sendmail is always mocked here — these tests never actually send
mail (Emails are muted on this bench anyway per house rules) and never hit
AWS. They verify:
  - signature compatibility (existing callers need zero changes)
  - correct translation of arguments into frappe.sendmail() kwargs
  - cc/bcc/reply_to normalization (None / str / list all work)
  - recipient + email-address validation (ISS-28 delegation to core)
  - batching/pacing behavior in send_email_in_batches
  - one bad item does not abort the rest of a batch
"""

import types
import unittest
from unittest import mock

import frappe
from frappe.tests.utils import FrappeTestCase

from aws_integration.utils.email import (
    is_html,
    sendmail,
    send_email_in_batches,
    validate_email,
)
from aws_integration.utils.suppression import suppress


def _fake_settings(source_email="notifications@unityedu.test", sender_name="Unity Notices"):
    """A minimal stand-in for the AWS Settings single doctype, with just the
    attributes sendmail() reads off it."""
    return types.SimpleNamespace(source_email=source_email, sender_name=sender_name)


_REAL_GET_CACHED_DOC = frappe.get_cached_doc


def _patch_aws_settings(source_email="notifications@unityedu.test", sender_name="Unity Notices"):
    """Patch ``frappe.get_cached_doc`` so ONLY "AWS Settings" lookups are
    stubbed; every other doctype falls through to the real
    ``frappe.get_cached_doc``.

    NOTE (post-review, round 2): a blanket ``mock.patch(..., return_value=...)``
    used to be safe here because ``sendmail()`` was the only thing calling
    ``frappe.get_cached_doc`` in these tests. Now that ``sendmail()`` also
    writes a real "AWS SES Logs" record (the observability fix), a real
    ``doc.insert()`` runs underneath — which internally calls
    ``frappe.utils.now_datetime()`` -> ``get_system_timezone()`` ->
    ``frappe.get_system_settings()`` -> ``frappe.get_cached_doc("System
    Settings")``. A blanket stub would hijack that call too and return the
    fake AWS Settings namespace instead, breaking doc insertion with an
    AttributeError. This side_effect-based patch only intercepts "AWS
    Settings" and delegates everything else to the real implementation.
    """
    settings = _fake_settings(source_email=source_email, sender_name=sender_name)

    def _side_effect(doctype, *args, **kwargs):
        if doctype == "AWS Settings":
            return settings
        return _REAL_GET_CACHED_DOC(doctype, *args, **kwargs)

    return mock.patch(
        "aws_integration.utils.email.frappe.get_cached_doc", side_effect=_side_effect
    )


class TestValidateEmail(FrappeTestCase):
    """ISS-28: validate_email() now delegates to frappe.utils.validate_email_address
    instead of a hand-rolled regex."""

    def test_valid_addresses(self):
        self.assertTrue(validate_email("student.parent@example.com"))
        self.assertTrue(validate_email("Name Here <name.here@example.co.in>"))

    def test_invalid_addresses(self):
        self.assertFalse(validate_email(""))
        self.assertFalse(validate_email(None))
        self.assertFalse(validate_email("not-an-email"))
        self.assertFalse(validate_email("@example.com"))
        self.assertFalse(validate_email("user@"))
        self.assertFalse(validate_email("user@domain"))

    def test_multi_at_address_is_rejected(self):
        # REGRESSION (post-review, round 2): frappe.utils.validate_email_address()
        # parses its input via email.utils.parseaddr and, for a malformed
        # multi-"@" string like "a@b@c.com", truncates it to "b@c.com" and
        # returns that as a valid address instead of rejecting the whole
        # string — the OLD hand-rolled regex here correctly rejected this.
        # validate_email() now adds a narrow parseaddr-based guard in front
        # of core specifically to close this gap. This was previously
        # (incorrectly) asserted as "inherited, acceptable leniency" — it
        # is not; it was a real regression, now fixed.
        self.assertFalse(validate_email("a@b@c.com"))
        # Same malformed address, bracketed — core's leniency (and the bug)
        # applies here too.
        self.assertFalse(validate_email("Name <a@b@c.com>"))

    def test_is_html_unchanged(self):
        # is_html() is no longer used by sendmail() internally, but stays
        # exported for backward compatibility — verify it still works.
        self.assertTrue(is_html("<p>Hello</p>"))
        self.assertFalse(is_html("Plain text only"))


class TestSendmailSignatureCompatibility(FrappeTestCase):
    """Every existing caller (edu_quality walsh/admin.py, unity_parent_app
    api/admin.py) calls sendmail(subject, message, recepient, cc_recepient,
    bcc_recepient[, reply_tos]) — verify that exact shape still works,
    positionally and by keyword."""

    def setUp(self):
        self.settings_patch = _patch_aws_settings()
        self.settings_patch.start()
        self.addCleanup(self.settings_patch.stop)

    @mock.patch("aws_integration.utils.email.frappe.sendmail")
    def test_positional_call_matches_existing_callers(self, mock_sendmail):
        sendmail(
            "Notice Subject",
            "<p>Body</p>",
            ["parent@example.com"],
            ["cc@example.com"],
            ["bcc@example.com"],
        )
        self.assertEqual(mock_sendmail.call_count, 1)

    @mock.patch("aws_integration.utils.email.frappe.sendmail")
    def test_keyword_call_with_reply_tos(self, mock_sendmail):
        sendmail(
            subject="Notice Subject",
            message="Body text",
            recepient=["parent@example.com"],
            cc_recepient=[],
            bcc_recepient=[],
            reply_tos=["office@example.com"],
        )
        kwargs = mock_sendmail.call_args.kwargs
        self.assertEqual(kwargs["reply_to"], "office@example.com")

    @mock.patch("aws_integration.utils.email.frappe.sendmail")
    def test_single_string_recipient_allowed(self, mock_sendmail):
        # boto3-era callers sometimes passed a bare string rather than a
        # single-element list; both shapes must keep working.
        sendmail("Subject", "Body", "parent@example.com", None, None)
        kwargs = mock_sendmail.call_args.kwargs
        self.assertEqual(kwargs["recipients"], ["parent@example.com"])


class TestSendmailHalfScopedReferenceGuard(FrappeTestCase):
    """disposer Fix 1, layer 2 — sendmail() itself must reject a half-scoped
    (reference_doctype, reference_name) pair as early as possible, before it
    ever reaches mark_promotional_headers()/email_headers.py. Without this,
    a direct sendmail(..., promotional=True, reference_doctype="Student")
    call (reference_name omitted) would silently flow through as a GLOBAL
    unsubscribe once the message actually gets built."""

    def setUp(self):
        self.settings_patch = _patch_aws_settings()
        self.settings_patch.start()
        self.addCleanup(self.settings_patch.stop)

    @mock.patch("aws_integration.utils.email.frappe.sendmail")
    def test_doctype_without_name_raises_and_does_not_call_core_sendmail(self, mock_sendmail):
        with self.assertRaises(frappe.ValidationError):
            sendmail(
                "Subject",
                "Body",
                ["a@example.com"],
                None,
                None,
                promotional=True,
                reference_doctype="Student",
            )
        mock_sendmail.assert_not_called()

    @mock.patch("aws_integration.utils.email.frappe.sendmail")
    def test_name_without_doctype_raises_and_does_not_call_core_sendmail(self, mock_sendmail):
        with self.assertRaises(frappe.ValidationError):
            sendmail(
                "Subject",
                "Body",
                ["a@example.com"],
                None,
                None,
                promotional=True,
                reference_name="STU-0001",
            )
        mock_sendmail.assert_not_called()

    @mock.patch("aws_integration.utils.email.frappe.sendmail")
    def test_fully_paired_reference_still_sends(self, mock_sendmail):
        # Regression guard — a properly-paired reference must still work.
        sendmail(
            "Subject",
            "Body",
            ["a@example.com"],
            None,
            None,
            promotional=True,
            reference_doctype="Student",
            reference_name="STU-0001",
        )
        mock_sendmail.assert_called_once()

    @mock.patch("aws_integration.utils.email.frappe.sendmail")
    def test_fully_global_both_empty_still_sends(self, mock_sendmail):
        # Regression guard — the legitimate global-unsubscribe path (both
        # reference_doctype and reference_name omitted) must not be broken.
        sendmail(
            "Subject",
            "Body",
            ["a@example.com"],
            None,
            None,
            promotional=True,
        )
        mock_sendmail.assert_called_once()


class TestSendmailRoutesThroughCore(FrappeTestCase):
    """Verify sendmail() calls Frappe core's frappe.sendmail() with the
    right translation of arguments — this is the actual Route B -> Route A
    behavior under test."""

    def setUp(self):
        self.settings_patch = _patch_aws_settings()
        self.settings_patch.start()
        self.addCleanup(self.settings_patch.stop)

    @mock.patch("aws_integration.utils.email.frappe.sendmail")
    def test_recipients_cc_bcc_and_subject_message_passthrough(self, mock_sendmail):
        sendmail(
            "Fee Reminder",
            "<b>Please pay</b>",
            ["a@example.com", "b@example.com"],
            ["cc1@example.com"],
            ["bcc1@example.com"],
        )
        kwargs = mock_sendmail.call_args.kwargs
        self.assertEqual(kwargs["recipients"], ["a@example.com", "b@example.com"])
        self.assertEqual(kwargs["cc"], ["cc1@example.com"])
        self.assertEqual(kwargs["bcc"], ["bcc1@example.com"])
        self.assertEqual(kwargs["subject"], "Fee Reminder")
        self.assertEqual(kwargs["message"], "<b>Please pay</b>")

    @mock.patch("aws_integration.utils.email.frappe.sendmail")
    def test_sender_built_from_aws_settings(self, mock_sendmail):
        sendmail("Subject", "Body", ["a@example.com"], None, None)
        kwargs = mock_sendmail.call_args.kwargs
        self.assertEqual(kwargs["sender"], "Unity Notices <notifications@unityedu.test>")

    @mock.patch("aws_integration.utils.email.frappe.sendmail")
    def test_sender_falls_back_to_bare_email_without_sender_name(self, mock_sendmail):
        with _patch_aws_settings(source_email="notifications@unityedu.test", sender_name=""):
            sendmail("Subject", "Body", ["a@example.com"], None, None)
        kwargs = mock_sendmail.call_args.kwargs
        self.assertEqual(kwargs["sender"], "notifications@unityedu.test")

    @mock.patch("aws_integration.utils.email.frappe.sendmail")
    def test_delayed_true_so_it_enqueues_rather_than_sends_synchronously(self, mock_sendmail):
        # This is the whole point of the consolidation: mail goes into the
        # Email Queue and is delivered by the existing (untouched) flush
        # toggle in AWSSettings.handle_email_flush(), not sent inline here.
        sendmail("Subject", "Body", ["a@example.com"], None, None)
        kwargs = mock_sendmail.call_args.kwargs
        self.assertTrue(kwargs["delayed"])

    @mock.patch("aws_integration.utils.email.frappe.sendmail")
    def test_multiple_reply_tos_uses_first_and_does_not_raise(self, mock_sendmail):
        # Core frappe.sendmail() only supports a single Reply-To string;
        # boto3 SES accepted a list. Verify the graceful degradation.
        sendmail(
            "Subject",
            "Body",
            ["a@example.com"],
            None,
            None,
            reply_tos=["first@example.com", "second@example.com"],
        )
        kwargs = mock_sendmail.call_args.kwargs
        self.assertEqual(kwargs["reply_to"], "first@example.com")

    @mock.patch("aws_integration.utils.email.frappe.sendmail")
    def test_no_reply_to_when_not_given(self, mock_sendmail):
        sendmail("Subject", "Body", ["a@example.com"], None, None)
        kwargs = mock_sendmail.call_args.kwargs
        self.assertIsNone(kwargs["reply_to"])


class TestSendmailWritesSesLog(FrappeTestCase):
    """OBSERVABILITY FIX (post-review, round 2): sendmail() must write an
    "AWS SES Logs" record for every attempt — success (status "Sent") and,
    as an improvement over the old boto3-era behavior, failure too (status
    "Not Sent") — since send_email_in_batches() swallows individual
    failures and this doctype is the only per-attempt trace left otherwise.
    edu_quality's permission_import.py and unity_dev_tools's
    anonymize_core.py both assume this doctype carries real rows."""

    def setUp(self):
        self.settings_patch = _patch_aws_settings()
        self.settings_patch.start()
        self.addCleanup(self.settings_patch.stop)

    @mock.patch("aws_integration.utils.email.frappe.sendmail")
    def test_successful_send_writes_sent_log(self, mock_sendmail):
        mock_sendmail.return_value = None
        before = frappe.db.count("AWS SES Logs")

        sendmail(
            "Fee Reminder",
            "<b>Please pay</b>",
            ["parent@example.com"],
            ["cc@example.com"],
            None,
        )

        self.assertEqual(frappe.db.count("AWS SES Logs"), before + 1)
        log = frappe.get_last_doc("AWS SES Logs")
        self.assertEqual(log.status, "Sent")
        self.assertEqual(log.subject, "Fee Reminder")
        self.assertEqual(log.message, "<b>Please pay</b>")
        self.assertEqual(log.recepients, "parent@example.com")
        self.assertEqual(log.cc_recepients, "cc@example.com")
        # "from" stores whatever sendmail() built as the sender (display
        # name + address, matching what frappe.sendmail() itself was given).
        # Frappe's Data fieldtype HTML-escapes "<"/">" on save.
        self.assertEqual(log.get("from"), "Unity Notices &lt;notifications@unityedu.test&gt;")

    @mock.patch("aws_integration.utils.email.frappe.sendmail")
    def test_failed_send_writes_not_sent_log_and_still_raises(self, mock_sendmail):
        mock_sendmail.side_effect = frappe.ValidationError("boom")
        before = frappe.db.count("AWS SES Logs")

        with self.assertRaises(frappe.ValidationError):
            sendmail("Subject", "Body", ["parent@example.com"], None, None)

        self.assertEqual(frappe.db.count("AWS SES Logs"), before + 1)
        log = frappe.get_last_doc("AWS SES Logs")
        self.assertEqual(log.status, "Not Sent")

    @mock.patch("aws_integration.utils.email.frappe.sendmail")
    def test_validation_failure_before_core_call_writes_no_log(self, mock_sendmail):
        # A bad recipient address never even reaches frappe.sendmail() —
        # nothing was attempted, so nothing should be logged.
        before = frappe.db.count("AWS SES Logs")

        with self.assertRaises(frappe.ValidationError):
            sendmail("Subject", "Body", ["not-an-email"], None, None)

        mock_sendmail.assert_not_called()
        self.assertEqual(frappe.db.count("AWS SES Logs"), before)


class TestSendmailValidation(FrappeTestCase):
    def setUp(self):
        self.settings_patch = _patch_aws_settings()
        self.settings_patch.start()
        self.addCleanup(self.settings_patch.stop)

    @mock.patch("aws_integration.utils.email.frappe.sendmail")
    def test_no_recipient_raises_and_does_not_call_core_sendmail(self, mock_sendmail):
        with self.assertRaises(frappe.ValidationError):
            sendmail("Subject", "Body", None, None, None)
        mock_sendmail.assert_not_called()

    @mock.patch("aws_integration.utils.email.frappe.sendmail")
    def test_empty_recipient_list_raises(self, mock_sendmail):
        with self.assertRaises(frappe.ValidationError):
            sendmail("Subject", "Body", [], None, None)
        mock_sendmail.assert_not_called()

    @mock.patch("aws_integration.utils.email.frappe.sendmail")
    def test_invalid_recipient_address_raises(self, mock_sendmail):
        with self.assertRaises(frappe.ValidationError):
            sendmail("Subject", "Body", ["not-an-email"], None, None)
        mock_sendmail.assert_not_called()

    @mock.patch("aws_integration.utils.email.frappe.sendmail")
    def test_invalid_cc_address_raises(self, mock_sendmail):
        with self.assertRaises(frappe.ValidationError):
            sendmail("Subject", "Body", ["ok@example.com"], ["bad-cc"], None)
        mock_sendmail.assert_not_called()


class TestSendEmailInBatches(FrappeTestCase):
    """send_email_in_batches(data) — structure documented in the function's
    own docstring: {key: {subject, content, recepients, cc_recepients,
    bcc_recepients, reply_tos}}."""

    def setUp(self):
        self.settings_patch = _patch_aws_settings()
        self.settings_patch.start()
        self.addCleanup(self.settings_patch.stop)

        self.sleep_patch = mock.patch("aws_integration.utils.email.time.sleep")
        self.mock_sleep = self.sleep_patch.start()
        self.addCleanup(self.sleep_patch.stop)

    def _batch_size_patch(self, size):
        return mock.patch(
            "aws_integration.utils.email.frappe.get_value", return_value=size
        )

    @mock.patch("aws_integration.utils.email.frappe.sendmail")
    def test_calls_sendmail_once_per_item(self, mock_sendmail):
        data = {
            "s1": {
                "subject": "Sub1",
                "content": "Body1",
                "recepients": ["s1@example.com"],
                "cc_recepients": [],
                "bcc_recepients": [],
            },
            "s2": {
                "subject": "Sub2",
                "content": "Body2",
                "recepients": ["s2@example.com"],
                "cc_recepients": [],
                "bcc_recepients": [],
            },
        }
        with self._batch_size_patch(5):
            send_email_in_batches(data)
        self.assertEqual(mock_sendmail.call_count, 2)

    @mock.patch("aws_integration.utils.email.frappe.sendmail")
    def test_reply_tos_now_passed_through(self, mock_sendmail):
        # Pre-existing bug fix: the old implementation accepted a
        # "reply_tos" key per its own documented structure but never
        # forwarded it to sendmail(). Verify it now does.
        data = {
            "s1": {
                "subject": "Sub1",
                "content": "Body1",
                "recepients": ["s1@example.com"],
                "cc_recepients": [],
                "bcc_recepients": [],
                "reply_tos": ["office@example.com"],
            },
        }
        with self._batch_size_patch(5):
            send_email_in_batches(data)
        kwargs = mock_sendmail.call_args.kwargs
        self.assertEqual(kwargs["reply_to"], "office@example.com")

    @mock.patch("aws_integration.utils.email.frappe.sendmail")
    def test_batching_respects_email_batch_size_and_sleeps_between_chunks(self, mock_sendmail):
        data = {
            f"s{i}": {
                "subject": "Sub",
                "content": "Body",
                "recepients": [f"s{i}@example.com"],
                "cc_recepients": [],
                "bcc_recepients": [],
            }
            for i in range(5)
        }
        with self._batch_size_patch(2):
            send_email_in_batches(data)
        # 5 items / batch size 2 -> 3 chunks (2, 2, 1) -> 3 sleeps
        self.assertEqual(self.mock_sleep.call_count, 3)
        self.assertEqual(mock_sendmail.call_count, 5)

    @mock.patch("aws_integration.utils.email.frappe.sendmail")
    def test_zero_or_missing_batch_size_does_not_hang_or_crash(self, mock_sendmail):
        data = {
            "s1": {
                "subject": "Sub",
                "content": "Body",
                "recepients": ["s1@example.com"],
                "cc_recepients": [],
                "bcc_recepients": [],
            },
        }
        with self._batch_size_patch(None):
            send_email_in_batches(data)
        self.assertEqual(mock_sendmail.call_count, 1)

    @mock.patch("aws_integration.utils.email.frappe.log_error")
    @mock.patch("aws_integration.utils.email.sendmail")
    def test_one_bad_item_does_not_abort_the_rest_of_the_batch(
        self, mock_sendmail, mock_log_error
    ):
        # sendmail() can now raise synchronously (e.g. invalid address).
        # The old boto3 path never raised (it caught-and-logged internally),
        # so send_email_in_batches must not regress the "bad row doesn't
        # block the batch" behavior existing callers rely on.
        def side_effect(subject, message, recepient, cc, bcc, reply_tos=None, **kwargs):
            # PART 2: send_email_in_batches() now also passes
            # promotional/reference_doctype/reference_name kwargs through
            # to sendmail() on every call — accept (and ignore) them here
            # via **kwargs so this Part 1 regression test still exercises
            # only what it originally cared about.
            if recepient == ["bad@example.com"]:
                raise frappe.ValidationError("bad address")
            return None

        mock_sendmail.side_effect = side_effect

        data = {
            "good1": {
                "subject": "Sub",
                "content": "Body",
                "recepients": ["good1@example.com"],
                "cc_recepients": [],
                "bcc_recepients": [],
            },
            "bad": {
                "subject": "Sub",
                "content": "Body",
                "recepients": ["bad@example.com"],
                "cc_recepients": [],
                "bcc_recepients": [],
            },
            "good2": {
                "subject": "Sub",
                "content": "Body",
                "recepients": ["good2@example.com"],
                "cc_recepients": [],
                "bcc_recepients": [],
            },
        }
        with self._batch_size_patch(5):
            send_email_in_batches(data)

        self.assertEqual(mock_sendmail.call_count, 3)
        mock_log_error.assert_called_once()


class TestAWSSettingsSendEmailDeprecated(FrappeTestCase):
    """AWS Settings.send_email() (the direct boto3 SES method) is now dead
    code — verify it throws loudly instead of silently doing the old,
    bypassing-the-queue thing if anyone still calls it directly."""

    def test_raises_when_called_directly(self):
        settings = frappe.get_single("AWS Settings")
        with self.assertRaises(frappe.ValidationError):
            settings.send_email(
                destinations=types.SimpleNamespace(
                    to_service_format=lambda: {"ToAddresses": ["a@example.com"]}
                ),
                subject="Subject",
                content="Body",
            )


class TestSendmailSuppressionGate(FrappeTestCase):
    """PART 3 — email-governance-engine suppression gate integration.

    Verifies the precise distinction the task calls out: all-suppressed
    -> quiet no-op (no throw, core frappe.sendmail() never called); a
    genuinely empty recipient list for any OTHER reason -> the pre-existing
    frappe.throw() behavior is unchanged; and partial suppression filters
    only the suppressed addresses out of recipients/cc/bcc, leaving the
    unsuppressed ones untouched.
    """

    def setUp(self):
        self.settings_patch = _patch_aws_settings()
        self.settings_patch.start()
        self.addCleanup(self.settings_patch.stop)

    def tearDown(self):
        frappe.db.delete("Email Suppression Entry", {"recipient_email": ["like", "%@example.com"]})
        frappe.db.delete("Email Unsubscribe", {"email": ["like", "%@example.com"]})
        super().tearDown()

    @mock.patch("aws_integration.utils.email.frappe.sendmail")
    def test_all_recipients_suppressed_is_a_quiet_no_op(self, mock_sendmail):
        suppress("only@example.com", reason="Hard Bounce", is_global=True)

        result = sendmail("Subject", "Body", ["only@example.com"], None, None)

        self.assertIsNone(result)
        mock_sendmail.assert_not_called()

    @mock.patch("aws_integration.utils.email.frappe.sendmail")
    def test_all_suppressed_no_op_logs_the_actual_addresses(self, mock_sendmail):
        # OBSERVABILITY FIX (post-review): the no-op log line must carry the
        # actual suppressed addresses, not just a count, so an admin can
        # grep the log for a specific address ("why didn't Student X get
        # this notice").
        suppress("only@example.com", reason="Hard Bounce", is_global=True)
        suppress("second@example.com", reason="Complaint", is_global=True)

        with mock.patch("aws_integration.utils.email.frappe.logger") as mock_logger:
            log_instance = mock_logger.return_value
            result = sendmail(
                "Subject", "Body", ["only@example.com", "second@example.com"], None, None
            )

        self.assertIsNone(result)
        mock_sendmail.assert_not_called()
        self.assertTrue(log_instance.info.called)
        logged_args = log_instance.info.call_args[0]
        # Last positional arg is the suppressed-addresses list interpolated
        # into the log format string.
        logged_addresses = logged_args[-1]
        self.assertIn("only@example.com", logged_addresses)
        self.assertIn("second@example.com", logged_addresses)

    @mock.patch("aws_integration.utils.email.frappe.sendmail")
    def test_genuinely_empty_recipient_input_still_throws(self, mock_sendmail):
        # No suppression involved at all here — this is the pre-existing
        # "caller passed nothing" case, which must still raise.
        with self.assertRaises(frappe.ValidationError):
            sendmail("Subject", "Body", None, None, None)
        mock_sendmail.assert_not_called()

    @mock.patch("aws_integration.utils.email.frappe.sendmail")
    def test_genuinely_empty_recipient_list_still_throws(self, mock_sendmail):
        with self.assertRaises(frappe.ValidationError):
            sendmail("Subject", "Body", [], None, None)
        mock_sendmail.assert_not_called()

    @mock.patch("aws_integration.utils.email.frappe.sendmail")
    def test_partial_suppression_filters_only_suppressed_addresses(self, mock_sendmail):
        suppress("bad@example.com", reason="Complaint", is_global=True)

        sendmail(
            "Subject",
            "Body",
            ["good@example.com", "bad@example.com"],
            ["bad@example.com", "goodcc@example.com"],
            ["goodbcc@example.com"],
        )

        kwargs = mock_sendmail.call_args.kwargs
        self.assertEqual(kwargs["recipients"], ["good@example.com"])
        self.assertEqual(kwargs["cc"], ["goodcc@example.com"])
        self.assertEqual(kwargs["bcc"], ["goodbcc@example.com"])

    @mock.patch("aws_integration.utils.email.frappe.sendmail")
    def test_scoped_suppression_only_applies_within_matching_scope(self, mock_sendmail):
        suppress(
            "scoped@example.com",
            reason="Unsubscribe",
            reference_doctype="DocType",
            reference_name="User",
        )

        # Same address, different reference scope -> not suppressed here.
        sendmail(
            "Subject",
            "Body",
            ["scoped@example.com"],
            None,
            None,
            reference_doctype="DocType",
            reference_name="Role",
        )
        kwargs = mock_sendmail.call_args.kwargs
        self.assertEqual(kwargs["recipients"], ["scoped@example.com"])

    @mock.patch("aws_integration.utils.email.frappe.sendmail")
    def test_no_suppression_entries_behaves_exactly_as_before(self, mock_sendmail):
        sendmail("Subject", "Body", ["clean@example.com"], ["cleancc@example.com"], None)
        kwargs = mock_sendmail.call_args.kwargs
        self.assertEqual(kwargs["recipients"], ["clean@example.com"])
        self.assertEqual(kwargs["cc"], ["cleancc@example.com"])

    @mock.patch("aws_integration.utils.email.frappe.sendmail")
    def test_cc_only_suppressed_does_not_trigger_no_op_path(self, mock_sendmail):
        # Only the To list controls the no-op path (matching the
        # pre-existing "at least one recipient" semantics, which never
        # looked at cc/bcc either) — a fully-suppressed cc with a clean To
        # must still send normally, just with cc filtered out.
        suppress("ccbad@example.com", reason="Manual", is_global=True)

        sendmail("Subject", "Body", ["good@example.com"], ["ccbad@example.com"], None)

        kwargs = mock_sendmail.call_args.kwargs
        self.assertEqual(kwargs["recipients"], ["good@example.com"])
        self.assertEqual(kwargs["cc"], [])


class TestSendEmailInBatchesReferenceNameDefault(FrappeTestCase):
    """PART 3 — send_email_in_batches() defaults reference_name to the
    item's own dict key when reference_doctype is set but reference_name
    is not."""

    def setUp(self):
        self.settings_patch = _patch_aws_settings()
        self.settings_patch.start()
        self.addCleanup(self.settings_patch.stop)

        self.sleep_patch = mock.patch("aws_integration.utils.email.time.sleep")
        self.mock_sleep = self.sleep_patch.start()
        self.addCleanup(self.sleep_patch.stop)

    @mock.patch("aws_integration.utils.email.frappe.sendmail")
    def test_batch_level_reference_doctype_defaults_reference_name_to_item_key(
        self, mock_sendmail
    ):
        # NOTE: the dict key doubles as the reference_name the fallback
        # will default to (see send_email_in_batches docstring point 3), so
        # it must itself be a real DocType name for the Dynamic Link
        # validation on the Email Unsubscribe dual-write (below) to accept
        # it — "User" is used here purely as a valid, always-present
        # DocType name, not because the reference is semantically a User.
        data = {
            "User": {
                "subject": "Sub",
                "content": "Body",
                "recepients": ["parent100@example.com"],
                "cc_recepients": [],
                "bcc_recepients": [],
            },
        }
        with mock.patch("aws_integration.utils.email.frappe.get_value", return_value=5):
            send_email_in_batches(data, reference_doctype="DocType")

        # reference_doctype/reference_name aren't passed to core
        # frappe.sendmail() directly (see email_headers.py docstring) but
        # DO drive the suppression-scope lookup inside sendmail(); assert
        # indirectly via the suppression gate actually scoping correctly.
        suppress(
            "parent100@example.com",
            reason="Manual",
            reference_doctype="DocType",
            reference_name="User",
        )
        mock_sendmail.reset_mock()
        with mock.patch("aws_integration.utils.email.frappe.get_value", return_value=5):
            send_email_in_batches(data, reference_doctype="DocType")
        # Suppressed for exactly the defaulted reference_name ("User", the
        # item's own dict key) -> all-suppressed no-op -> sendmail()
        # returns without calling core.
        mock_sendmail.assert_not_called()

    @mock.patch("aws_integration.utils.email.frappe.sendmail")
    def test_per_item_reference_name_override_is_not_clobbered(self, mock_sendmail):
        data = {
            "Role": {
                "subject": "Sub",
                "content": "Body",
                "recepients": ["parent200@example.com"],
                "cc_recepients": [],
                "bcc_recepients": [],
                "reference_name": "EXPLICIT-NAME",
            },
        }
        suppress(
            "parent200@example.com",
            reason="Manual",
            reference_doctype="DocType",
            reference_name="Role",
        )
        with mock.patch("aws_integration.utils.email.frappe.get_value", return_value=5):
            send_email_in_batches(data, reference_doctype="DocType")

        # Suppression was recorded against reference_name="Role" (what the
        # fallback default WOULD have used, from the dict key), but the
        # item explicitly overrides reference_name to "EXPLICIT-NAME", so
        # the scope no longer matches -> not suppressed -> core
        # frappe.sendmail() IS called.
        mock_sendmail.assert_called_once()

    def tearDown(self):
        frappe.db.delete(
            "Email Suppression Entry", {"recipient_email": ["like", "%@example.com"]}
        )
        frappe.db.delete("Email Unsubscribe", {"email": ["like", "%@example.com"]})
        super().tearDown()

    @mock.patch("aws_integration.utils.email.frappe.sendmail")
    def test_no_reference_doctype_leaves_reference_name_none(self, mock_sendmail):
        # No reference_doctype at all -> the default must NOT kick in;
        # behaves exactly as Part 1/2 (reference_name stays whatever the
        # item/default gave, i.e. None here).
        data = {
            "s1": {
                "subject": "Sub",
                "content": "Body",
                "recepients": ["s1@example.com"],
                "cc_recepients": [],
                "bcc_recepients": [],
            },
        }
        with mock.patch("aws_integration.utils.email.frappe.get_value", return_value=5):
            send_email_in_batches(data)
        mock_sendmail.assert_called_once()


if __name__ == "__main__":
    unittest.main()
