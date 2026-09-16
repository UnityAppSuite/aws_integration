# Copyright (c) 2026, Hybrowlabs Technologies and Contributors
# See license.txt
"""Email Suppression Entry — email-governance-engine, Part 3 (suppression gate).

This doctype is the app-owned, queryable record of "this address must not
receive mail" — distinct from (but deliberately kept in sync with, see
``aws_integration/utils/suppression.py``) core Frappe's own ``Email
Unsubscribe`` doctype, which already gates every ``frappe.sendmail()`` call
bench-wide via ``EmailQueue.get_unsubscribed_user_emails()`` /
``final_recipients()``/``final_cc()``/``final_bcc()``.

WHY A SEPARATE DOCTYPE INSTEAD OF JUST USING CORE'S ``Email Unsubscribe``
---------------------------------------------------------------------------
``Email Unsubscribe`` only carries ``email``/``reference_doctype``/
``reference_name``/``global_unsubscribe`` — there is nowhere on it to record
*why* an address was suppressed (SES complaint vs hard bounce vs a manual
admin action vs a one-click unsubscribe), *who*/*what* added it, or a
free-text provenance string for support/audit purposes. ``Email Suppression
Entry`` carries that richer, app-specific record; ``aws_integration.utils.
suppression.suppress()`` writes both — this doctype for the audit trail and
programmatic per-send filtering via ``aws_integration.utils.email.sendmail()``
(see Part 3 integration there), and a mirrored ``Email Unsubscribe`` row so
core's own universal per-send filtering also catches it, bench-wide, without
touching any other app.

EXPLICIT ``is_global`` FLAG — NOT INFERRED FROM BLANK REFERENCE FIELDS
---------------------------------------------------------------------------
Unlike a design that treats "no reference_doctype/reference_name" as
shorthand for "suppress everywhere", this doctype requires the caller to say
so explicitly via ``is_global``. Blank reference fields with
``is_global=0`` is not a supported/meaningful state — see ``validate()``
below, which enforces this exactly the way core's own ``Email Unsubscribe.
validate()`` enforces the equivalent constraint on ``global_unsubscribe``
(read that controller directly if this looks unfamiliar — it is the source
this was mirrored from).

DEDUPLICATION — EXPLICIT QUERY, NOT A DB-LEVEL UNIQUE INDEX
---------------------------------------------------------------------------
A composite unique index across ``(recipient_email, reference_doctype,
reference_name, is_global)`` is fragile in MariaDB once ``reference_doctype``/
``reference_name`` are nullable (Dynamic Link) columns: MariaDB's unique
index treats every row with a NULL in an indexed column as distinct from
every other such row, silently allowing unlimited "duplicate" global-scope
rows to pile up (since global rows deliberately leave both reference columns
NULL) while still throwing an opaque DB-level IntegrityError for the
non-global, fully-populated case. Core Frappe's own ``Email Unsubscribe``
sidesteps this by doing the duplicate check *in Python*, in ``validate()``,
with an explicit ``frappe.get_all()`` existence query, and raising
``frappe.DuplicateEntryError`` (a caller-catchable, well-known exception —
see ``aws_integration.utils.suppression.suppress()``, which relies on
catching exactly this) instead of a raw DB integrity error. This mirrors
that pattern exactly, one-for-one.
"""

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.query_builder import DocType


class EmailSuppressionEntry(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		added_by: DF.Link | None
		is_global: DF.Check
		reason: DF.Literal["Complaint", "Hard Bounce", "Manual", "Unsubscribe"]
		recipient_email: DF.Data
		reference_doctype: DF.Link | None
		reference_name: DF.DynamicLink | None
		source: DF.Data | None

	# end: auto-generated types

	def validate(self):
		self.added_by = self.added_by or frappe.session.user

		if not self.is_global and not (self.reference_doctype and self.reference_name):
			frappe.throw(
				_("Reference Document Type and Reference Name are required unless Is Global is checked"),
				frappe.MandatoryError,
			)

		if self.is_global:
			if frappe.get_all(
				"Email Suppression Entry",
				filters={
					"recipient_email": self.recipient_email,
					"is_global": 1,
					"name": ["!=", self.name],
				},
			):
				frappe.throw(
					_("{0} is already globally suppressed").format(self.recipient_email),
					frappe.DuplicateEntryError,
				)
		else:
			if frappe.get_all(
				"Email Suppression Entry",
				filters={
					"recipient_email": self.recipient_email,
					"reference_doctype": self.reference_doctype,
					"reference_name": self.reference_name,
					"is_global": 0,
					"name": ["!=", self.name],
				},
			):
				frappe.throw(
					_("{0} is already suppressed for {1} {2}").format(
						self.recipient_email, self.reference_doctype, self.reference_name
					),
					frappe.DuplicateEntryError,
				)


def get_suppressed_emails(addresses, reference_doctype=None, reference_name=None):
	"""Return the subset of ``addresses`` that are suppressed, either
	globally or for the given ``(reference_doctype, reference_name)`` scope.

	One query, not N+1 — mirrors core's
	``EmailQueue.get_unsubscribed_user_emails()`` pattern
	(``frappe/email/doctype/email_queue/email_queue.py``) exactly: a single
	``frappe.qb`` query with an ``IN`` on the candidate addresses, ANDed with
	an OR of (exact reference match) or (globally suppressed).

	:param addresses: iterable of candidate email addresses to check.
	:param reference_doctype: optional scope to check against, alongside
		``reference_name``.
	:param reference_name: see ``reference_doctype``.
	:return: list of the addresses (subset of the input) that are suppressed.
	"""
	addresses = list({address for address in (addresses or []) if address})
	if not addresses:
		return []

	EmailSuppressionEntry = DocType("Email Suppression Entry")

	suppressed = (
		frappe.qb.from_(EmailSuppressionEntry)
		.select(EmailSuppressionEntry.recipient_email)
		.where(
			EmailSuppressionEntry.recipient_email.isin(addresses)
			& (
				(
					(EmailSuppressionEntry.reference_doctype == reference_doctype)
					& (EmailSuppressionEntry.reference_name == reference_name)
				)
				| (EmailSuppressionEntry.is_global == 1)
			)
		)
		.distinct()
	).run(pluck=True)

	return suppressed or []
