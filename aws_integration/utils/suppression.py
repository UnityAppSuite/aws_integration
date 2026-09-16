# Copyright (c) 2026, Hybrowlabs Technologies and Contributors
# See license.txt
"""Email governance suppression gate — email-governance-engine, Part 3.

Public API surface for checking/recording/removing email-address
suppression. Backed by two doctypes, written together and kept in sync by
this module:

  1. ``Email Suppression Entry`` (this app, ``aws_integration.aws_integration.
     doctype.email_suppression_entry`` — see that module's docstring for the
     full design rationale) — the audit-trail record (why/who/when/source),
     and the source of truth this app's own ``sendmail()`` filters against
     (see Part 3 integration in ``aws_integration/utils/email.py``).
  2. Core Frappe's ``Email Unsubscribe`` — mirrored here on every
     ``suppress()``/``unsuppress()`` call so core's own, ALREADY-WIRED,
     bench-wide per-send filtering (``EmailQueue.get_unsubscribed_user_emails()``
     / ``final_recipients()`` / ``final_cc()`` / ``final_bcc()``, which fires
     on *every* ``frappe.sendmail()`` call in this bench, not just this
     app's) also honors the suppression. This is the deliberate design
     decision that gets bench-wide coverage (every app's OTP-unrelated,
     bulk/notice-style sends) without monkey-patching core or touching any
     other app's code.

Both writes are individually idempotent: a caller suppressing an
already-suppressed address is a legitimate double-suppress (e.g. two SES
complaint notifications for the same address), not an error — each write
catches its own doctype's ``frappe.DuplicateEntryError`` and moves on.

ATOMICITY — both writes happen inside one DB savepoint (see ``suppress()``):
if the second write (``Email Unsubscribe``) raises anything other than its
own ``DuplicateEntryError`` (for example core's ``Email Unsubscribe.
on_update()`` — see its side-effect note on ``suppress()`` below — raising
``frappe.DoesNotExistError`` because ``reference_name`` doesn't actually
exist), the savepoint is rolled back so the first write (``Email
Suppression Entry``) is undone too, and the original exception is
re-raised. After ``suppress()`` returns or raises, either both doctype rows
exist or neither does — never a partial write.

SIDE EFFECT — every non-global ``suppress()`` call, via the mirrored
``Email Unsubscribe`` insert, triggers core's ``Email Unsubscribe.
on_update()``, which adds a visible ``"Left this conversation"`` comment
directly onto the referenced document (``reference_doctype``/
``reference_name``) itself. This is expected, correct core behavior, not a
bug introduced here — flagged so it isn't a surprise to a future reader.
"""

import random
import string

import frappe
from frappe import _

from aws_integration.aws_integration.doctype.email_suppression_entry.email_suppression_entry import (
	get_suppressed_emails,
)

__all__ = ["is_suppressed", "suppress", "unsuppress", "get_suppressed_emails"]


def is_suppressed(email, reference_doctype=None, reference_name=None):
	"""Return True if ``email`` is suppressed — either globally, or scoped
	to ``(reference_doctype, reference_name)`` when given.

	Thin, single-address convenience wrapper around
	``get_suppressed_emails()`` (the batch-shaped lookup
	``aws_integration.utils.email.sendmail()`` actually uses) for callers
	that only have one address to check.
	"""
	if not email:
		return False
	return bool(
		get_suppressed_emails(
			[email], reference_doctype=reference_doctype, reference_name=reference_name
		)
	)


