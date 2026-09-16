# Copyright (c) 2026, Hybrowlabs Technologies and Contributors
# See license.txt
"""Email governance header injection — email-governance-engine, Part 2.

This module implements the ``make_email_body_message`` core hook target that
injects two families of outbound MIME headers onto every email this bench
sends, through *any* app, not just ``aws_integration``:

  1. ``X-SES-Configuration-Set`` — tells AWS SES which configuration set
     (i.e. which event-publishing/reputation bucket) a send belongs to.
  2. ``List-Unsubscribe`` and ``List-Unsubscribe-Post`` (RFC 8058 one-click
     — see "IMPLEMENTED" below) — the standard mail-client-recognised
     unsubscribe affordance, but ONLY for sends a caller has explicitly
     opted in as "promotional".

WHERE THIS HOOKS IN
--------------------
``frappe/email/email_body.py``'s ``EMail.make()`` ends with::

    for hook in frappe.get_hooks("make_email_body_message"):
        frappe.get_attr(hook)(self)

``self`` there is the ``EMail`` instance mid-build: ``msg_root`` (the root
``MIMEMultipart``) already carries ``Subject``/``From``/``To``/``Date``/etc,
and — critically — any ``email_headers`` the caller passed into
``frappe.sendmail(email_headers=...)`` have ALREADY been applied as real
``X-``-prefixed headers on ``msg_root`` (see
``QueueBuilder.prepare_email_content()`` in
``frappe/email/doctype/email_queue/email_queue.py``: it calls
``mail.add_headers(self.email_headers)`` and only afterwards does
``as_dict()`` call ``mail.as_string()``, which is what actually triggers
``validate()`` + ``make()`` + this hook). ``self.recipients``/``self.cc``/
``self.bcc`` are, by this point, concrete lists (``EMail.validate()``, which
``as_string()`` always calls immediately before ``make()``, replaces the
constructor's one-shot ``filter()`` object with a real list comprehension —
verified by reading ``EMail.__init__`` + ``EMail.validate()`` directly, not
assumed).

This hook fires **once per built message** (i.e. once per Email Queue row),
synchronously, in the same process/call-stack as whatever code originally
called ``frappe.sendmail()`` — whether or not that send is ``delayed=True``.
It does NOT fire per-recipient at flush time. This matters for the
List-Unsubscribe URL discussion below.

THE SAFE-DEFAULT / OPT-IN DESIGN (constraint: never touch OTP/password-reset)
-------------------------------------------------------------------------------
``make_email_body_message`` is a *global* hook — it fires for every email
built anywhere in this bench: core Frappe's own OTP and password-reset
mail, workflow alerts, Newsletter sends, every app's ``frappe.sendmail()``
call, not just this app's. There is no reliable way to distinguish
"promotional" from "transactional" purely by inspecting the built ``EMail``
object — ``self`` carries no notion of caller intent.

So this module refuses to guess. ``List-Unsubscribe`` is injected **only**
when an explicit marker header is present on the message, and that marker
is set **only** by ``aws_integration.utils.email.sendmail()`` /
``send_email_in_batches()`` when a caller passes ``promotional=True``
(default ``False``). No marker -> no ``List-Unsubscribe``, full stop. OTP,
password reset, and every other transactional send in this bench (which
never sets the marker) is structurally untouched by this hook, by
construction — not by convention, not by a doctype allowlist that could
someday be misconfigured.

THE MARKER-HEADER MECHANISM (verified, not assumed)
-----------------------------------------------------
``make_email_body_message`` hook functions only ever receive ``self`` — no
access to the original ``frappe.sendmail()`` kwargs. The only channel this
module found, by reading the actual code path end to end, for smuggling
caller intent through to hook time is ``frappe.sendmail()``'s existing
``email_headers`` parameter (added for exactly this kind of
"custom-header injection" use case — see its docstring in
``frappe/__init__.py``): whatever dict is passed there becomes real
``X-``-prefixed headers on ``msg_root`` *before* the hook loop runs. So
``aws_integration.utils.email.sendmail(..., promotional=True)`` piggybacks a
marker onto that same channel (see ``mark_promotional_headers()`` below),
and this hook reads it back off ``self.msg_root`` and then deletes it, so
recipients never see the internal marker in their actual received headers.

CONFIGURATION-SET INJECTION IS UNCONDITIONAL (not gated on promotional)
--------------------------------------------------------------------------
Per the AWS Settings ``ses_transactional_configuration_set`` /
``ses_promotional_configuration_set`` fields, ``X-SES-Configuration-Set`` is
added to every message once the relevant field is populated — transactional
sends get the transactional set, promotional sends get the promotional set.
This is deliberately unconditional (unlike List-Unsubscribe): the header is
informational to AWS only, changes no recipient-visible behavior, and both
"buckets" are useful metadata for AWS-side reputation/event tracking
regardless of whether a send happened to go through this app's
``sendmail()`` or straight through core ``frappe.sendmail()`` from
somewhere else in the bench.

VERIFIED: does the SES *SMTP* interface honor this header? (Part 1 already
consolidated this app's transport onto core Frappe's SMTP-based Email
Queue flush, not the SES API.) Yes — this is documented, first-party AWS
behavior, not an assumption: "If you are using the SMTP interface or the
SendRawEmail API operation, you can specify a configuration set by
including the [...] header in your email: X-SES-CONFIGURATION-SET:
ConfigSet [...] Amazon SES removes the header[s] before sending the email."
(https://docs.aws.amazon.com/ses/latest/dg/using-configuration-sets-in-email.html,
"Specifying a configuration set when you send email"). CAVEAT worth flagging
for the disposer: an AWS re:Post thread
(https://repost.aws/questions/QUBXioPYX_S0ySEXYIRUgn1g/aws-ses-over-smtp-seems-to-ignore-x-ses-configuration-set-header)
reports the header being silently ignored in some SMTP setups (suspected
causes in that thread: a default configuration set already bound to the
sending identity taking precedence, or a header-casing/placement quirk in
the reporter's own mail client). This module follows the documented
contract exactly (a literal ``X-SES-Configuration-Set`` header, case as
shown in AWS's own docs, added straight to ``msg_root``), but actually
seeing the resulting SES/CloudWatch event data land in the *chosen*
configuration set on this bench's real AWS account has NOT been verified
here — flagged as **needs-Badal-confirmation** once real config set names
are filled into AWS Settings and a real send is traced.

IMPLEMENTED: RFC 8058 one-click ``List-Unsubscribe-Post`` (Part 5)
--------------------------------------------------------------------------
Full RFC 8058 one-click unsubscribe requires the mail client to be able to
``POST`` to the URL in ``List-Unsubscribe`` with body
``List-Unsubscribe=One-Click``, and for the server to honor that POST.
Core Frappe's built-in unsubscribe endpoint
(``frappe.email.queue.unsubscribe``, in ``frappe/email/queue.py``) calls
``verify_request()`` (``frappe.utils.verified_command``), which validates a
*signed query string* and explicitly rejects any request carrying a POST
body/form data (``valid_request_data = not (frappe.request.form or
frappe.request.data)``) — so a compliant mail client's one-click POST
against it would never actually register. Advertising
``List-Unsubscribe-Post: List-Unsubscribe=One-Click`` against an endpoint
that cannot honor it would be worse than not advertising it at all.

Part 3 introduced this app's own ``Email Suppression Entry`` doctype and
``aws_integration.utils.suppression.suppress()``. Part 5 (this change)
adds the missing piece: a genuinely POST-accepting, signed-token-gated
receiver — ``aws_integration.api.unsubscribe.one_click_unsubscribe`` (see
that module and ``aws_integration.utils.unsubscribe_token`` for the full
design) — and this module now points ``List-Unsubscribe`` at it and sets
``List-Unsubscribe-Post`` alongside it. The unsubscribe URL is now signed
with this app's own stateless token (not core's ``get_unsubcribed_url()``,
which mints links only core's GET-only endpoint understands) so the same
link that's advertised is the one the new receiver actually understands.

KNOWN LIMITATION: List-Unsubscribe URL is per-MESSAGE, not per-RECIPIENT
--------------------------------------------------------------------------
This hook fires once per built ``EMail``/Email Queue row, before Frappe's
own per-recipient placeholder substitution (the ``<!--recipient-->`` /
``<!--unsubscribe_url-->`` tokens the body/``To`` header use, replaced later
per-recipient in ``QueueBuilder.build_message()`` at flush/send time). The
``https:`` unsubscribe URL this hook builds is signed for one specific
email address (``mail.recipients[0]``) at hook time — it is only guaranteed
correct when that Email Queue row has exactly one final recipient. Multiple
recipients from `To`/`cc`/`bcc` sharing a single MIME message will all see
the *same* header, resolved to the *first* recipient's address only. This
is flagged loudly (a runtime log line) when it happens, but is not silently
"fixed" here — the honest fix is per-recipient message splitting
(``frappe.sendmail(queue_separately=True)``), which
``aws_integration.utils.email.sendmail()`` does not yet thread through to
core. Left as an explicit, documented follow-up, not solved in Part 2.

NAMED CALLERS THAT ARE UNSAFE TO MARK ``promotional=True`` TODAY
--------------------------------------------------------------------------
This is not an abstract risk — two real, live call sites in the bench
build genuinely multi-recipient ``To`` lists and would silently mis-sign
the unsubscribe URL for every recipient but the first if ever switched to
``promotional=True``:

- ``unity_parent_app/unity_parent_app/api/admin.py::enqueued_ids_notice_emails``
  (via ``send_custom_notification`` → ``send_email_in_batches``, line ~1074)
  builds ``"recepients": [student_email, *guardian_emails]`` — a multi-address
  ``To`` list whenever a student has one or more guardian emails on file,
  which is the normal case, not an edge case.
- ``unity_parent_app/unity_parent_app/api/admin.py::send_test_mail``
  (~line 719-813) builds its ``To`` list from a comma-split string of
  arbitrary test addresses — also genuinely multi-recipient.

Do not flip ``promotional=True`` on either call site until
``queue_separately=True`` is threaded through ``aws_integration.utils.
email.sendmail()``/``send_email_in_batches()``, or these two call sites are
restructured to one recipient per Email Queue row. This was verified by
direct disposer review against the real code, not assumed — the earlier,
more general framing of this limitation understated how close to "already
live" this landmine is.
"""

