import re
import time
import frappe
from email.utils import parseaddr
from frappe import _, cint, msgprint
from itertools import islice

from frappe.email.queue import (
    EMAIL_QUEUE_BATCH_FAILURE_THRESHOLD_COUNT,
    EMAIL_QUEUE_BATCH_FAILURE_THRESHOLD_PERCENT,
    get_queue,
)
from frappe.utils import now_datetime
from frappe.utils import validate_email_address as _frappe_validate_email_address

from aws_integration.utils.email_headers import mark_promotional_headers
from aws_integration.aws_integration.doctype.email_suppression_entry.email_suppression_entry import (
    get_suppressed_emails,
)


class SESDestination:
    """Contains data about an email destination.

    NOTE (transport consolidation, Part 1): no longer used internally by
    ``sendmail()`` — that function now builds recipient/cc/bcc as plain
    lists for ``frappe.sendmail()`` instead of a boto3 SES ``Destination``
    payload. Kept in place only because it is part of this module's public
    surface (``aws_integration/utils/__init__.py`` does
    ``from aws_integration.utils.email import *``) and nothing in this bench
    was found to import it directly (checked via repo-wide grep), so
    removing it is a judgment call rather than a hard requirement. If a
    future audit confirms no external references, this class can be
    deleted.
    """

    def __init__(self, tos, ccs=None, bccs=None):
        self.tos = tos
        self.ccs = ccs
        self.bccs = bccs

    def to_service_format(self):
        svc_format = {"ToAddresses": self.tos}
        if self.ccs:
            svc_format["CcAddresses"] = self.ccs
        if self.bccs:
            svc_format["BccAddresses"] = self.bccs
        return svc_format


def validate_email(email):
    """Validate a single email address.

    ISS-28: previously a hand-rolled regex (``[^@]+@[^@]+\\.[^@]+``) that
    both under- and over-accepted addresses (e.g. it happily matched
    ``"a@b@c.d"`` and rejected some valid-but-unusual addresses). This now
    delegates to Frappe core's own ``frappe.utils.validate_email_address``
    — the same validator that core ``frappe.sendmail()`` / the Email Queue
    apply when the message is actually built and sent, so an address that
    passes here is guaranteed to also be accepted downstream.

    REGRESSION FIX (post-review, round 2): core's ``validate_email_address``
    parses its input with ``email.utils.parseaddr`` under the hood, which is
    deliberately lenient about *header-style* input (e.g. picking one address
    out of ``"Name <a@b>, other@x"``). For a single malformed multi-``@``
    address like ``"a@b@c.com"`` — or even the bracketed
    ``"Name <a@b@c.com>"`` — this leniency means core happily truncates it to
    a syntactically-valid tail (``"b@c.com"``) and returns that as truthy,
    instead of rejecting the malformed input outright. The OLD hand-rolled
    regex correctly rejected these. We do NOT want to revert to a hand-rolled
    regex (that duplicated validation logic is exactly ISS-28's original
    complaint) — instead we add one narrow, idiomatic guard in front of core:
    reject up front when ``email.utils.parseaddr`` (the same stdlib parser
    core itself uses) cannot extract a usable address from the string at all.
    Empirically this is precisely what happens for every multi-``@``
    malformed variant we tried (``"a@b@c.com"``, ``"Name <a@b@c.com>"``,
    ``"<a@b@c.com>"``, ``"user@"``) — ``parseaddr`` returns an empty address
    part for all of them — while every legitimate address (plain, plus-tagged,
    quoted-local-part, or ``"Name <addr>"`` form) still parses to a non-empty
    address and falls through to core's validator unchanged. So this guard
    only removes cases core would have mishandled; it changes nothing for
    every input the old test suite already exercised as valid or invalid.

    Signature (single string in, bool out) is unchanged so every existing
    caller (AWS Settings.validate(), this module) needs no changes.
    """
    if not email:
        return False
    _, addr = parseaddr(email)
    if not addr:
        return False
    return bool(_frappe_validate_email_address(email, throw=False))


def is_html(text):
    """Regular expression to check for HTML tags.

    No longer used internally by ``sendmail()`` (frappe.sendmail()/its MIME
    builder decides HTML-vs-text handling itself — see the docstring on
    ``sendmail()`` for why that is actually the fix for a real bug in the
    old boto3 path). Kept for backward compatibility of this module's
    public import surface.
    """
    html_pattern = re.compile(r"<([a-zA-Z]+)[^>]*>(.*?)</\1>|<([a-zA-Z]+)[^>]*>")
    return bool(html_pattern.search(text))


