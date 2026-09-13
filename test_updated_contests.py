"""
Unit tests verifying the updated contest names, structures, salary caps,
collection requirements, and mathematical payout schedules.
"""

from decimal import Decimal
import unittest

from contest_config import get_default_contests
from multi_lineup_planner import (
    LineupSlotAssignment,
    OwnedCard,
    PlannerCard,
    PlannerLineup,
    validate_lineup,
)
from optimizer_core import CONTESTS


class UpdatedContestsTests(unittest.TestCase):
    def setUp(self):
        self.contests = get_default_contests()

    def test_all_six_master_contests_exist_and_are_enabled(self):
        expected_names = [
            "Inferno",
            "Volcano",
            "Flamethrower",
            "Scorcher",
            "Primetime Pyro",
            "Flex Appeal",
        ]
        for name in expected_names:
            self.assertIn(name, self.contests)
            self.assertTrue(self.contests[name].enabled, f"{name} should be enabled")

    def test_payout_tables_sum_exactly_to_prize_pool(self):
        expected_pools = {
            "Inferno": 15000.0,
            "Volcano": 7000.0,
            "Scorcher": 3000.0,
            "Primetime Pyro": 2500.0,
            "Flex Appeal": 2500.0,
        }

        self.assertEqual(self.contests["Flamethrower"].payout_bands, [])
        self.assertEqual(self.contests["Flamethrower"].historical_thresholds, [])
        for name, expected_pool in expected_pools.items():
            cfg = self.contests[name]
            self.assertEqual(cfg.prize_pool, expected_pool)

            total_payout = Decimal("0.00")
            for band in cfg.payout_bands:
                count = Decimal(str(band.places))
                prize = Decimal(str(band.payout_per_entry))
                total_payout += count * prize

            self.assertAlmostEqual(
                float(total_payout),
                expected_pool,
                delta=0.01,
                msg=f"{name} payout sum ({float(total_payout)}) does not match prize pool ({expected_pool})",
            )

    def test_salary_ranges_and_entry_caps(self):
        specs = {
            "Inferno": {"min": 30000, "max": 70000, "entries": 8, "slots": 8},
            "Volcano": {"min": 25000, "max": 52000, "entries": 6, "slots": 6},
            "Flamethrower": {"min": 30000, "max": 62000, "entries": 6, "slots": 6},
            "Scorcher": {"min": 20000, "max": 40000, "entries": 5, "slots": 5},
            "Primetime Pyro": {"min": 0, "max": 48000, "entries": 6, "slots": 6},
            "Flex Appeal": {"min": 25000, "max": 52000, "entries": 6, "slots": 6},
        }

        for name, spec in specs.items():
            cfg = self.contests[name]
            core = CONTESTS[name]

            self.assertEqual(cfg.minimum_salary, spec["min"])
            self.assertEqual(cfg.maximum_salary, spec["max"])
            self.assertEqual(core.minimum_salary, spec["min"])
            self.assertEqual(core.maximum_salary, spec["max"])

            self.assertEqual(cfg.default_entry_limit, spec["entries"])
            self.assertEqual(len(cfg.slots), spec["slots"])
            self.assertEqual(len(core.slots), spec["slots"])

    def test_collection_requirements_validation(self):
        def make_card(card_id, athlete_key, pos, coll, coll_group="Core"):
            owned = OwnedCard(
                card_id=card_id,
                player_name=athlete_key.title(),
                athlete_key=athlete_key,
                team="KC",
                position=pos,
                multiplier=1.0,
                roster_salary=5000,
                status="Active",
                source_row=1,
                collection=coll,
                collection_group=coll_group,
            )
            return PlannerCard(
                card=owned,
                raw_projection=15.0,
                adjusted_projection=15.0,
                weekly_salary=5000,
                salary_source="dff",
                is_matched=True,
                is_eligible=True,
            )

        flamethrower_slots = [
            LineupSlotAssignment("QB_1", "QB", make_card("c1", "qb1", "QB", "2025 Core", "Core")),
            LineupSlotAssignment("RB_1", "RB", make_card("c2", "rb1", "RB", "2025 Core", "Core")),
            LineupSlotAssignment("WR_1", "WR", make_card("c3", "wr1", "WR", "2025 Core", "Core")),
            LineupSlotAssignment("TE_1", "TE", make_card("c4", "te1", "TE", "2025 Core", "Core")),
            LineupSlotAssignment("Flex_1", "Flex", make_card("c5", "rb2", "RB", "2025 Core", "Core")),
            LineupSlotAssignment("Superflex_1", "Superflex", make_card("c6", "wr2", "WR", "2025 Core", "Core")),
        ]

        valid_flamethrower = PlannerLineup(
            lineup_id="flamethrower_1",
            contest_name="Flamethrower",
            slots=flamethrower_slots,
        )
        is_valid, errors = validate_lineup(valid_flamethrower, self.contests["Flamethrower"])
        self.assertTrue(is_valid, f"All Core cards should be valid for Flamethrower: {errors}")

        flamethrower_slots_invalid = list(flamethrower_slots)
        flamethrower_slots_invalid[5] = LineupSlotAssignment(
            "Superflex_1", "Superflex", make_card("c6_pt", "wr2", "WR", "Primetime-WR", "Primetime")
        )
        invalid_flamethrower = PlannerLineup(
            lineup_id="flamethrower_2",
            contest_name="Flamethrower",
            slots=flamethrower_slots_invalid,
        )
        is_valid, errors = validate_lineup(invalid_flamethrower, self.contests["Flamethrower"])
        self.assertFalse(is_valid)
        self.assertTrue(any("requires at least 6 Core cards" in e for e in errors))

        pyro_slots_valid = [
            LineupSlotAssignment("QB_1", "QB", make_card("p1", "qb1", "QB", "Primetime-QB", "Primetime")),
            LineupSlotAssignment("RB_1", "RB", make_card("p2", "rb1", "RB", "Primetime-RB", "Primetime")),
            LineupSlotAssignment("WR_1", "WR", make_card("p3", "wr1", "WR", "Primetime-WR", "Primetime")),
            LineupSlotAssignment("TE_1", "TE", make_card("p4", "te1", "TE", "Primetime-TE", "Primetime")),
            LineupSlotAssignment("Flex_1", "Flex", make_card("p5", "rb2", "RB", "2025 Core", "Core")),
            LineupSlotAssignment("Flex_2", "Flex", make_card("p6", "wr2", "WR", "2025 Core", "Core")),
        ]
        valid_pyro = PlannerLineup(
            lineup_id="pyro_1",
            contest_name="Primetime Pyro",
            slots=pyro_slots_valid,
        )
        is_valid, errors = validate_lineup(valid_pyro, self.contests["Primetime Pyro"])
        self.assertTrue(is_valid, f"Primetime Pyro lineup should be valid: {errors}")

        pyro_slots_core = list(pyro_slots_valid)
        pyro_slots_core[3] = LineupSlotAssignment("TE_1", "TE", make_card("p4_c", "te1", "TE", "2025 Core", "Core"))
        lineup_more_core = PlannerLineup(
            lineup_id="pyro_2",
            contest_name="Primetime Pyro",
            slots=pyro_slots_core,
        )
        is_valid_core, errors_core = validate_lineup(lineup_more_core, self.contests["Primetime Pyro"])
        self.assertTrue(is_valid_core, f"Primetime Pyro with 3 Core and 3 Primetime cards is now valid: {errors_core}")



if __name__ == "__main__":
    unittest.main()
