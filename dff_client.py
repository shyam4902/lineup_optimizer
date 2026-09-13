"""Daily Fantasy Fuel (DFF) client, projection parser, and snapshot manager.

Supports live structured extraction from DFF, local CSV fallback, user projection
overrides preservation, and change detection between snapshots.
"""

from __future__ import annotations

import csv
import io
import json
import math
import os
import re
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from optimizer_core import POSITIONS, normalize_name, normalize_position, normalize_team


DFF_BASE_URL = "https://www.dailyfantasyfuel.com"
DFF_SLATES_ENDPOINT = f"{DFF_BASE_URL}/data/slates/next/nfl/dk?x=1"
DFF_PLAYERS_ENDPOINT_TEMPLATE = f"{DFF_BASE_URL}/data/playerdetails/nfl/dk/{{slate_id}}?x=1"

DEFAULT_USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko)"


@dataclass
class DFFPlayerProjection:
    player_name: str
    athlete_key: str
    team: str
    position: str
    salary: int
    raw_projection: float
    source_projection: float
    is_overridden: bool = False
    player_id: str = ""
    draft_id: str = ""
    opp: str = ""
    injury_status: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class SlateInfo:
    slate_id: str
    slate_type: str
    game_count: int
    team_count: int
    start_string: str
    showdown_flag: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ExtractionDiagnostic:
    reason: str
    player_name: str | None = None
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ProjectionsSnapshot:
    slate_id: str
    source: str
    fetched_at: str
    total_raw_rows: int
    offensive_player_count: int
    positive_projection_count: int
    dst_excluded_count: int
    other_excluded_count: int
    players: list[DFFPlayerProjection]
    diagnostics: list[ExtractionDiagnostic] = field(default_factory=list)
    slate_info: SlateInfo | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "slate_id": self.slate_id,
            "source": self.source,
            "fetched_at": self.fetched_at,
            "total_raw_rows": self.total_raw_rows,
            "offensive_player_count": self.offensive_player_count,
            "positive_projection_count": self.positive_projection_count,
            "dst_excluded_count": self.dst_excluded_count,
            "other_excluded_count": self.other_excluded_count,
            "players": [p.to_dict() for p in self.players],
            "diagnostics": [d.to_dict() for d in self.diagnostics],
            "slate_info": self.slate_info.to_dict() if self.slate_info else None,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ProjectionsSnapshot:
        players = [
            DFFPlayerProjection(
                player_name=p["player_name"],
                athlete_key=p["athlete_key"],
                team=p["team"],
                position=p["position"],
                salary=int(p.get("salary", 0)),
                raw_projection=float(p.get("raw_projection", 0.0)),
                source_projection=float(p.get("source_projection", p.get("raw_projection", 0.0))),
                is_overridden=bool(p.get("is_overridden", False)),
                player_id=str(p.get("player_id", "")),
                draft_id=str(p.get("draft_id", "")),
                opp=str(p.get("opp", "")),
                injury_status=str(p.get("injury_status", "")),
            )
            for p in data.get("players", [])
        ]
        diagnostics = [
            ExtractionDiagnostic(
                reason=d.get("reason", ""),
                player_name=d.get("player_name"),
                detail=d.get("detail", ""),
            )
            for d in data.get("diagnostics", [])
        ]
        slate_info_raw = data.get("slate_info")
        slate_info = (
            SlateInfo(
                slate_id=slate_info_raw.get("slate_id", ""),
                slate_type=slate_info_raw.get("slate_type", ""),
                game_count=int(slate_info_raw.get("game_count", 0)),
                team_count=int(slate_info_raw.get("team_count", 0)),
                start_string=slate_info_raw.get("start_string", ""),
                showdown_flag=int(slate_info_raw.get("showdown_flag", 0)),
            )
            if slate_info_raw
            else None
        )
        return cls(
            slate_id=data.get("slate_id", ""),
            source=data.get("source", "unknown"),
            fetched_at=data.get("fetched_at", ""),
            total_raw_rows=int(data.get("total_raw_rows", 0)),
            offensive_player_count=int(data.get("offensive_player_count", len(players))),
            positive_projection_count=int(data.get("positive_projection_count", 0)),
            dst_excluded_count=int(data.get("dst_excluded_count", 0)),
            other_excluded_count=int(data.get("other_excluded_count", 0)),
            players=players,
            diagnostics=diagnostics,
            slate_info=slate_info,
        )


