# Copyright (c) 2026, Hybrowlabs Technologies and Contributors
# See license.txt
"""Unit tests for aws_integration/utils/email_headers.py — email-governance-
engine Part 2 (header injection via the make_email_body_message hook).

These tests exercise ``inject_governance_headers()`` directly against a
lightweight stand-in for Frappe core's ``EMail`` object (real
``email.message.EmailMessage`` as ``msg_root`` — the same header get/set/
delete semantics the real hook relies on — with plain attributes for
``sender``/``recipients``/``subject``), rather than mocking away the whole
email module. This is deliberate: the marker-header mechanism this module
depends on (email_headers -> real X-headers on msg_root -> read back by the
hook) is exactly what's under test, so faking it with a MagicMock would
test nothing real.

Also covers ``mark_promotional_headers()`` (the marker-building helper
``aws_integration.utils.email.sendmail()`` calls) and the updated
``sendmail()``/``send_email_in_batches()`` signatures from
``aws_integration/utils/test_email.py``'s sibling module.

Per the task's scope boundary: no test here calls a real edu_quality/
unity_parent_app caller with promotional=True — these tests only prove the
mechanism works when aws_integration's own functions are called directly
with the marker set.
"""

import email.message
import types
import unittest
from unittest import mock

import frappe
from frappe.tests.utils import FrappeTestCase

from aws_integration.utils.email_headers import (
    PROMOTIONAL_HEADER,
    UNSUB_DOCTYPE_HEADER,
    UNSUB_NAME_HEADER,
    inject_governance_headers,
    mark_promotional_headers,
)
from aws_integration.utils.test_email import _patch_aws_settings


class _FakeEMail:
    """Minimal stand-in for frappe.email.email_body.EMail, carrying only
    what inject_governance_headers() actually reads/writes: msg_root
    (real email.message.EmailMessage — same header semantics as the real
    MIMEMultipart), sender, recipients, subject, and the set_header()
    method (mirroring EMail.set_header()'s delete-then-set behavior)."""

    def __init__(self, sender="Unity Notices <notifications@unityedu.test>", recipients=None, subject="Test Subject"):
        self.msg_root = email.message.EmailMessage()
        self.sender = sender
        self.recipients = recipients if recipients is not None else ["parent@example.com"]
        self.subject = subject

    def set_header(self, key, value):
        if key in self.msg_root:
            del self.msg_root[key]
        self.msg_root[key] = value


def _apply_marker_headers(mail, headers):
    """Simulate what EMail.add_headers() does: prepend "X-" if missing and
    set on msg_root, BEFORE the hook runs — exactly the order
    QueueBuilder.prepare_email_content() uses in real Frappe core."""
    for key, value in headers.items():
        header_key = key if key.startswith("X-") else f"X-{key}"
        mail.set_header(header_key, value)


def _settings_patch(transactional="", promotional=""):
    settings = types.SimpleNamespace(
        get=lambda key: {
            "ses_transactional_configuration_set": transactional,
            "ses_promotional_configuration_set": promotional,
        }.get(key)
    )
    return mock.patch(
        "aws_integration.utils.email_headers.frappe.get_cached_doc", return_value=settings
    )


class TestMarkPromotionalHeaders(FrappeTestCase):
    def test_marker_only(self):
        headers = mark_promotional_headers()
        self.assertEqual(headers, {"Aws-Promotional": "1"})

    def test_marker_with_reference(self):
        headers = mark_promotional_headers(reference_doctype="Notification Log", reference_name="NL-001")
        self.assertEqual(
            headers,
            {
                "Aws-Promotional": "1",
                "Aws-Unsub-Doctype": "Notification Log",
                "Aws-Unsub-Name": "NL-001",
            },
        )

    def test_marker_without_reference_name_omits_it(self):
        headers = mark_promotional_headers(reference_doctype="Notification Log")
        self.assertNotIn("Aws-Unsub-Name", headers)


