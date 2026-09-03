import csv
import tempfile
import unittest
from pathlib import Path

from fixture_parser import parse_fixture


class FixtureParserTest(unittest.TestCase):
    def test_preserves_control_like_rows_for_bridge_classification(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            fixture_path = Path(temporary_directory) / "results.csv"
            with fixture_path.open("w", newline="", encoding="utf-8") as fixture:
                writer = csv.writer(fixture)
                writer.writerow(["Sample Name", "Result", "Test Code"])
                writer.writerow(["CPOS-001", "Positive", "ASSAY-A"])
                writer.writerow(["NTC-001", "Negative", "ASSAY-A"])
                writer.writerow(["PATIENT-001", "Detected", "ASSAY-A"])

            results = parse_fixture(
                str(fixture_path),
                {
                    "format": "CSV",
                    "column_mapping": {
                        "sampleId": "Sample Name",
                        "result": "Result",
                        "testCode": "Test Code",
                    },
                },
            )

        self.assertEqual(
            ["CPOS-001", "NTC-001", "PATIENT-001"],
            [result["sampleId"] for result in results],
        )


if __name__ == "__main__":
    unittest.main()