def fetch_dff_slates(user_agent: str = DEFAULT_USER_AGENT) -> list[SlateInfo]:
    """Fetch list of available slates from DFF."""
    req = urllib.request.Request(DFF_SLATES_ENDPOINT, headers={"User-Agent": user_agent})
    with urllib.request.urlopen(req, timeout=12) as resp:
        raw = resp.read().decode("utf-8")
        data = json.loads(raw)
    slates: list[SlateInfo] = []
    for item in data:
        slates.append(
            SlateInfo(
                slate_id=str(item.get("url", "")),
                slate_type=str(item.get("slate_type", "")),
                game_count=int(item.get("game_count", 0)),
                team_count=int(item.get("team_count", 0)),
                start_string=str(item.get("start_string", "")),
                showdown_flag=int(item.get("showdown_flag", 0)),
            )
        )
    return slates


def fetch_dff_player_details(slate_id: str, user_agent: str = DEFAULT_USER_AGENT) -> list[dict[str, Any]]:
    """Fetch raw player details list for a given slate ID."""
    url = DFF_PLAYERS_ENDPOINT_TEMPLATE.format(slate_id=slate_id)
    req = urllib.request.Request(url, headers={"User-Agent": user_agent})
    with urllib.request.urlopen(req, timeout=15) as resp:
        raw = resp.read().decode("utf-8")
        data = json.loads(raw)
    if not isinstance(data, list):
        raise ValueError(f"DFF returned non-list response for slate {slate_id}")
    return data


def parse_dff_players(
    raw_players: list[dict[str, Any]],
    slate_id: str,
    slate_info: SlateInfo | None = None,
    user_overrides: Mapping[str, float] | None = None,
    min_offensive_players: int = 20,
) -> ProjectionsSnapshot:
    """Validate and parse raw DFF player rows into a ProjectionsSnapshot."""
    overrides = user_overrides or {}
    players: list[DFFPlayerProjection] = []
    diagnostics: list[ExtractionDiagnostic] = []

    dst_count = 0
    other_excluded_count = 0
    positive_count = 0
    seen_athletes: set[str] = set()

    for row in raw_players:
        pos_raw = str(row.get("position_code") or row.get("position_detailed") or "").upper().strip()
        first_name = str(row.get("first_name") or "").strip()
        last_name = str(row.get("last_name") or "").strip()
        full_name = f"{first_name} {last_name}".strip() if last_name else first_name

        if pos_raw == "DST":
            dst_count += 1
            diagnostics.append(ExtractionDiagnostic("dst_excluded", full_name, f"team={row.get('team')}"))
            continue

        position = normalize_position(pos_raw)
        if position not in POSITIONS:
            other_excluded_count += 1
            diagnostics.append(ExtractionDiagnostic("unsupported_position", full_name, f"position={pos_raw}"))
            continue

        team = normalize_team(row.get("team"))
        if not full_name or not team:
            other_excluded_count += 1
            diagnostics.append(ExtractionDiagnostic("malformed_player_row", full_name, "missing name or team"))
            continue

        athlete_key = normalize_name(full_name)
        if athlete_key in seen_athletes:
            diagnostics.append(ExtractionDiagnostic("duplicate_athlete_in_feed", full_name, f"team={team}; pos={position}"))
            # Keep the first or primary
            continue
        seen_athletes.add(athlete_key)

        try:
            raw_ppg = float(str(row.get("ppg", 0)).strip().replace(",", ""))
        except (ValueError, TypeError):
            raw_ppg = 0.0

        try:
            salary = int(float(str(row.get("salary", 0)).strip().replace(",", "").replace("$", "")))
        except (ValueError, TypeError):
            salary = 0

        if raw_ppg <= 0:
            diagnostics.append(ExtractionDiagnostic("zero_or_negative_projection", full_name, f"ppg={raw_ppg}"))
        else:
            positive_count += 1

        is_overridden = athlete_key in overrides
        effective_proj = float(overrides[athlete_key]) if is_overridden else raw_ppg

        players.append(
            DFFPlayerProjection(
                player_name=full_name,
                athlete_key=athlete_key,
                team=team,
                position=position,
                salary=salary,
                raw_projection=effective_proj,
                source_projection=raw_ppg,
                is_overridden=is_overridden,
                player_id=str(row.get("player_id", "")),
                draft_id=str(row.get("draft_id", "")),
                opp=str(row.get("opp", "")),
                injury_status=str(row.get("injury_status", "")),
            )
        )

    if len(players) < min_offensive_players:
        raise ValueError(
            f"DFF feed rejected: found only {len(players)} valid offensive players (minimum {min_offensive_players} required)."
        )

    now_iso = datetime.now(timezone.utc).isoformat()
    return ProjectionsSnapshot(
        slate_id=slate_id,
        source="dailyfantasyfuel_api",
        fetched_at=now_iso,
        total_raw_rows=len(raw_players),
        offensive_player_count=len(players),
        positive_projection_count=positive_count,
        dst_excluded_count=dst_count,
        other_excluded_count=other_excluded_count,
        players=players,
        diagnostics=diagnostics,
        slate_info=slate_info,
    )


