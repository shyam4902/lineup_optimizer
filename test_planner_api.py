"""Integration tests for Weekly Lineup Planner Flask REST APIs with isolated storage."""

import csv
import io
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import app as app_module
import multi_lineup_planner
from app import PlannerAppState, app
from dff_client import DFFPlayerProjection, ProjectionsSnapshot
from multi_lineup_planner import LineupSlotAssignment, OwnedCard, PlannerLineup, WeeklyPlan


class PlannerApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        app.config.update(TESTING=True, MAX_CONTENT_LENGTH=8 * 1024 * 1024)

    def setUp(self):
        self.client = app.test_client()
        self.orig_state = app_module.STATE
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.test_state = PlannerAppState(root_dir=self.tmp_dir.name)
        app_module.STATE = self.test_state

        # Seed synthetic cards and snapshot to ensure deterministic tests
        self.test_cards = [
            OwnedCard("qb1", "Patrick Mahomes", "patrickmahomes", "KC", "QB", 1.0, 7500, "Active", 1),
            OwnedCard("qb2", "Josh Allen", "joshallen", "BUF", "QB", 1.0, 8000, "Active", 2),
            OwnedCard("qb3", "Lamar Jackson", "lamarjackson", "BAL", "QB", 1.0, 7800, "Active", 3),
            OwnedCard("qb4", "Jalen Hurts", "jalenhurts", "PHI", "QB", 1.0, 7600, "Active", 4),
            OwnedCard("rb1", "Christian McCaffrey", "christianmccaffrey", "SF", "RB", 1.0, 7000, "Active", 5),
            OwnedCard("rb2", "Derrick Henry", "derrickhenry", "BAL", "RB", 1.0, 6500, "Active", 6),
            OwnedCard("rb3", "Breece Hall", "breecehall", "NYJ", "RB", 1.0, 6400, "Active", 7),
            OwnedCard("rb4", "Bijan Robinson", "bijanrobinson", "ATL", "RB", 1.0, 6300, "Active", 8),
            OwnedCard("wr1", "Justin Jefferson", "justinjefferson", "MIN", "WR", 1.0, 7000, "Active", 9),
            OwnedCard("wr2", "CeeDee Lamb", "ceedeelamb", "DAL", "WR", 1.0, 7000, "Active", 10),
            OwnedCard("wr3", "Ja'Marr Chase", "jamarrchase", "CIN", "WR", 1.0, 6500, "Active", 11),
            OwnedCard("wr4", "Amon-Ra St. Brown", "amonrastbrown", "DET", "WR", 1.0, 6500, "Active", 12),
            OwnedCard("wr5", "Tyreek Hill", "tyreekhill", "MIA", "WR", 1.0, 6000, "Active", 13),
            OwnedCard("wr6", "Davante Adams", "davanteadams", "LV", "WR", 1.0, 5500, "Active", 14),
            OwnedCard("wr7", "A.J. Brown", "ajbrown", "PHI", "WR", 1.0, 6200, "Active", 15),
            OwnedCard("wr8", "Garrett Wilson", "garrettwilson", "NYJ", "WR", 1.0, 5800, "Active", 16),
            OwnedCard("te1", "Travis Kelce", "traviskelce", "KC", "TE", 1.0, 5500, "Active", 17),
            OwnedCard("te2", "Mark Andrews", "markandrews", "BAL", "TE", 1.0, 5000, "Active", 18),
            OwnedCard("te3", "Sam LaPorta", "samlaporta", "DET", "TE", 1.0, 4800, "Active", 19),
            OwnedCard("te4", "Trey McBride", "treymcbride", "ARI", "TE", 1.0, 4600, "Active", 20),
        ]
        self.test_players = [
            DFFPlayerProjection(c.player_name, c.athlete_key, c.team, c.position, c.roster_salary, 20.0, 20.0)
            for c in self.test_cards
        ]
        self.test_snapshot = ProjectionsSnapshot(
            slate_id="TEST_SLATE",
            source="unit_test",
            fetched_at="2026-09-08T00:00:00Z",
            total_raw_rows=len(self.test_players),
            offensive_player_count=len(self.test_players),
            positive_projection_count=len(self.test_players),
            dst_excluded_count=0,
            other_excluded_count=0,
            players=self.test_players,
            diagnostics=[],
        )
        self.test_state.roster_cards = self.test_cards
        self.test_state.snapshot = self.test_snapshot
        self.test_state.snapshot_store.save_snapshot(self.test_snapshot)
        self.test_state.recompute_planner_cards()

    def tearDown(self):
        app_module.STATE = self.orig_state
        self.tmp_dir.cleanup()

    def test_projection_upload_updates_week_scores_and_saved_plan_without_losing_edits(self):
        from dataclasses import asdict, replace

        state = self.test_state
        state.roster_cards[0] = replace(state.roster_cards[0], multiplier=1.5)
        state.recompute_planner_cards()
        self.client.post("/api/planner/generate", json={"contest_distribution": {"Scorcher": 1}})
        lineup = state.active_plan.lineups[0]
        lineup.is_locked = True
        lineup.slots[0].is_locked = True
        placements = [(s.card.card.card_id, s.is_locked) for s in lineup.slots]
        for key, edits in {
            "joshallen": {"game_status": "IN_PROGRESS", "live_points": 12, "remaining_projection": 5},
            "lamarjackson": {"game_status": "FINAL", "final_points": 24},
            "jalenhurts": {"remaining_projection": 17},
            "derrickhenry": {"game_status": "IN_PROGRESS", "live_points": 9},
        }.items():
            self.assertEqual(self.client.post("/api/live/update_player_state", json={"athlete_key": key, **edits}).status_code, 200)
        protected = {key: asdict(state.live_states[key]) for key in
                     ("joshallen", "lamarjackson", "jalenhurts", "derrickhenry")}
        state.overrides["breecehall"] = 33
        state.excluded_athlete_keys.add("bijanrobinson")
        state.recompute_planner_cards()
        self.assertEqual(self.client.post("/api/planner/save").status_code, 200)
        saved_bytes = (state.plans_dir / "latest_plan.json").read_bytes()
        csv_file = io.StringIO()
        writer = csv.writer(csv_file)
        writer.writerow(["Player", "Team", "Position", "Projection"])
        writer.writerows((c.player_name, c.team, c.position, 30) for c in self.test_cards)
        response = self.client.post("/api/projections/upload", data={
            "projection_file": (io.BytesIO(csv_file.getvalue().encode()), "updated.csv")})
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertEqual((state.plans_dir / "latest_plan.json").read_bytes(), saved_bytes)

        # Loading a plan saved before the upload must still use today's projections.
        for reload_plan in (False, True):
            with self.subTest(reload_plan=reload_plan):
                if reload_plan:
                    self.assertEqual(self.client.post("/api/planner/load").status_code, 200)
                live = self.client.get("/api/live/state").get_json()
                self.assertEqual(live["player_states"]["patrickmahomes"]["remaining_projection"], 30)
                self.assertEqual(live["player_states"]["patrickmahomes"]["remaining_stdev"], 10.5)
                self.assertEqual(live["player_states"]["breecehall"]["remaining_projection"], 33)
                for key, expected in protected.items():
                    self.assertEqual(asdict(state.live_states[key]), expected)
                self.assertEqual(state.live_states["patrickmahomes"].effective_mean(1.5), 45)
                self.assertTrue(state.active_plan.lineups[0].is_locked)
                self.assertEqual([(s.card.card.card_id, s.is_locked) for s in state.active_plan.lineups[0].slots], placements)
                self.assertEqual(state.excluded_athlete_keys, {"bijanrobinson"})
                for view in live["lineups"]:
                    self.assertAlmostEqual(view["projected_total"], sum(s["total_projected_points"] for s in view["slots"]), places=2)

    def test_projection_upload_refreshes_live_projection_used_by_week_scores(self):
        self.client.post("/api/planner/generate", json={"contest_distribution": {"Scorcher": 1}})
        csv_text = "Player,Team,Position,Projection\n" + "\n".join(
            f"{card.player_name},{card.team},{card.position},30" for card in self.test_cards
        )

        response = self.client.post("/api/projections/upload", data={
            "projection_file": (io.BytesIO(csv_text.encode()), "week-3.csv"),
        })

        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertTrue(all(card.raw_projection == 30 for card in self.test_state.planner_cards))
        self.assertTrue(all(player.remaining_projection == 30 for player in self.test_state.live_states.values()))
        live = self.client.get("/api/live/state").get_json()
        for lineup in live["lineups"]:
            self.assertAlmostEqual(
                lineup["projected_total"],
                sum(slot["total_projected_points"] for slot in lineup["slots"]),
                places=2,
            )

    def test_all_selected_entries_without_quality_gate(self):
        from dataclasses import replace
        self.test_state.contests["Scorcher"] = replace(self.test_state.contests["Scorcher"],
            slots=("Flex",), minimum_salary=0, maximum_salary=8000)
        response = self.client.post("/api/planner/generate", json={"contest_distribution": {"Scorcher": 5}})
        self.assertEqual(response.status_code, 200)
        plan = response.get_json()["plan"]
        self.assertEqual(len(plan["lineups"]), 5)
        self.assertEqual(plan["quality_threshold"], 0)
        self.assertEqual({l["contest_name"] for l in plan["lineups"]}, {"Scorcher"})
        self.assertEqual(len({s["card"]["card_id"] for l in plan["lineups"] for s in l["slots"]}), 5)
        for distribution in ({}, {"Scorcher": 0}, {"Scorcher": -1}, {"Scorcher": 6}, {"Spark": 1}, {"Unknown": 1}):
            self.assertEqual(self.client.post("/api/planner/generate", json={"contest_distribution": distribution}).status_code, 400)
        self.assertEqual(len(self.test_state.active_plan.lineups), 5)

    def test_roster_pricing_generation_rebuild_export_and_legacy_plan_restore(self):
        import csv
        import io
        from dataclasses import replace

        state = self.test_state
        state.contests["Scorcher"] = replace(state.contests["Scorcher"],
            slots=("Flex",), minimum_salary=0, maximum_salary=10000, collection_requirements={})
        state.roster_cards = [
            OwnedCard("costly", "Costly WR", "costlywr", "KC", "WR", 1.5, 12000, "Active", 2),
            OwnedCard("fits", "Fits WR", "fitswr", "BUF", "WR", 1.5, 9000, "Active", 3),
        ]
        state.snapshot.players = [
            DFFPlayerProjection(c.player_name, c.athlete_key, c.team, c.position,
                                8000 if c.card_id == "costly" else 6000, 30, 30)
            for c in state.roster_cards
        ]
        response = self.client.post("/api/planner/generate", json={
            "contest_distribution": {"Scorcher": 1}, "salary_source": "dff"})
        self.assertEqual(response.status_code, 200, response.get_json())
        lineup = state.active_plan.lineups[0]
        self.assertEqual(lineup.slots[0].card.card.card_id, "fits")
        self.assertEqual(lineup.total_salary, 9000)
        self.assertEqual(lineup.total_projection, 45)
        self.assertEqual(state.salary_source, "roster")
        self.assertEqual(state.active_plan.salary_source, "roster")
        candidates = self.client.get("/api/planner/candidates", query_string={
            "lineup_id": lineup.lineup_id, "slot_name": "Flex"}).get_json()["cards"]
        self.assertNotIn("costly", {c["card_id"] for c in candidates})
        response = self.client.post("/api/planner/edit_lineup", json={
            "lineup_id": lineup.lineup_id, "action": "rebuild"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(lineup.total_salary, 9000)

        # Simulate an old plan that fitted the cap only because it used base salaries.
        self.client.post("/api/planner/save")
        path = state.plans_dir / "latest_plan.json"
        saved = json.loads(path.read_text())
        saved["salary_source"] = "dff"
        saved["lineups"][0]["slots"][0]["card"]["card_id"] = "costly"
        saved["lineups"][0]["slots"][0]["is_locked"] = True
        saved["lineups"][0]["unused_salary_flag"] = True
        saved["lineups"][0]["unused_salary_note"] = "Stale base salary warning"
        saved["contest_benchmarks"] = {"Scorcher": {"salary": 8000, "benchmark_score": 45}}
        path.write_text(json.dumps(saved))
        response = self.client.post("/api/planner/load")
        self.assertEqual(response.status_code, 200, response.get_json())
        restored = state.active_plan.lineups[0]
        self.assertEqual(restored.slots[0].card.card.card_id, "costly")
        self.assertTrue(restored.slots[0].is_locked)
        self.assertEqual(restored.total_salary, 12000)
        self.assertEqual(restored.remaining_salary, -2000)
        self.assertFalse(restored.is_valid)
        self.assertFalse(restored.unused_salary_flag)
        self.assertEqual(state.active_plan.contest_benchmarks, {})
        self.assertEqual(state.active_plan.salary_source, "roster")
        exported = list(csv.DictReader(io.StringIO(self.client.get("/api/planner/export").get_data(as_text=True))))
        self.assertEqual(int(exported[0]["Weekly Salary"]), 12000)
        self.assertEqual(int(exported[0]["Lineup Total Salary"]), 12000)
        state.recompute_planner_cards()
        state.sync_active_plan()
        self.assertEqual(restored.total_salary, 12000)
        self.assertEqual([c.weekly_salary for c in state.planner_cards], [12000, 9000])

    def test_exclusion_swap_rebuild_and_restore(self):
        self.client.post("/api/planner/generate", json={"contest_distribution": {"Scorcher": 2}})
        lineup = self.test_state.active_plan.lineups[0]
        slot = lineup.slots[0]
        candidates = self.client.get("/api/planner/candidates", query_string={"lineup_id": lineup.lineup_id, "slot_name": slot.slot_name}).get_json()["cards"]
        other_used = {s.card.card.card_id for l in self.test_state.active_plan.lineups[1:] for s in l.slots}
        self.assertTrue(candidates)
        self.assertTrue(all(c["card_id"] not in other_used and c["remaining_salary"] >= 0 for c in candidates))
        replacement = next(c for c in candidates if c["card_id"] != slot.card.card.card_id)
        result = self.client.post("/api/planner/swap", json={"lineup_id": lineup.lineup_id, "slot_name": slot.slot_name, "new_card_id": replacement["card_id"]})
        self.assertEqual(result.status_code, 200)
        self.assertTrue(slot.is_locked)
        self.client.post("/api/planner/edit_lineup", json={"lineup_id": lineup.lineup_id, "action": "rebuild"})
        self.assertEqual(slot.card.card.card_id, replacement["card_id"])
        key = replacement["athlete_key"]
        self.assertEqual(self.client.post("/api/planner/exclude", json={"athlete_key": key, "excluded": True}).status_code, 200)
        self.assertFalse(slot.card.is_eligible)
        self.assertTrue(slot.is_locked)  # Rule 5: exclusion must not clear a protected lock
        self.assertFalse(lineup.is_valid)
        self.client.post("/api/planner/save")
        self.test_state.excluded_athlete_keys = set()
        self.client.post("/api/planner/load")
        self.assertIn(key, self.test_state.excluded_athlete_keys)
        # Excluded player may remain in locked slots (rule 5), but no unlocked slot should have them.
        for l in self.test_state.active_plan.lineups:
            for s in l.slots:
                if s.card and s.card.card.athlete_key == key:
                    self.assertTrue(s.is_locked, "Excluded player in an unlocked slot after regeneration")
        self.client.post("/api/planner/exclude", json={"athlete_key": key, "excluded": False})
        self.assertTrue(next(c for c in self.test_state.planner_cards if c.card.card_id == replacement["card_id"]).is_eligible)
        first = self.test_state.active_plan.lineups[0]
        self.assertEqual(self.client.post("/api/planner/edit_lineup", json={"lineup_id": first.lineup_id, "action": "remove"}).status_code, 200)
        self.assertEqual(len(self.test_state.active_plan.lineups), 1)

    def test_planner_state_endpoint(self):
        res = self.client.get("/api/planner/state")
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertEqual(data["status"], "success")
        self.assertIn("contests", data)
        self.assertIn("Scorcher", data["contests"])
        self.assertIn("Wildfire", data["contests"])
        self.assertFalse(data["contests"]["Spark"]["enabled"])
        self.assertEqual(len(data["cards"]), len(self.test_cards))

    def test_private_preview_requires_password_and_blocks_cross_site_requests(self):
        import base64
        token = base64.b64encode(b"friend:test-password").decode()
        with patch.dict(app.config, {"PREVIEW_PASSWORD": "test-password"}):
            self.assertEqual(self.client.get("/api/planner/state").status_code, 401)
            self.assertEqual(self.client.post("/api/planner/save").status_code, 401)
            headers = {"Authorization": f"Basic {token}"}
            response = self.client.get("/api/planner/state", headers=headers)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.headers["Cache-Control"], "no-store")
            headers["Sec-Fetch-Site"] = "cross-site"
            self.assertEqual(self.client.get("/", headers=headers).status_code, 200)
            self.assertEqual(self.client.get("/api/check_update", headers=headers).status_code, 403)
            headers["Origin"] = "https://another-site.example"
            self.assertEqual(self.client.post("/api/planner/save", headers=headers).status_code, 403)
            with patch.dict(app.config, {"PREVIEW_USERNAME": "alex"}):
                self.assertEqual(self.client.get("/", headers={"Authorization": f"Basic {token}"}).status_code, 401)
                alex_token = base64.b64encode(b"alex:test-password").decode()
                self.assertEqual(self.client.get("/", headers={"Authorization": f"Basic {alex_token}"}).status_code, 200)

    def test_requirements_endpoint(self):
        res = self.client.get("/api/requirements")
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertEqual(data["status"], "success")
        self.assertGreaterEqual(len(data["requirements"]), 4)

    @patch("planner_update_check.run_weekly_update_check")
    def test_check_update_endpoint(self, mock_update_check):
        mock_update_check.return_value = {
            "status": "NO_CHANGE",
            "message": "Projections are up to date",
            "snapshot_id": "TEST_SLATE",
        }
        res = self.client.get("/api/check_update")
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertEqual(data["status"], "NO_CHANGE")
        mock_update_check.assert_called_once()

    def test_generate_and_swap_api(self):
        res = self.client.post(
            "/api/planner/generate",
            data=json.dumps({"target_count": 2, "contest_distribution": {"Scorcher": 2}}),
            content_type="application/json",
        )
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertEqual(data["status"], "success")
        plan = data["plan"]
        self.assertEqual(len(plan["lineups"]), 2)

        l1 = plan["lineups"][0]
        l1_id = l1["lineup_id"]
        qb_slot = next(s for s in l1["slots"] if s["slot_kind"] == "QB")

        # Swap QB slot with empty to make lineup invalid
        swap_res = self.client.post(
            "/api/planner/swap",
            data=json.dumps({"lineup_id": l1_id, "slot_name": qb_slot["slot_name"], "new_card_id": ""}),
            content_type="application/json",
        )
        self.assertEqual(swap_res.status_code, 200)
        swap_data = swap_res.get_json()
        self.assertEqual(swap_data["status"], "success")
        self.assertFalse(swap_data["lineup"]["is_valid"])

    def test_dynamic_auto_mix_selection(self):
        # When contest_distribution is None or omitted, solver chooses the most profitable mix
        res = self.client.post(
            "/api/planner/generate",
            data=json.dumps({"target_count": 3, "contest_distribution": None}),
            content_type="application/json",
        )
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertEqual(data["status"], "success")
        plan = data["plan"]
        self.assertEqual(len(plan["lineups"]), 3)
        # All generated lineups must be valid and assigned to enabled contests
        for l in plan["lineups"]:
            self.assertIn(l["contest_name"], ("Inferno", "Volcano", "Flamethrower", "Scorcher", "Primetime Pyro", "Flex Appeal", "Wildfire"))
            self.assertTrue(l["is_valid"])

    def test_default_generation_stops_at_available_cards(self):
        self.test_state.contests = {"Scorcher": self.test_state.contests["Scorcher"]}
        keep = {"qb1", "qb2", "qb3", "qb4", "rb1", "wr1", "wr2", "te1"}
        self.test_state.roster_cards = [c for c in self.test_cards if c.card_id in keep]
        self.test_state.recompute_planner_cards()
        response = self.client.post("/api/planner/generate", json={})
        self.assertEqual(response.status_code, 200, response.get_json())
        lineups = response.get_json()["plan"]["lineups"]
        self.assertEqual(len(lineups), 1)
        self.assertTrue(lineups[0]["is_valid"])
        self.assertNotIn('id="target-lineup-count"', self.client.get("/").get_data(as_text=True))

    def test_generation_rejects_bad_limits(self):
        for payload in ([1], {"target_count": -1}, {"target_count": True}, {"target_count": "ten"}):
            self.assertEqual(self.client.post("/api/planner/generate", json=payload).status_code, 400)

    def test_toggle_lock_lineup_and_slot(self):
        # Generate plan first
        self.client.post(
            "/api/planner/generate",
            data=json.dumps({"target_count": 1, "contest_distribution": {"Scorcher": 1}}),
            content_type="application/json",
        )
        lineup_id = self.test_state.active_plan.lineups[0].lineup_id

        # Lock entire lineup
        res_lineup = self.client.post(
            "/api/planner/toggle_lock",
            data=json.dumps({"lineup_id": lineup_id}),
            content_type="application/json",
        )
        self.assertEqual(res_lineup.status_code, 200)
        lineup_data = res_lineup.get_json()["lineup"]
        self.assertTrue(lineup_data["is_locked"])
        self.assertTrue(all(s["is_locked"] for s in lineup_data["slots"]))

        # Unlock specific slot
        res_slot = self.client.post(
            "/api/planner/toggle_lock",
            data=json.dumps({"lineup_id": lineup_id, "slot_name": "QB"}),
            content_type="application/json",
        )
        self.assertEqual(res_slot.status_code, 200)
        lineup_data_2 = res_slot.get_json()["lineup"]
        qb_slot = next(s for s in lineup_data_2["slots"] if s["slot_name"] == "QB")
        self.assertFalse(qb_slot["is_locked"])

    def test_projection_override_api(self):
        res = self.client.post(
            "/api/overrides/update",
            data=json.dumps({"athlete_key": "patrickmahomes", "override_value": 31.4}),
            content_type="application/json",
        )
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertEqual(data["status"], "success")
        self.assertEqual(data["overrides"]["patrickmahomes"], 31.4)

        # Reset override
        res_reset = self.client.post(
            "/api/overrides/update",
            data=json.dumps({"athlete_key": "patrickmahomes", "reset": True}),
            content_type="application/json",
        )
        self.assertEqual(res_reset.status_code, 200)
        self.assertNotIn("patrickmahomes", res_reset.get_json()["overrides"])

    def test_save_and_load_plan_api(self):
        # Generate plan
        self.client.post(
            "/api/planner/generate",
            data=json.dumps({
                "target_count": 2,
                "contest_distribution": {"Scorcher": 2},
                "salary_source": "dff",
            }),
            content_type="application/json",
        )
        player = self.test_state.live_states["patrickmahomes"]
        player.final_points = 23.5
        player.game_status = "FINAL"
        player.kickoff_time = "2026-09-13T17:00:00+00:00"
        self.test_state.dashboard_schedule = {"season": 2026, "week": 1}
        # Save plan
        save_res = self.client.post("/api/planner/save")
        self.assertEqual(save_res.status_code, 200)
        self.assertEqual(save_res.get_json()["status"], "success")

        # Clear memory state
        self.test_state.active_plan = None
        self.test_state.live_states = {}
        self.test_state.dashboard_schedule = None

        # Load plan back
        load_res = self.client.post("/api/planner/load")
        self.assertEqual(load_res.status_code, 200)
        load_data = load_res.get_json()
        self.assertEqual(load_data["status"], "success")
        self.assertEqual(len(load_data["plan"]["lineups"]), 2)
        self.assertIsNotNone(self.test_state.active_plan)
        restored = self.test_state.live_states["patrickmahomes"]
        self.assertEqual(restored.final_points, 23.5)
        self.assertTrue(restored.is_locked())
        self.assertEqual(restored.kickoff_time, "2026-09-13T17:00:00+00:00")
        self.assertEqual(self.test_state.dashboard_schedule["week"], 1)

    def test_reset_keeps_saved_plan_loadable_with_locks_and_live_scores(self):
        self.assertEqual(self.client.post("/api/planner/generate", json={
            "target_count": 1, "contest_distribution": {"Scorcher": 1},
        }).status_code, 200)
        lineup = self.test_state.active_plan.lineups[0]
        slot = lineup.slots[0]
        response = self.client.post("/api/planner/toggle_lock", json={
            "lineup_id": lineup.lineup_id, "slot_name": slot.slot_name,
        })
        self.assertEqual(response.status_code, 200, response.get_json())
        player = self.test_state.live_states[slot.card.card.athlete_key]
        player.game_status = "IN_PROGRESS"
        player.live_points = 12.5

        self.assertEqual(self.client.post("/api/planner/save").status_code, 200)
        saved_path = self.test_state.plans_dir / "latest_plan.json"
        saved_bytes = saved_path.read_bytes()

        reset = self.client.post("/api/planner/reset_week")
        self.assertEqual(reset.status_code, 200, reset.get_json())
        self.assertIsNone(self.test_state.active_plan)
        self.assertEqual(saved_path.read_bytes(), saved_bytes)

        loaded = self.client.post("/api/planner/load")
        self.assertEqual(loaded.status_code, 200, loaded.get_json())
        restored_lineup = self.test_state.active_plan.lineups[0]
        restored_slot = next(s for s in restored_lineup.slots if s.slot_name == slot.slot_name)
        self.assertTrue(restored_slot.is_locked)
        restored_player = self.test_state.live_states[slot.card.card.athlete_key]
        self.assertEqual(restored_player.game_status, "IN_PROGRESS")
        self.assertEqual(restored_player.live_points, 12.5)
        self.assertEqual(saved_path.read_bytes(), saved_bytes)

    def test_friend_roster_persists_without_loading_another_workspace(self):
        import io
        roster = b"player_name,team,position,multiplier,salary,status\nPatrick Mahomes,KC,QB,1,7500,Active\n"
        response = self.client.post("/api/roster/upload", data={
            "roster_file": (io.BytesIO(roster), "roster.csv"),
        })
        self.assertEqual(response.status_code, 200)
        restarted = PlannerAppState(root_dir=self.tmp_dir.name)
        self.assertEqual(len(restarted.roster_cards), 1)
        self.assertEqual(restarted.roster_cards[0].player_name, "Patrick Mahomes")
        with tempfile.TemporaryDirectory() as other_root:
            separate = PlannerAppState(root_dir=other_root)
            self.assertEqual(separate.roster_cards, [])
            self.assertIsNone(separate.active_plan)

    def test_export_plan_csv(self):
        self.client.post(
            "/api/planner/generate",
            data=json.dumps({"target_count": 2, "contest_distribution": {"Scorcher": 2}}),
            content_type="application/json",
        )
        res = self.client.get("/api/planner/export")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.content_type, "text/csv; charset=utf-8")
        text = res.get_data(as_text=True)
        self.assertIn("Lineup ID,Contest,Slot,Player", text)

    def test_live_state_and_missing_inputs_endpoint(self):
        res = self.client.get("/api/live/state")
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertEqual(data["status"], "success")
        self.assertIn("missing_inputs", data)
        self.assertIn("thresholds_notice", data["missing_inputs"])

    def test_dashboard_schedule_import_is_validated_atomic_and_preserves_scores(self):
        from datetime import datetime, timezone

        path = Path(self.tmp_dir.name) / "board.json"
        board = {"season": "2026", "generated_at": "2026-09-08T00:00:00Z", "games": [
            {"week": 1, "away_team": "KC", "home_team": "BUF", "kickoff": "2026-09-10T00:20:00Z"},
            {"week": 2, "away_team": "KC", "home_team": "BUF", "kickoff": "2026-09-17T00:20:00Z"},
        ]}
        path.write_text(json.dumps(board))
        endpoint = "/api/live/import_dashboard_schedule"
        player = self.test_state.live_states["patrickmahomes"]
        player.live_points = 8.5
        with patch.dict("os.environ", {"NFL_DASHBOARD_BOARD_PATH": str(path)}):
            for body in ([], {"season": 2026, "week": 0}, {"season": 2025, "week": 1}, {"season": 2026, "week": 3}):
                self.assertEqual(self.client.post(endpoint, json=body).status_code, 400)
                self.assertEqual(player.kickoff_time, "")
            response = self.client.post(endpoint, json={"season": 2026, "week": 1})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.get_json()["schedule"]["updated_players"], 3)
            self.assertEqual(player.live_points, 8.5)
            self.assertFalse(player.is_locked(datetime(2026, 9, 10, 0, 19, tzinfo=timezone.utc)))
            self.assertTrue(player.is_locked(datetime(2026, 9, 10, 0, 20, tzinfo=timezone.utc)))
            self.assertEqual(self.client.post(endpoint, json={"season": 2026, "week": 1}).get_json()["schedule"]["updated_players"], 0)
            self.assertEqual(self.client.post(endpoint, json={"season": 2026, "week": 2}).status_code, 400)
            self.assertEqual(player.kickoff_time, "2026-09-10T00:20:00+00:00")
            player.kickoff_time = ""
            board["games"][0]["kickoff"] = "2026-09-10T00:20:00"  # No timezone.
            path.write_text(json.dumps(board))
            self.assertEqual(self.client.post(endpoint, json={"season": 2026, "week": 1}).status_code, 400)
            self.assertEqual(player.kickoff_time, "")
            path.unlink()
            self.assertEqual(self.client.post(endpoint, json={"season": 2026, "week": 1}).status_code, 503)

    def test_live_load_scenario_and_recommend_reallocations(self):
        # Load Thursday breakout scenario using {scenario: ...} key
        res_load = self.client.post(
            "/api/live/load_scenario",
            data=json.dumps({"scenario": "thursday_breakout"}),
            content_type="application/json",
        )
        self.assertEqual(res_load.status_code, 200)
        data_load = res_load.get_json()
        self.assertEqual(data_load["status"], "success")
        self.assertEqual(len(data_load["promising_lineups"]), 2)

        # Verify state endpoint returns all contract fields for the UI
        res_state = self.client.get("/api/live/state")
        self.assertEqual(res_state.status_code, 200)
        state_data = res_state.get_json()
        self.assertIn("lineups", state_data)
        self.assertIn("players", state_data)
        self.assertIn("missing_inputs_report", state_data)
        l0 = state_data["lineups"][0]
        self.assertIn("current_banked_score", l0)
        self.assertIn("remaining_expected_points", l0)
        self.assertIn("p_cash", l0)
        self.assertIn("p_first", l0)
        self.assertIn("p_top", l0)
        self.assertIn("top_tier_name", l0)
        self.assertIn("slots", l0)

        # Run recommend reallocations
        res_rec = self.client.post("/api/live/recommend_reallocations")
        self.assertEqual(res_rec.status_code, 200)
        data_rec = res_rec.get_json()
        self.assertEqual(data_rec["status"], "success")
        plan_dict = data_rec["reallocation_plan"]
        self.assertTrue(plan_dict["is_valid"])

        # Check state after recommendations has formatted swaps with aliases
        res_state_post_rec = self.client.get("/api/live/state")
        realloc_state = res_state_post_rec.get_json()["reallocation_plan"]
        self.assertIsNotNone(realloc_state)
        if realloc_state.get("swaps"):
            s0 = realloc_state["swaps"][0]
            self.assertIn("old_player_name", s0)
            self.assertIn("old_card_id", s0)
            self.assertIn("payout_delta", s0)

        # Apply reallocations
        res_apply = self.client.post("/api/live/apply_reallocations")
        self.assertEqual(res_apply.status_code, 200)
        data_apply = res_apply.get_json()
        self.assertEqual(data_apply["status"], "success")
        self.assertIn("Successfully applied", data_apply["message"])
        self.assertGreaterEqual(data_apply["swaps_applied"], 1)

    def test_live_player_update_api(self):
        # Load scenario and update a player's score and kickoff time
        self.client.post(
            "/api/live/load_scenario",
            data=json.dumps({"scenario_name": "thursday_breakout"}),
            content_type="application/json",
        )
        res_update = self.client.post(
            "/api/live/update_player_state",
            data=json.dumps({
                "athlete_key": "amonrastbrown",
                "game_status": "FINAL",
                "actual_score": 28.5,
                "remaining_projection": 0.0,
                "kickoff_time": "2026-09-13T20:20:00-04:00",
                "profile_type": "reliable",
            }),
            content_type="application/json",
        )
        self.assertEqual(res_update.status_code, 200)
        p_data = res_update.get_json()["player_state"]
        self.assertEqual(p_data["game_status"], "FINAL")
        self.assertEqual(p_data["final_points"], 28.5)

    def test_live_apply_reallocations_rejects_stale_or_locked_swaps(self):
        self.client.post(
            "/api/live/load_scenario",
            data=json.dumps({"scenario_name": "thursday_breakout"}),
            content_type="application/json",
        )
        self.client.post("/api/live/recommend_reallocations")

        # Mutate the lineup underneath to make the recommendation stale
        l1 = self.test_state.active_plan.lineups[0]
        l1.slots[0].card = None

        res_apply = self.client.post("/api/live/apply_reallocations")
        self.assertEqual(res_apply.status_code, 400)
        self.assertIn("Validation failed", res_apply.get_json()["message"])

    def test_live_simulate_timeline_locks_kickoffs(self):
        # Post Thursday simulation
        res = self.client.post(
            "/api/live/simulate_timeline",
            data=json.dumps({"preset": "post_thursday"}),
            content_type="application/json",
        )
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertEqual(data["status"], "success")
        self.assertIsNotNone(data["simulated_time"])


