"""Unit tests for the throttled parallel SES batch sender.

Run: bench --site <site> run-tests --module aws_integration.utils.test_email_batches
"""

import time
from unittest.mock import MagicMock, patch

from frappe.tests.utils import FrappeTestCase

from aws_integration.utils import email as email_utils


def _data(count):
    return {
        f"S{i}": {
            "subject": f"Subject {i}",
            "content": "<p>hello</p>",
            "recepients": [f"s{i}@x.com"],
            "cc_recepients": [f"g{i}@x.com"],
            "bcc_recepients": [],
        }
        for i in range(count)
    }


def _settings(rate=25):
    settings = MagicMock()
    settings.enable_bulk_ses_email = 1
    settings.email_batch_size = rate
    settings.sender_name = "Test School"
    settings.source_email = "noreply@x.com"
    return settings


class TestSendEmailInBatches(FrappeTestCase):
    def _run(self, data, client, rate=25):
        settings = _settings(rate)
        settings.get_ses_client.return_value = client
        with patch.object(email_utils.frappe, "get_single", return_value=settings), patch.object(
            email_utils, "_add_ses_logs"
        ) as add_logs:
            failures = email_utils.send_email_in_batches(data)
        return failures, add_logs

    def test_all_sent_and_logged(self):
        client = MagicMock()
        client.send_email.return_value = {"MessageId": "mid"}
        failures, add_logs = self._run(_data(5), client)
        # None (not {}) on full success — old callers pass this through API responses
        self.assertIsNone(failures)
        self.assertEqual(client.send_email.call_count, 5)
        add_logs.assert_called_once()

    def test_one_failure_does_not_abort_batch(self):
        client = MagicMock()

        def send(**kwargs):
            if "Subject 2" in kwargs["Content"]["Simple"]["Subject"]["Data"]:
                raise Exception("MessageRejected")
            return {"MessageId": "mid"}

        client.send_email.side_effect = send
        failures, _ = self._run(_data(5), client)
        self.assertEqual(list(failures), ["S2"])
        self.assertEqual(client.send_email.call_count, 5)

    def test_missing_recipient_reported_without_send(self):
        data = _data(2)
        data["S1"]["recepients"] = [None]
        client = MagicMock()
        client.send_email.return_value = {"MessageId": "mid"}
        failures, _ = self._run(data, client)
        self.assertEqual(list(failures), ["S1"])
        self.assertEqual(client.send_email.call_count, 1)

    def test_rate_throttle_paces_submissions_evenly(self):
        client = MagicMock()
        client.send_email.return_value = {"MessageId": "mid"}
        start = time.monotonic()
        # 6 emails at 2/sec, evenly spaced => last submission at ~2.5s
        failures, _ = self._run(_data(6), client, rate=2)
        elapsed = time.monotonic() - start
        self.assertIsNone(failures)
        self.assertGreaterEqual(elapsed, 2.0)

    def test_throttling_error_is_retried(self):
        client = MagicMock()
        error = Exception("slow down")
        error.response = {"Error": {"Code": "Throttling"}}
        client.send_email.side_effect = [error, {"MessageId": "mid"}]
        with patch.object(email_utils.time, "sleep"):
            failures, _ = self._run(_data(1), client)
        self.assertIsNone(failures)
        self.assertEqual(client.send_email.call_count, 2)

    def test_network_error_is_retried(self):
        from botocore.exceptions import EndpointConnectionError

        client = MagicMock()
        client.send_email.side_effect = [
            EndpointConnectionError(endpoint_url="https://ses.example"),
            {"MessageId": "mid"},
        ]
        with patch.object(email_utils.time, "sleep"):
            failures, _ = self._run(_data(1), client)
        self.assertIsNone(failures)
        self.assertEqual(client.send_email.call_count, 2)

    def test_permanent_error_not_retried(self):
        client = MagicMock()
        error = Exception("bad address")
        error.response = {"Error": {"Code": "MessageRejected"}}
        client.send_email.side_effect = error
        failures, _ = self._run(_data(1), client)
        self.assertEqual(client.send_email.call_count, 1)
        self.assertIn("S0", failures)