def chunk(iterable, size):
    """Yield successive chunks of a specified size from an iterable."""
    iterator = iter(iterable)
    for first in iterator:
        yield [first, *islice(iterator, size - 1)]


def _as_address_list(value):
    """Coerce a recipient-ish argument (None / str / list / tuple) into a
    clean list of non-empty address strings.

    Every existing caller passes either ``None``, a single string, or a
    list of strings for recepient/cc_recepient/bcc_recepient/reply_tos —
    this normalizes all three shapes so downstream code has one thing to
    deal with.
    """
    if not value:
        return []
    if isinstance(value, str):
        return [value] if value.strip() else []
    return [v for v in value if v]


def _record_ses_log(subject, message, sender, recipients, cc, bcc, status):
    """Write an "AWS SES Logs" record for one send attempt.

    OBSERVABILITY FIX (post-review, round 2): the old ``AWS Settings.send_email()``
    (Route B, now dead — it throws immediately) called
    ``AWS Settings.add_ses_logs()`` after every successful boto3 send. Nothing
    in the new ``sendmail()`` (Route A, enqueue-through-core) replaced that
    call, so "AWS SES Logs" silently stopped being populated. Two things in
    this bench depend on that doctype carrying real rows:
      - edu_quality's ``permission_import.py`` ships permission rules scoped
        to "AWS SES Logs" as a Reference Document Type — a doctype nobody
        ever writes to makes those permission rows dead weight and (for
        anyone actually relying on the log for support/debugging) hides
        real send activity behind an always-empty list view.
      - unity_dev_tools's PII-anonymization tooling (``anonymize_core.py``)
        truncates "tabAWS SES Logs" as one of its integration-payload tables,
        on the explicit assumption that production sites accumulate real
        rows there that need scrubbing.
    This restores that population, using the doctype's existing fields only
    (no schema change) — same field set the old ``add_ses_logs()`` wrote to
    (from/subject/message/status/recepients/cc_recepients/bcc_recepients).
    ``message_id`` is intentionally left blank: Route A enqueues into the
    Email Queue (``delayed=True``) and does not hand back a provider message
    id synchronously the way the old direct boto3 call did, so there is
    nothing genuine to put there.

    DECISION — log failures too, not just successes (unlike the old code,
    which only ever logged after a *successful* boto3 send and never wrote a
    row for a failure): ``send_email_in_batches()`` deliberately swallows
    individual send failures per-item (frappe.log_error only, no doctype
    trace) so one bad row doesn't abort a batch. Without a log entry here,
    those swallowed failures leave literally no trace in "AWS SES Logs" for
    someone auditing "did this notice go out" — only in the generic Error
    Log, keyed by traceback rather than by recipient/subject. Logging the
    failed attempt too (status "Not Sent", reusing the doctype's existing
    Select options instead of inventing a new status) makes this an
    observability improvement over the old behavior, not just parity, at no
    schema cost. This never raises itself — a logging failure must never mask
    or replace the real send outcome for the caller.
    """
    try:
        log = frappe.get_doc(
            {
                "doctype": "AWS SES Logs",
                "subject": subject,
                "message": message,
                "status": status,
                "from": sender,
            }
        )
        log.recepients = ", ".join(recipients or [])
        log.cc_recepients = ", ".join(cc or [])
        log.bcc_recepients = ", ".join(bcc or [])
        log.insert(ignore_permissions=True)
    except Exception:
        frappe.log_error(
            title=_("AWS Integration: failed to write AWS SES Logs record"),
            message=frappe.get_traceback(),
        )


