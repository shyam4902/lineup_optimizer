"""Contest configurations, payout structures, and historical threshold data for GameBlazers."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence


@dataclass(frozen=True)
class PayoutBand:
    start_rank: int
    end_rank: int
    payout_per_entry: float
    description: str

    @property
    def places(self) -> int:
        return self.end_rank - self.start_rank + 1

    @property
    def total_band_payout(self) -> float:
        return self.places * self.payout_per_entry


@dataclass(frozen=True)
class HistoricalThreshold:
    tier: str
    rank_cutoff: int
    estimate: float
    lower: float
    upper: float
    training_weeks: tuple[int, ...]
    source_date: str = "2024-12-07"
    model_notes: str = "Bayesian regression fitted(summary=TRUE)[1,] on historical contest top-tier scores."


@dataclass
class ContestConfig:
    name: str
    contest_id: str
    enabled: bool
    eligible: bool
    slots: tuple[str, ...]
    minimum_salary: int
    maximum_salary: int
    entry_fee: float
    default_entry_limit: int | None
    prize_pool: float
    payout_bands: list[PayoutBand]
    historical_thresholds: list[HistoricalThreshold]
    collection_requirements: dict[str, tuple[int, int]] = field(default_factory=dict)
    notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "contest_id": self.contest_id,
            "enabled": self.enabled,
            "eligible": self.eligible,
            "slots": list(self.slots),
            "minimum_salary": self.minimum_salary,
            "maximum_salary": self.maximum_salary,
            "entry_fee": self.entry_fee,
            "default_entry_limit": self.default_entry_limit,
            "prize_pool": self.prize_pool,
            "collection_requirements": {k: list(v) for k, v in self.collection_requirements.items()},
            "payout_bands": [
                {
                    "start_rank": band.start_rank,
                    "end_rank": band.end_rank,
                    "payout_per_entry": band.payout_per_entry,
                    "description": band.description,
                }
                for band in self.payout_bands
            ],
            "historical_thresholds": [
                {
                    "tier": th.tier,
                    "rank_cutoff": th.rank_cutoff,
                    "estimate": th.estimate,
                    "lower": th.lower,
                    "upper": th.upper,
                    "training_weeks": list(th.training_weeks),
                    "source_date": th.source_date,
                    "model_notes": th.model_notes,
                }
                for th in self.historical_thresholds
            ],
            "notes": self.notes,
        }


# Scorcher Payout Table ($3,000 Prize Pool, Top 360)
SCORCHER_PAYOUT_BANDS = [
    PayoutBand(1, 1, 240.0, "1st place"),
    PayoutBand(2, 2, 130.0, "2nd place"),
    PayoutBand(3, 3, 95.0, "3rd place"),
    PayoutBand(4, 4, 75.0, "4th place"),
    PayoutBand(5, 5, 65.0, "5th place"),
    PayoutBand(6, 10, 45.0, "6th - 10th place"),
    PayoutBand(11, 15, 30.0, "11th - 15th place"),
    PayoutBand(16, 20, 25.0, "16th - 20th place"),
    PayoutBand(21, 30, 20.0, "21st - 30th place"),
    PayoutBand(31, 40, 15.0, "31st - 40th place"),
    PayoutBand(41, 50, 10.0, "41st - 50th place"),
    PayoutBand(51, 75, 9.0, "51st - 75th place"),
    PayoutBand(76, 100, 8.0, "76th - 100th place"),
    PayoutBand(101, 150, 6.0, "101st - 150th place"),
    PayoutBand(151, 200, 5.0, "151st - 200th place"),
    PayoutBand(201, 250, 4.0, "201st - 250th place"),
    PayoutBand(251, 300, 3.0, "251st - 300th place"),
    PayoutBand(301, 360, 2.0, "301st - 360th place"),
]

SCORCHER_HISTORICAL_THRESHOLDS = [
    HistoricalThreshold("Top360", 360, 86.00, 75.00, 95.00, (8, 10, 11, 13)),
    HistoricalThreshold("Top200", 200, 99.66, 89.18, 109.96, (8, 10, 11, 13)),
    HistoricalThreshold("Top100", 100, 110.45, 98.89, 122.04, (8, 10, 11, 13)),
    HistoricalThreshold("Top75", 75, 116.58, 105.41, 128.94, (8, 10, 11, 13)),
    HistoricalThreshold("Top50", 50, 122.02, 111.24, 134.86, (8, 10, 11, 13)),
    HistoricalThreshold("Top20", 20, 132.30, 125.04, 141.28, (8, 10, 11, 13)),
    HistoricalThreshold("Top10", 10, 138.61, 133.65, 146.83, (8, 10, 11, 13)),
]

# Volcano Payout Table ($7,000 Prize Pool, Top 500)
VOLCANO_PAYOUT_BANDS = [
    PayoutBand(1, 1, 700.0, "1st place"),
    PayoutBand(2, 2, 380.0, "2nd place"),
    PayoutBand(3, 3, 260.0, "3rd place"),
    PayoutBand(4, 4, 200.0, "4th place"),
    PayoutBand(5, 5, 160.0, "5th place"),
    PayoutBand(6, 10, 110.0, "6th - 10th place"),
    PayoutBand(11, 15, 70.0, "11th - 15th place"),
    PayoutBand(16, 20, 55.0, "16th - 20th place"),
    PayoutBand(21, 30, 40.0, "21st - 30th place"),
    PayoutBand(31, 40, 30.0, "31st - 40th place"),
    PayoutBand(41, 50, 25.0, "41st - 50th place"),
    PayoutBand(51, 75, 20.0, "51st - 75th place"),
    PayoutBand(76, 100, 15.0, "76th - 100th place"),
    PayoutBand(101, 150, 10.0, "101st - 150th place"),
    PayoutBand(151, 200, 9.0, "151st - 200th place"),
    PayoutBand(201, 250, 7.0, "201st - 250th place"),
    PayoutBand(251, 300, 6.0, "251st - 300th place"),
    PayoutBand(301, 350, 5.0, "301st - 350th place"),
    PayoutBand(351, 400, 4.0, "351st - 400th place"),
    PayoutBand(401, 450, 3.0, "401st - 450th place"),
    PayoutBand(451, 500, 2.0, "451st - 500th place"),
]
WILDFIRE_PAYOUT_BANDS = VOLCANO_PAYOUT_BANDS

VOLCANO_HISTORICAL_THRESHOLDS = [
    HistoricalThreshold("Top500", 500, 105.00, 98.00, 112.00, (11, 12, 13)),
    HistoricalThreshold("Top300", 300, 123.08, 120.58, 125.40, (11, 12, 13)),
    HistoricalThreshold("Top200", 200, 134.72, 130.40, 138.03, (11, 12, 13)),
    HistoricalThreshold("Top100", 100, 148.15, 142.25, 152.85, (11, 12, 13)),
    HistoricalThreshold("Top50", 50, 160.76, 152.97, 166.13, (11, 12, 13)),
    HistoricalThreshold("Top20", 20, 174.31, 164.06, 185.07, (11, 12, 13)),
    HistoricalThreshold("Top7", 7, 187.67, 180.31, 192.79, (11, 12, 13)),
]
WILDFIRE_HISTORICAL_THRESHOLDS = VOLCANO_HISTORICAL_THRESHOLDS

# No verified Flamethrower payout schedule or historical score sample is available.
FLAMETHROWER_PAYOUT_BANDS = []
FLAMETHROWER_HISTORICAL_THRESHOLDS = []

# Primetime Pyro Payout Table ($2,500 Prize Pool, Top 120)
PRIMETIME_PYRO_PAYOUT_BANDS = [
    PayoutBand(1, 1, 250.0, "1st place"),
    PayoutBand(2, 2, 150.0, "2nd place"),
    PayoutBand(3, 3, 110.0, "3rd place"),
    PayoutBand(4, 4, 85.0, "4th place"),
    PayoutBand(5, 5, 75.0, "5th place"),
    PayoutBand(6, 10, 50.0, "6th - 10th place"),
    PayoutBand(11, 15, 35.0, "11th - 15th place"),
    PayoutBand(16, 20, 30.0, "16th - 20th place"),
    PayoutBand(21, 30, 25.0, "21st - 30th place"),
    PayoutBand(31, 40, 20.0, "31st - 40th place"),
    PayoutBand(41, 50, 15.0, "41st - 50th place"),
    PayoutBand(51, 75, 10.0, "51st - 75th place"),
    PayoutBand(76, 120, 9.0, "76th - 120th place"),
]

PRIMETIME_PYRO_HISTORICAL_THRESHOLDS = [
    HistoricalThreshold("Top120", 120, 115.00, 105.00, 125.00, (8, 10, 11, 13)),
    HistoricalThreshold("Top75", 75, 125.00, 115.00, 135.00, (8, 10, 11, 13)),
    HistoricalThreshold("Top50", 50, 135.00, 125.00, 145.00, (8, 10, 11, 13)),
    HistoricalThreshold("Top20", 20, 150.00, 140.00, 160.00, (8, 10, 11, 13)),
    HistoricalThreshold("Top10", 10, 160.00, 150.00, 170.00, (8, 10, 11, 13)),
    HistoricalThreshold("Top5", 5, 170.00, 160.00, 180.00, (8, 10, 11, 13)),
]

# Flex Appeal Payout Table ($2,500 Prize Pool, Top 120)
FLEX_APPEAL_PAYOUT_BANDS = [
    PayoutBand(1, 1, 250.0, "1st place"),
    PayoutBand(2, 2, 150.0, "2nd place"),
    PayoutBand(3, 3, 110.0, "3rd place"),
    PayoutBand(4, 4, 85.0, "4th place"),
    PayoutBand(5, 5, 75.0, "5th place"),
    PayoutBand(6, 10, 50.0, "6th - 10th place"),
    PayoutBand(11, 15, 35.0, "11th - 15th place"),
    PayoutBand(16, 20, 30.0, "16th - 20th place"),
    PayoutBand(21, 30, 25.0, "21st - 30th place"),
    PayoutBand(31, 40, 20.0, "31st - 40th place"),
    PayoutBand(41, 50, 15.0, "41st - 50th place"),
    PayoutBand(51, 75, 10.0, "51st - 75th place"),
    PayoutBand(76, 120, 9.0, "76th - 120th place"),
]

FLEX_APPEAL_HISTORICAL_THRESHOLDS = [
    HistoricalThreshold("Top120", 120, 118.00, 108.00, 128.00, (8, 10, 11, 13)),
    HistoricalThreshold("Top75", 75, 128.00, 118.00, 138.00, (8, 10, 11, 13)),
    HistoricalThreshold("Top50", 50, 138.00, 128.00, 148.00, (8, 10, 11, 13)),
    HistoricalThreshold("Top20", 20, 152.00, 142.00, 162.00, (8, 10, 11, 13)),
    HistoricalThreshold("Top10", 10, 165.00, 155.00, 175.00, (8, 10, 11, 13)),
    HistoricalThreshold("Top5", 5, 175.00, 165.00, 185.00, (8, 10, 11, 13)),
]

# Spark Payout Bands (beginner contest reference)
SPARK_PAYOUT_BANDS = [
    PayoutBand(1, 1, 250.0, "1st place"),
    PayoutBand(2, 2, 100.0, "2nd place"),
    PayoutBand(3, 3, 50.0, "3rd place"),
    PayoutBand(4, 4, 40.0, "4th place"),
    PayoutBand(5, 5, 30.0, "5th place"),
    PayoutBand(6, 7, 20.0, "6th - 7th place"),
    PayoutBand(8, 10, 10.0, "8th - 10th place"),
    PayoutBand(11, 20, 8.0, "11th - 20th place"),
    PayoutBand(21, 50, 6.0, "21st - 50th place"),
    PayoutBand(51, 100, 4.0, "51st - 100th place"),
]

SPARK_HISTORICAL_THRESHOLDS = [
    HistoricalThreshold("Top200", 200, 63.58, 55.76, 68.06, (11, 12, 13)),
    HistoricalThreshold("Top100", 100, 80.20, 72.71, 86.85, (11, 12, 13)),
    HistoricalThreshold("Top50", 50, 100.98, 95.46, 104.95, (11, 12, 13)),
    HistoricalThreshold("Top10", 10, 117.28, 110.81, 123.13, (11, 12, 13)),
]

# Inferno Payout Table ($15,000 Prize Pool, Top 560)
INFERNO_PAYOUT_BANDS = [
    PayoutBand(1, 1, 1500.0, "1st place"),
    PayoutBand(2, 2, 800.0, "2nd place"),
    PayoutBand(3, 3, 550.0, "3rd place"),
    PayoutBand(4, 4, 420.0, "4th place"),
    PayoutBand(5, 5, 340.0, "5th place"),
    PayoutBand(6, 10, 220.0, "6th - 10th place"),
    PayoutBand(11, 15, 140.0, "11th - 15th place"),
    PayoutBand(16, 20, 110.0, "16th - 20th place"),
    PayoutBand(21, 30, 80.0, "21st - 30th place"),
    PayoutBand(31, 40, 60.0, "31st - 40th place"),
    PayoutBand(41, 50, 50.0, "41st - 50th place"),
    PayoutBand(51, 75, 40.0, "51st - 75th place"),
    PayoutBand(76, 100, 30.0, "76th - 100th place"),
    PayoutBand(101, 150, 25.0, "101st - 150th place"),
    PayoutBand(151, 200, 20.0, "151st - 200th place"),
    PayoutBand(201, 250, 15.0, "201st - 250th place"),
    PayoutBand(251, 300, 10.0, "251st - 300th place"),
    PayoutBand(301, 350, 9.0, "301st - 350th place"),
    PayoutBand(351, 400, 8.0, "351st - 400th place"),
    PayoutBand(401, 480, 7.0, "401st - 480th place"),
    PayoutBand(481, 560, 6.0, "481st - 560th place"),
]

INFERNO_HISTORICAL_THRESHOLDS = [
    HistoricalThreshold("Top560", 560, 140.00, 130.00, 150.00, (10, 13)),
    HistoricalThreshold("Top400", 400, 156.75, 146.00, 167.50, (10, 13)),
    HistoricalThreshold("Top200", 200, 175.50, 168.00, 183.00, (10, 13)),
    HistoricalThreshold("Top100", 100, 194.70, 193.00, 196.40, (10, 13)),
    HistoricalThreshold("Top50", 50, 211.00, 209.50, 212.50, (10, 13)),
    HistoricalThreshold("Top20", 20, 228.40, 224.00, 232.80, (10, 13)),
    HistoricalThreshold("Top10", 10, 241.80, 237.00, 246.60, (10, 13)),
]


def get_default_contests() -> dict[str, ContestConfig]:
    return {
        "Inferno": ContestConfig(
            name="Inferno",
            contest_id="inferno",
            enabled=True,
            eligible=True,
            slots=("QB", "RB", "RB", "WR", "WR", "TE", "Flex", "Superflex"),
            minimum_salary=30000,
            maximum_salary=70000,
            entry_fee=0.0,
            default_entry_limit=8,
            prize_pool=15000.0,
            payout_bands=list(INFERNO_PAYOUT_BANDS),
            historical_thresholds=list(INFERNO_HISTORICAL_THRESHOLDS),
            collection_requirements={},
            notes="Inferno contest. 8 slots, $15k prize pool, $30k-$70k salary range, max 8 entries.",
        ),
        "Volcano": ContestConfig(
            name="Volcano",
            contest_id="volcano",
            enabled=True,
            eligible=True,
            slots=("QB", "RB", "WR", "TE", "Flex", "Flex"),
            minimum_salary=25000,
            maximum_salary=52000,
            entry_fee=0.0,
            default_entry_limit=6,
            prize_pool=7000.0,
            payout_bands=list(VOLCANO_PAYOUT_BANDS),
            historical_thresholds=list(VOLCANO_HISTORICAL_THRESHOLDS),
            collection_requirements={},
            notes="Volcano contest. 6 slots, $7k prize pool, $25k-$52k salary range, max 6 entries.",
        ),
        "Flamethrower": ContestConfig(
            name="Flamethrower",
            contest_id="flamethrower",
            enabled=True,
            eligible=True,
            slots=("QB", "RB", "WR", "TE", "Flex", "Superflex"),
            minimum_salary=30000,
            maximum_salary=62000,
            entry_fee=0.0,
            default_entry_limit=6,
            prize_pool=5000.0,
            payout_bands=list(FLAMETHROWER_PAYOUT_BANDS),
            historical_thresholds=list(FLAMETHROWER_HISTORICAL_THRESHOLDS),
            collection_requirements={"Core": (6, 6)},
            notes="Flamethrower contest. 6 slots with Superflex, $5k prize pool, $30k-$62k salary range, max 6 entries. Requires 6 Core Collection cards.",
        ),
        "Scorcher": ContestConfig(
            name="Scorcher",
            contest_id="scorcher",
            enabled=True,
            eligible=True,
            slots=("QB", "RB", "WR", "TE", "Flex"),
            minimum_salary=20000,
            maximum_salary=40000,
            entry_fee=0.0,
            default_entry_limit=5,
            prize_pool=3000.0,
            payout_bands=list(SCORCHER_PAYOUT_BANDS),
            historical_thresholds=list(SCORCHER_HISTORICAL_THRESHOLDS),
            collection_requirements={},
            notes="Scorcher contest. 5 slots, $3k prize pool, $20k-$40k salary range, max 5 entries.",
        ),
        "Primetime Pyro": ContestConfig(
            name="Primetime Pyro",
            contest_id="primetime_pyro",
            enabled=True,
            eligible=True,
            slots=("QB", "RB", "WR", "TE", "Flex", "Flex"),
            minimum_salary=0,
            maximum_salary=48000,
            entry_fee=0.0,
            default_entry_limit=6,
            prize_pool=2500.0,
            payout_bands=list(PRIMETIME_PYRO_PAYOUT_BANDS),
            historical_thresholds=list(PRIMETIME_PYRO_HISTORICAL_THRESHOLDS),
            collection_requirements={},
            notes="Primetime Pyro contest. 6 slots, $2.5k prize pool, $0-$48k salary range, max 6 entries.",
        ),
        "Flex Appeal": ContestConfig(
            name="Flex Appeal",
            contest_id="flex_appeal",
            enabled=True,
            eligible=True,
            slots=("QB", "Flex", "Flex", "Flex", "Flex", "Flex"),
            minimum_salary=25000,
            maximum_salary=52000,
            entry_fee=0.0,
            default_entry_limit=6,
            prize_pool=2500.0,
            payout_bands=list(FLEX_APPEAL_PAYOUT_BANDS),
            historical_thresholds=list(FLEX_APPEAL_HISTORICAL_THRESHOLDS),
            collection_requirements={"Core": (6, 6)},
            notes="Flex Appeal contest. 1 QB + 5 Flex slots, $2.5k prize pool, $25k-$52k salary range, max 6 entries. Requires 6 Core Collection cards.",
        ),
        "Wildfire": ContestConfig(
            name="Wildfire",
            contest_id="wildfire",
            enabled=True,
            eligible=True,
            slots=("QB", "RB", "WR", "TE", "Flex", "Flex"),
            minimum_salary=25000,
            maximum_salary=52000,
            entry_fee=0.0,
            default_entry_limit=6,
            prize_pool=7000.0,
            payout_bands=list(VOLCANO_PAYOUT_BANDS),
            historical_thresholds=list(VOLCANO_HISTORICAL_THRESHOLDS),
            collection_requirements={},
            notes="Wildfire contest (aliased to Volcano).",
        ),
        "Spark": ContestConfig(
            name="Spark",
            contest_id="spark",
            enabled=False,
            eligible=False,
            slots=("QB", "RB", "WR", "TE"),
            minimum_salary=16000,
            maximum_salary=32000,
            entry_fee=0.0,
            default_entry_limit=None,
            prize_pool=1000.0,
            payout_bands=list(SPARK_PAYOUT_BANDS),
            historical_thresholds=list(SPARK_HISTORICAL_THRESHOLDS),
            notes="Beginner contest. Excluded from recommendations.",
        ),
    }