class V1Packet1RegressionTests(unittest.TestCase):
    """V1 packet 1: preserve locked placements, scores, and state rules."""

    @classmethod
    def setUpClass(cls):
        app.config.update(TESTING=True, MAX_CONTENT_LENGTH=8 * 1024 * 1024)

    def setUp(self):
        self.client = app.test_client()
        self.orig_state = app_module.STATE
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.test_state = PlannerAppState(root_dir=self.tmp_dir.name)
        app_module.STATE = self.test_state
        self.test_cards = [
            OwnedCard("qb1", "Patrick Mahomes", "patrickmahomes", "KC", "QB", 1.5, 7500, "Active", 1),
            OwnedCard("qb2", "Josh Allen", "joshallen", "BUF", "QB", 1.0, 8000, "Active", 2),
            OwnedCard("qb3", "Lamar Jackson", "lamarjackson", "BAL", "QB", 1.0, 7800, "Active", 3),
            OwnedCard("qb4", "Jalen Hurts", "jalenhurts", "PHI", "QB", 1.0, 7600, "Active", 4),
            OwnedCard("rb1", "Derrick Henry", "derrickhenry", "BAL", "RB", 1.0, 6500, "Active", 5),
            OwnedCard("rb2", "Breece Hall", "breecehall", "NYJ", "RB", 1.0, 6400, "Active", 6),
            OwnedCard("rb3", "Bijan Robinson", "bijanrobinson", "ATL", "RB", 1.0, 6300, "Active", 7),
            OwnedCard("rb4", "Saquon Barkley", "saquonbarkley", "PHI", "RB", 1.0, 6200, "Active", 8),
            OwnedCard("wr1", "Justin Jefferson", "justinjefferson", "MIN", "WR", 1.0, 7000, "Active", 9),
            OwnedCard("wr2", "CeeDee Lamb", "ceedeelamb", "DAL", "WR", 1.0, 7000, "Active", 10),
            OwnedCard("wr3", "Ja'Marr Chase", "jamarrchase", "CIN", "WR", 1.0, 6500, "Active", 11),
            OwnedCard("wr4", "Amon-Ra St. Brown", "amonrastbrown", "DET", "WR", 1.0, 6500, "Active", 12),
            OwnedCard("wr5", "Tyreek Hill", "tyreekhill", "MIA", "WR", 1.0, 6000, "Active", 13),
            OwnedCard("wr6", "Davante Adams", "davanteadams", "LV", "WR", 1.0, 5500, "Active", 14),
            OwnedCard("wr7", "A.J. Brown", "ajbrown", "PHI", "WR", 1.0, 6200, "Active", 15),
            OwnedCard("wr8", "Garrett Wilson", "garrettwilson", "NYJ", "WR", 1.0, 5800, "Active", 16),
            OwnedCard("te1", "Travis Kelce", "traviskelce", "KC", "TE", 1.0, 5500, "Active", 17),
            OwnedCard("te2", "Mark Andrews", "markandrews", "BAL", "TE", 1.0, 5000, "Active", 18),
            OwnedCard("te3", "Sam LaPorta", "samlaporta", "DET", "TE", 1.0, 4800, "Active", 19),
            OwnedCard("te4", "Trey McBride", "treymcbride", "ARI", "TE", 1.0, 4600, "Active", 20),
        ]
        from dff_client import DFFPlayerProjection, ProjectionsSnapshot
        self.test_players = [
            DFFPlayerProjection(c.player_name, c.athlete_key, c.team, c.position, c.roster_salary, 20.0, 20.0)
            for c in self.test_cards
        ]
        snap = ProjectionsSnapshot(
            slate_id="TEST", source="unit_test", fetched_at="2026-09-08T00:00:00Z",
            total_raw_rows=len(self.test_players), offensive_player_count=len(self.test_players),
            positive_projection_count=len(self.test_players), dst_excluded_count=0,
            other_excluded_count=0, players=self.test_players, diagnostics=[],
        )
        self.test_state.roster_cards = self.test_cards
        self.test_state.snapshot = snap
        self.test_state.snapshot_store.save_snapshot(snap)
        self.test_state.recompute_planner_cards()

    def tearDown(self):
        app_module.STATE = self.orig_state
        self.tmp_dir.cleanup()

    def test_rebuild_preserves_thursday_locked_slot(self):
        """Rule 3: a Thursday Final player must not prevent rebuilding. Their slot stays fixed."""
        self.client.post("/api/planner/generate", json={"contest_distribution": {"Scorcher": 1}})
        lineup = self.test_state.active_plan.lineups[0]
        te_slot = next(s for s in lineup.slots if s.slot_kind == "TE")
        te_card_id = te_slot.card.card.card_id
        te_akey = te_slot.card.card.athlete_key

        # Mark TE as FINAL (Thursday game finished)
        self.test_state.live_states[te_akey].game_status = "FINAL"
        self.test_state.live_states[te_akey].final_points = 18.0

        # Rebuild should succeed, preserving the TE slot
        result = self.client.post("/api/planner/edit_lineup",
                                   json={"lineup_id": lineup.lineup_id, "action": "rebuild"})
        self.assertEqual(result.status_code, 200, result.get_json())
        rebuilt_te = next(s for s in lineup.slots if s.slot_kind == "TE")
        self.assertEqual(rebuilt_te.card.card.card_id, te_card_id)

    def test_rebuild_after_csv_omits_started_players_preserves_scores_and_eligibility(self):
        self.client.post("/api/planner/generate", json={"contest_distribution": {"Scorcher": 1}})
        lineup = self.test_state.active_plan.lineups[0]
        slot = next(s for s in lineup.slots if s.card.card.card_id == "qb1")
        slot.is_locked = True
        key = slot.card.card.athlete_key
        response = self.client.post("/api/live/update_player_state", json={
            "athlete_key": key, "game_status": "FINAL", "final_points": -2.0})
        self.assertEqual(response.status_code, 200, response.get_json())
        used = {s.card.card.card_id for s in lineup.slots}
        unused = next(c for c in self.test_cards if c.card_id not in used)
        self.client.post("/api/live/update_player_state", json={
            "athlete_key": unused.athlete_key, "game_status": "FINAL", "final_points": 100})
        csv_text = "player_name,team,position,salary,ppg_projection\n" + "\n".join(
            f"{p.player_name},{p.team},{p.position},{p.salary},25"
            for p in self.test_players if p.athlete_key not in {key, unused.athlete_key})
        csv_text += "\nReserve One,KC,WR,5000,10\nReserve Two,BUF,RB,5000,10\n"
        response = self.client.post("/api/projections/upload", data={
            "projection_file": (io.BytesIO(csv_text.encode()), "remaining-games.csv")})
        self.assertEqual(response.status_code, 200, response.get_json())
        scores = {k: v.to_dict() for k, v in self.test_state.live_states.items()}
        self.client.post("/api/planner/save")
        saved = (self.test_state.plans_dir / "latest_plan.json").read_bytes()

        for action in ("rebuild", "load", "generate"):
            with self.subTest(action=action):
                if action == "rebuild":
                    response = self.client.post("/api/planner/edit_lineup", json={
                        "lineup_id": lineup.lineup_id, "action": "rebuild"})
                elif action == "load":
                    response = self.client.post("/api/planner/load")
                else:
                    response = self.client.post("/api/planner/generate", json={
                        "contest_distribution": {"Scorcher": 1}})
                self.assertEqual(response.status_code, 200, response.get_json())
                current = self.test_state.active_plan.lineups[0]
                fixed = next(s for s in current.slots if s.slot_name == slot.slot_name)
                self.assertEqual(fixed.card.card.card_id, "qb1")
                self.assertTrue(fixed.is_locked)
                self.assertTrue(current.is_valid, current.validation_errors)
                self.assertEqual(fixed.card.adjusted_projection, -3.0)
                self.assertFalse(next(c for c in self.test_state.planner_cards
                                      if c.card.card_id == unused.card_id).is_eligible)
                self.assertNotIn(unused.card_id, {s.card.card.card_id for s in current.slots})
                self.assertEqual({k: v.to_dict() for k, v in self.test_state.live_states.items()}, scores)
                self.assertEqual((self.test_state.plans_dir / "latest_plan.json").read_bytes(), saved)

        # Zero finals and in-progress banked points remain valid without a CSV row.
        for update, expected in (({"game_status": "FINAL", "final_points": 0}, 0),
                                 ({"game_status": "IN_PROGRESS", "live_points": 5}, 7.5)):
            self.client.post("/api/live/update_player_state", json={"athlete_key": key, **update})
            response = self.client.post("/api/planner/edit_lineup", json={
                "lineup_id": current.lineup_id, "action": "rebuild"})
            self.assertEqual(response.status_code, 200, response.get_json())
            self.assertEqual(next(s.card.adjusted_projection for s in current.slots
                                  if s.card.card.card_id == "qb1"), expected)

        # A manual lock alone cannot make an unprojected upcoming player eligible.
        self.client.post("/api/live/update_player_state", json={
            "athlete_key": key, "game_status": "UPCOMING"})
        response = self.client.post("/api/planner/edit_lineup", json={
            "lineup_id": current.lineup_id, "action": "rebuild"})
        self.assertEqual(response.status_code, 400)
        self.assertIn("Locked card qb1 is unavailable", response.get_json()["message"])

    def test_exclusion_preserves_lock_on_assigned_copy(self):
        """Rule 5: excluding a player must never clear a locked slot."""
        self.client.post("/api/planner/generate", json={"contest_distribution": {"Scorcher": 1}})
        lineup = self.test_state.active_plan.lineups[0]
        slot = lineup.slots[0]
        slot.is_locked = True
        akey = slot.card.card.athlete_key

        self.client.post("/api/planner/exclude", json={"athlete_key": akey, "excluded": True})
        self.assertTrue(slot.is_locked)
        self.assertFalse(slot.card.is_eligible)

    def test_score_handling_with_multiplier(self):
        """Rule 8: raw scores 0, -2, 10 on a 1.5x copy yield 0, -3, 15 actual points."""
        import live_lineup_manager
        self.client.post("/api/planner/generate", json={"contest_distribution": {"Scorcher": 1}})
        akey = "patrickmahomes"
        p = self.test_state.live_states[akey]
        mult = 1.5  # qb1 multiplier

        for raw, expected in [(0.0, 0.0), (-2.0, -3.0), (10.0, 15.0)]:
            self.client.post("/api/live/update_player_state", json={
                "athlete_key": akey, "game_status": "FINAL",
                "actual_score": raw, "remaining_projection": 0.0,
            })
            p = self.test_state.live_states[akey]
            self.assertEqual(p.final_points, raw)
            self.assertEqual(p.remaining_projection, 0.0)
            actual = p.effective_mean(mult)
            self.assertAlmostEqual(actual, expected, places=2,
                                    msg=f"Raw {raw} * {mult}x should be {expected}, got {actual}")

    def test_final_status_zeroes_remaining_projection(self):
        """Rule 9: final players contribute zero remaining projection."""
        self.client.post("/api/planner/generate", json={"contest_distribution": {"Scorcher": 1}})
        akey = "derrickhenry"
        # Set remaining projection first
        self.client.post("/api/live/update_player_state", json={
            "athlete_key": akey, "remaining_projection": 15.0,
        })
        self.assertEqual(self.test_state.live_states[akey].remaining_projection, 15.0)

        # Mark as FINAL
        self.client.post("/api/live/update_player_state", json={
            "athlete_key": akey, "game_status": "FINAL", "actual_score": 12.0,
        })
        p = self.test_state.live_states[akey]
        self.assertEqual(p.game_status, "FINAL")
        self.assertEqual(p.final_points, 12.0)
        self.assertEqual(p.remaining_projection, 0.0)
        self.assertEqual(p.remaining_stdev, 0.0)

    def test_invalid_score_rejected_without_mutation(self):
        """Rule 8: nonnumeric and nonfinite scores must not mutate state."""
        self.client.post("/api/planner/generate", json={"contest_distribution": {"Scorcher": 1}})
        akey = "derrickhenry"
        # Set a known state
        self.client.post("/api/live/update_player_state", json={
            "athlete_key": akey, "actual_score": 5.0,
        })
        self.assertEqual(self.test_state.live_states[akey].live_points, 5.0)

        # NaN, Infinity, and string should all be rejected
        for bad in [float("nan"), float("inf"), "hello"]:
            res = self.client.post("/api/live/update_player_state", json={
                "athlete_key": akey, "actual_score": bad,
            })
            self.assertEqual(res.status_code, 400, f"Should reject {bad}")
            self.assertEqual(self.test_state.live_states[akey].live_points, 5.0,
                              f"State mutated by invalid value {bad}")

    def test_save_load_preserves_all_state(self):
        """Rule 10: save/load preserves assignments, locks, scores, exclusions, and tiers."""
        self.client.post("/api/planner/generate", json={"contest_distribution": {"Scorcher": 1}})
        lineup = self.test_state.active_plan.lineups[0]
        lineup_id = lineup.lineup_id

        # Lock a slot
        slot = lineup.slots[0]
        self.client.post("/api/planner/toggle_lock", json={
            "lineup_id": lineup_id, "slot_name": slot.slot_name,
        })
        self.assertTrue(slot.is_locked)

        # Set a score
        akey = slot.card.card.athlete_key
        self.client.post("/api/live/update_player_state", json={
            "athlete_key": akey, "game_status": "FINAL", "actual_score": 22.0,
        })

        # Exclude another player
        other_akey = "tyreekhill"
        self.client.post("/api/planner/exclude", json={"athlete_key": other_akey, "excluded": True})

        # Save
        save_res = self.client.post("/api/planner/save")
        self.assertEqual(save_res.status_code, 200)

        # Nuke in-memory state
        self.test_state.active_plan = None
        self.test_state.live_states = {}
        self.test_state.excluded_athlete_keys = set()

        # Load
        load_res = self.client.post("/api/planner/load")
        self.assertEqual(load_res.status_code, 200)

        # Verify
        restored_lineup = self.test_state.active_plan.lineups[0]
        self.assertEqual(restored_lineup.lineup_id, lineup_id)
        restored_slot = next(s for s in restored_lineup.slots if s.slot_name == slot.slot_name)
        self.assertTrue(restored_slot.is_locked)
        self.assertIn(other_akey, self.test_state.excluded_athlete_keys)
        restored_p = self.test_state.live_states[akey]
        self.assertEqual(restored_p.final_points, 22.0)
        self.assertEqual(restored_p.game_status, "FINAL")

    def test_generation_failure_preserves_existing_plan(self):
        """Rule 10: solver failure must not discard protected entries."""
        self.client.post("/api/planner/generate", json={"contest_distribution": {"Scorcher": 1}})
        plan_before = self.test_state.active_plan
        lineup_count_before = len(plan_before.lineups)
        self.assertGreater(lineup_count_before, 0)

        # Try to generate with impossible distribution (non-existent contest)
        res = self.client.post("/api/planner/generate", json={
            "contest_distribution": {"NoSuchContest": 1},
        })
        self.assertEqual(res.status_code, 400)
        # Original plan must be intact
        self.assertEqual(len(self.test_state.active_plan.lineups), lineup_count_before)


