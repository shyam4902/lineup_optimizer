import io
import re
import unittest
from pathlib import Path

from app import app


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "fixtures" / "optimizer_v1"


class FlaskOptimizerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        app.config.update(TESTING=True, MAX_CONTENT_LENGTH=2 * 1024 * 1024)

    def setUp(self):
        self.client = app.test_client()

    def upload(self, path):
        return (io.BytesIO(path.read_bytes()), path.name)

    def optimize(self, roster, projections, contest="Spark"):
        return self.client.post(
            "/optimize",
            data={
                "roster_file": self.upload(roster),
                "projection_file": self.upload(projections),
                "contest": contest,
            },
            content_type="multipart/form-data",
        )

    def test_get_renders_both_uploads_and_all_canonical_contests(self):
        response = self.client.get("/")
        body = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn('name="roster_file"', body)
        self.assertIn('name="projection_file"', body)
        for contest in ("Spark", "Scorcher", "Wildfire", "Flex Appeal", "Flamethrower", "Inferno"):
            self.assertIn(contest, body)

    def test_valid_upload_returns_complete_lineup_and_totals(self):
        response = self.optimize(FIXTURES / "valid_cards.csv", FIXTURES / "valid_projections.csv")
        body = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn("Complete optimal lineup", body)
        self.assertIn("Total adjusted projection", body)
        self.assertIn("Total salary", body)
        self.assertIn("Remaining salary", body)
        self.assertEqual(len(re.findall(r'<tr data-slot="', body)), 4)
        self.assertRegex(body, r"\$[0-9,]+")

    def test_flex_appeal_returns_six_configured_slots(self):
        response = self.optimize(
            FIXTURES / "flex_appeal_cards.csv",
            FIXTURES / "flex_appeal_projections.csv",
            "Flex Appeal",
        )
        body = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn("Flex Appeal", body)
        self.assertEqual(len(re.findall(r'<tr data-slot="', body)), 6)

    def test_missing_roster_file_is_rejected_clearly(self):
        response = self.client.post(
            "/optimize",
            data={"projection_file": self.upload(FIXTURES / "valid_projections.csv"), "contest": "Spark"},
            content_type="multipart/form-data",
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("Choose a roster CSV file", response.get_data(as_text=True))

    def test_selected_contest_is_preserved_on_validation_error(self):
        response = self.client.post(
            "/optimize",
            data={
                "projection_file": self.upload(FIXTURES / "valid_projections.csv"),
                "contest": "Flex Appeal",
            },
            content_type="multipart/form-data",
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn('<option value="Flex Appeal" selected>', response.get_data(as_text=True))

    def test_missing_projection_file_is_rejected_clearly(self):
        response = self.client.post(
            "/optimize",
            data={"roster_file": self.upload(FIXTURES / "valid_cards.csv"), "contest": "Spark"},
            content_type="multipart/form-data",
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("Choose a projections CSV file", response.get_data(as_text=True))

    def test_unsupported_file_extension_is_rejected(self):
        response = self.client.post(
            "/optimize",
            data={
                "roster_file": (io.BytesIO(b"player_name,team\nPlayer,KC\n"), "roster.txt"),
                "projection_file": self.upload(FIXTURES / "valid_projections.csv"),
                "contest": "Spark",
            },
            content_type="multipart/form-data",
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("must be a CSV file", response.get_data(as_text=True))

    def test_uppercase_csv_extensions_are_accepted(self):
        response = self.client.post(
            "/optimize",
            data={
                "roster_file": (io.BytesIO((FIXTURES / "valid_cards.csv").read_bytes()), "roster.CSV"),
                "projection_file": (io.BytesIO((FIXTURES / "valid_projections.csv").read_bytes()), "projections.CsV"),
                "contest": "Spark",
            },
            content_type="multipart/form-data",
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("Complete optimal lineup", response.get_data(as_text=True))

    def test_invalid_utf8_is_rejected_without_a_traceback(self):
        response = self.client.post(
            "/optimize",
            data={
                "roster_file": (io.BytesIO(b"player_name,team,position,multiplier,salary,status\n\xff"), "roster.csv"),
                "projection_file": self.upload(FIXTURES / "valid_projections.csv"),
                "contest": "Spark",
            },
            content_type="multipart/form-data",
        )
        body = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 400)
        self.assertIn("decoded as UTF-8", body)
        self.assertNotIn("Traceback", body)

    def test_unknown_contest_is_rejected_server_side(self):
        response = self.optimize(
            FIXTURES / "valid_cards.csv",
            FIXTURES / "valid_projections.csv",
            "Not a contest",
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("Select a valid contest", response.get_data(as_text=True))

    def test_malformed_schema_explains_required_columns(self):
        response = self.client.post(
            "/optimize",
            data={
                "roster_file": (io.BytesIO(b"player_name,position\nPlayer,QB\n"), "roster.csv"),
                "projection_file": self.upload(FIXTURES / "valid_projections.csv"),
                "contest": "Spark",
            },
            content_type="multipart/form-data",
        )
        body = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn("Input could not be optimized", body)
        self.assertIn("missing columns", body)
        self.assertNotIn('data-slot="', body)

    def test_infeasible_fixture_displays_no_partial_lineup(self):
        response = self.optimize(
            FIXTURES / "infeasible_missing_te_cards.csv",
            FIXTURES / "infeasible_missing_te_projections.csv",
        )
        body = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn("no eligible card", body.lower())
        self.assertNotIn('data-slot="', body)

    def test_edge_fixture_displays_grouped_diagnostic_counts(self):
        response = self.optimize(FIXTURES / "edge_cards.csv", FIXTURES / "edge_projections.csv")
        body = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        for reason in ("Non-active", "Zero projection", "Missing projection", "Exact visible duplicate collapsed"):
            self.assertIn(reason, body)
        self.assertIn("Roster rows read", body)
        self.assertIn("Matched cards", body)
        self.assertNotIn("Matched</dt>", body)

    def test_requests_do_not_share_upload_state(self):
        first = self.optimize(FIXTURES / "valid_cards.csv", FIXTURES / "valid_projections.csv")
        second = self.optimize(
            FIXTURES / "greedy_counterexample_cards.csv",
            FIXTURES / "greedy_counterexample_projections.csv",
        )
        self.assertIn("Josh Allen", first.get_data(as_text=True))
        self.assertIn("Affordable QB", second.get_data(as_text=True))
        self.assertNotIn("Josh Allen", second.get_data(as_text=True))


if __name__ == "__main__":
    unittest.main()
