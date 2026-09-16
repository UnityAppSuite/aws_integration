# Copyright (c) 2026, Hybrowlabs Technologies and Contributors
# See license.txt
"""RFC 8058 one-click unsubscribe POST receiver — email-governance-engine,
Part 5.

Answers the ``List-Unsubscribe``/``List-Unsubscribe-Post`` headers
``aws_integration.utils.email_headers`` now injects on promotional sends
(see that module's docstring, formerly "DEFERRED", now wired up here).

WHY ``allow_guest=True`` IS SAFE HERE (verified, not assumed)
------------------------------------------------------------------
This mirrors the real bench precedent for a guest-callable, state-changing
POST: ``frappe.core.doctype.user.user.update_password`` (also
``@frappe.whitelist(allow_guest=True, methods=["POST"])``) is safe as a
guest endpoint *because* it is gated by an opaque, signed, single-purpose
token (a password-reset key), not by session/CSRF — the same shape used
here (a signed unsubscribe token, see
``aws_integration.utils.unsubscribe_token``).

CSRF is structurally not a gap for either endpoint: Frappe's CSRF check
compares the request's ``X-Frappe-CSRF-Token`` header against
``frappe.session.data.csrf_token`` (see
``frappe.website.utils.is_csrf_token_valid`` / the check wired into
``frappe.app.handle_request``); a Guest session has no CSRF token (it's
tied to an authenticated session's cache entry), so CSRF validation is
already a structural no-op for *every* guest-accessible POST endpoint in
this bench, not a gap specific to this one. The thing actually standing in
for authorization here is possession of a valid, unexpired, HMAC-signed
token — exactly like ``update_password``'s reset key.

WHY THE TOKEN TRAVELS IN THE QUERY STRING, NOT THE POST BODY
-------------------------------------------------------------------
RFC 8058 fixes the POST body to the literal ``List-Unsubscribe=One-Click``
(so a compliant mail client will send exactly that body, nothing else) —
there is no room in the body for this app's own token. So the token must
be embedded in the ``List-Unsubscribe`` URL itself and read back off the
query string (``frappe.form_dict``), not the body.

WHY A BAD/EXPIRED TOKEN STILL RETURNS A PLAIN 2xx
-------------------------------------------------------
RFC 8058's whole point is "the mail client gets a fire-and-forget POST that
just works, no interaction required" — a mail client has no UI for
surfacing "your unsubscribe link expired" to the end user, and a client
retrying a truly-expired/garbled link has no way to recover regardless of
what HTTP status this endpoint returns. Raising (which
``frappe.whitelist``-wrapped views turn into a non-2xx JSON error response)
would be actively counter to that spirit and would also make a legitimate,
still-in-flight retry of a token that expires mid-flight surface as a
visible failure for no actionable reason. So: valid token -> suppress
(idempotent, see below) -> 200. Invalid/expired/malformed token -> still
200 (nothing recorded, silently a no-op) — logged server-side (for our own
visibility into abuse/expiry patterns) but never surfaced to the caller as
an error. This is a deliberate choice, not an oversight — flagged here for
the disposer to weigh against "should an invalid token 404/400 instead" for
observability/abuse-detection reasons.

IDEMPOTENCY
-----------
A valid token can be POSTed any number of times (see
``unsubscribe_token``'s own docstring on why tokens are stateless/
not single-use) and must produce the exact same end state every time:
``aws_integration.utils.suppression.suppress()`` already swallows its own
``DuplicateEntryError`` on both doctypes it writes (see that module's
docstring), so replaying this endpoint with the same token is a no-op
after the first successful call — no error, no duplicate rows. See
``test_unsubscribe.py`` for the proof.
"""

from __future__ import annotations

import frappe
from frappe import _
from frappe.rate_limiter import rate_limit

from aws_integration.utils.suppression import suppress
from aws_integration.utils.unsubscribe_token import verify_unsubscribe_token

#: Generous enough to absorb legitimate mail-client retries of the same
#: link (RFC 8058 explicitly allows retrying), tight enough to blunt a
#: POST-flood against this endpoint. IP-based (the decorator's default),
#: since Guest requests carry no other stable identity to key on.
RATE_LIMIT_COUNT = 30
RATE_LIMIT_WINDOW_SECONDS = 60 * 60


@frappe.whitelist(allow_guest=True, methods=["POST"])
@rate_limit(limit=RATE_LIMIT_COUNT, seconds=RATE_LIMIT_WINDOW_SECONDS)
def one_click_unsubscribe(token=None, **kwargs):
	"""RFC 8058 one-click unsubscribe POST target.

	:param token: the signed unsubscribe token, read from the query string
		(``frappe.form_dict`` — see module docstring for why it can't be in
		the POST body). Falls back to ``frappe.form_dict.get("token")`` in
		case the caller (or a test) posted it as a form field instead of a
		querystring param — both are accepted, see ``test_unsubscribe.py``.
	:param kwargs: swallows any other querystring/form params a mail
		client or proxy may add (e.g. cache-busting params) without this
		view breaking on an unexpected keyword.

	Always responds with a plain 2xx — see module docstring "WHY A BAD/
	EXPIRED TOKEN STILL RETURNS A PLAIN 2xx".
	"""
	token = token or frappe.form_dict.get("token")

	decoded = verify_unsubscribe_token(token)
	if not decoded:
		frappe.logger("aws_integration").info(
			"one_click_unsubscribe: rejected invalid/expired/malformed token "
			"from %s", frappe.local.request_ip
		)
		return _ok_response()

	email = decoded["recipient_email"]
	reference_doctype = decoded.get("reference_doctype")
	reference_name = decoded.get("reference_name")
	# A token minted without a reference (see unsubscribe_token docstring)
	# is a global unsubscribe; one minted with both is scoped to that one
	# reference — mirrors suppress()'s own mutually-exclusive scoping rule.
	is_global = not (reference_doctype and reference_name)

	try:
		suppress(
			email,
			reason="Unsubscribe",
			source="One-Click Unsubscribe",
			reference_doctype=reference_doctype,
			reference_name=reference_name,
			is_global=is_global,
		)
	except Exception:
		# Never let a downstream failure (e.g. suppress()'s own reference
		# validation) surface as a non-2xx to the mail client — see module
		# docstring. Logged for our own visibility; the client sees a plain
		# success either way, per the RFC 8058 "no interaction" contract.
		frappe.log_error(
			title=_("one_click_unsubscribe: failed to record suppression"),
			message=frappe.get_traceback(),
		)

	return _ok_response()


def _ok_response():
	"""A minimal, always-2xx JSON body. RFC 8058 does not require any
	particular response body/content-type — mail clients treat any 2xx as
	"unsubscribe accepted" and do not render the response to the user."""
	frappe.local.response["http_status_code"] = 200
	return {"message": "ok"}
