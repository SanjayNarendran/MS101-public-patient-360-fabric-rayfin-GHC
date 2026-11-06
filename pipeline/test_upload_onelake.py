import unittest
from unittest.mock import Mock, patch

import requests


CONFIG = {
    "WORKSPACE_ID": "workspace",
    "CLINICAL_LH": "clinical",
    "GENOMICS_LH": "genomics",
    "IMAGING_LH": "imaging",
    "PATIENT360_LH": "patient360",
    "PATIENT360_SQL": "endpoint",
}

with patch("fabric_config.load", return_value=CONFIG):
    import upload_onelake


def response(status_code=200, status=None, error=None, location=True):
    result = Mock(spec=requests.Response)
    result.status_code = status_code
    result.headers = {"Location": "https://example.invalid/operations/1"} if location else {}
    result.json.return_value = {"status": status, "error": error}
    return result


class OperationWaitTests(unittest.TestCase):
    def setUp(self):
        self.sleep = patch.object(upload_onelake.time, "sleep")
        self.token = patch.object(upload_onelake, "token", return_value="test-token")
        self.sleep.start()
        self.token.start()
        self.addCleanup(self.sleep.stop)
        self.addCleanup(self.token.stop)

    def test_synchronous_success_does_not_poll(self):
        with patch.object(upload_onelake.requests, "get") as get:
            upload_onelake.wait(response())
            get.assert_not_called()

    def test_running_operation_is_polled_until_success(self):
        states = [response(status="NotStarted"), response(status="Running"), response(status="Succeeded")]
        with patch.object(upload_onelake.requests, "get", side_effect=states) as get:
            upload_onelake.wait(response(status_code=202))
            self.assertEqual(get.call_count, 3)

    def test_completed_operation_is_successful(self):
        with patch.object(upload_onelake.requests, "get", return_value=response(status="Completed")):
            upload_onelake.wait(response(status_code=202))

    def test_failed_or_cancelled_operation_raises(self):
        for status in ("Failed", "Cancelled", "Canceled"):
            with self.subTest(status=status), patch.object(
                upload_onelake.requests, "get", return_value=response(status=status, error="reason")
            ):
                with self.assertRaisesRegex(RuntimeError, status):
                    upload_onelake.wait(response(status_code=202))

    def test_missing_status_url_raises(self):
        with self.assertRaisesRegex(RuntimeError, "without a status URL"):
            upload_onelake.wait(response(status_code=202, location=False))

    def test_unexpected_status_raises(self):
        with patch.object(upload_onelake.requests, "get", return_value=response(status="Undefined")):
            with self.assertRaisesRegex(RuntimeError, "Unexpected"):
                upload_onelake.wait(response(status_code=202))

    def test_poll_http_error_raises(self):
        failed = response(status="Running")
        failed.raise_for_status.side_effect = requests.HTTPError("status query failed")
        with patch.object(upload_onelake.requests, "get", return_value=failed):
            with self.assertRaises(requests.HTTPError):
                upload_onelake.wait(response(status_code=202))

    def test_operation_timeout_is_an_error(self):
        with patch.object(upload_onelake.time, "monotonic", side_effect=[0, 1801]), \
                patch.object(upload_onelake.requests, "get") as get:
            with self.assertRaisesRegex(RuntimeError, "Timed out"):
                upload_onelake.wait(response(status_code=202))
        get.assert_not_called()


class ShortcutTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.object(upload_onelake.time, "sleep"))
        self.enterContext(patch.object(upload_onelake, "token", return_value="test-token"))
        self.results = [{"request": {"path": "Files", "name": silo}, "status": "Succeeded"}
                        for silo in ("clinical", "genomics", "imaging")]

    def bulk_response(self):
        resp = response()
        resp.json.return_value = {"value": self.results}
        return resp

    def test_synchronous_success_refreshes_metadata(self):
        with patch.object(upload_onelake, "fabric", side_effect=[self.bulk_response(), response()]) as api:
            upload_onelake.shortcuts([])
        self.assertEqual(api.call_count, 2)
        self.assertIn("/refreshMetadata", api.call_args.args[1])

    def test_partial_or_incomplete_results_stop_before_refresh(self):
        variants = [
            self.results[:2],
            self.results[:2] + [self.results[0]],
            self.results[:2] + [{"request": {"path": "Files", "name": "imaging"}, "status": "Failed"}],
        ]
        for results in variants:
            with self.subTest(results=results):
                self.results = results
                with patch.object(upload_onelake, "fabric", return_value=self.bulk_response()) as api:
                    with self.assertRaisesRegex(RuntimeError, "shortcut"):
                        upload_onelake.shortcuts([])
                self.assertEqual(api.call_count, 1)

    def test_missing_result_body_is_an_error(self):
        resp = response()
        resp.json.return_value = {}
        with patch.object(upload_onelake, "fabric", return_value=resp):
            with self.assertRaisesRegex(RuntimeError, "shortcut"):
                upload_onelake.shortcuts([])

    def test_async_result_is_checked_before_refresh(self):
        for success in (True, False):
            with self.subTest(success=success):
                self.results[-1]["status"] = "Succeeded" if success else "Failed"
                with patch.object(upload_onelake, "fabric", side_effect=[response(202), response()]) as api, \
                        patch.object(upload_onelake.requests, "get",
                                     side_effect=[response(status="Succeeded"), self.bulk_response()]) as get:
                    if success:
                        upload_onelake.shortcuts([])
                    else:
                        with self.assertRaisesRegex(RuntimeError, "shortcut"):
                            upload_onelake.shortcuts([])
                self.assertTrue(get.call_args.args[0].endswith("/operations/1/result"))
                self.assertEqual(api.call_count, 2 if success else 1)


if __name__ == "__main__":
    unittest.main()