class TestInjectGovernanceHeadersMarkerHandling(FrappeTestCase):
    """The marker headers must be consumed and stripped — never leaked to
    the actual outbound/recipient-visible message."""

    def test_no_marker_no_list_unsubscribe_and_no_leak(self):
        mail = _FakeEMail()
        with _settings_patch():
            inject_governance_headers(mail)
        self.assertNotIn("List-Unsubscribe", mail.msg_root)
        self.assertNotIn(PROMOTIONAL_HEADER, mail.msg_root)

    def test_marker_present_is_stripped_after_processing(self):
        mail = _FakeEMail()
        _apply_marker_headers(mail, mark_promotional_headers(reference_doctype="X", reference_name="Y"))
        with _settings_patch():
            inject_governance_headers(mail)
        self.assertNotIn(PROMOTIONAL_HEADER, mail.msg_root)
        self.assertNotIn(UNSUB_DOCTYPE_HEADER, mail.msg_root)
        self.assertNotIn(UNSUB_NAME_HEADER, mail.msg_root)


class TestListUnsubscribeInjectionGatedOnMarker(FrappeTestCase):
    """DESIGN CONSTRAINT 1/2: never inject List-Unsubscribe unless the
    promotional marker is explicitly present — this is what makes OTP/
    password-reset mail structurally safe."""

    def test_transactional_send_never_gets_list_unsubscribe(self):
        mail = _FakeEMail()
        # No marker applied at all — this is what every OTP/password-reset
        # send in the bench looks like to this hook.
        with _settings_patch():
            inject_governance_headers(mail)
        self.assertNotIn("List-Unsubscribe", mail.msg_root)

    def test_promotional_send_with_full_reference_gets_https_and_mailto(self):
        mail = _FakeEMail(recipients=["parent@example.com"])
        _apply_marker_headers(
            mail, mark_promotional_headers(reference_doctype="Notification Log", reference_name="NL-1")
        )
        with _settings_patch():
            with mock.patch(
                "aws_integration.utils.email_headers.generate_unsubscribe_token",
                return_value="TOKEN123",
            ) as mock_token:
                inject_governance_headers(mail)

        mock_token.assert_called_once()
        call_kwargs = mock_token.call_args.kwargs
        self.assertEqual(call_kwargs["reference_doctype"], "Notification Log")
        self.assertEqual(call_kwargs["reference_name"], "NL-1")
        self.assertEqual(call_kwargs["recipient_email"], "parent@example.com")

        header = mail.msg_root["List-Unsubscribe"]
        self.assertIn("aws_integration.api.unsubscribe.one_click_unsubscribe", header)
        self.assertIn("token=TOKEN123", header)
        self.assertIn("mailto:notifications@unityedu.test", header)
        # RFC 8058 one-click header must accompany a genuine https: link.
        self.assertEqual(mail.msg_root["List-Unsubscribe-Post"], "List-Unsubscribe=One-Click")

    def test_promotional_send_without_reference_still_gets_global_https_link(self):
        # Unlike the old core-endpoint path, reference_doctype/name are no
        # longer required to build the https: link — an unscoped token
        # decodes to a global unsubscribe at receiver time. See
        # unsubscribe_token module docstring.
        mail = _FakeEMail(recipients=["parent@example.com"])
        _apply_marker_headers(mail, mark_promotional_headers())  # no doctype/name
        with _settings_patch():
            with mock.patch(
                "aws_integration.utils.email_headers.generate_unsubscribe_token",
                return_value="TOKEN456",
            ) as mock_token:
                inject_governance_headers(mail)

        mock_token.assert_called_once()
        call_kwargs = mock_token.call_args.kwargs
        self.assertIsNone(call_kwargs["reference_doctype"])
        self.assertIsNone(call_kwargs["reference_name"])

        header = mail.msg_root["List-Unsubscribe"]
        self.assertIn("token=TOKEN456", header)
        self.assertIn("mailto:notifications@unityedu.test", header)
        self.assertEqual(mail.msg_root["List-Unsubscribe-Post"], "List-Unsubscribe=One-Click")

    def test_promotional_send_with_no_recipient_gets_mailto_only_and_no_post_header(self):
        mail = _FakeEMail(recipients=[])
        _apply_marker_headers(
            mail, mark_promotional_headers(reference_doctype="Notification Log", reference_name="NL-1")
        )
        with _settings_patch():
            with mock.patch(
                "aws_integration.utils.email_headers.generate_unsubscribe_token"
            ) as mock_token:
                inject_governance_headers(mail)

        mock_token.assert_not_called()
        header = mail.msg_root["List-Unsubscribe"]
        self.assertNotIn("https://", header)
        self.assertIn("mailto:notifications@unityedu.test", header)
        self.assertNotIn("List-Unsubscribe-Post", mail.msg_root)

    def test_promotional_send_with_multiple_recipients_still_works_and_warns(self):
        # Known limitation (documented in the module): the URL is only
        # signed for the first recipient. Verify it degrades gracefully
        # (still builds *a* header) rather than crashing.
        mail = _FakeEMail(recipients=["a@example.com", "b@example.com"])
        _apply_marker_headers(
            mail, mark_promotional_headers(reference_doctype="Notification Log", reference_name="NL-1")
        )
        with _settings_patch():
            with mock.patch(
                "aws_integration.utils.email_headers.generate_unsubscribe_token",
                return_value="TOKEN789",
            ) as mock_token:
                inject_governance_headers(mail)

        # Only signed for the first recipient — documented limitation.
        self.assertEqual(mock_token.call_args.kwargs["recipient_email"], "a@example.com")
        self.assertIn("List-Unsubscribe", mail.msg_root)

    def test_unsubscribe_url_build_failure_does_not_raise_or_block_mailto(self):
        mail = _FakeEMail(recipients=["parent@example.com"])
        _apply_marker_headers(
            mail, mark_promotional_headers(reference_doctype="Notification Log", reference_name="NL-1")
        )
        with _settings_patch():
            with mock.patch(
                "aws_integration.utils.email_headers.generate_unsubscribe_token",
                side_effect=Exception("signing blew up"),
            ):
                # Must not raise — this hook must never break mail sending.
                inject_governance_headers(mail)
        header = mail.msg_root["List-Unsubscribe"]
        self.assertIn("mailto:", header)
        self.assertNotIn("https://", header)
        # https link failed to build, so the one-click POST header must not
        # be advertised either — nothing to POST to.
        self.assertNotIn("List-Unsubscribe-Post", mail.msg_root)