from __future__ import annotations

import email.utils

import frappe
from frappe import _
from frappe.utils import get_url

from aws_integration.utils.unsubscribe_token import generate_unsubscribe_token

#: Marker headers used to smuggle caller intent from
#: ``aws_integration.utils.email.sendmail(..., promotional=True)`` through
#: to this hook. These are stripped from the outbound message before it is
#: ever queued/sent — recipients never see them.
PROMOTIONAL_HEADER = "X-Aws-Promotional"
UNSUB_DOCTYPE_HEADER = "X-Aws-Unsub-Doctype"
UNSUB_NAME_HEADER = "X-Aws-Unsub-Name"

#: This app's RFC 8058 one-click unsubscribe POST receiver. See module
#: docstring "IMPLEMENTED" section and ``aws_integration.api.unsubscribe``.
ONE_CLICK_UNSUBSCRIBE_METHOD = "/api/method/aws_integration.api.unsubscribe.one_click_unsubscribe"


def mark_promotional_headers(reference_doctype=None, reference_name=None):
    """Build the ``email_headers`` dict that
    ``aws_integration.utils.email.sendmail()`` passes into
    ``frappe.sendmail(email_headers=...)`` to mark a send as promotional.

    This is the ONLY supported way to trigger List-Unsubscribe injection —
    see the module docstring's "SAFE-DEFAULT / OPT-IN DESIGN" section for
    why that is a deliberate, hard requirement, not a convenience default.

    :param reference_doctype: Optional. If given (together with
        ``reference_name``), scopes the resulting unsubscribe token (and
        eventual suppression) to that one reference instead of a global
        unsubscribe — see ``aws_integration.utils.unsubscribe_token``.
    :param reference_name: See ``reference_doctype``.
    """
    headers = {"Aws-Promotional": "1"}
    if reference_doctype:
        headers["Aws-Unsub-Doctype"] = reference_doctype
    if reference_name:
        headers["Aws-Unsub-Name"] = reference_name
    return headers


