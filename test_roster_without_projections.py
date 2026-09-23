"""A new workspace shows uploaded cards before weekly projections arrive."""

import io
import tempfile
import unittest
from unittest.mock import patch

import app as app_module


class RosterWithoutProjectionsTests(unittest.TestCase):
    def test_roster_is_visible_before_projections_and_after_restart(self):
        with tempfile.TemporaryDirectory() as root:
            state = app_module.PlannerAppState(root_dir=root)
            with patch.object(app_module, "STATE", state):
                client = app_module.app.test_client()
                response = client.post("/api/roster/upload", data={
                    "roster_file": (io.BytesIO(
                        b"player_name,team,position,multiplier,salary,status\n"
                        b"Patrick Mahomes,KC,QB,1.5,7500,Active\n"
                    ), "roster.csv"),
                })
                self.assertEqual(response.status_code, 200)
                for current in (state, app_module.PlannerAppState(root_dir=root)):
                    app_module.STATE = current
                    data = client.get("/api/planner/state").get_json()
                    self.assertEqual(data["roster_count"], 1)
                    self.assertEqual(len(data["cards"]), 1)
                    self.assertEqual(data["cards"][0]["player_name"], "Patrick Mahomes")
                    self.assertEqual(data["cards"][0]["weekly_salary"], 7500)
                    self.assertFalse(data["cards"][0]["is_eligible"])
                    self.assertIsNone(data["snapshot"])
                    self.assertEqual(client.post("/api/planner/generate", json={}).status_code, 400)

                response = client.post("/api/projections/upload", data={
                    "projection_file": (io.BytesIO(
                        b"Player,Team,Position,Projection\nPatrick Mahomes,KC,QB,20\n"
                        + "".join(f"Test Player {i},KC,WR,10\n" for i in range(19)).encode()
                    ), "projections.csv"),
                })
                self.assertEqual(response.status_code, 200, response.get_json())
                data = client.get("/api/planner/state").get_json()
                self.assertEqual(len(data["cards"]), 1)
                self.assertEqual(data["cards"][0]["adjusted_projection"], 30)
                self.assertTrue(data["cards"][0]["is_eligible"])


if __name__ == "__main__":
    unittest.main()
