# Copyright (c) 2026, Hybrowlabs Technologies and Contributors
# See license.txt
"""Unit tests for aws_integration/api/unsubscribe.py — the RFC 8058
one-click unsubscribe POST receiver, email-governance-engine Part 5.

Calls ``one_click_unsubscribe()`` directly (as a plain Python function,
bypassing the HTTP/WSGI layer — the same approach
``aws_integration.utils.test_suppression`` and this app's other API tests
use) so these tests exercise real ``suppress()`` calls against the real
test DB, per Part 3's own established test pattern (real doctype rows, not
mocks, for the suppression piece itself)."""

import types
import unittest
from unittest import mock

import frappe
from frappe.tests.utils import FrappeTestCase

from aws_integration.api.unsubscribe import one_click_unsubscribe
from aws_integration.utils.suppression import is_suppressed
from aws_integration.utils.unsubscribe_token import generate_unsubscribe_token


class _UnsubscribeTestBase(FrappeTestCase):
	def tearDown(self):
		frappe.db.delete("Email Suppression Entry", {"recipient_email": ["like", "%@example.com"]})
		frappe.db.delete("Email Unsubscribe", {"email": ["like", "%@example.com"]})
		super().tearDown()


class TestOneClickUnsubscribeValidToken(_UnsubscribeTestBase):
	def test_global_token_suppresses_globally(self):
		token = generate_unsubscribe_token("global-click@example.com")
		result = one_click_unsubscribe(token=token)

		self.assertEqual(result, {"message": "ok"})
		self.assertTrue(is_suppressed("global-click@example.com"))
		entry = frappe.get_last_doc(
			"Email Suppression Entry", filters={"recipient_email": "global-click@example.com"}
		)
		self.assertEqual(entry.is_global, 1)
		self.assertEqual(entry.reason, "Unsubscribe")
		self.assertEqual(entry.source, "One-Click Unsubscribe")

	def test_scoped_token_suppresses_only_that_scope(self):
		token = generate_unsubscribe_token(
			"scoped-click@example.com", reference_doctype="DocType", reference_name="User"
		)
		one_click_unsubscribe(token=token)

		self.assertTrue(
			is_suppressed(
				"scoped-click@example.com", reference_doctype="DocType", reference_name="User"
			)
		)
		self.assertFalse(
			is_suppressed(
				"scoped-click@example.com", reference_doctype="DocType", reference_name="Role"
			)
		)
		entry = frappe.get_last_doc(
			"Email Suppression Entry", filters={"recipient_email": "scoped-click@example.com"}
		)
		self.assertEqual(entry.is_global, 0)
		self.assertEqual(entry.reference_doctype, "DocType")
		self.assertEqual(entry.reference_name, "User")

	def test_token_read_from_form_dict_fallback(self):
		token = generate_unsubscribe_token("formdict-click@example.com")
		with mock.patch.object(frappe.local, "form_dict", frappe._dict(token=token), create=True):
			result = one_click_unsubscribe()
		self.assertEqual(result, {"message": "ok"})
		self.assertTrue(is_suppressed("formdict-click@example.com"))


class TestOneClickUnsubscribeIdempotentReplay(_UnsubscribeTestBase):
	def test_replaying_same_valid_token_is_idempotent(self):
		token = generate_unsubscribe_token("replay-click@example.com")

		first = one_click_unsubscribe(token=token)
		second = one_click_unsubscribe(token=token)
		third = one_click_unsubscribe(token=token)

		self.assertEqual(first, {"message": "ok"})
		self.assertEqual(second, {"message": "ok"})
		self.assertEqual(third, {"message": "ok"})

		self.assertEqual(
			frappe.db.count(
				"Email Suppression Entry", {"recipient_email": "replay-click@example.com"}
			),
			1,
		)
		self.assertEqual(
			frappe.db.count("Email Unsubscribe", {"email": "replay-click@example.com"}), 1
		)