class V1Packet2ManualConstructionTests(unittest.TestCase):
    """V1 packet 2: empty entries, editable drafts, clearing, and safe transfers."""

    @classmethod
    def setUpClass(cls):
        app.config.update(TESTING=True, MAX_CONTENT_LENGTH=8 * 1024 * 1024)

    def setUp(self):
        self.client = app.test_client()
        self.orig_state = app_module.STATE
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.test_state = PlannerAppState(root_dir=self.tmp_dir.name)
        app_module.STATE = self.test_state
        self.test_cards = [
            OwnedCard("qb1", "Patrick Mahomes", "patrickmahomes", "KC", "QB", 1.5, 7500, "Active", 1),
            OwnedCard("qb2", "Josh Allen", "joshallen", "BUF", "QB", 1.0, 8000, "Active", 2),
            OwnedCard("qb3", "Lamar Jackson", "lamarjackson", "BAL", "QB", 1.0, 7800, "Active", 3),
            OwnedCard("qb4", "Jalen Hurts", "jalenhurts", "PHI", "QB", 1.0, 7600, "Active", 4),
            OwnedCard("qb5", "Anthony Richardson", "anthonyrichardson", "IND", "QB", 1.0, 15000, "Active", 21),
            OwnedCard("rb1", "Derrick Henry", "derrickhenry", "BAL", "RB", 1.0, 6500, "Active", 5),
            OwnedCard("rb2", "Breece Hall", "breecehall", "NYJ", "RB", 1.0, 6400, "Active", 6),
            OwnedCard("rb3", "Bijan Robinson", "bijanrobinson", "ATL", "RB", 1.0, 6300, "Active", 7),
            OwnedCard("rb4", "Saquon Barkley", "saquonbarkley", "PHI", "RB", 1.0, 6200, "Active", 8),
            OwnedCard("wr1", "Justin Jefferson", "justinjefferson", "MIN", "WR", 1.0, 7000, "Active", 9),
            OwnedCard("wr1b", "Justin Jefferson", "justinjefferson", "MIN", "WR", 1.0, 7000, "Active", 22),
            OwnedCard("wr2", "CeeDee Lamb", "ceedeelamb", "DAL", "WR", 1.0, 7000, "Active", 10),
            OwnedCard("wr3", "Ja'Marr Chase", "jamarrchase", "CIN", "WR", 1.0, 6500, "Active", 11),
            OwnedCard("wr4", "Amon-Ra St. Brown", "amonrastbrown", "DET", "WR", 1.0, 6500, "Active", 12),
            OwnedCard("wr5", "Tyreek Hill", "tyreekhill", "MIA", "WR", 1.0, 6000, "Active", 13),
            OwnedCard("wr6", "Davante Adams", "davanteadams", "LV", "WR", 1.0, 5500, "Active", 14),
            OwnedCard("wr8", "Garrett Wilson", "garrettwilson", "NYJ", "WR", 1.0, 5800, "Active", 16),
            OwnedCard("te1", "Travis Kelce", "traviskelce", "KC", "TE", 1.0, 5500, "Active", 17),
            OwnedCard("te2", "Mark Andrews", "markandrews", "BAL", "TE", 1.0, 5000, "Active", 18),
            OwnedCard("te3", "Sam LaPorta", "samlaporta", "DET", "TE", 1.0, 4800, "Active", 19),
            OwnedCard("te4", "Trey McBride", "treymcbride", "ARI", "TE", 1.0, 4600, "Active", 20),
        ]
        seen_athletes: set[str] = set()
        self.test_players = []
        for card in self.test_cards:
            if card.athlete_key in seen_athletes:
                continue
            seen_athletes.add(card.athlete_key)
            self.test_players.append(
                DFFPlayerProjection(card.player_name, card.athlete_key, card.team, card.position, card.roster_salary, 20.0, 20.0)
            )
        snapshot = ProjectionsSnapshot(
            slate_id="PACKET2", source="unit_test", fetched_at="2026-09-11T00:00:00Z",
            total_raw_rows=len(self.test_players), offensive_player_count=len(self.test_players),
            positive_projection_count=len(self.test_players), dst_excluded_count=0,
            other_excluded_count=0, players=self.test_players, diagnostics=[],
        )
        self.test_state.roster_cards = self.test_cards
        self.test_state.snapshot = snapshot
        self.test_state.snapshot_store.save_snapshot(snapshot)
        self.test_state.recompute_planner_cards()

    def tearDown(self):
        app_module.STATE = self.orig_state
        self.tmp_dir.cleanup()

    def _card(self, card_id):
        return next(c for c in self.test_state.planner_cards if c.card.card_id == card_id)

    def _build_lineup(self, lineup_id, contest_name, card_ids):
        contest = self.test_state.contests[contest_name]
        self.assertEqual(len(contest.slots), len(card_ids))
        seen_kinds: dict[str, int] = {}
        slots = []
        for kind, card_id in zip(contest.slots, card_ids):
            seen_kinds[kind] = seen_kinds.get(kind, 0) + 1
            label = kind if seen_kinds[kind] == 1 else f"{kind}{seen_kinds[kind]}"
            card = self._card(card_id) if card_id else None
            slots.append(LineupSlotAssignment(slot_name=label, slot_kind=kind, card=card))
        return PlannerLineup(lineup_id=lineup_id, contest_name=contest_name, slots=slots)

    def _install_plan(self, lineups, contest_distribution=None):
        now = datetime.now(timezone.utc).isoformat()
        plan = WeeklyPlan(
            plan_id="packet2", name="Packet 2", created_at=now, updated_at=now,
            slate_id="PACKET2", salary_source="dff", contests=dict(self.test_state.contests),
            lineups=lineups, roster_cards=list(self.test_state.planner_cards),
            projection_overrides={}, excluded_athlete_keys=[],
            inventory_summary={"contest_distribution": contest_distribution},
        )
        self.test_state.active_plan = plan
        self.test_state.sync_active_plan()
        return plan

    @staticmethod
    def _snapshot(plan):
        return [
            (l.lineup_id, l.is_locked, [
                (s.slot_name, s.card.card.card_id if s.card else None, s.is_locked) for s in l.slots
            ])
            for l in plan.lineups
        ]

    def test_add_empty_entry_and_entry_cap(self):
        self.client.post("/api/planner/generate", json={"contest_distribution": {"Scorcher": 1}})
        self.assertEqual(len(self.test_state.active_plan.lineups), 1)

        # Scorcher allows 5 entries; add four empty drafts up to the cap.
        for expected_count in (2, 3, 4, 5):
            res = self.client.post("/api/planner/edit_lineup", json={"action": "add", "contest_name": "Scorcher", "contest_distribution": {"Scorcher": 5}})
            self.assertEqual(res.status_code, 200, res.get_json())
            self.assertEqual(len(self.test_state.active_plan.lineups), expected_count)

        draft = self.test_state.active_plan.lineups[-1]
        self.assertEqual([s.slot_name for s in draft.slots], ["QB", "RB", "WR", "TE", "Flex"])
        self.assertTrue(all(s.card is None for s in draft.slots))
        self.assertFalse(draft.is_valid)
        self.assertEqual(draft.total_salary, 0)

        # Additional manual entries beyond default limit succeed without being blocked.
        res_sixth = self.client.post("/api/planner/edit_lineup", json={"action": "add", "contest_name": "Scorcher"})
        self.assertEqual(res_sixth.status_code, 200, res_sixth.get_json())
        self.assertEqual(len(self.test_state.active_plan.lineups), 6)

        # An explicit entry_limit passed in the request is enforced when provided.
        res_limited = self.client.post("/api/planner/edit_lineup", json={"action": "add", "contest_name": "Scorcher", "entry_limit": 6})
        self.assertEqual(res_limited.status_code, 400)
        self.assertIn("allowed entries", res_limited.get_json()["message"])

        # Adding a contest that wasn't in the initial generation succeeds.
        res = self.client.post("/api/planner/edit_lineup", json={"action": "add", "contest_name": "Volcano"})
        self.assertEqual(res.status_code, 200, res.get_json())
        self.assertEqual(len(self.test_state.active_plan.lineups), 7)

        # An unknown or invalid contest is rejected without mutation.
        snap_after_volcano = self._snapshot(self.test_state.active_plan)
        res = self.client.post("/api/planner/edit_lineup", json={"action": "add", "contest_name": "InvalidContest"})
        self.assertEqual(res.status_code, 400)
        self.assertEqual(self._snapshot(self.test_state.active_plan), snap_after_volcano)

    def test_can_add_more_infernos_and_flamethrowers_beyond_initial_selection(self):
        # Generate 1 Inferno and 1 Flamethrower
        self.client.post("/api/planner/generate", json={"contest_distribution": {"Inferno": 1, "Flamethrower": 1}})
        self.assertEqual(len(self.test_state.active_plan.lineups), 2)

        # Adding more Infernos beyond initial selection succeeds
        res_inf2 = self.client.post("/api/planner/edit_lineup", json={"action": "add", "contest_name": "Inferno"})
        self.assertEqual(res_inf2.status_code, 200, res_inf2.get_json())
        self.assertEqual(len(self.test_state.active_plan.lineups), 3)

        # Add more Infernos beyond master default limit of 8
        for _ in range(8):
            res = self.client.post("/api/planner/edit_lineup", json={"action": "add", "contest_name": "Inferno"})
            self.assertEqual(res.status_code, 200)

        # We now have 10 Infernos in the plan (1 initial + 1 + 8)
        self.assertEqual(sum(1 for l in self.test_state.active_plan.lineups if l.contest_name == "Inferno"), 10)

        # Add more Flamethrowers beyond master default limit of 6
        for _ in range(6):
            res = self.client.post("/api/planner/edit_lineup", json={"action": "add", "contest_name": "Flamethrower"})
            self.assertEqual(res.status_code, 200)

        # We now have 7 Flamethrowers (1 initial + 6 added)
        self.assertEqual(sum(1 for l in self.test_state.active_plan.lineups if l.contest_name == "Flamethrower"), 7)

    def test_primetime_pyro_default_entry_limit(self):
        # Master contest default_entry_limit for Primetime Pyro is 3
        contest = self.test_state.contests["Primetime Pyro"]
        self.assertEqual(contest.default_entry_limit, 3)

    def test_generation_does_not_restrict_subsequent_manual_additions(self):
        # User starts with just 1 Scorcher from the batch generator.
        self.client.post("/api/planner/generate", json={"contest_distribution": {"Scorcher": 1}})
        self.assertEqual(len(self.test_state.active_plan.lineups), 1)

        # User decides to add a Volcano lineup - should succeed.
        res_volcano = self.client.post("/api/planner/edit_lineup", json={"action": "add", "contest_name": "Volcano"})
        self.assertEqual(res_volcano.status_code, 200, res_volcano.get_json())

        # User decides to add another Scorcher lineup beyond the initial generator count - should succeed.
        res_scorcher2 = self.client.post("/api/planner/edit_lineup", json={"action": "add", "contest_name": "Scorcher"})
        self.assertEqual(res_scorcher2.status_code, 200, res_scorcher2.get_json())

        # User adds a Primetime Pyro lineup - should succeed.
        res_pyro = self.client.post("/api/planner/edit_lineup", json={"action": "add", "contest_name": "Primetime Pyro"})
        self.assertEqual(res_pyro.status_code, 200, res_pyro.get_json())

        # All 4 lineups are in the plan.
        self.assertEqual(len(self.test_state.active_plan.lineups), 4)

    def test_partial_draft_fills_step_by_step(self):
        self.client.post("/api/planner/generate", json={"contest_distribution": {"Scorcher": 1}})
        added = self.client.post("/api/planner/edit_lineup", json={"action": "add", "contest_name": "Scorcher", "contest_distribution": {"Scorcher": 2}})
        self.assertEqual(added.status_code, 200, added.get_json())
        draft_id = added.get_json()["lineup"]["lineup_id"]
        draft = next(l for l in self.test_state.active_plan.lineups if l.lineup_id == draft_id)

        for slot in list(draft.slots):
            candidates = self.client.get(
                "/api/planner/candidates",
                query_string={"lineup_id": draft_id, "slot_name": slot.slot_name},
            ).get_json()["cards"]
            self.assertTrue(candidates, f"No candidates while filling {slot.slot_name} in an incomplete draft")
            chosen = min(candidates, key=lambda c: c["weekly_salary"])
            swap = self.client.post("/api/planner/swap", json={
                "lineup_id": draft_id, "slot_name": slot.slot_name, "new_card_id": chosen["card_id"],
            })
            self.assertEqual(swap.status_code, 200, swap.get_json())

        contest = self.test_state.contests["Scorcher"]
        self.assertTrue(all(s.card is not None for s in draft.slots))
        self.assertTrue(all(s.is_locked for s in draft.slots), "Every explicit placement must lock its slot")
        self.assertTrue(draft.is_valid, draft.validation_errors)
        self.assertGreaterEqual(draft.total_salary, contest.minimum_salary)
        self.assertLessEqual(draft.total_salary, contest.maximum_salary)

    def test_exclusion_prevents_new_placement_in_draft(self):
        self.client.post("/api/planner/generate", json={"contest_distribution": {"Scorcher": 1}})
        added = self.client.post("/api/planner/edit_lineup", json={"action": "add", "contest_name": "Scorcher", "contest_distribution": {"Scorcher": 2}})
        draft_id = added.get_json()["lineup"]["lineup_id"]
        excluded_card_id = "qb4"
        athlete_key = self._card(excluded_card_id).card.athlete_key
        self.client.post("/api/planner/exclude", json={"athlete_key": athlete_key, "excluded": True})

        candidates = self.client.get(
            "/api/planner/candidates",
            query_string={"lineup_id": draft_id, "slot_name": "QB"},
        ).get_json()["cards"]
        self.assertNotIn(excluded_card_id, {c["card_id"] for c in candidates})

        swap = self.client.post("/api/planner/swap", json={
            "lineup_id": draft_id, "slot_name": "QB", "new_card_id": excluded_card_id,
        })
        self.assertEqual(swap.status_code, 400)
        draft = next(l for l in self.test_state.active_plan.lineups if l.lineup_id == draft_id)
        self.assertIsNone(draft.slots[0].card)

    def test_clear_slot_and_protected_slots(self):
        self.client.post("/api/planner/generate", json={"contest_distribution": {"Scorcher": 1}})
        lineup = self.test_state.active_plan.lineups[0]
        first, second = lineup.slots[0], lineup.slots[1]

        res = self.client.post("/api/planner/edit_lineup", json={
            "lineup_id": lineup.lineup_id, "action": "clear", "slot_name": first.slot_name,
        })
        self.assertEqual(res.status_code, 200, res.get_json())
        self.assertIsNone(first.card)
        self.assertFalse(first.is_locked)
        self.assertFalse(lineup.is_valid)

        # A manually locked slot cannot be cleared.
        self.client.post("/api/planner/toggle_lock", json={"lineup_id": lineup.lineup_id, "slot_name": second.slot_name})
        locked_card_id = second.card.card.card_id
        res = self.client.post("/api/planner/edit_lineup", json={
            "lineup_id": lineup.lineup_id, "action": "clear", "slot_name": second.slot_name,
        })
        self.assertEqual(res.status_code, 400)
        self.assertEqual(second.card.card.card_id, locked_card_id)

        # After a manual unlock, final players can be cleared and placed again.
        self.client.post("/api/planner/toggle_lock", json={"lineup_id": lineup.lineup_id, "slot_name": second.slot_name})
        self.assertFalse(second.is_locked)
        athlete_key = second.card.card.athlete_key
        self.client.post("/api/live/update_player_state", json={
            "athlete_key": athlete_key, "game_status": "FINAL", "actual_score": 11.0,
        })
        res = self.client.post("/api/planner/edit_lineup", json={
            "lineup_id": lineup.lineup_id, "action": "clear", "slot_name": second.slot_name,
        })
        self.assertEqual(res.status_code, 200, res.get_json())
        self.assertIsNone(second.card)
        candidates = self.client.get("/api/planner/candidates", query_string={
            "lineup_id": lineup.lineup_id, "slot_name": second.slot_name,
        }).get_json()["cards"]
        self.assertIn(locked_card_id, {c["card_id"] for c in candidates})
        res = self.client.post("/api/planner/swap", json={
            "lineup_id": lineup.lineup_id, "slot_name": second.slot_name, "new_card_id": locked_card_id,
        })
        self.assertEqual(res.status_code, 200, res.get_json())
        self.assertEqual(second.card.card.card_id, locked_card_id)
        # Replacing a final player also remains possible and preserves their score.
        res = self.client.post("/api/planner/swap", json={
            "lineup_id": lineup.lineup_id, "slot_name": second.slot_name, "new_card_id": None,
        })
        self.assertEqual(res.status_code, 200, res.get_json())
        self.assertEqual(self.test_state.live_states[athlete_key].final_points, 11.0)

    def test_move_into_empty_slot(self):
        self.client.post("/api/planner/generate", json={"contest_distribution": {"Scorcher": 1}})
        source = self.test_state.active_plan.lineups[0]
        added = self.client.post("/api/planner/edit_lineup", json={"action": "add", "contest_name": "Scorcher", "contest_distribution": {"Scorcher": 2}})
        draft_id = added.get_json()["lineup"]["lineup_id"]
        source_slot = source.slots[0]
        moved_card_id = source_slot.card.card.card_id

        res = self.client.post("/api/planner/move", json={
            "source_lineup_id": source.lineup_id, "source_slot_name": source_slot.slot_name,
            "destination_lineup_id": draft_id, "destination_slot_name": "QB",
        })
        self.assertEqual(res.status_code, 200, res.get_json())
        payload = res.get_json()
        self.assertEqual(payload["affected_lineup_ids"], [source.lineup_id, draft_id])
        self.assertEqual(len(payload["lineups"]), 2)

        draft = next(l for l in self.test_state.active_plan.lineups if l.lineup_id == draft_id)
        destination_slot = next(s for s in draft.slots if s.slot_name == "QB")
        self.assertEqual(destination_slot.card.card.card_id, moved_card_id)
        self.assertTrue(destination_slot.is_locked)
        self.assertIsNone(source_slot.card)
        self.assertFalse(source_slot.is_locked)
        self.assertFalse(source.is_valid)

    def test_exchange_two_unlocked_entries(self):
        plan = self._install_plan([
            self._build_lineup("a", "Scorcher", ["qb1", "rb1", "wr1", "te1", "rb2"]),
            self._build_lineup("b", "Scorcher", ["qb2", "rb3", "wr2", "te2", "wr3"]),
        ], contest_distribution={"Scorcher": 2})
        a, b = plan.lineups
        a_qb, b_qb = a.slots[0], b.slots[0]
        a_card_id, b_card_id = a_qb.card.card.card_id, b_qb.card.card.card_id

        res = self.client.post("/api/planner/move", json={
            "source_lineup_id": "a", "source_slot_name": a_qb.slot_name,
            "destination_lineup_id": "b", "destination_slot_name": b_qb.slot_name,
        })
        self.assertEqual(res.status_code, 200, res.get_json())
        self.assertEqual(a_qb.card.card.card_id, b_card_id)
        self.assertEqual(b_qb.card.card.card_id, a_card_id)
        self.assertTrue(a_qb.is_locked and b_qb.is_locked)
        self.assertTrue(a.is_valid, a.validation_errors)
        self.assertTrue(b.is_valid, b.validation_errors)

    def test_exchange_within_single_lineup(self):
        plan = self._install_plan([
            self._build_lineup("a", "Scorcher", ["qb1", "rb1", "wr1", "te1", "rb2"]),
        ], contest_distribution={"Scorcher": 1})
        lineup = plan.lineups[0]
        rb_slot, flex_slot = lineup.slots[1], lineup.slots[4]
        rb_card_id, flex_card_id = rb_slot.card.card.card_id, flex_slot.card.card.card_id

        res = self.client.post("/api/planner/move", json={
            "source_lineup_id": "a", "source_slot_name": rb_slot.slot_name,
            "destination_lineup_id": "a", "destination_slot_name": flex_slot.slot_name,
        })
        self.assertEqual(res.status_code, 200, res.get_json())
        self.assertEqual(res.get_json()["affected_lineup_ids"], ["a"])
        self.assertEqual(rb_slot.card.card.card_id, flex_card_id)
        self.assertEqual(flex_slot.card.card.card_id, rb_card_id)
        self.assertTrue(rb_slot.is_locked and flex_slot.is_locked)
        self.assertTrue(lineup.is_valid, lineup.validation_errors)

    def test_move_rejections_leave_both_entries_unchanged(self):
        # 1. An exchange that pushes one entry over the salary cap.
        plan = self._install_plan([
            self._build_lineup("a", "Scorcher", ["qb4", "rb1", "wr1", "te1", "rb2"]),
            self._build_lineup("b", "Scorcher", ["qb5", "rb3", "wr6", "te3", "wr8"]),
        ], contest_distribution={"Scorcher": 2})
        before = self._snapshot(plan)
        res = self.client.post("/api/planner/move", json={
            "source_lineup_id": "a", "source_slot_name": "QB",
            "destination_lineup_id": "b", "destination_slot_name": "QB",
        })
        self.assertEqual(res.status_code, 400, res.get_json())
        self.assertIn("exceeds maximum cap", res.get_json()["message"])
        self.assertEqual(self._snapshot(plan), before)

        # 2. An exchange that violates slot position rules.
        plan = self._install_plan([
            self._build_lineup("a", "Scorcher", ["qb4", "rb1", "wr1", "te1", "rb2"]),
            self._build_lineup("b", "Scorcher", ["qb3", "rb3", "wr6", "te3", "wr8"]),
        ], contest_distribution={"Scorcher": 2})
        before = self._snapshot(plan)
        res = self.client.post("/api/planner/move", json={
            "source_lineup_id": "a", "source_slot_name": "QB",
            "destination_lineup_id": "b", "destination_slot_name": "RB",
        })
        self.assertEqual(res.status_code, 400, res.get_json())
        self.assertIn("requires", res.get_json()["message"])
        self.assertEqual(self._snapshot(plan), before)

        # 3. An exchange that repeats an athlete inside one entry.
        plan = self._install_plan([
            self._build_lineup("a", "Scorcher", ["qb4", "rb1", "wr1", "te1", "rb2"]),
            self._build_lineup("b", "Scorcher", ["qb3", "rb3", "wr1b", "te3", "wr6"]),
        ], contest_distribution={"Scorcher": 2})
        before = self._snapshot(plan)
        res = self.client.post("/api/planner/move", json={
            "source_lineup_id": "a", "source_slot_name": "WR",
            "destination_lineup_id": "b", "destination_slot_name": "Flex",
        })
        self.assertEqual(res.status_code, 400, res.get_json())
        self.assertIn("Duplicate athlete", res.get_json()["message"])
        self.assertEqual(self._snapshot(plan), before)

    def test_manual_locks_block_moves_but_game_status_does_not(self):
        plan = self._install_plan([
            self._build_lineup("a", "Scorcher", ["qb4", "rb1", "wr1", "te1", "rb2"]),
            self._build_lineup("b", "Scorcher", ["qb3", "rb3", "wr6", "te3", "wr8"]),
        ], contest_distribution={"Scorcher": 2})
        a, b = plan.lineups
        before = self._snapshot(plan)

        # A manually locked destination rejects the move.
        self.client.post("/api/planner/toggle_lock", json={"lineup_id": "b", "slot_name": "QB"})
        res = self.client.post("/api/planner/move", json={
            "source_lineup_id": "a", "source_slot_name": "QB",
            "destination_lineup_id": "b", "destination_slot_name": "QB",
        })
        self.assertEqual(res.status_code, 400)
        self.assertIn("locked", res.get_json()["message"])
        self.client.post("/api/planner/toggle_lock", json={"lineup_id": "b", "slot_name": "QB"})
        self.assertEqual(self._snapshot(plan), before)

        # A locked source lineup rejects the move.
        self.client.post("/api/planner/toggle_lock", json={"lineup_id": "a"})
        res = self.client.post("/api/planner/move", json={
            "source_lineup_id": "a", "source_slot_name": "QB",
            "destination_lineup_id": "b", "destination_slot_name": "QB",
        })
        self.assertEqual(res.status_code, 400)
        self.client.post("/api/planner/toggle_lock", json={"lineup_id": "a"})
        self.assertEqual(self._snapshot(plan), before)

        # Final and in-progress players may be exchanged to match real entries.
        athlete_key = a.slots[0].card.card.athlete_key
        other_key = b.slots[0].card.card.athlete_key
        self.client.post("/api/live/update_player_state", json={
            "athlete_key": athlete_key, "game_status": "FINAL", "actual_score": 9.0,
        })
        self.client.post("/api/live/update_player_state", json={
            "athlete_key": other_key, "game_status": "IN_PROGRESS", "actual_score": 7.5,
        })
        scores = {key: player.to_dict() for key, player in self.test_state.live_states.items()}
        res = self.client.post("/api/planner/move", json={
            "source_lineup_id": "a", "source_slot_name": "QB",
            "destination_lineup_id": "b", "destination_slot_name": "QB",
        })
        self.assertEqual(res.status_code, 200, res.get_json())
        self.assertEqual(a.slots[0].card.card.athlete_key, other_key)
        self.assertEqual(b.slots[0].card.card.athlete_key, athlete_key)
        self.assertEqual({key: player.to_dict() for key, player in self.test_state.live_states.items()}, scores)
        res = self.client.post("/api/planner/edit_lineup", json={"action": "remove", "lineup_id": "b"})
        self.assertEqual(res.status_code, 200, res.get_json())
        self.assertEqual(self.test_state.live_states[athlete_key].final_points, 9.0)