class TestConfigurationSetInjection(FrappeTestCase):
    """X-SES-Configuration-Set is unconditional (not gated on the
    promotional marker) — applies to every send once configured."""

    def test_transactional_send_gets_transactional_set(self):
        mail = _FakeEMail()
        with _settings_patch(transactional="walnut-transactional", promotional="walnut-promotional"):
            inject_governance_headers(mail)
        self.assertEqual(mail.msg_root["X-SES-Configuration-Set"], "walnut-transactional")

    def test_promotional_send_gets_promotional_set(self):
        mail = _FakeEMail(recipients=["parent@example.com"])
        _apply_marker_headers(mail, mark_promotional_headers())
        with _settings_patch(transactional="walnut-transactional", promotional="walnut-promotional"):
            inject_governance_headers(mail)
        self.assertEqual(mail.msg_root["X-SES-Configuration-Set"], "walnut-promotional")

    def test_no_header_when_fields_blank(self):
        mail = _FakeEMail()
        with _settings_patch(transactional="", promotional=""):
            inject_governance_headers(mail)
        self.assertNotIn("X-SES-Configuration-Set", mail.msg_root)

    def test_settings_fetch_failure_does_not_raise(self):
        mail = _FakeEMail()
        with mock.patch(
            "aws_integration.utils.email_headers.frappe.get_cached_doc",
            side_effect=Exception("db unavailable"),
        ):
            # Must not raise.
            inject_governance_headers(mail)
        self.assertNotIn("X-SES-Configuration-Set", mail.msg_root)


class TestInjectGovernanceHeadersNeverRaises(FrappeTestCase):
    """This hook fires for EVERY email in the bench (OTP included) — a bug
    here must never take down unrelated mail sending."""

    def test_broken_msg_root_get_does_not_propagate(self):
        mail = _FakeEMail()

        class _BrokenMsgRoot:
            def get(self, *a, **kw):
                raise RuntimeError("boom")

            def __contains__(self, key):
                return False

        mail.msg_root = _BrokenMsgRoot()
        # Must not raise.
        inject_governance_headers(mail)