def _get_aws_settings():
    """Best-effort fetch of the AWS Settings singleton. Never raises —
    this hook must never be the reason an unrelated email fails to send."""
    try:
        return frappe.get_cached_doc("AWS Settings")
    except Exception:
        return None


def _read_and_clear_marker(mail):
    """Read the promotional marker headers off ``mail.msg_root`` and strip
    them from the outbound message (they are an internal implementation
    detail, not something a recipient should ever see in their raw
    headers).

    Returns ``(is_promotional, unsub_doctype, unsub_name)``.
    """
    msg_root = mail.msg_root
    is_promotional = msg_root.get(PROMOTIONAL_HEADER) == "1"
    unsub_doctype = msg_root.get(UNSUB_DOCTYPE_HEADER) or None
    unsub_name = msg_root.get(UNSUB_NAME_HEADER) or None

    for header in (PROMOTIONAL_HEADER, UNSUB_DOCTYPE_HEADER, UNSUB_NAME_HEADER):
        if header in msg_root:
            del msg_root[header]

    return is_promotional, unsub_doctype, unsub_name


def _inject_configuration_set_header(mail, is_promotional):
    """Set ``X-SES-Configuration-Set`` from AWS Settings, unconditionally
    (regardless of the promotional marker) — see module docstring."""
    settings = _get_aws_settings()
    if not settings:
        return

    config_set = (
        settings.get("ses_promotional_configuration_set")
        if is_promotional
        else settings.get("ses_transactional_configuration_set")
    )
    if config_set:
        mail.set_header("X-SES-Configuration-Set", config_set)