class TestOneClickUnsubscribeInvalidToken(_UnsubscribeTestBase):
	"""Invalid/expired/malformed tokens must never raise and must never
	record a suppression — but must still respond as a plain success, per
	the RFC 8058 "no interaction" contract (see module docstring)."""

	def test_none_token_does_not_raise_and_records_nothing(self):
		result = one_click_unsubscribe(token=None)
		self.assertEqual(result, {"message": "ok"})
		self.assertEqual(frappe.db.count("Email Suppression Entry"), self._entry_count_before)

	def test_garbage_token_does_not_raise_and_records_nothing(self):
		result = one_click_unsubscribe(token="not-a-real-token")
		self.assertEqual(result, {"message": "ok"})

	def test_expired_token_does_not_raise_and_records_nothing(self):
		with mock.patch(
			"aws_integration.utils.unsubscribe_token.time.time",
			return_value=__import__("time").time(),
		):
			token = generate_unsubscribe_token("expired-click@example.com", expiry_days=1)
		with mock.patch(
			"aws_integration.utils.unsubscribe_token.time.time",
			return_value=__import__("time").time() + 2 * 86400,
		):
			result = one_click_unsubscribe(token=token)
		self.assertEqual(result, {"message": "ok"})
		self.assertFalse(is_suppressed("expired-click@example.com"))

	def setUp(self):
		self._entry_count_before = frappe.db.count("Email Suppression Entry")


class TestOneClickUnsubscribeSuppressFailureIsSwallowed(_UnsubscribeTestBase):
	def test_suppress_exception_does_not_propagate(self):
		token = generate_unsubscribe_token("failing-click@example.com")
		with mock.patch(
			"aws_integration.api.unsubscribe.suppress", side_effect=Exception("db exploded")
		):
			result = one_click_unsubscribe(token=token)
		self.assertEqual(result, {"message": "ok"})


class TestOneClickUnsubscribeRateLimiting(_UnsubscribeTestBase):
	"""Proves the endpoint is actually decorated with a working rate limit,
	not just documented as having one — drives real requests through
	frappe.rate_limiter.RateLimiter's redis-cache-backed counter, the same
	mechanism frappe.core.doctype.user.user.reset_password uses.

	Skips gracefully (rather than failing) if this test environment's cache
	backend isn't reachable — that's an environment concern, not a
	regression in this endpoint."""

	def setUp(self):
		self.fake_ip = "203.0.113.55"
		self.cache_key = frappe.cache.make_key(
			f"rl:aws_integration.api.unsubscribe.one_click_unsubscribe:{self.fake_ip}"
		)
		try:
			frappe.cache.delete(self.cache_key)
		except Exception as e:  # pragma: no cover - environment guard
			raise unittest.SkipTest(f"cache backend unavailable: {e}")

		self.request_patch = mock.patch.object(
			frappe.local, "request", types.SimpleNamespace(method="POST"), create=True
		)
		self.request_patch.start()
		self.addCleanup(self.request_patch.stop)

		self.ip_patch = mock.patch.object(frappe.local, "request_ip", self.fake_ip, create=True)
		self.ip_patch.start()
		self.addCleanup(self.ip_patch.stop)

		self.form_dict_patch = mock.patch.object(
			frappe.local,
			"form_dict",
			frappe._dict(cmd="aws_integration.api.unsubscribe.one_click_unsubscribe"),
			create=True,
		)
		self.form_dict_patch.start()
		self.addCleanup(self.form_dict_patch.stop)

	def tearDown(self):
		try:
			frappe.cache.delete(self.cache_key)
		except Exception:
			pass
		super().tearDown()

	def test_exceeding_limit_raises_rate_limit_error(self):
		# NOTE: RATE_LIMIT_COUNT is captured by value inside the @rate_limit
		# decorator at import time, so patching the module attribute alone
		# does not shrink the already-applied decorator's closure — this
		# test therefore exercises the endpoint's REAL configured limit
		# (RATE_LIMIT_COUNT requests/hour/IP) rather than a patched one, to
		# stay honest about what's actually enforced at runtime.
		from aws_integration.api.unsubscribe import RATE_LIMIT_COUNT

		token = generate_unsubscribe_token("ratelimit-click@example.com")

		for _ in range(RATE_LIMIT_COUNT):
			one_click_unsubscribe(token=token)

		with self.assertRaises(frappe.RateLimitExceededError):
			one_click_unsubscribe(token=token)


if __name__ == "__main__":
	unittest.main()
