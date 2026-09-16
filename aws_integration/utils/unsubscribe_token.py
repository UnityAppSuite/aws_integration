# Copyright (c) 2026, Hybrowlabs Technologies and Contributors
# See license.txt
"""Signed, stateless unsubscribe tokens — email-governance-engine, Part 5.

Backs the RFC 8058 one-click unsubscribe POST receiver
(``aws_integration.api.unsubscribe.one_click_unsubscribe``). A token encodes
who is unsubscribing, optionally what they're unsubscribing from
(``reference_doctype``/``reference_name`` — omitted means "global"), and
when the token expires — all signed so a token cannot be forged or altered
in transit, and none of it needs a server-side row to verify.

WHY REUSE CORE'S OWN SIGNING PRIMITIVE
----------------------------------------
Core Frappe already has exactly this primitive:
``frappe.utils.verified_command._sign_message()`` (HMAC-SHA512 over the
message, keyed by ``frappe.utils.verified_command.get_secret()`` — the
site's configured ``secret`` if set, else the site's Fernet encryption key,
see that module for details) and ``verify_request()``/``get_signed_params()``
build on top of it for core's own GET-only unsubscribe link
(``frappe.email.queue.unsubscribe`` / ``get_unsubcribed_url()``).

Reusing ``_sign_message()``/``get_secret()`` directly — rather than
inventing a second secret and a second HMAC scheme for this app — means
this token's forgery-resistance is exactly as strong as core's own signed
links, with no new secret-management surface to get wrong. The leading
underscore on ``_sign_message`` marks it "module-private by convention", not
"do not reuse" — it takes a single ``str`` and returns a hex digest with no
side effects or app-specific assumptions baked in, so importing it directly
is safe and is the same thing core's own ``get_signed_params()`` does.

WHY THIS IS NOT JUST ``get_signed_params()``/``verify_request()`` VERBATIM
------------------------------------------------------------------------------
``verify_request()`` is hard-wired to ``frappe.request`` global state
(reads ``frappe.local.flags.signed_query_string`` /
``frappe.request.query_string`` directly) and to ``GET``-only semantics
(``valid_method = frappe.request.method == "GET"``) — RFC 8058 is
POST-only, and the token here needs to be independently
generatable/verifiable (for tests, and to keep this module decoupled from
request-global state) rather than only checkable against "the current
request". So this module signs/verifies an explicit token string using the
same underlying primitive, rather than the higher-level request-coupled
helpers built on top of it.

STATELESS, TIME-LIMITED, NOT SINGLE-USE (by design)
-------------------------------------------------------
Per RFC 8058 section 5 (retry behavior): a mail client that does not get a
prompt response to its POST may legitimately retry it. A single-use/
tracked token would make the *second*, entirely legitimate retry of the
*same* URL fail or behave differently from the first — which is exactly
backwards for a "make unsubscribe reliable" feature. So a token is valid
for every POST made before its expiry, verified purely from the token's own
signed contents (no DB row to look up, consume, or race on). The
"idempotent replay" property this gives the receiver endpoint for free is
intentional, not a side effect: ``aws_integration.utils.suppression.
suppress()`` is itself idempotent (see its own docstring), so
token-replay -> repeated ``suppress()`` calls -> still exactly one
suppression record, no matter how many times a client retries.

TOKEN FORMAT
------------
``<url-safe-base64(json payload, no padding)>.<hex hmac-sha512 signature>``

The payload is intentionally small and flat (``email``/``doctype``/``name``/
``expiry``) — no nested structures, no extensibility surface that would
invite scope creep into this being a general-purpose signed-token utility.
"""

from __future__ import annotations

import base64
import hmac
import json
import time

import frappe
from frappe.utils.verified_command import _sign_message

#: Default token lifetime. Chosen to comfortably outlive any single Email
#: Queue row's retry/reporting window while still eventually expiring, per
#: the plan's "time-limited but not single-use" requirement.
DEFAULT_EXPIRY_DAYS = 45


def _b64url_encode(raw: bytes) -> str:
	return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64url_decode(value: str) -> bytes:
	padding = "=" * (-len(value) % 4)
	return base64.urlsafe_b64decode(value + padding)


