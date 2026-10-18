import json
import sqlite3
import unittest
from unittest.mock import MagicMock, patch

import market_advisor
from app import app


class MarketAdvisorTests(unittest.TestCase):
    def setUp(self):
        self.app = app
        self.client = self.app.test_client()

    def test_recommend_undercut_price(self):
        # Above $20: undercut by $2.00
        self.assertEqual(market_advisor.recommend_undercut_price(35.00, 5.00), 33.00)
        # Above $10: undercut by $1.00
        self.assertEqual(market_advisor.recommend_undercut_price(15.00, 4.00), 14.00)
        # Above $5: undercut by $0.50
        self.assertEqual(market_advisor.recommend_undercut_price(9.00, 4.00), 8.50)
        # Above $2: undercut by $0.25
        self.assertEqual(market_advisor.recommend_undercut_price(4.00, 1.00), 3.75)
        # Below $2: undercut by $0.10
        self.assertEqual(market_advisor.recommend_undercut_price(1.50, 0.50), 1.40)
        # Never drop below fair value
        self.assertEqual(market_advisor.recommend_undercut_price(5.10, 5.00), 5.05)

    def test_compute_fair_value_with_db(self):
        if not market_advisor.DEFAULT_DB.exists():
            self.skipTest("Database file not present")

        conn = sqlite3.connect(market_advisor.DEFAULT_DB)
        try:
            res = market_advisor.compute_fair_value(conn, "Kyren Williams", multiplier=1.3, is_core=True)
            self.assertEqual(res["player_name"], "Kyren Williams")
            self.assertIsNotNone(res["fair_value"])
            self.assertGreater(res["fair_value"], 0)
            self.assertGreater(res["comps_count"], 0)
        finally:
            conn.close()

    def test_evaluate_roster_opportunities_surplus_and_premium(self):
        sample_cards = [
            {
                "Player": "Test Star",
                "Multiplier": 1.3,
                "Collection": "2025 Core",
                "Tradeable": "Yes",
                "Team": "LAR",
                "Positions": "Rb",
            },
            {
                "Player": "Test Star",
                "Multiplier": 1.3,
                "Collection": "2025 Core",
                "Tradeable": "Yes",
                "Team": "LAR",
                "Positions": "Rb",
            },
            {
                "Player": "Non Tradeable",
                "Multiplier": 1.0,
                "Collection": "2025 Core",
                "Tradeable": "No",
                "Team": "SF",
                "Positions": "Wr",
            },
        ]

        # Mock market floor and comps
        with patch.object(
            market_advisor,
            "get_live_market_floor",
            return_value={
                "player_name": "Test Star",
                "target_multiplier": 1.3,
                "total_active_listings": 3,
                "cheapest_for_multiplier": 20.00,
                "cheapest_overall": 15.00,
                "cached": False,
            },
        ), patch.object(
            market_advisor,
            "compute_fair_value",
            return_value={
                "player_name": "Test Star",
                "multiplier": 1.3,
                "fair_value": 5.00,
                "comps_count": 15,
                "confidence": "High",
            },
        ):
            eval_res = market_advisor.evaluate_roster_opportunities(sample_cards, core_only=True)
            summary = eval_res["summary"]
            items = eval_res["items"]

            self.assertEqual(summary["total_tradeable_cards"], 2)
            self.assertEqual(summary["unique_cards_evaluated"], 1)
            self.assertEqual(summary["premium_opportunities"], 1)
            self.assertEqual(summary["surplus_opportunities"], 1)

            card_eval = items[0]
            self.assertEqual(card_eval["player"], "Test Star")
            self.assertEqual(card_eval["status"], "PREMIUM_SELL")
            self.assertTrue(card_eval["is_surplus"])
            self.assertEqual(card_eval["copies_owned"], 2)
            self.assertEqual(card_eval["floor_price"], 20.00)
            self.assertEqual(card_eval["recommended_price"], 18.00)
            self.assertGreater(card_eval["premium_dollars"], 10.0)

    def test_api_market_advisor_endpoint(self):
        sample_cards = [
            {
                "Player": "Kyren Williams",
                "Multiplier": 1.3,
                "Collection": "2025 Core",
                "Tradeable": "Yes",
                "Team": "LAR",
                "Positions": "Rb",
            }
        ]

        with patch.object(
            market_advisor,
            "evaluate_roster_opportunities",
            return_value={
                "summary": {
                    "total_tradeable_cards": 1,
                    "unique_cards_evaluated": 1,
                    "premium_opportunities": 1,
                    "surplus_opportunities": 0,
                    "monopoly_opportunities": 0,
                    "estimated_potential_profit": 20.0,
                    "evaluated_at": "2026-10-08T12:00:00Z",
                },
                "items": [
                    {
                        "player": "Kyren Williams",
                        "team": "LAR",
                        "pos": "Rb",
                        "multiplier": 1.3,
                        "collection": "2025 Core",
                        "copies_owned": 1,
                        "is_surplus": False,
                        "fair_value": 5.67,
                        "floor_price": 35.00,
                        "recommended_price": 33.00,
                        "status": "PREMIUM_SELL",
                        "premium_dollars": 29.33,
                    }
                ],
            },
        ):
            resp = self.client.get("/api/market/advisor")
            self.assertEqual(resp.status_code, 200)
            data = resp.get_json()
            self.assertEqual(data["status"], "success")
            self.assertEqual(data["summary"]["premium_opportunities"], 1)
            self.assertEqual(data["items"][0]["player"], "Kyren Williams")

    def test_api_player_floor_endpoint(self):
        with patch.object(
            market_advisor,
            "get_live_market_floor",
            return_value={
                "player_name": "Puka Nacua",
                "target_multiplier": 1.5,
                "total_active_listings": 2,
                "cheapest_for_multiplier": 27.11,
                "cheapest_overall": 27.11,
                "cached": False,
            },
        ):
            resp = self.client.get("/api/market/player-floor?player=Puka+Nacua&mult=1.5")
            self.assertEqual(resp.status_code, 200)
            data = resp.get_json()
            self.assertEqual(data["status"], "success")
            self.assertEqual(data["data"]["cheapest_for_multiplier"], 27.11)

    def test_floor_cache_hit(self):
        market_advisor._FLOOR_CACHE.clear()
        with patch.object(market_advisor, "_load_disk_cache"), \
             patch.object(market_advisor, "_save_disk_cache"), \
             patch.object(
            market_advisor,
            "fetch_player_active_listings",
            return_value=[
                {
                    "id": "item-1",
                    "price": 10.0,
                    "marketplaceListingItemDetails": {
                        "playerItemDetails": {
                            "multiplier": 1.2,
                            "playerDetails": {"fullName": "Cache Player"},
                            "playerCollectionDetails": {"isCoreCollection": True},
                        }
                    },
                }
            ],
        ) as mock_fetch:
            res1 = market_advisor.get_live_market_floor("Cache Player", target_multiplier=1.2, is_core=True, token="dummy")
            self.assertEqual(res1["cheapest_for_multiplier"], 10.0)
            self.assertEqual(mock_fetch.call_count, 1)

            # Second call should hit in-memory cache and NOT call fetch_player_active_listings
            res2 = market_advisor.get_live_market_floor("Cache Player", target_multiplier=1.2, is_core=True, token="dummy")
            self.assertEqual(res2["cheapest_for_multiplier"], 10.0)
            self.assertEqual(mock_fetch.call_count, 1)


if __name__ == "__main__":
    unittest.main()