def parse_projections_csv_stream(
    stream: io.StringIO | io.TextIOBase,
    source_name: str = "custom_csv",
    user_overrides: Mapping[str, float] | None = None,
) -> ProjectionsSnapshot:
    """Parse projection CSV file or stream, capturing weekly salary if present."""
    reader = csv.DictReader(stream)
    headers = list(reader.fieldnames or [])
    if not headers:
        raise ValueError("Projections CSV has no headers")

    overrides = user_overrides or {}
    players: list[DFFPlayerProjection] = []
    diagnostics: list[ExtractionDiagnostic] = []
    seen_athletes: set[str] = set()

    # Find relevant column names
    header_map: dict[str, str] = {re.sub(r"[^a-z0-9]", "", h.casefold()): h for h in headers}

    name_col = next((header_map[k] for k in ("playername", "player") if k in header_map), None)
    first_name_col = next((header_map[k] for k in ("firstname", "first") if k in header_map), None)
    last_name_col = next((header_map[k] for k in ("lastname", "last") if k in header_map), None)

    team_col = next((header_map[k] for k in ("team", "teamcode") if k in header_map), None)
    pos_col = next((header_map[k] for k in ("position", "positions", "pos") if k in header_map), None)
    proj_col = next((header_map[k] for k in ("ppgprojection", "3dproj", "rawprojection", "projection", "proj", "ppg") if k in header_map), None)
    salary_col = next((header_map[k] for k in ("draftkings", "salary", "dk", "sal") if k in header_map), None)

    # Provenance columns
    week_col = next((header_map[k] for k in ("week",) if k in header_map), None)
    date_col = next((header_map[k] for k in ("gamedate", "date") if k in header_map), None)
    slate_col = next((header_map[k] for k in ("slate", "slatetype", "slatename") if k in header_map), None)

    if (not name_col and not first_name_col) or not proj_col:
        raise ValueError(f"Projections CSV must contain a Player (or first_name) and Projection column. Found: {headers}")

    total_rows = 0
    positive_count = 0
    dst_count = 0
    other_excluded = 0

    detected_slate = "csv_import"
    detected_week = ""
    game_dates: set[str] = set()

    for row in reader:
        total_rows += 1
        if first_name_col:
            fn = str(row.get(first_name_col, "") or "").strip()
            ln = str(row.get(last_name_col, "") or "").strip() if last_name_col else ""
            raw_name = f"{fn} {ln}".strip() if ln else fn
        else:
            raw_name = str(row.get(name_col, "") or "").strip()

        raw_team = row.get(team_col) if team_col else None
        raw_pos = row.get(pos_col) if pos_col else None

        # Capture provenance if present
        if slate_col and row.get(slate_col) and detected_slate == "csv_import":
            s_val = str(row.get(slate_col)).strip()
            if s_val:
                detected_slate = s_val
        if week_col and row.get(week_col) and not detected_week:
            detected_week = str(row.get(week_col)).strip()
        if date_col and row.get(date_col):
            d_val = str(row.get(date_col)).strip()
            if d_val:
                game_dates.add(d_val)

        # Check if name has suffix like 'Josh Allen QB BUF'
        if not raw_pos or not raw_team:
            m = re.match(r"^\s*(.+?)\s+(QB|RB|WR|TE|DST)\s+([A-Za-z]{2,3})\s*$", raw_name, re.IGNORECASE)
            if m:
                raw_name = m.group(1).strip()
                raw_pos = m.group(2).strip()
                raw_team = m.group(3).strip()

        pos_norm = normalize_position(raw_pos)
        if pos_norm == "DST":
            dst_count += 1
            diagnostics.append(ExtractionDiagnostic("dst_excluded", raw_name))
            continue
        if pos_norm not in POSITIONS:
            other_excluded += 1
            diagnostics.append(ExtractionDiagnostic("unsupported_position", raw_name, f"position={raw_pos}"))
            continue

        team_norm = normalize_team(raw_team)
        if not raw_name or not team_norm:
            other_excluded += 1
            continue

        athlete_key = normalize_name(raw_name)
        if athlete_key in seen_athletes:
            diagnostics.append(ExtractionDiagnostic("duplicate_athlete_in_feed", raw_name))
            continue
        seen_athletes.add(athlete_key)

        try:
            raw_proj_str = str(row.get(proj_col, 0)).strip().replace(",", "")
            raw_proj = float(raw_proj_str)
            if not math.isfinite(raw_proj):
                raw_proj = 0.0
        except (ValueError, TypeError):
            raw_proj = 0.0

        salary = 0
        if salary_col and row.get(salary_col):
            try:
                sal_num = float(str(row.get(salary_col, 0)).strip().replace(",", "").replace("$", ""))
                salary = int(sal_num) if math.isfinite(sal_num) else 0
            except (ValueError, TypeError):
                salary = 0

        if raw_proj <= 0:
            diagnostics.append(ExtractionDiagnostic("zero_or_negative_projection", raw_name, f"proj={raw_proj}"))
        else:
            positive_count += 1

        is_overridden = athlete_key in overrides
        effective_proj = float(overrides[athlete_key]) if is_overridden else raw_proj

        players.append(
            DFFPlayerProjection(
                player_name=raw_name,
                athlete_key=athlete_key,
                team=team_norm,
                position=pos_norm,
                salary=salary,
                raw_projection=effective_proj,
                source_projection=raw_proj,
                is_overridden=is_overridden,
                opp=str(row.get("opp", "") or "").strip(),
                injury_status=str(row.get("injury_status", "") or "").strip(),
            )
        )

    # Validate snapshot gatekeeping: reject unusable snapshots before committing
    if len(players) < 20:
        raise ValueError(
            f"Projections CSV rejected: found only {len(players)} valid offensive players (minimum 20 required)."
        )

    now_iso = datetime.now(timezone.utc).isoformat()
    slate_start_str = f"Week {detected_week} ({', '.join(sorted(game_dates))})" if detected_week or game_dates else ""
    slate_info = SlateInfo(
        slate_id=detected_slate,
        slate_type=detected_slate,
        game_count=len(game_dates),
        team_count=len(set(p.team for p in players)),
        start_string=slate_start_str,
    ) if (detected_slate != "csv_import" or slate_start_str) else None

    return ProjectionsSnapshot(
        slate_id=detected_slate,
        source=source_name,
        fetched_at=now_iso,
        total_raw_rows=total_rows,
        offensive_player_count=len(players),
        positive_projection_count=positive_count,
        dst_excluded_count=dst_count,
        other_excluded_count=other_excluded,
        players=players,
        diagnostics=diagnostics,
        slate_info=slate_info,
    )