class V1Packet4StartedPlayerTests(unittest.TestCase):
    """V1 packet 4: regeneration must keep started players fixed, and must explain conflicts."""

    @classmethod
    def setUpClass(cls):
        app.config.update(TESTING=True, MAX_CONTENT_LENGTH=8 * 1024 * 1024)

    def setUp(self):
        self.client = app.test_client()
        self.orig_state = app_module.STATE
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.test_state = PlannerAppState(root_dir=self.tmp_dir.name)
        app_module.STATE = self.test_state
        self.test_cards = [
            OwnedCard("qb1", "Patrick Mahomes", "patrickmahomes", "KC", "QB", 1.5, 7500, "Active", 1),
            OwnedCard("qb2", "Josh Allen", "joshallen", "BUF", "QB", 1.0, 8000, "Active", 2),
            OwnedCard("qb3", "Lamar Jackson", "lamarjackson", "BAL", "QB", 1.0, 7800, "Active", 3),
            OwnedCard("qb4", "Jalen Hurts", "jalenhurts", "PHI", "QB", 1.0, 7600, "Active", 4),
            OwnedCard("rb1", "Derrick Henry", "derrickhenry", "BAL", "RB", 1.0, 6500, "Active", 5),
            OwnedCard("rb2", "Breece Hall", "breecehall", "NYJ", "RB", 1.0, 6400, "Active", 6),
            OwnedCard("rb3", "Bijan Robinson", "bijanrobinson", "ATL", "RB", 1.0, 6300, "Active", 7),
            OwnedCard("rb4", "Saquon Barkley", "saquonbarkley", "PHI", "RB", 1.0, 6200, "Active", 8),
            OwnedCard("wr1", "Justin Jefferson", "justinjefferson", "MIN", "WR", 1.0, 7000, "Active", 9),
            OwnedCard("wr2", "CeeDee Lamb", "ceedeelamb", "DAL", "WR", 1.0, 7000, "Active", 10),
            OwnedCard("wr3", "Ja'Marr Chase", "jamarrchase", "CIN", "WR", 1.0, 6500, "Active", 11),
            OwnedCard("wr4", "Amon-Ra St. Brown", "amonrastbrown", "DET", "WR", 1.0, 6500, "Active", 12),
            OwnedCard("wr5", "Tyreek Hill", "tyreekhill", "MIA", "WR", 1.0, 6000, "Active", 13),
            OwnedCard("wr6", "Davante Adams", "davanteadams", "LV", "WR", 1.0, 5500, "Active", 14),
            OwnedCard("wr7", "A.J. Brown", "ajbrown", "PHI", "WR", 1.0, 6200, "Active", 15),
            OwnedCard("wr8", "Garrett Wilson", "garrettwilson", "NYJ", "WR", 1.0, 5800, "Active", 16),
            OwnedCard("te1", "Travis Kelce", "traviskelce", "KC", "TE", 1.0, 5500, "Active", 17),
            OwnedCard("te2", "Mark Andrews", "markandrews", "BAL", "TE", 1.0, 5000, "Active", 18),
            OwnedCard("te3", "Sam LaPorta", "samlaporta", "DET", "TE", 1.0, 4800, "Active", 19),
            OwnedCard("te4", "Trey McBride", "treymcbride", "ARI", "TE", 1.0, 4600, "Active", 20),
        ]
        self.test_players = [
            DFFPlayerProjection(c.player_name, c.athlete_key, c.team, c.position, c.roster_salary, 20.0, 20.0)
            for c in self.test_cards
        ]
        snapshot = ProjectionsSnapshot(
            slate_id="PACKET4", source="unit_test", fetched_at="2026-09-12T00:00:00Z",
            total_raw_rows=len(self.test_players), offensive_player_count=len(self.test_players),
            positive_projection_count=len(self.test_players), dst_excluded_count=0,
            other_excluded_count=0, players=self.test_players, diagnostics=[],
        )
        self.test_state.roster_cards = self.test_cards
        self.test_state.snapshot = snapshot
        self.test_state.snapshot_store.save_snapshot(snapshot)
        self.test_state.recompute_planner_cards()

    def tearDown(self):
        app_module.STATE = self.orig_state
        self.tmp_dir.cleanup()

    def _generate(self, distribution):
        response = self.client.post("/api/planner/generate", json={"contest_distribution": distribution})
        self.assertEqual(response.status_code, 200, response.get_json())
        return self.test_state.active_plan

    def _started_slot(self, game_status):
        """Generate two Scorcher entries and mark the first entry's QB as started."""
        plan = self._generate({"Scorcher": 2})
        lineup = plan.lineups[0]
        slot = next(s for s in lineup.slots if s.slot_kind == "QB")
        card_id = slot.card.card.card_id
        athlete_key = slot.card.card.athlete_key
        self.assertFalse(slot.is_locked, "generated slots start unlocked")
        self.client.post("/api/live/update_player_state", json={
            "athlete_key": athlete_key, "game_status": game_status, "actual_score": 3.0,
        })
        return plan, lineup, slot, card_id, athlete_key

    def _assert_slot_held(self, lineup_id, slot_name, card_id):
        lineup = next(l for l in self.test_state.active_plan.lineups if l.lineup_id == lineup_id)
        slot = next(s for s in lineup.slots if s.slot_name == slot_name)
        self.assertIsNotNone(slot.card, "started placement was emptied by regeneration")
        self.assertEqual(slot.card.card.card_id, card_id)
        self.assertTrue(slot.is_locked, "regeneration should report the placement as locked")

    def _make_started_player_unattractive(self, athlete_key: str) -> None:
        """Drop the projection so regeneration would rather replace the player than keep them."""
        response = self.client.post("/api/overrides/update", json={
            "athlete_key": athlete_key, "override_value": 0.5,
        })
        self.assertEqual(response.status_code, 200, response.get_json())

    def test_regenerate_all_preserves_a_final_thursday_placement(self):
        plan, lineup, slot, card_id, athlete_key = self._started_slot("FINAL")
        lineup_id, slot_name = lineup.lineup_id, slot.slot_name
        self._make_started_player_unattractive(athlete_key)

        regenerated = self._generate({"Scorcher": 2})

        self._assert_slot_held(lineup_id, slot_name, card_id)
        self.assertGreater(len(regenerated.lineups), 0)
        self.assertEqual(self.test_state.live_states[athlete_key].final_points, 3.0)
        self.assertEqual(self.test_state.live_states[athlete_key].remaining_projection, 0.0)

    def test_regenerate_all_preserves_an_in_progress_placement(self):
        plan, lineup, slot, card_id, athlete_key = self._started_slot("IN_PROGRESS")
        lineup_id, slot_name = lineup.lineup_id, slot.slot_name
        self._make_started_player_unattractive(athlete_key)

        self._generate({"Scorcher": 2})

        self._assert_slot_held(lineup_id, slot_name, card_id)
        self.assertEqual(self.test_state.live_states[athlete_key].live_points, 3.0)

    def test_generate_with_two_locked_slots_in_one_lineup(self):
        """The packet 3 fix: two locked slots must not emit duplicate PuLP constraint names."""
        plan = self._generate({"Scorcher": 2})
        lineup = plan.lineups[0]
        for slot_kind in ("QB", "TE"):
            slot = next(s for s in lineup.slots if s.slot_kind == slot_kind)
            self.client.post("/api/planner/toggle_lock", json={
                "lineup_id": lineup.lineup_id, "slot_name": slot.slot_name,
            })
        held = {s.slot_name: s.card.card.card_id for s in lineup.slots if s.is_locked}
        self.assertEqual(len(held), 2)

        response = self.client.post("/api/planner/generate", json={"contest_distribution": {"Scorcher": 2}})
        self.assertEqual(response.status_code, 200, response.get_json())

        for slot_name, card_id in held.items():
            self._assert_slot_held(lineup.lineup_id, slot_name, card_id)

    def test_generate_explains_a_started_entry_in_an_unselected_contest(self):
        """Rule 11: deselecting a tier with a protected entry explains the conflict and keeps the plan."""
        plan, lineup, slot, card_id, athlete_key = self._started_slot("FINAL")
        before = [(l.lineup_id, s.slot_name, s.card.card.card_id if s.card else None, s.is_locked)
                  for l in plan.lineups for s in l.slots]

        response = self.client.post("/api/planner/generate", json={"contest_distribution": {"Volcano": 1}})

        self.assertEqual(response.status_code, 400)
        self.assertIn("unselected contest", response.get_json()["message"])
        after = [(l.lineup_id, s.slot_name, s.card.card.card_id if s.card else None, s.is_locked)
                 for l in self.test_state.active_plan.lineups for s in l.slots]
        self.assertEqual(after, before)


