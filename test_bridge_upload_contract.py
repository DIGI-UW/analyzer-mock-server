import io
import tempfile
import unittest
import urllib.error
from unittest.mock import Mock, patch

from api import MockAPIHandler


class TestBridgeUploadContract(unittest.TestCase):
    def test_bridge_rejection_fails_the_simulation(self):
        handler = object.__new__(MockAPIHandler)
        handler._send_json = Mock()
        rejection = urllib.error.HTTPError(
            "https://bridge/admin/upload",
            401,
            "Unauthorized",
            {},
            io.BytesIO(b"Unauthorized"),
        )
        self.addCleanup(rejection.close)

        with tempfile.NamedTemporaryFile(suffix=".xlsx") as fixture:
            fixture.write(b"fixture")
            fixture.flush()
            with patch("urllib.request.urlopen", side_effect=rejection):
                handler._upload_fixture_to_bridge(
                    "hain_fluorocycler",
                    {"analyzer": {"name": "Bruker FluoroCycler XT"}},
                    {"format": "XLSX"},
                    fixture.name,
                    "fixtures/fluorocycler-xt/results.xlsx",
                    [{"sampleId": "DEV01263000000000001", "result": "Detected"}],
                    {"analyzer_id": "11", "test_code": "VIH-1"},
                )

        status, payload = handler._send_json.call_args.args
        self.assertEqual(502, status)
        self.assertEqual(401, payload["bridge_status"])


if __name__ == "__main__":
    unittest.main()