def compare_snapshots(old: ProjectionsSnapshot | None, new: ProjectionsSnapshot) -> dict[str, Any]:
    """Compare two snapshots to determine change category.

    Returns dict with 'status' in:
      - 'NEW_SLATE': slate ID changed or slate dates changed
      - 'PROJECTIONS_CHANGED': projections or salaries updated for existing slate
      - 'NO_CHANGE': projections and metadata are unchanged
      - 'INITIAL_SNAPSHOT': old snapshot was None
    """
    if old is None:
        return {
            "status": "INITIAL_SNAPSHOT",
            "message": f"Loaded initial snapshot with {new.offensive_player_count} offensive players.",
            "changed_player_count": 0,
            "max_delta": 0.0,
            "changed_players": [],
        }

    if old.slate_id != new.slate_id:
        return {
            "status": "NEW_SLATE",
            "message": f"New slate detected: {new.slate_id} (previously {old.slate_id}).",
            "changed_player_count": 0,
            "max_delta": 0.0,
            "changed_players": [],
        }

    old_map = {p.athlete_key: p for p in old.players}
    new_map = {p.athlete_key: p for p in new.players}

    changed_players: list[dict[str, Any]] = []
    max_delta = 0.0

    for key, new_p in new_map.items():
        if key not in old_map:
            changed_players.append({
                "player_name": new_p.player_name,
                "team": new_p.team,
                "position": new_p.position,
                "type": "added",
                "old_proj": None,
                "new_proj": new_p.source_projection,
                "old_salary": None,
                "new_salary": new_p.salary,
            })
            continue

        old_p = old_map[key]
        delta_proj = round(abs(new_p.source_projection - old_p.source_projection), 4)
        delta_sal = abs(new_p.salary - old_p.salary)

        if delta_proj > 0.001 or delta_sal > 0:
            if delta_proj > max_delta:
                max_delta = delta_proj
            changed_players.append({
                "player_name": new_p.player_name,
                "team": new_p.team,
                "position": new_p.position,
                "type": "modified",
                "old_proj": old_p.source_projection,
                "new_proj": new_p.source_projection,
                "old_salary": old_p.salary,
                "new_salary": new_p.salary,
                "delta_proj": delta_proj,
                "delta_salary": delta_sal,
            })

    for key, old_p in old_map.items():
        if key not in new_map:
            changed_players.append({
                "player_name": old_p.player_name,
                "team": old_p.team,
                "position": old_p.position,
                "type": "removed",
                "old_proj": old_p.source_projection,
                "new_proj": None,
            })

    if not changed_players:
        return {
            "status": "NO_CHANGE",
            "message": f"No projection or salary changes detected across {len(new_map)} players.",
            "changed_player_count": 0,
            "max_delta": 0.0,
            "changed_players": [],
        }

    return {
        "status": "PROJECTIONS_CHANGED",
        "message": f"Projections updated: {len(changed_players)} player values changed (max delta: {max_delta:.2f} pts).",
        "changed_player_count": len(changed_players),
        "max_delta": max_delta,
        "changed_players": changed_players[:20],
    }


