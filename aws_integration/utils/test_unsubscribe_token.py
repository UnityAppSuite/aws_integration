# Copyright (c) 2026, Hybrowlabs Technologies and Contributors
# See license.txt
"""Unit tests for aws_integration/utils/unsubscribe_token.py —
email-governance-engine Part 5 signed-token primitive.

Pure signature/expiry/round-trip logic — no doctype writes, no network, no
mocking of frappe.sendmail. The only Frappe dependency is
``frappe.utils.verified_command.get_secret()`` (site's encryption key/
configured secret), which is already available in the test site's
site_config, same as every other test in this app that touches signed
links."""

import time
import unittest
from unittest import mock

from frappe.tests.utils import FrappeTestCase

from aws_integration.utils.unsubscribe_token import (
	generate_unsubscribe_token,
	verify_unsubscribe_token,
)


class TestGenerateUnsubscribeToken(FrappeTestCase):
	def test_falsy_email_raises(self):
		import frappe

		with self.assertRaises(frappe.ValidationError):
			generate_unsubscribe_token(None)
		with self.assertRaises(frappe.ValidationError):
			generate_unsubscribe_token("")

	def test_half_scoped_doctype_without_name_raises(self):
		# disposer Fix 1 — exactly one of reference_doctype/reference_name
		# must never silently pass through: it used to decode, at the
		# receiver, to a GLOBAL unsubscribe instead of the caller's intended
		# scoped one.
		import frappe

		with self.assertRaises(frappe.ValidationError):
			generate_unsubscribe_token("a@example.com", reference_doctype="Student")

	def test_half_scoped_name_without_doctype_raises(self):
		import frappe

		with self.assertRaises(frappe.ValidationError):
			generate_unsubscribe_token("a@example.com", reference_name="STU-0001")

	def test_fully_paired_reference_still_works(self):
		# Regression guard — a properly-paired reference must not be
		# affected by the half-scoped guard.
		token = generate_unsubscribe_token(
			"a@example.com", reference_doctype="Student", reference_name="STU-0001"
		)
		decoded = verify_unsubscribe_token(token)
		self.assertIsNotNone(decoded)
		self.assertEqual(decoded["reference_doctype"], "Student")
		self.assertEqual(decoded["reference_name"], "STU-0001")

	def test_fully_global_both_empty_still_works(self):
		# Regression guard — the legitimate global-unsubscribe path (both
		# reference_doctype and reference_name omitted) must not be broken
		# by the half-scoped guard.
		token = generate_unsubscribe_token("a@example.com")
		decoded = verify_unsubscribe_token(token)
		self.assertIsNotNone(decoded)
		self.assertIsNone(decoded["reference_doctype"])
		self.assertIsNone(decoded["reference_name"])

	def test_returns_dot_delimited_string(self):
		token = generate_unsubscribe_token("a@example.com")
		self.assertIsInstance(token, str)
		self.assertEqual(token.count("."), 1)

	def test_token_is_url_safe(self):
		token = generate_unsubscribe_token(
			"a@example.com", reference_doctype="Notification Log", reference_name="NL-1"
		)
		# Only URL-safe base64 chars + "." are allowed — nothing that would
		# need percent-encoding in a query string.
		allowed = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_.")
		self.assertTrue(set(token) <= allowed, token)


class TestVerifyUnsubscribeTokenRoundTrip(FrappeTestCase):
	def test_round_trip_with_reference(self):
		token = generate_unsubscribe_token(
			"parent@example.com", reference_doctype="Notification Log", reference_name="NL-1"
		)
		decoded = verify_unsubscribe_token(token)
		self.assertIsNotNone(decoded)
		self.assertEqual(decoded["recipient_email"], "parent@example.com")
		self.assertEqual(decoded["reference_doctype"], "Notification Log")
		self.assertEqual(decoded["reference_name"], "NL-1")

	def test_round_trip_without_reference(self):
		token = generate_unsubscribe_token("parent@example.com")
		decoded = verify_unsubscribe_token(token)
		self.assertIsNotNone(decoded)
		self.assertEqual(decoded["recipient_email"], "parent@example.com")
		self.assertIsNone(decoded["reference_doctype"])
		self.assertIsNone(decoded["reference_name"])

	def test_replay_is_idempotent_and_always_verifies_the_same(self):
		token = generate_unsubscribe_token("parent@example.com")
		first = verify_unsubscribe_token(token)
		second = verify_unsubscribe_token(token)
		third = verify_unsubscribe_token(token)
		self.assertEqual(first, second)
		self.assertEqual(second, third)