def generate_unsubscribe_token(
	recipient_email,
	reference_doctype=None,
	reference_name=None,
	expiry_days=DEFAULT_EXPIRY_DAYS,
):
	"""Build a signed, URL-safe, stateless unsubscribe token.

	:param recipient_email: required — the address this token authorizes
		unsubscribing. Raises ``frappe.ValidationError`` if falsy: a token
		with no recipient is meaningless and must never be handed out.
	:param reference_doctype: optional. Together with ``reference_name``,
		scopes the eventual unsubscribe to one reference (e.g. one
		Newsletter). If either is omitted, the token decodes to a *global*
		unsubscribe request — see ``aws_integration.api.unsubscribe`` for
		how the receiver interprets that.
	:param reference_name: see ``reference_doctype``.
	:param expiry_days: how many days from now the token remains valid.

	:returns: the token string, safe to embed directly in a URL query
		parameter with no further escaping beyond normal query-string
		encoding (it uses only ``[A-Za-z0-9._-]``, all URL-safe).
	"""
	if not recipient_email:
		frappe.throw(frappe._("recipient_email is required to generate an unsubscribe token"))

	# REVISION (disposer Fix 1) — half-scoped reference guard.
	# ``reference_doctype``/``reference_name`` must be given together or not
	# at all. Exactly one truthy value used to flow through unchecked and
	# encode as e.g. {"doctype": "Student", "name": None} — which the
	# receiver (aws_integration.api.unsubscribe.one_click_unsubscribe) reads
	# back as ``is_global = not (reference_doctype and reference_name)`` ->
	# True, silently turning a caller's half-formed scoped reference into a
	# GLOBAL unsubscribe. This is the single earliest choke point every
	# caller (direct sendmail() calls, send_email_in_batches(), and this
	# module's own tests) necessarily passes through, so the guard lives
	# here rather than only at a specific call site. Safe to hard-throw:
	# the only caller of this function today,
	# ``email_headers._inject_list_unsubscribe_headers()``, already wraps
	# the call in a broad try/except that falls back to mailto:-only + a
	# logged error on any failure (see that module's docstring and
	# ``test_email_headers.test_unsubscribe_url_build_failure_does_not_raise_or_block_mailto``)
	# — so a caller mistake here degrades gracefully rather than blocking
	# mail or silently over-scoping.
	if bool(reference_doctype) != bool(reference_name):
		frappe.throw(
			frappe._(
				"reference_doctype and reference_name must both be set or both be "
				"empty — got one without the other (reference_doctype={0}, "
				"reference_name={1})."
			).format(reference_doctype, reference_name)
		)

	payload = {
		"email": recipient_email,
		"doctype": reference_doctype or None,
		"name": reference_name or None,
		"expiry": int(time.time()) + int(expiry_days) * 86400,
	}
	payload_b64 = _b64url_encode(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
	signature = _sign_message(payload_b64)
	return f"{payload_b64}.{signature}"


def verify_unsubscribe_token(token):
	"""Verify a token produced by ``generate_unsubscribe_token()``.

	Never raises — a bad, tampered, malformed, or expired token is an
	expected input from the outside world (a stale bookmarked link, a
	replayed/garbled POST, an attacker probing the endpoint), not a bug in
	this process, so every failure mode collapses to a plain ``None``
	return rather than a propagated exception.

	:returns: on success, ``{"recipient_email": str, "reference_doctype":
		str | None, "reference_name": str | None}``. ``None`` on any
		failure (bad signature, expired, malformed, wrong type, etc).
	"""
	try:
		if not token or not isinstance(token, str) or "." not in token:
			return None

		payload_b64, _, signature = token.rpartition(".")
		if not payload_b64 or not signature:
			return None

		expected_signature = _sign_message(payload_b64)
		if not hmac.compare_digest(signature, expected_signature):
			return None

		payload = json.loads(_b64url_decode(payload_b64).decode("utf-8"))

		expiry = int(payload.get("expiry", 0))
		if expiry < int(time.time()):
			return None

		email = payload.get("email")
		if not email:
			return None

		return {
			"recipient_email": email,
			"reference_doctype": payload.get("doctype") or None,
			"reference_name": payload.get("name") or None,
		}
	except Exception:
		# Malformed base64/JSON, wrong types, etc — all collapse to "not a
		# valid token" rather than propagating. See docstring.
		return None