class TestSendmailPromotionalKwargThreading(FrappeTestCase):
    """aws_integration.utils.email.sendmail()/send_email_in_batches() —
    verify the new promotional/reference_doctype/reference_name kwargs
    thread through to frappe.sendmail(email_headers=...) correctly, and
    that existing (promotional=False) behavior is unchanged."""

    def setUp(self):
        self.settings_patch = _patch_aws_settings()
        self.settings_patch.start()
        self.addCleanup(self.settings_patch.stop)

    @mock.patch("aws_integration.utils.email.frappe.sendmail")
    def test_default_promotional_false_passes_no_email_headers(self, mock_sendmail):
        from aws_integration.utils.email import sendmail

        sendmail("Subject", "Body", ["a@example.com"], None, None)
        kwargs = mock_sendmail.call_args.kwargs
        self.assertIsNone(kwargs["email_headers"])

    @mock.patch("aws_integration.utils.email.frappe.sendmail")
    def test_promotional_true_builds_marker_headers(self, mock_sendmail):
        from aws_integration.utils.email import sendmail

        sendmail(
            "Subject",
            "Body",
            ["a@example.com"],
            None,
            None,
            promotional=True,
            reference_doctype="Notification Log",
            reference_name="NL-1",
        )
        kwargs = mock_sendmail.call_args.kwargs
        self.assertEqual(
            kwargs["email_headers"],
            {
                "Aws-Promotional": "1",
                "Aws-Unsub-Doctype": "Notification Log",
                "Aws-Unsub-Name": "NL-1",
            },
        )

    @mock.patch("aws_integration.utils.email.frappe.sendmail")
    def test_promotional_true_without_reference_still_marks(self, mock_sendmail):
        from aws_integration.utils.email import sendmail

        sendmail("Subject", "Body", ["a@example.com"], None, None, promotional=True)
        kwargs = mock_sendmail.call_args.kwargs
        self.assertEqual(kwargs["email_headers"], {"Aws-Promotional": "1"})

    @mock.patch("aws_integration.utils.email.frappe.sendmail")
    def test_send_email_in_batches_threads_promotional_to_every_item(self, mock_sendmail):
        from aws_integration.utils.email import send_email_in_batches

        data = {
            "s1": {
                "subject": "Sub1",
                "content": "Body1",
                "recepients": ["s1@example.com"],
                "cc_recepients": [],
                "bcc_recepients": [],
            },
        }
        with mock.patch("aws_integration.utils.email.frappe.get_value", return_value=5):
            send_email_in_batches(data, promotional=True, reference_doctype="Notification Log", reference_name="NL-1")
        kwargs = mock_sendmail.call_args.kwargs
        self.assertEqual(
            kwargs["email_headers"],
            {
                "Aws-Promotional": "1",
                "Aws-Unsub-Doctype": "Notification Log",
                "Aws-Unsub-Name": "NL-1",
            },
        )

    @mock.patch("aws_integration.utils.email.frappe.sendmail")
    def test_send_email_in_batches_per_item_override(self, mock_sendmail):
        from aws_integration.utils.email import send_email_in_batches

        data = {
            "promo": {
                "subject": "Sub1",
                "content": "Body1",
                "recepients": ["s1@example.com"],
                "cc_recepients": [],
                "bcc_recepients": [],
                "promotional": True,
                "reference_doctype": "Notification Log",
                "reference_name": "NL-1",
            },
            "plain": {
                "subject": "Sub2",
                "content": "Body2",
                "recepients": ["s2@example.com"],
                "cc_recepients": [],
                "bcc_recepients": [],
            },
        }
        with mock.patch("aws_integration.utils.email.frappe.get_value", return_value=5):
            send_email_in_batches(data)  # batch-level default promotional=False

        calls = mock_sendmail.call_args_list
        promo_call = next(c for c in calls if c.kwargs["email_headers"])
        plain_call = next(c for c in calls if not c.kwargs["email_headers"])
        self.assertEqual(promo_call.kwargs["email_headers"]["Aws-Promotional"], "1")
        self.assertIsNone(plain_call.kwargs["email_headers"])

    @mock.patch("aws_integration.utils.email.frappe.sendmail")
    def test_default_batch_promotional_false_preserves_part1_behavior(self, mock_sendmail):
        from aws_integration.utils.email import send_email_in_batches

        data = {
            "s1": {
                "subject": "Sub1",
                "content": "Body1",
                "recepients": ["s1@example.com"],
                "cc_recepients": [],
                "bcc_recepients": [],
            },
        }
        with mock.patch("aws_integration.utils.email.frappe.get_value", return_value=5):
            send_email_in_batches(data)
        kwargs = mock_sendmail.call_args.kwargs
        self.assertIsNone(kwargs["email_headers"])


if __name__ == "__main__":
    unittest.main()
