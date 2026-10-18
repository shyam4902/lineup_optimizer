"""Tests for schedule time slots, dates, and opponent indicators in the lineup maker."""

from __future__ import annotations

import io
import unittest

from dff_client import DFFPlayerProjection, parse_dff_players, parse_projections_csv_stream
from live_lineup_manager import PlayerLiveState
from multi_lineup_planner import OwnedCard, PlannerCard, join_cards_with_projections
from optimizer_core import NormalizedCard, NormalizedProjection, _Slot, _result_lineup


class ScheduleLineupColumnsTests(unittest.TestCase):
    def test_dff_player_projection_preserves_schedule_and_opp(self):
        proj = DFFPlayerProjection(
            player_name="Josh Allen",
            athlete_key="joshallen",
            team="BUF",
            position="QB",
            salary=7400,
            raw_projection=22.7,
            source_projection=22.7,
            opp="DET",
            game_date="2026-09-17",
            kickoff_time="2026-09-17T20:15:00-04:00",
        )
        d = proj.to_dict()
        self.assertEqual(d["opp"], "DET")
        self.assertEqual(d["game_date"], "2026-09-17")
        self.assertEqual(d["kickoff_time"], "2026-09-17T20:15:00-04:00")

    def test_parse_dff_players_extracts_schedule_fields(self):
        raw_rows = [
            {
                "first_name": "Josh",
                "last_name": "Allen",
                "position_detailed": "QB",
                "team": "BUF",
                "ppg": "24.5",
                "salary": "7500",
                "opp": "DET",
                "game_date": "2026-09-17",
                "kickoff": "2026-09-17T20:15:00Z",
            }
        ] + [
            {
                "first_name": f"Player{i}",
                "last_name": "Test",
                "position_detailed": "WR",
                "team": "KC",
                "ppg": "10.0",
                "salary": "5000",
            }
            for i in range(25)
        ]
        snapshot = parse_dff_players(raw_rows, "test_slate")
        josh = next(p for p in snapshot.players if p.athlete_key == "joshallen")
        self.assertEqual(josh.opp, "DET")
        self.assertEqual(josh.game_date, "2026-09-17")
        self.assertEqual(josh.kickoff_time, "2026-09-17T20:15:00Z")

    def test_parse_projections_csv_stream_extracts_opp_and_dates(self):
        csv_text = (
            "player_name,position,team,projection,salary,opp,game_date,kickoff\n"
            + "Josh Allen,QB,BUF,22.7,7400,DET,2026-09-17,2026-09-17T20:15:00Z\n"
            + "\n".join(f"Player {i},WR,KC,12.0,5000,LAC,2026-09-20,2026-09-20T17:00:00Z" for i in range(25))
        )
        snapshot = parse_projections_csv_stream(io.StringIO(csv_text))
        josh = next(p for p in snapshot.players if p.athlete_key == "joshallen")
        self.assertEqual(josh.opp, "DET")
        self.assertEqual(josh.game_date, "2026-09-17")
        self.assertEqual(josh.kickoff_time, "2026-09-17T20:15:00Z")

    def test_join_cards_with_projections_forwards_schedule_to_planner_card(self):
        card = OwnedCard(
            card_id="c_allen_1",
            player_name="Josh Allen",
            athlete_key="joshallen",
            team="BUF",
            position="QB",
            multiplier=1.25,
            roster_salary=7400,
            status="Active",
            rarity="Rare",
            collection="2025 Core",
            source_row=2,
        )
        proj = DFFPlayerProjection(
            player_name="Josh Allen",
            athlete_key="joshallen",
            team="BUF",
            position="QB",
            salary=7400,
            raw_projection=22.7,
            source_projection=22.7,
            opp="DET",
            game_date="2026-09-17",
            kickoff_time="2026-09-17T20:15:00Z",
        )
        planner_cards = join_cards_with_projections([card], [proj], salary_source="roster")
        self.assertEqual(len(planner_cards), 1)
        pc = planner_cards[0]
        self.assertTrue(pc.is_matched)
        self.assertEqual(pc.opp, "DET")
        self.assertEqual(pc.game_date, "2026-09-17")
        self.assertEqual(pc.kickoff_time, "2026-09-17T20:15:00Z")

        d = pc.to_dict()
        self.assertEqual(d["opp"], "DET")
        self.assertEqual(d["game_date"], "2026-09-17")
        self.assertEqual(d["kickoff_time"], "2026-09-17T20:15:00Z")

    def test_player_live_state_opp_and_game_date(self):
        pls = PlayerLiveState(
            athlete_key="joshallen",
            player_name="Josh Allen",
            team="BUF",
            position="QB",
            opp="vs DET",
            game_date="2026-09-17",
            kickoff_time="2026-09-17T20:15:00Z",
        )
        d = pls.to_dict()
        self.assertEqual(d["opp"], "vs DET")
        self.assertEqual(d["game_date"], "2026-09-17")
        self.assertEqual(d["kickoff_time"], "2026-09-17T20:15:00Z")

    def test_optimizer_core_result_lineup_includes_schedule_columns(self):
        card = NormalizedCard(
            display_name="Josh Allen",
            athlete_key="joshallen",
            team="BUF",
            position="QB",
            multiplier=1.25,
            salary=7400,
            status="Active",
            source_row=1,
            card_row_id="r1",
            source_values={},
        )
        proj = NormalizedProjection(
            display_name="Josh Allen",
            athlete_key="joshallen",
            team="BUF",
            position="QB",
            raw_projection=24.0,
            source_row=1,
            source_values={"opp": "DET", "kickoff": "2026-09-17T20:15:00Z", "game_date": "2026-09-17"},
        )
        slot = _Slot(label="QB", kind="QB", order=0)
        rows = _result_lineup([card], [slot], {"r1": proj})
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["opp"], "DET")
        self.assertEqual(row["kickoff_time"], "2026-09-17T20:15:00Z")
        self.assertEqual(row["game_date"], "2026-09-17")
        self.assertEqual(row["time_display"], "2026-09-17T20:15:00Z")

    def test_schedule_window_classification(self):
        import schedule_manager
        # Thursday 8:15 PM ET (Friday 00:15 UTC)
        self.assertEqual(schedule_manager.classify_game_window("2026-09-25T00:15:00+00:00"), "TNF")
        # Sunday 1:00 PM ET (17:00 UTC)
        self.assertEqual(schedule_manager.classify_game_window("2026-09-27T17:00:00+00:00"), "SUN_EARLY")
        # Sunday 4:25 PM ET (20:25 UTC)
        self.assertEqual(schedule_manager.classify_game_window("2026-09-27T20:25:00+00:00"), "SUN_LATE")
        # Sunday 8:20 PM ET (Monday 00:20 UTC)
        self.assertEqual(schedule_manager.classify_game_window("2026-09-28T00:20:00+00:00"), "SNF")
        # Monday 8:15 PM ET (Tuesday 00:15 UTC)
        self.assertEqual(schedule_manager.classify_game_window("2026-09-29T00:15:00+00:00"), "MNF")

    def test_order_lineup_slots_for_late_swap_places_later_players_in_flex(self):
        import schedule_manager
        from multi_lineup_planner import LineupSlotAssignment, OwnedCard, PlannerCard

        def make_card(cid, name, pos, ko, salary=5000):
            oc = OwnedCard(cid, name, cid, "KC", pos, 1.0, salary, "Active", 1)
            return PlannerCard(oc, 15.0, 15.0, salary, "roster", True, kickoff_time=ko)

        # Inverted arrangement: Early RB in Flex, Late RB in RB1
        slots = [
            LineupSlotAssignment("QB", "QB", make_card("c1", "QB1", "QB", "2026-09-27T17:00:00+00:00")),
            LineupSlotAssignment("RB1", "RB", make_card("c2", "RB Late", "RB", "2026-09-27T20:25:00+00:00")),
            LineupSlotAssignment("RB2", "RB", make_card("c3", "RB Early", "RB", "2026-09-27T17:00:00+00:00")),
            LineupSlotAssignment("WR1", "WR", make_card("c4", "WR Late", "WR", "2026-09-28T00:15:00+00:00")), # MNF
            LineupSlotAssignment("WR2", "WR", make_card("c5", "WR Early", "WR", "2026-09-27T17:00:00+00:00")),
            LineupSlotAssignment("TE", "TE", make_card("c6", "TE Early", "TE", "2026-09-27T17:00:00+00:00")),
            LineupSlotAssignment("Flex", "Flex", make_card("c7", "RB Mid", "RB", "2026-09-27T17:00:00+00:00")),
            LineupSlotAssignment("Superflex", "Superflex", make_card("c8", "QB Late", "QB", "2026-09-27T20:25:00+00:00")),
        ]

        ordered = schedule_manager.order_lineup_slots_for_late_swap(slots)
        ordered_map = {s.slot_name: s.card.card.player_name for s in ordered}

        # RB1 and RB2 must get early RBs
        self.assertEqual(ordered_map["RB1"], "RB Early")
        self.assertEqual(ordered_map["RB2"], "RB Mid")
        # Flex must get the 4:25 PM RB Late
        self.assertEqual(ordered_map["Flex"], "RB Late")
        # Superflex must get QB Late
        self.assertEqual(ordered_map["Superflex"], "QB Late")

        # Total points and total cards are 100% preserved
        orig_ids = sorted(s.card.card.card_id for s in slots)
        ordered_ids = sorted(s.card.card.card_id for s in ordered)
        self.assertEqual(orig_ids, ordered_ids)

    def test_order_lineup_slots_preserves_locked_slots(self):
        import schedule_manager
        from multi_lineup_planner import LineupSlotAssignment, OwnedCard, PlannerCard

        def make_card(cid, name, pos, ko, salary=5000):
            oc = OwnedCard(cid, name, cid, "KC", pos, 1.0, salary, "Active", 1)
            return PlannerCard(oc, 15.0, 15.0, salary, "roster", True, kickoff_time=ko)

        # Flex is locked with Early RB
        slots = [
            LineupSlotAssignment("QB", "QB", make_card("c1", "QB1", "QB", "2026-09-27T17:00:00+00:00")),
            LineupSlotAssignment("RB1", "RB", make_card("c2", "RB Late", "RB", "2026-09-27T20:25:00+00:00")),
            LineupSlotAssignment("RB2", "RB", make_card("c3", "RB Mid", "RB", "2026-09-27T17:00:00+00:00")),
            LineupSlotAssignment("Flex", "Flex", make_card("c4", "RB Locked Early", "RB", "2026-09-27T17:00:00+00:00"), is_locked=True),
        ]

        ordered = schedule_manager.order_lineup_slots_for_late_swap(slots)
        ordered_map = {s.slot_name: s.card.card.player_name for s in ordered}

        # Locked slot stays exactly as it was
        self.assertEqual(ordered_map["Flex"], "RB Locked Early")
        flex_slot = next(s for s in ordered if s.slot_name == "Flex")
        self.assertTrue(flex_slot.is_locked)

    def test_api_optimize_slots_endpoint(self):
        import app as app_module
        from multi_lineup_planner import LineupSlotAssignment, OwnedCard, PlannerCard, PlannerLineup, WeeklyPlan

        def make_card(cid, name, pos, ko, salary=5000):
            oc = OwnedCard(cid, name, cid, "KC", pos, 1.0, salary, "Active", 1)
            return PlannerCard(oc, 15.0, 15.0, salary, "roster", True, kickoff_time=ko)

        qbs = [c for c in app_module.STATE.planner_cards if c.card.position == "QB" and c.is_eligible]
        rbs = [c for c in app_module.STATE.planner_cards if c.card.position == "RB" and c.is_eligible]
        self.assertTrue(len(qbs) >= 1 and len(rbs) >= 3)

        qb = qbs[0]
        rb_early = rbs[0]
        rb_mid = rbs[1]
        rb_late = rbs[2]

        rb_early.kickoff_time = "2026-09-27T17:00:00+00:00"
        rb_mid.kickoff_time = "2026-09-27T17:00:00+00:00"
        rb_late.kickoff_time = "2026-09-27T20:25:00+00:00"

        # Inverted arrangement: rb_late in RB1, rb_mid in Flex
        slots = [
            LineupSlotAssignment("QB", "QB", qb),
            LineupSlotAssignment("RB1", "RB", rb_late),
            LineupSlotAssignment("RB2", "RB", rb_early),
            LineupSlotAssignment("Flex", "Flex", rb_mid),
        ]
        lineup = PlannerLineup("test_1", "Scorcher", slots=slots)
        app_module.STATE.active_plan = WeeklyPlan(
            plan_id="test_plan",
            name="Test",
            created_at="",
            updated_at="",
            slate_id="258A8",
            salary_source="roster",
            contests=app_module.STATE.contests,
            lineups=[lineup],
            roster_cards=app_module.STATE.planner_cards,
            projection_overrides={},
            excluded_athlete_keys=[],
        )

        with app_module.app.test_client() as client:
            res = client.post("/api/planner/edit_lineup", json={"lineup_id": "test_1", "action": "optimize_slots"})
            self.assertEqual(res.status_code, 200)
            data = res.get_json()
            self.assertEqual(data["status"], "success")
            l_slots = {s["slot_name"]: s["card"]["player_name"] for s in data["lineup"]["slots"]}
            self.assertEqual(l_slots["Flex"], rb_late.card.player_name)

    def test_order_lineup_slots_handles_dict_representation(self):
        import schedule_manager
        c_early = {"player_name": "Early Guy", "position": "WR", "kickoff_time": "2026-09-27T17:00:00+00:00"}
        c_late = {"player_name": "Late Guy", "position": "WR", "kickoff_time": "2026-09-27T20:25:00+00:00"}
        slots = [
            {"slot_name": "WR1", "slot_kind": "WR", "card": c_late, "is_locked": False},
            {"slot_name": "Flex", "slot_kind": "Flex", "card": c_early, "is_locked": False},
        ]
        reordered = schedule_manager.order_lineup_slots_for_late_swap(slots)
        self.assertEqual(reordered[0]["card"]["player_name"], "Early Guy")
        self.assertEqual(reordered[1]["card"]["player_name"], "Late Guy")



if __name__ == "__main__":
    unittest.main()