class SnapshotStore:
    """Manages reading, persisting, and versioning projection snapshots."""

    def __init__(self, directory: Path | str = "data/snapshots"):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.latest_file = self.directory / "latest_snapshot.json"
        self.overrides_file = self.directory / "projection_overrides.json"

    def load_overrides(self) -> dict[str, float]:
        if not self.overrides_file.exists():
            return {}
        try:
            with self.overrides_file.open("r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}

    def save_overrides(self, overrides: Mapping[str, float]) -> None:
        with self.overrides_file.open("w", encoding="utf-8") as f:
            json.dump(dict(overrides), f, indent=2)

    def load_latest(self) -> ProjectionsSnapshot | None:
        if not self.latest_file.exists():
            return None
        try:
            with self.latest_file.open("r", encoding="utf-8") as f:
                data = json.load(f)
            return ProjectionsSnapshot.from_dict(data)
        except Exception:
            return None

    def save_snapshot(self, snapshot: ProjectionsSnapshot) -> Path:
        if snapshot.offensive_player_count < 20 or len(snapshot.players) < 20:
            raise ValueError(
                f"Cannot save unusable snapshot: only {snapshot.offensive_player_count} offensive players (minimum 20 required)."
            )

        # Atomic write to latest_file
        temp_file = self.latest_file.with_suffix(".tmp")
        with temp_file.open("w", encoding="utf-8") as f:
            json.dump(snapshot.to_dict(), f, indent=2)
        temp_file.replace(self.latest_file)

        # Also save an archival copy for history
        safe_slate = re.sub(r"[^a-zA-Z0-9_-]", "_", snapshot.slate_id)
        archive_name = f"snapshot_{safe_slate}_{snapshot.fetched_at[:10]}.json"
        archive_file = self.directory / archive_name
        try:
            with archive_file.open("w", encoding="utf-8") as f:
                json.dump(snapshot.to_dict(), f, indent=2)
        except Exception:
            pass
        return self.latest_file