def sendmail(
    subject,
    message,
    recepient,
    cc_recepient,
    bcc_recepient,
    reply_tos=None,
    promotional=False,
    reference_doctype=None,
    reference_name=None,
):
    """Send a single email.

    PART 3 ADDITION — email-governance-engine suppression gate
    -------------------------------------------------------------
    Immediately after the recepient/cc/bcc lists are normalized into plain
    lists (``_as_address_list()``) and BEFORE the "at least one recipient"
    check below, every address across all three lists is checked against
    ``Email Suppression Entry`` (``aws_integration.aws_integration.doctype.
    email_suppression_entry.get_suppressed_emails()`` — one query, not N+1)
    scoped to this call's ``reference_doctype``/``reference_name`` (or
    globally suppressed, regardless of scope). Any suppressed address is
    silently dropped from whichever of ``recipients``/``cc``/``bcc`` it was
    in before the send is ever attempted.

    This is a real bug fix to the "no recipient" check, not just an
    addition: if ``recipients`` (the To list) was non-empty before
    suppression filtering but becomes empty because every one of them was
    suppressed, this is treated as a quiet, successful no-op — logged at
    info level, no ``frappe.throw()`` — since "every intended recipient
    opted out / bounced / complained" is an expected, non-exceptional
    outcome of a governance-aware system, not a caller error. If
    ``recipients`` was empty for any OTHER reason (the caller genuinely
    passed no recipient to begin with), the pre-existing
    ``frappe.throw()`` behavior is unchanged — that is still a caller bug.
    ``cc``/``bcc`` suppression is applied the same way but never triggers
    the no-op path by itself (only the To list does, matching the
    pre-existing "at least one recipient" semantics, which never looked at
    cc/bcc either).

    No ``AWS SES Logs`` row is written for the all-suppressed no-op case:
    that doctype's ``status`` Select only has ``Not Sent``/``Sent``, both of
    which already carry a specific meaning (attempted-and-failed /
    attempted-and-enqueued) that "never attempted, intentionally skipped"
    would conflate with. Extending that Select is a plausible follow-up but
    is a schema change to an unrelated doctype outside Part 3's stated
    scope (``Email Suppression Entry`` only) — flagged for the disposer
    rather than done unilaterally here.

    PART 2 ADDITION — email-governance-engine header injection
    -------------------------------------------------------------
    ``promotional`` (default ``False``, i.e. safe/opt-in — see
    ``aws_integration/utils/email_headers.py`` module docstring for why
    this MUST default to False and never be inferred) marks this specific
    send as promotional. When True, the ``make_email_body_message`` hook
    (``aws_integration.utils.email_headers.inject_governance_headers``)
    injects a ``List-Unsubscribe`` header onto the built message — never
    otherwise, and never for OTP/password-reset/any other mail that
    doesn't explicitly opt in here.

    ``reference_doctype``/``reference_name`` are optional and only matter
    when ``promotional=True``: if given, they let the hook build a working
    ``https:`` List-Unsubscribe link via core's existing (GET-only, see
    module docstring "DEFERRED" section) unsubscribe endpoint, which
    requires a doctype+name to record the opt-out against. If omitted, the
    hook still adds a ``mailto:`` fallback, just not the ``https:`` form.
    These are NOT passed through to ``frappe.sendmail()``'s own
    ``reference_doctype``/``reference_name`` params (core's built-in
    unsubscribe-link-in-footer mechanism is a separate, orthogonal feature
    from this header-injection mechanism — deliberately left untouched
    here) — they are threaded only as marker headers for this hook to read.

    SCOPE NOTE: no real caller in this bench passes ``promotional=True``
    yet. This adds the CAPABILITY only — activating it on a real send (e.g.
    edu_quality/unity_parent_app School Notice) is out of this change's
    authorization; see the PROPOSER report for Part 2.

    TRANSPORT CONSOLIDATION (Part 1) — Route B -> Route A
    -------------------------------------------------------
    This used to build a raw boto3 ``sesv2.send_email()`` call via
    ``AWS Settings.send_email()`` ("Route B"), completely bypassing
    Frappe's Email Queue, its MIME builder, and every core email hook.
    It now enqueues through core ``frappe.sendmail()`` ("Route A"), which
    is what every other Frappe/Unity email already goes through.

    WHY this is a real fix, not just a refactor: Route B's hand-rolled SES
    "Simple" payload put the body under *either* ``Content.Simple.Body.Text``
    *or* ``Content.Simple.Body.Html`` (see the old ``is_html()`` branch) —
    never both. SES therefore sent a single-part message instead of a
    correct ``multipart/alternative`` (html + plain-text fallback). Core
    Frappe's MIME builder (``frappe/email/email_body.py``) already handles
    this correctly for every other send path in the framework, so folding
    this into Route A makes the multipart/alternative problem moot without
    any extra code here. It also means ``make_email_body_message`` and any
    other core email hooks now fire for these sends too — previously they
    never did, because Route B never touched core's email pipeline at all.

    Actual delivery (which transport moves mail out of the Email Queue, and
    at what pace) is decided downstream, by the *existing*
    ``AWSSettings.handle_email_flush()`` toggle:
      - ``enable_aws`` on  -> queue flushed by
        ``aws_integration.utils.email.flush_email_queue`` (rate-paced)
      - ``enable_aws`` off -> queue flushed by Frappe's own
        ``frappe.email.queue.flush``
    This function does not need to know or care which one is active; it
    only builds and enqueues the message. That toggle is untouched by this
    change.

    Signature is backward compatible with the pre-existing implementation
    (``subject, message, recepient, cc_recepient, bcc_recepient,
    reply_tos=None``) — every existing caller (edu_quality's
    ``walsh/admin.py`` School Notice code, unity_parent_app's
    ``api/admin.py`` School Notice code) needs zero changes; Part 2 only
    appends new optional, safely-defaulted keyword arguments
    (``promotional``, ``reference_doctype``, ``reference_name``) after the
    Part 1 signature.

    Known behavior differences from Route B (see PROPOSER report for full
    discussion):
      - Reply-To: Route B accepted a *list* of reply-to addresses
        (``ReplyToAddresses``). Core ``frappe.sendmail()`` only supports a
        single Reply-To string. If more than one is given here, we use the
        first and log the rest being dropped — no existing caller passes
        more than one today (confirmed by repo-wide grep), so this is a
        latent-only difference.
      - Failure semantics: Route B's boto3 call happened synchronously and
        any exception was caught-and-logged inside
        ``AWS Settings.send_email()`` (it never raised to the caller).
        Route A is enqueue-only here (``delayed=True``); actual delivery
        failures now surface later, per-item, during queue flush (see
        ``flush_email_queue``), which already has its own try/except and
        error logging. What *does* raise synchronously now is input
        validation (missing recipient, invalid address) — deliberately,
        since silently dropping a bad send was arguably the original bug.
        ``send_email_in_batches()`` below wraps each item in try/except so
        one bad address cannot abort the rest of a batch, preserving the
        fault-tolerant, "one bad row doesn't block the rest" behavior the
        existing callers rely on.
    """
    # REVISION (disposer Fix 1) — second, earlier layer of the same
    # half-scoped-reference guard also enforced in
    # ``unsubscribe_token.generate_unsubscribe_token()``. Catching a caller's
    # mistake here — right at the public entrypoint every direct
    # ``sendmail()`` call goes through — gives a more actionable error
    # (naming this function, not the internal token module three layers
    # down) and rejects it before it ever reaches ``mark_promotional_headers()``
    # / ``email_headers.py`` at all. reference_doctype/reference_name must be
    # given together (a fully-scoped reference) or both omitted (a global
    # send) — never exactly one, which would otherwise silently decode, at
    # the unsubscribe receiver, to a GLOBAL unsubscribe instead of the
    # caller's intended scoped one.
    if bool(reference_doctype) != bool(reference_name):
        frappe.throw(
            _(
                "reference_doctype and reference_name must both be provided together, "
                "or both left empty — got only one of them (reference_doctype={0}, "
                "reference_name={1})."
            ).format(reference_doctype, reference_name)
        )

    recipients = _as_address_list(recepient)
    cc = _as_address_list(cc_recepient)
    bcc = _as_address_list(bcc_recepient)
    reply_tos = _as_address_list(reply_tos)

    # PART 3 — suppression gate. See docstring above for the "quiet
    # no-op when everyone was suppressed" vs. "throw on genuinely-empty
    # input" distinction this deliberately preserves/changes.
    recipients_before_suppression = list(recipients)
    all_addresses = list({*recipients, *cc, *bcc})
    suppressed = set()
    if all_addresses:
        suppressed = set(
            get_suppressed_emails(
                all_addresses, reference_doctype=reference_doctype, reference_name=reference_name
            )
        )
        if suppressed:
            recipients = [addr for addr in recipients if addr not in suppressed]
            cc = [addr for addr in cc if addr not in suppressed]
            bcc = [addr for addr in bcc if addr not in suppressed]

    if not recipients:
        if recipients_before_suppression:
            # OBSERVABILITY FIX (post-review): log the actual suppressed
            # addresses (from the To list specifically — the ones that
            # caused this no-op), not just a count, so an admin
            # investigating "why didn't Student X get this notice" can
            # grep the log for the address itself.
            suppressed_recipients = sorted(
                addr for addr in recipients_before_suppression if addr in suppressed
            )
            frappe.logger("aws_integration").info(
                "sendmail(): all %d recipient(s) for subject %r were suppressed "
                "(Email Suppression Entry / Email Unsubscribe) — skipping send, "
                "not an error. Suppressed addresses: %s",
                len(recipients_before_suppression),
                subject,
                suppressed_recipients,
            )
            return None
        frappe.throw(_("At least one recipient email address is required to send an email."))

    invalid = [addr for addr in (*recipients, *cc, *bcc) if not validate_email(addr)]
    if invalid:
        frappe.throw(
            _("Cannot send email — invalid email address(es): {0}").format(", ".join(invalid))
        )

    settings = frappe.get_cached_doc("AWS Settings")
    sender = None
    if settings.source_email:
        sender = (
            f"{settings.sender_name} <{settings.source_email}>"
            if settings.sender_name
            else settings.source_email
        )

    reply_to = None
    if reply_tos:
        reply_to = reply_tos[0]
        if len(reply_tos) > 1:
            frappe.logger("aws_integration").info(
                "sendmail(): multiple reply_tos %s given; core frappe.sendmail() "
                "only supports a single Reply-To address, using %r and dropping the rest.",
                reply_tos,
                reply_to,
            )

    email_headers = None
    if promotional:
        email_headers = mark_promotional_headers(
            reference_doctype=reference_doctype, reference_name=reference_name
        )

    try:
        result = frappe.sendmail(
            recipients=recipients,
            sender=sender,
            subject=subject,
            message=message,
            cc=cc,
            bcc=bcc,
            reply_to=reply_to,
            delayed=True,
            email_headers=email_headers,
        )
    except Exception:
        _record_ses_log(subject, message, sender, recipients, cc, bcc, status="Not Sent")
        raise

    _record_ses_log(subject, message, sender, recipients, cc, bcc, status="Sent")
    return result