class TestVerifyUnsubscribeTokenRejection(FrappeTestCase):
	"""None of these must ever raise — a bad token is expected input."""

	def test_none_token(self):
		self.assertIsNone(verify_unsubscribe_token(None))

	def test_empty_string_token(self):
		self.assertIsNone(verify_unsubscribe_token(""))

	def test_non_string_token(self):
		self.assertIsNone(verify_unsubscribe_token(12345))
		self.assertIsNone(verify_unsubscribe_token({"email": "a@example.com"}))

	def test_no_dot_separator(self):
		self.assertIsNone(verify_unsubscribe_token("not-a-valid-token-at-all"))

	def test_garbage_payload_with_valid_looking_shape(self):
		self.assertIsNone(verify_unsubscribe_token("not-base64-@@@.not-a-signature"))

	def test_tampered_signature_is_rejected(self):
		token = generate_unsubscribe_token("parent@example.com")
		payload_b64, _, signature = token.rpartition(".")
		tampered = f"{payload_b64}.{signature[:-1]}{'0' if signature[-1] != '0' else '1'}"
		self.assertIsNone(verify_unsubscribe_token(tampered))

	def test_tampered_payload_is_rejected(self):
		# Changing the payload without re-signing must invalidate it —
		# proves the signature actually covers the payload content, not
		# just its presence.
		token = generate_unsubscribe_token("parent@example.com")
		payload_b64, _, signature = token.rpartition(".")
		swapped_payload = generate_unsubscribe_token("attacker@example.com").rpartition(".")[0]
		forged = f"{swapped_payload}.{signature}"
		self.assertIsNone(verify_unsubscribe_token(forged))

	def test_expired_token_is_rejected(self):
		token = generate_unsubscribe_token("parent@example.com", expiry_days=45)
		# Fast-forward past expiry without waiting 45 real days.
		with mock.patch(
			"aws_integration.utils.unsubscribe_token.time.time",
			return_value=time.time() + 46 * 86400,
		):
			self.assertIsNone(verify_unsubscribe_token(token))

	def test_token_still_valid_just_before_expiry(self):
		token = generate_unsubscribe_token("parent@example.com", expiry_days=45)
		with mock.patch(
			"aws_integration.utils.unsubscribe_token.time.time",
			return_value=time.time() + 44 * 86400,
		):
			self.assertIsNotNone(verify_unsubscribe_token(token))

	def test_custom_expiry_days_respected(self):
		token = generate_unsubscribe_token("parent@example.com", expiry_days=1)
		with mock.patch(
			"aws_integration.utils.unsubscribe_token.time.time",
			return_value=time.time() + 2 * 86400,
		):
			self.assertIsNone(verify_unsubscribe_token(token))

	def test_malformed_base64_in_payload_segment(self):
		self.assertIsNone(verify_unsubscribe_token("!!!not-base64!!!.deadbeef"))

	def test_valid_base64_but_not_json(self):
		import base64

		junk_b64 = base64.urlsafe_b64encode(b"not json at all").decode().rstrip("=")
		from aws_integration.utils.unsubscribe_token import _sign_message

		signature = _sign_message(junk_b64)
		self.assertIsNone(verify_unsubscribe_token(f"{junk_b64}.{signature}"))

	def test_valid_json_but_missing_email(self):
		import base64
		import json

		payload = json.dumps({"doctype": None, "name": None, "expiry": int(time.time()) + 86400})
		payload_b64 = base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")
		from aws_integration.utils.unsubscribe_token import _sign_message

		signature = _sign_message(payload_b64)
		self.assertIsNone(verify_unsubscribe_token(f"{payload_b64}.{signature}"))


if __name__ == "__main__":
	unittest.main()