def suppress(
	email,
	reason,
	source=None,
	reference_doctype=None,
	reference_name=None,
	is_global=False,
):
	"""Record ``email`` as suppressed, both in this app's ``Email
	Suppression Entry`` (audit trail + this app's own filtering) and in
	core's ``Email Unsubscribe`` (bench-wide per-send filtering — see module
	docstring).

	:param email: the address to suppress. No-op if falsy.
	:param reason: one of ``Email Suppression Entry``'s ``reason`` Select
		options (``Complaint``/``Hard Bounce``/``Manual``/``Unsubscribe``).
	:param source: optional free-text provenance (e.g. ``"SES Feedback"``,
		``"One-Click Unsubscribe"``, ``"Admin Portal"``).
	:param reference_doctype: required (together with ``reference_name``)
		unless ``is_global=True`` — see ``Email Suppression Entry.validate()``
		for why a scoped, non-global suppression with no reference is not a
		supported state. Ignored (and cleared) when ``is_global=True``: a
		global suppression is never also reference-scoped, by design — the
		two are mutually exclusive on both doctypes.
	:param reference_name: see ``reference_doctype``.
	:param is_global: if True, suppress this address for every
		reference/every send, not just one doctype+name.

	ATOMICITY: both writes are wrapped in one DB savepoint (see module
	docstring's "ATOMICITY" note). If either write raises anything other
	than its own ``DuplicateEntryError``, both are rolled back and the
	exception is re-raised — never a partial write across the two
	doctypes, regardless of what the caller does with the exception
	afterward.

	SIDE EFFECT: a non-global suppression adds a visible comment on the
	referenced document itself — see module docstring's "SIDE EFFECT" note.
	"""
	if not email:
		return

	is_global = bool(is_global)

	if is_global:
		# A global suppression is never also reference-scoped on either
		# doctype (mirrors core's Email Unsubscribe: global_unsubscribe=1
		# rows carry no reference). Any reference_doctype/reference_name the
		# caller passed alongside is_global=True is dropped rather than
		# silently kept, so the two doctypes can't end up storing different
		# scopes for what is supposed to be the same suppression event.
		reference_doctype = None
		reference_name = None
	elif not (reference_doctype and reference_name):
		frappe.throw(
			_(
				"reference_doctype and reference_name are required to suppress {0} "
				"unless is_global=True"
			).format(email)
		)

	# PARTIAL-WRITE FIX (post-review): both inserts happen inside one
	# savepoint. Each write still individually swallows its own
	# DuplicateEntryError (legitimate double-suppress, see module
	# docstring) — but ANY OTHER exception from either write (most
	# notably: core's Email Unsubscribe.on_update() raising
	# frappe.DoesNotExistError when reference_name doesn't exist — a real,
	# deterministic trigger, not just a theoretical race) rolls back to
	# the savepoint, undoing the first write too, before re-raising. This
	# guarantees "both rows exist or neither does" independent of what the
	# caller does with the re-raised exception.
	save_point = "aws_integration_suppress_" + "".join(random.choices(string.ascii_lowercase, k=10))
	frappe.db.savepoint(save_point)
	try:
		try:
			frappe.get_doc(
				{
					"doctype": "Email Suppression Entry",
					"recipient_email": email,
					"reason": reason,
					"source": source,
					"reference_doctype": reference_doctype,
					"reference_name": reference_name,
					"is_global": 1 if is_global else 0,
				}
			).insert(ignore_permissions=True)
		except frappe.DuplicateEntryError:
			pass

		try:
			frappe.get_doc(
				{
					"doctype": "Email Unsubscribe",
					"email": email,
					"reference_doctype": reference_doctype,
					"reference_name": reference_name,
					"global_unsubscribe": 1 if is_global else 0,
				}
			).insert(ignore_permissions=True)
		except frappe.DuplicateEntryError:
			pass
	except Exception:
		frappe.db.rollback(save_point=save_point)
		raise
	else:
		frappe.db.release_savepoint(save_point)


def unsuppress(email, reference_doctype=None, reference_name=None, is_global=None):
	"""Admin correction path — remove ``email``'s suppression from both
	doctypes.

	:param is_global: explicit, for symmetry with ``suppress()``'s own
		``is_global`` kwarg (readability nit from review — the previous
		implicit inference was not a correctness bug, since the underlying
		filters are exact-match with no ambiguity risk, just less
		self-documenting). Defaults to ``None``, which preserves the
		original inferred behavior: scoped (``is_global=False``) when both
		``reference_doctype``/``reference_name`` are given, global
		(``is_global=True``) otherwise. Passing an explicit ``True``/
		``False`` overrides the inference the same way.

	Scope is otherwise inferred from whether ``reference_doctype``/
	``reference_name`` are both given: if so, only the matching *scoped*
	(non-global) entries are removed; if not, only *global* entries are
	removed. This mirrors ``suppress()``'s own scoping rule (global and
	scoped are mutually exclusive, never mixed) so an admin correcting a
	scoped suppression cannot accidentally remove an unrelated global one,
	or vice versa.

	No-op (not an error) if no matching entry exists in either doctype.
	"""
	if not email:
		return

	if is_global is None:
		is_global = not (reference_doctype and reference_name)
	is_global = bool(is_global)

	if not is_global and reference_doctype and reference_name:
		entry_filters = {
			"recipient_email": email,
			"reference_doctype": reference_doctype,
			"reference_name": reference_name,
			"is_global": 0,
		}
		unsub_filters = {
			"email": email,
			"reference_doctype": reference_doctype,
			"reference_name": reference_name,
			"global_unsubscribe": 0,
		}
	else:
		entry_filters = {"recipient_email": email, "is_global": 1}
		unsub_filters = {"email": email, "global_unsubscribe": 1}

	for name in frappe.get_all("Email Suppression Entry", filters=entry_filters, pluck="name"):
		frappe.delete_doc("Email Suppression Entry", name, ignore_permissions=True)

	for name in frappe.get_all("Email Unsubscribe", filters=unsub_filters, pluck="name"):
		frappe.delete_doc("Email Unsubscribe", name, ignore_permissions=True)