def send_email_in_batches(data, promotional=False, reference_doctype=None, reference_name=None):
    """
    Structure of data:
    {
        "key_name": {
            "subject": "Subject",
            "content": "Content",
            "recepients": [],
            "cc_recepients": [],
            "bcc_recepients": [],
            "reply_tos": [],
            "promotional": False,
            "reference_doctype": None,
            "reference_name": None,
        }
    }

    PART 2 ADDITION — ``promotional``/``reference_doctype``/``reference_name``
    -------------------------------------------------------------------------
    Same meaning as on ``sendmail()`` (see its docstring) — default
    ``promotional=False`` (safe/opt-in), threaded through to every item's
    ``sendmail()`` call so the whole batch shares one classification. Any
    individual item's dict may override this per-item via its own
    ``"promotional"``/``"reference_doctype"``/``"reference_name"`` keys
    (falls back to this function's arguments when the item omits them) —
    useful when a single batch mixes both kinds of mail, though no existing
    caller does this today (see SCOPE NOTE on ``sendmail()``).

    Batching / rate-limiting behavior is preserved from the pre-existing
    implementation: entries are chunked by ``AWS Settings.email_batch_size``
    and there is a ``time.sleep(1)`` pause between chunks. We looked at two
    unmerged prior-art branches before deciding to keep this simple:
      - ``unity-org/ses-fix``: a Redis-backed per-second token-bucket
        (``SESRateLimiter``).
      - ``unity-org/feat/rate-paced-ses-batch-email``: a
        ``ThreadPoolExecutor`` + evenly-spaced-slot pacer sending raw boto3
        in parallel worker threads.
    Neither was cherry-picked wholesale: both are built around firing SES
    directly (boto3), which is exactly the Route B pattern this change
    retires — porting either rate limiter as-is would mean re-introducing a
    parallel, un-queued send path. A Redis- or thread-pool-based pacer for
    the *queued* (Route A) path is a reasonable Part 2 follow-up, but is out
    of scope for "transport consolidation."

    Two deliberate behavior changes vs. the pre-existing implementation,
    both called out here since a caller-facing docstring is the place a
    future reader will look:
      1. ``reply_tos`` is now actually passed through to ``sendmail()``.
         The old code accepted a ``reply_tos`` key in the per-item dict (see
         the structure comment above, which already documented it) but
         never read it — a latent bug. Fixed here since it costs nothing to
         fix and matches the documented contract.
      3. PART 3 ADDITION — ``reference_name`` convenience default: if a
         ``reference_doctype`` is set (batch-level default or per-item
         override) but ``reference_name`` is not, ``reference_name``
         defaults to the item's own dict key (the ``key`` this loop already
         iterates over). Real callers in this bench (edu_quality's and
         unity_parent_app's School Notice functions) key their per-item
         dicts by Student ID but do not currently pass ``reference_name``
         explicitly — this means, once those callers are updated (separate,
         not-yet-authorized work, out of Part 3's scope) to pass only
         ``reference_doctype="Student"`` at the batch level, per-item
         suppression scoping (and List-Unsubscribe scoping, Part 2) works
         automatically without every call site also having to thread
         ``reference_name`` through by hand. This default only fires when
         ``reference_doctype`` is set and ``reference_name`` is not — a
         batch with no reference_doctype at all (the common case today)
         behaves exactly as before.
      2. Each item is now wrapped in its own try/except. ``sendmail()`` can
         raise synchronously now (see its docstring) where the old boto3
         path never did; this restores the "one bad row doesn't abort the
         batch" behavior the callers (edu_quality / unity_parent_app School
         Notice bulk-send loops) rely on structurally, even though none of
         them wrap this call in a try/except themselves.
    """
    email_batch_size = cint(
        frappe.get_value("AWS Settings", "AWS Settings", "email_batch_size")
    ) or 1

    for group in chunk(list(data.keys()), email_batch_size):
        for key in group:
            item = data[key]
            item_reference_doctype = item.get("reference_doctype", reference_doctype)
            item_reference_name = item.get("reference_name", reference_name)
            if item_reference_doctype and not item_reference_name:
                # PART 3 — see docstring point 3: default reference_name to
                # this item's own dict key when a reference_doctype is set
                # but no explicit reference_name was given.
                item_reference_name = key
            try:
                sendmail(
                    item.get("subject"),
                    item.get("content"),
                    item.get("recepients"),
                    item.get("cc_recepients"),
                    item.get("bcc_recepients"),
                    item.get("reply_tos"),
                    promotional=item.get("promotional", promotional),
                    reference_doctype=item_reference_doctype,
                    reference_name=item_reference_name,
                )
            except Exception:
                frappe.log_error(
                    title=_("AWS Integration: send_email_in_batches failed for {0}").format(key),
                    message=frappe.get_traceback(),
                )
        time.sleep(1)