def _mailto_fallback_address(mail):
    """Extract a bare email address from ``mail.sender`` (which may be in
    ``"Display Name <addr>"`` form) to use as the ``mailto:`` fallback
    target. No dedicated "unsubscribe inbox" exists yet, so this reuses the
    sender address — a human monitoring that inbox is the RFC 2369-era
    expectation for the mailto: form anyway."""
    _, addr = email.utils.parseaddr(mail.sender or "")
    return addr or None


def _inject_list_unsubscribe_headers(mail, is_promotional, unsub_doctype, unsub_name):
    """Set ``List-Unsubscribe`` (and, when an https: link was built,
    ``List-Unsubscribe-Post``) ONLY for sends explicitly marked
    promotional. See module docstring "IMPLEMENTED" section."""
    if not is_promotional:
        return

    recipients = list(mail.recipients or [])
    if len(recipients) != 1:
        frappe.logger("aws_integration").warning(
            "inject_governance_headers(): promotional send %r has %d recipients "
            "sharing one message — List-Unsubscribe can only be signed correctly "
            "for a single recipient per message. The header will resolve to only "
            "the first recipient (or be mailto:-only if none). Use "
            "frappe.sendmail(queue_separately=True) upstream for promotional bulk "
            "sends (not yet threaded through aws_integration.utils.email.sendmail()).",
            mail.subject,
            len(recipients),
        )
    recipient = recipients[0] if recipients else None

    parts = []
    https_link_added = False

    if recipient:
        # reference_doctype/reference_name are optional here (unlike the
        # old core-endpoint path, which required both) — a token minted
        # with neither decodes, at receiver time, to a global unsubscribe.
        # See aws_integration.utils.unsubscribe_token module docstring.
        try:
            token = generate_unsubscribe_token(
                recipient_email=recipient,
                reference_doctype=unsub_doctype,
                reference_name=unsub_name,
            )
            https_url = get_url(f"{ONE_CLICK_UNSUBSCRIBE_METHOD}?token={token}")
            parts.append(f"<{https_url}>")
            https_link_added = True
        except Exception:
            frappe.log_error(
                title=_("aws_integration: failed to build List-Unsubscribe https: link"),
                message=frappe.get_traceback(),
            )
    else:
        frappe.logger("aws_integration").info(
            "inject_governance_headers(): promotional send %r has no resolvable "
            "recipient — omitting the https: List-Unsubscribe form, mailto: "
            "fallback only.",
            mail.subject,
        )

    mailto_addr = _mailto_fallback_address(mail)
    if mailto_addr:
        parts.append(f"<mailto:{mailto_addr}?subject=unsubscribe>")

    if parts:
        mail.set_header("List-Unsubscribe", ", ".join(parts))

    if https_link_added:
        # RFC 8058 one-click — only advertised when the https: link actually
        # points at our POST-accepting receiver (never for the mailto:-only
        # fallback case, which no mail client can one-click POST to).
        mail.set_header("List-Unsubscribe-Post", "List-Unsubscribe=One-Click")


def inject_governance_headers(mail):
    """``make_email_body_message`` hook target.

    Registered in ``hooks.py`` and called by
    ``frappe.email.email_body.EMail.make()`` for every email built anywhere
    in this bench, with ``mail`` being the ``EMail`` instance itself. See
    the module docstring for the full design rationale.

    This function must never raise: it fires unconditionally for every
    email in the system (including OTP/password-reset/etc from any app),
    and ``EMail.make()`` does not guard hook calls with a try/except — an
    uncaught exception here would break mail sending bench-wide. Every
    sub-step is individually wrapped so one failure (e.g. a bad AWS
    Settings value, or the unsubscribe URL signer raising) cannot take out
    the other, nor prevent the message from being built and sent.
    """
    try:
        is_promotional, unsub_doctype, unsub_name = _read_and_clear_marker(mail)
    except Exception:
        frappe.log_error(
            title=_("aws_integration: failed to read email governance marker headers"),
            message=frappe.get_traceback(),
        )
        return

    try:
        _inject_configuration_set_header(mail, is_promotional)
    except Exception:
        frappe.log_error(
            title=_("aws_integration: X-SES-Configuration-Set injection failed"),
            message=frappe.get_traceback(),
        )

    try:
        _inject_list_unsubscribe_headers(mail, is_promotional, unsub_doctype, unsub_name)
    except Exception:
        frappe.log_error(
            title=_("aws_integration: List-Unsubscribe injection failed"),
            message=frappe.get_traceback(),
        )