class V1CorrectionTests(unittest.TestCase):
    """Bounded V1 corrections: persistent remaining estimates and a first manual draft."""

    @classmethod
    def setUpClass(cls):
        app.config.update(TESTING=True, MAX_CONTENT_LENGTH=8 * 1024 * 1024)

    def setUp(self):
        self.client = app.test_client()
        self.orig_state = app_module.STATE
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.test_state = PlannerAppState(root_dir=self.tmp_dir.name)
        app_module.STATE = self.test_state
        self.test_cards = [
            OwnedCard("qb1", "Patrick Mahomes", "patrickmahomes", "KC", "QB", 1.5, 7500, "Active", 1),
            OwnedCard("qb2", "Josh Allen", "joshallen", "BUF", "QB", 1.0, 8000, "Active", 2),
            OwnedCard("qb3", "Lamar Jackson", "lamarjackson", "BAL", "QB", 1.0, 7800, "Active", 3),
            OwnedCard("qb4", "Jalen Hurts", "jalenhurts", "PHI", "QB", 1.0, 7600, "Active", 4),
            OwnedCard("qb5", "Anthony Richardson", "anthonyrichardson", "IND", "QB", 1.0, 15000, "Active", 21),
            OwnedCard("rb1", "Derrick Henry", "derrickhenry", "BAL", "RB", 1.0, 6500, "Active", 5),
            OwnedCard("rb2", "Breece Hall", "breecehall", "NYJ", "RB", 1.0, 6400, "Active", 6),
            OwnedCard("rb3", "Bijan Robinson", "bijanrobinson", "ATL", "RB", 1.0, 6300, "Active", 7),
            OwnedCard("rb4", "Saquon Barkley", "saquonbarkley", "PHI", "RB", 1.0, 6200, "Active", 8),
            OwnedCard("wr1", "Justin Jefferson", "justinjefferson", "MIN", "WR", 1.0, 7000, "Active", 9),
            OwnedCard("wr2", "CeeDee Lamb", "ceedeelamb", "DAL", "WR", 1.0, 7000, "Active", 10),
            OwnedCard("wr3", "Ja'Marr Chase", "jamarrchase", "CIN", "WR", 1.0, 6500, "Active", 11),
            OwnedCard("wr4", "Amon-Ra St. Brown", "amonrastbrown", "DET", "WR", 1.0, 6500, "Active", 12),
            OwnedCard("wr5", "Tyreek Hill", "tyreekhill", "MIA", "WR", 1.0, 6000, "Active", 13),
            OwnedCard("wr6", "Davante Adams", "davanteadams", "LV", "WR", 1.0, 5500, "Active", 14),
            OwnedCard("wr7", "A.J. Brown", "ajbrown", "PHI", "WR", 1.0, 6200, "Active", 15),
            OwnedCard("wr8", "Garrett Wilson", "garrettwilson", "NYJ", "WR", 1.0, 5800, "Active", 16),
            OwnedCard("te1", "Travis Kelce", "traviskelce", "KC", "TE", 1.0, 5500, "Active", 17),
            OwnedCard("te2", "Mark Andrews", "markandrews", "BAL", "TE", 1.0, 5000, "Active", 18),
            OwnedCard("te3", "Sam LaPorta", "samlaporta", "DET", "TE", 1.0, 4800, "Active", 19),
            OwnedCard("te4", "Trey McBride", "treymcbride", "ARI", "TE", 1.0, 4600, "Active", 20),
        ]
        self.test_players = [
            DFFPlayerProjection(c.player_name, c.athlete_key, c.team, c.position, c.roster_salary, 20.0, 20.0)
            for c in self.test_cards
        ]
        snapshot = ProjectionsSnapshot(
            slate_id="CORRECTION", source="unit_test", fetched_at="2026-09-12T00:00:00Z",
            total_raw_rows=len(self.test_players), offensive_player_count=len(self.test_players),
            positive_projection_count=len(self.test_players), dst_excluded_count=0,
            other_excluded_count=0, players=self.test_players, diagnostics=[],
        )
        self.test_state.roster_cards = self.test_cards
        self.test_state.snapshot = snapshot
        self.test_state.snapshot_store.save_snapshot(snapshot)
        self.test_state.recompute_planner_cards()

    def tearDown(self):
        app_module.STATE = self.orig_state
        self.tmp_dir.cleanup()

    def _generate(self, distribution):
        response = self.client.post("/api/planner/generate", json={"contest_distribution": distribution})
        self.assertEqual(response.status_code, 200, response.get_json())
        return self.test_state.active_plan

    def _live_player(self, athlete_key):
        return self.client.get("/api/live/state").get_json()["players"]

    def _add_draft(self, contest_name, entry_limit, distribution=None):
        return self.client.post("/api/planner/edit_lineup", json={
            "action": "add",
            "contest_name": contest_name,
            "entry_limit": entry_limit,
            "contest_distribution": distribution or {contest_name: entry_limit},
        })

    def test_seeded_projection_is_not_an_explicit_estimate(self):
        """A freshly seeded live state must not look like a user-supplied estimate."""
        seeded = self.test_state.live_states["patrickmahomes"]
        self.assertEqual(seeded.remaining_projection, 20.0)
        self.assertFalse(seeded.remaining_is_estimate)

    def test_unknown_in_progress_estimate_does_not_add_the_seed(self):
        """Rule 9: without an estimate, in-progress shows banked points only."""
        self._generate({"Scorcher": 1})
        akey = "patrickmahomes"
        self.client.post("/api/live/update_player_state", json={
            "athlete_key": akey, "game_status": "IN_PROGRESS", "actual_score": 10.0,
        })
        live = self.test_state.live_states[akey]
        self.assertFalse(live.remaining_is_estimate)
        # 10 raw * 1.5x, with the 20.0 seeded projection left out.
        self.assertEqual(live.effective_mean(1.5), 15.0)

        payload = next(p for p in self._live_player(akey) if p["athlete_key"] == akey)
        self.assertIs(payload["remaining_is_estimate"], False)

        lineup_payload = self.client.get("/api/live/state").get_json()["lineups"][0]
        qb_slot = next(s for s in lineup_payload["slots"] if s["card_id"] == "qb1")
        self.assertEqual(qb_slot["total_projected_points"], 15.0)

    def test_explicit_zero_estimate_is_known(self):
        """Zero is a supplied estimate, not a blank field."""
        self._generate({"Scorcher": 1})
        akey = "patrickmahomes"
        self.client.post("/api/live/update_player_state", json={
            "athlete_key": akey, "game_status": "IN_PROGRESS", "actual_score": 10.0,
            "remaining_projection": 0.0,
        })
        live = self.test_state.live_states[akey]
        self.assertTrue(live.remaining_is_estimate)
        self.assertEqual(live.remaining_projection, 0.0)
        self.assertEqual(live.effective_mean(1.5), 15.0)

    def test_cleared_estimate_returns_to_unknown(self):
        """Clearing the field sends the flag false and stops using the old value."""
        self._generate({"Scorcher": 1})
        akey = "patrickmahomes"
        self.client.post("/api/live/update_player_state", json={
            "athlete_key": akey, "game_status": "IN_PROGRESS", "actual_score": 10.0,
            "remaining_projection": 4.0,
        })
        self.assertEqual(self.test_state.live_states[akey].effective_mean(1.5), 21.0)

        response = self.client.post("/api/live/update_player_state", json={
            "athlete_key": akey, "game_status": "IN_PROGRESS", "remaining_is_estimate": False,
        })
        self.assertEqual(response.status_code, 200, response.get_json())
        live = self.test_state.live_states[akey]
        self.assertFalse(live.remaining_is_estimate)
        self.assertEqual(live.effective_mean(1.5), 15.0)

    def test_estimate_flag_survives_save_load_and_reload(self):
        """Save/load keeps the explicit/unknown distinction, not just the number."""
        self._generate({"Scorcher": 1})
        akey = "patrickmahomes"
        self.client.post("/api/live/update_player_state", json={
            "athlete_key": akey, "game_status": "IN_PROGRESS", "actual_score": 10.0,
            "remaining_projection": 4.0,
        })
        self.client.post("/api/planner/save")

        self.test_state.active_plan = None
        self.test_state.live_states = {}
        self.client.post("/api/planner/load")
        restored = self.test_state.live_states[akey]
        self.assertTrue(restored.remaining_is_estimate)
        self.assertEqual(restored.remaining_projection, 4.0)
        self.assertEqual(restored.effective_mean(1.5), 21.0)

    def test_legacy_saved_session_without_flag_loads_as_unknown(self):
        """An older saved plan must not guess that a seeded value was entered by hand."""
        self._generate({"Scorcher": 1})
        self.client.post("/api/planner/save")
        path = self.test_state.plans_dir / "latest_plan.json"
        payload = json.loads(path.read_text())
        for player in payload["live_session"]["players"]:
            player.pop("remaining_is_estimate", None)
        path.write_text(json.dumps(payload))

        self.test_state.active_plan = None
        self.test_state.live_states = {}
        response = self.client.post("/api/planner/load")
        self.assertEqual(response.status_code, 200, response.get_json())
        restored = self.test_state.live_states["patrickmahomes"]
        self.assertFalse(restored.remaining_is_estimate)

    def test_final_status_keeps_zero_remaining(self):
        """Rule 9 still holds: final players contribute actual points and no remaining."""
        self._generate({"Scorcher": 1})
        akey = "patrickmahomes"
        self.client.post("/api/live/update_player_state", json={
            "athlete_key": akey, "game_status": "IN_PROGRESS", "remaining_projection": 9.0,
        })
        self.client.post("/api/live/update_player_state", json={
            "athlete_key": akey, "game_status": "FINAL", "actual_score": 12.0,
        })
        live = self.test_state.live_states[akey]
        self.assertEqual(live.remaining_projection, 0.0)
        self.assertEqual(live.remaining_stdev, 0.0)
        self.assertFalse(live.remaining_is_estimate)
        self.assertEqual(live.effective_mean(1.5), 18.0)

    def test_first_manual_entry_needs_no_generation_and_no_solver(self):
        """A first draft must be creatable directly from the selected controls."""
        self.assertIsNone(self.test_state.active_plan)
        with patch.object(
            app_module.multi_lineup_planner, "solve_multi_lineup_allocation",
            side_effect=AssertionError("the solver must not run for a manual draft"),
        ):
            response = self._add_draft("Scorcher", 2, {"Scorcher": 2})
        self.assertEqual(response.status_code, 200, response.get_json())
        plan = self.test_state.active_plan
        self.assertIsNotNone(plan)
        self.assertEqual(len(plan.lineups), 1)
        self.assertEqual(plan.inventory_summary["contest_distribution"], {"Scorcher": 2})
        draft = plan.lineups[0]
        self.assertEqual(draft.contest_name, "Scorcher")
        self.assertTrue(all(s.card is None for s in draft.slots))
        # The response carries the plan and the new draft for the page.
        body = response.get_json()
        self.assertEqual(body["lineup"]["lineup_id"], draft.lineup_id)
        self.assertEqual(len(body["plan"]["lineups"]), 1)

    def test_manual_entry_requests_not_restricted_by_saved_selection(self):
        # Adding a draft with Scorcher does not prevent adding another Scorcher or Volcano
        self.assertEqual(self._add_draft("Scorcher", 1).status_code, 200)
        # Adding another Scorcher without passing entry_limit succeeds up to contest maximum (5)
        res_scorcher = self.client.post("/api/planner/edit_lineup", json={"action": "add", "contest_name": "Scorcher"})
        self.assertEqual(res_scorcher.status_code, 200)
        # Adding Volcano also succeeds
        res_volcano = self.client.post("/api/planner/edit_lineup", json={"action": "add", "contest_name": "Volcano"})
        self.assertEqual(res_volcano.status_code, 200)
        # An invalid contest is rejected
        res_invalid = self.client.post("/api/planner/edit_lineup", json={"action": "add", "contest_name": "UnknownTier"})
        self.assertEqual(res_invalid.status_code, 400)
        # Saving and loading preserves all manual additions
        self.assertEqual(self.client.post("/api/planner/save").status_code, 200)
        self.assertEqual(self.client.post("/api/planner/load").status_code, 200)
        self.assertEqual(len(self.test_state.active_plan.lineups), 3)

    def test_manual_draft_fills_reaches_selected_limit_and_saves(self):
        """Fill a manual draft, hit a selected limit below the max, then save/load."""
        contest = self.test_state.contests["Scorcher"]
        self.assertGreater(contest.default_entry_limit, 2)
        self.assertEqual(self._add_draft("Scorcher", 2).status_code, 200)
        draft = self.test_state.active_plan.lineups[0]
        for slot in list(draft.slots):
            candidates = self.client.get(
                "/api/planner/candidates",
                query_string={"lineup_id": draft.lineup_id, "slot_name": slot.slot_name},
            ).get_json()["cards"]
            self.assertTrue(candidates, f"No candidates while filling {slot.slot_name}")
            chosen = min(candidates, key=lambda c: c["weekly_salary"])
            swap = self.client.post("/api/planner/swap", json={
                "lineup_id": draft.lineup_id, "slot_name": slot.slot_name, "new_card_id": chosen["card_id"],
            })
            self.assertEqual(swap.status_code, 200, swap.get_json())
        self.assertTrue(all(s.card is not None for s in draft.slots))
        self.assertTrue(draft.is_valid, draft.validation_errors)

        # The selected count is 2 and the contest allows more, so a third is rejected.
        self.assertEqual(self._add_draft("Scorcher", 2).status_code, 200)
        count_before = len(self.test_state.active_plan.lineups)
        before = self.test_state.active_plan.inventory_summary["contest_distribution"]
        rejected = self._add_draft("Scorcher", 2)
        self.assertEqual(rejected.status_code, 400, rejected.get_json())
        self.assertEqual(len(self.test_state.active_plan.lineups), count_before)
        self.assertEqual(self.test_state.active_plan.inventory_summary["contest_distribution"], before)

        self.client.post("/api/planner/save")
        saved_ids = [l.lineup_id for l in self.test_state.active_plan.lineups]
        self.test_state.active_plan = None
        self.client.post("/api/planner/load")
        self.assertEqual([l.lineup_id for l in self.test_state.active_plan.lineups], saved_ids)
        rebuilt = self.test_state.active_plan.lineups[0]
        self.assertTrue(all(s.card is not None for s in rebuilt.slots))

    def test_rejected_first_add_creates_nothing(self):
        """Zero-count and unselected requests leave STATE.active_plan absent."""
        zero = self.client.post("/api/planner/edit_lineup", json={
            "action": "add", "contest_name": "Scorcher", "entry_limit": 0,
            "contest_distribution": {"Scorcher": 0},
        })
        self.assertEqual(zero.status_code, 400)
        self.assertIn("at least one", zero.get_json()["message"])

        unselected = self._add_draft("Volcano", 1, {"Scorcher": 1})
        self.assertEqual(unselected.status_code, 400)
        self.assertIn("selected contest tiers", unselected.get_json()["message"])

        bad_contest = self._add_draft("NotAContest", 1, {"Scorcher": 1})
        self.assertEqual(bad_contest.status_code, 400)
        self.assertIsNone(self.test_state.active_plan)

    def test_rejected_add_keeps_the_existing_plan(self):
        """A rejected add on an existing plan must not change lineups or tiers."""
        plan = self._generate({"Scorcher": 1})
        snapshot = [(l.lineup_id, tuple(s.card.card.card_id if s.card else None for s in l.slots)) for l in plan.lineups]
        tiers = dict(plan.inventory_summary["contest_distribution"])
        rejected = self._add_draft("Scorcher", 1)
        self.assertEqual(rejected.status_code, 400, rejected.get_json())
        self.assertEqual(
            [(l.lineup_id, tuple(s.card.card.card_id if s.card else None for s in l.slots)) for l in self.test_state.active_plan.lineups],
            snapshot,
        )
        self.assertEqual(self.test_state.active_plan.inventory_summary["contest_distribution"], tiers)