def flush_email_queue():
    """flush email queue, every time: called from scheduler.

    This should not be called outside of background jobs.
    """
    from frappe.email.doctype.email_queue.email_queue import EmailQueue

    # To avoid running jobs inside unit tests
    if frappe.are_emails_muted():
        msgprint(_("Emails are muted"))

    if cint(frappe.db.get_default("suspend_email_queue")) == 1:
        return
    
    email_batch_size = frappe.get_value(
        "AWS Settings", "AWS Settings", "email_batch_size"
    )

    email_queue_batch = get_queue(email_batch_size)
    if not email_queue_batch:
        return

    failed_email_queues = []
    for data in chunk(email_queue_batch, cint(email_batch_size)):
        for row in data:
            try:
                email_queue: EmailQueue = frappe.get_doc("Email Queue", row.name)
                email_queue.send()
            except Exception:
                frappe.get_doc("Email Queue", row.name).log_error()
                failed_email_queues.append(row.name)

                if (
                    len(failed_email_queues) / len(email_queue_batch)
                    > EMAIL_QUEUE_BATCH_FAILURE_THRESHOLD_PERCENT
                    and len(failed_email_queues) > EMAIL_QUEUE_BATCH_FAILURE_THRESHOLD_COUNT
                ):
                    frappe.throw(
                        _("Email Queue flushing aborted due to too many failures.")
                    )
        time.sleep(1)



def get_queue(email_batch_size=None):
    """
    Description:
    Get email queue from the database.
    email_batch_size is the number of emails to be sent in a batch per second.
    batch_per_minute is the number of emails to be sent in half a minute.
    batch_size is the number of emails to be sent in a batch.
    """
    batch_per_minute = cint(email_batch_size) * 30
    batch_size = batch_per_minute or cint(frappe.conf.email_queue_batch_size) or 500

    return frappe.db.sql(
		f"""select
			name, sender
		from
			`tabEmail Queue`
		where
			(status='Not Sent' or status='Partially Sent') and
			(send_after is null or send_after < %(now)s)
		order
			by priority desc, retry asc, creation asc
		limit {batch_size}""",
		{"now": now_datetime()},
		as_dict=True,
	)