class Week2RefreshRegressionTests(unittest.TestCase):
    """Sep. 16 refresh data must not remap protected live entries by row number."""

    @classmethod
    def setUpClass(cls):
        app.config.update(TESTING=True, MAX_CONTENT_LENGTH=8 * 1024 * 1024)

    def setUp(self):
        root = Path(__file__).resolve().parent
        self.roster_path = root / "ExportedRoster_20260916104224.csv"
        self.snapshot_path = root / "data" / "snapshots" / "snapshot_Thu-Mon_2026-09-16.json"
        self.client = app.test_client()
        self.orig_state = app_module.STATE
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.test_state = PlannerAppState(root_dir=self.tmp_dir.name)
        self.test_state.roster_cards = multi_lineup_planner.parse_roster_cards(self.roster_path)
        self.test_state.snapshot = ProjectionsSnapshot.from_dict(json.loads(self.snapshot_path.read_text()))
        self.test_state.recompute_planner_cards()
        app_module.STATE = self.test_state

    def tearDown(self):
        app_module.STATE = self.orig_state
        self.tmp_dir.cleanup()

    def test_reordered_sep16_roster_preserves_locked_slots_scores_and_card_uniqueness(self):
        with self.roster_path.open("r", encoding="utf-8-sig", newline="") as source:
            reader = csv.reader(source)
            rows = list(reader)
        reordered = io.StringIO()
        writer = csv.writer(reordered, lineterminator="\n")
        writer.writerow(rows[0])
        writer.writerows(reversed(rows[1:]))
        reordered_card_ids = {
            card.card_id
            for card in multi_lineup_planner.parse_roster_cards(io.StringIO(reordered.getvalue()))
        }

        generated = self.client.post(
            "/api/planner/generate",
            json={"contest_distribution": {"Scorcher": 2}, "target_count": 2},
        )
        self.assertEqual(generated.status_code, 200, generated.get_json())
        self.assertEqual(len(self.test_state.roster_cards), 346)
        self.assertEqual(sum(c.is_eligible for c in self.test_state.planner_cards), 325)

        protected = []
        for index, lineup in enumerate(self.test_state.active_plan.lineups):
            slot = next(
                slot for slot in lineup.slots
                if slot.card is not None and slot.card.card.card_id not in reordered_card_ids
            )
            locked = self.client.post(
                "/api/planner/toggle_lock",
                json={"lineup_id": lineup.lineup_id, "slot_name": slot.slot_name},
            )
            self.assertEqual(locked.status_code, 200, locked.get_json())
            score = 7.0 + index
            scored = self.client.post(
                "/api/live/update_player_state",
                json={
                    "athlete_key": slot.card.card.athlete_key,
                    "game_status": "IN_PROGRESS",
                    "actual_score": score,
                },
            )
            self.assertEqual(scored.status_code, 200, scored.get_json())
            protected.append((lineup.lineup_id, slot.slot_name, slot.card.card.athlete_key, score))

        before_ids = {
            (lineup_id, slot_name): next(
                slot.card.card.card_id
                for lineup in self.test_state.active_plan.lineups
                if lineup.lineup_id == lineup_id
                for slot in lineup.slots
                if slot.slot_name == slot_name
            )
            for lineup_id, slot_name, _, _ in protected
        }

        refreshed = self.client.post(
            "/api/roster/upload",
            data={"roster_file": (io.BytesIO(reordered.getvalue().encode("utf-8")), "sep16-reordered.csv")},
            content_type="multipart/form-data",
        )
        self.assertEqual(refreshed.status_code, 200, refreshed.get_json())

        assigned_ids = [
            slot.card.card.card_id
            for lineup in self.test_state.active_plan.lineups
            for slot in lineup.slots
            if slot.card is not None
        ]
        self.assertEqual(len(assigned_ids), len(set(assigned_ids)))

        for lineup_id, slot_name, athlete_key, score in protected:
            lineup = next(lineup for lineup in self.test_state.active_plan.lineups if lineup.lineup_id == lineup_id)
            slot = next(slot for slot in lineup.slots if slot.slot_name == slot_name)
            self.assertTrue(slot.is_locked)
            self.assertEqual(slot.card.card.athlete_key, athlete_key)
            self.assertNotEqual(slot.card.card.card_id, before_ids[(lineup_id, slot_name)])
            live = self.test_state.live_states[athlete_key]
            self.assertEqual(live.game_status, "IN_PROGRESS")
            self.assertEqual(live.live_points, score)


if __name__ == "__main__":
    unittest.main()
