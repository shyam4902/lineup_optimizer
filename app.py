from __future__ import annotations

import io
import copy
import json
import math
import os
import secrets
from collections import Counter
from dataclasses import asdict, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from flask import Flask, Response, jsonify, render_template, request

import contest_config
import dff_client
import live_lineup_manager
import market_advisor
import multi_lineup_planner
import payout_evaluator
import planner_update_check
import schedule_manager
from optimizer_core import CONTESTS, OptimizationResult, normalize_team, optimize_lineup


app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 8 * 1024 * 1024
app.config["TEMPLATES_AUTO_RELOAD"] = True
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "gameblazers-lineup-planner-secret-key")

password_file = os.environ.get("PLANNER_PASSWORD_FILE")
app.config["PREVIEW_PASSWORD"] = Path(password_file).read_text().strip() if password_file else None
app.config["PREVIEW_USERNAME"] = os.environ.get("PLANNER_USERNAME", "friend")
allow_public_preview = os.environ.get("PLANNER_ALLOW_PUBLIC") == "1"
if os.environ.get("PLANNER_INSTANCE_ROOT") and not app.config["PREVIEW_PASSWORD"] and not allow_public_preview:
    raise RuntimeError("An isolated preview requires PLANNER_PASSWORD_FILE.")


@app.before_request
def protect_private_preview():
    password = app.config.get("PREVIEW_PASSWORD")
    if password:
        auth = request.authorization
        if not auth or auth.type != "basic" or auth.username != app.config["PREVIEW_USERNAME"] or not secrets.compare_digest((auth.password or "").encode(), password.encode()):
            return Response("Sign in to the private GameBlazers preview.", 401,
                            {"WWW-Authenticate": 'Basic realm="GameBlazers private preview"'})
    if os.environ.get("PLANNER_INSTANCE_ROOT"):
        origin = request.headers.get("Origin")
        landing_page = request.method == "GET" and request.path in ("/", "/planner")
        if (request.headers.get("Sec-Fetch-Site") == "cross-site" and not landing_page) or (origin and urlsplit(origin).netloc != request.host):
            return Response("Cross-site requests are not allowed.", 403)


@app.after_request
def private_preview_headers(response):
    if os.environ.get("PLANNER_INSTANCE_ROOT"):
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-Content-Type-Options"] = "nosniff"
    return response

ALLOWED_EXTENSIONS = {"csv"}
DIAGNOSTIC_LABELS = {
    "non_active": "Non-active",
    "zero_projection": "Zero projection",
    "missing_projection": "Missing projection",
    "name_unmatched": "Name unmatched",
    "position_mismatch": "Position mismatch",
    "team_mismatch": "Team mismatch",
    "malformed_roster_row": "Malformed roster row",
    "malformed_projection_row": "Malformed projection row",
    "projection_ambiguous": "Projection ambiguity",
    "exact_visible_duplicate_collapsed": "Exact visible duplicate collapsed",
}
INFEASIBLE_MESSAGES = {
    "malformed_input_schema": "The uploaded files do not have the required columns or contain malformed input fields.",
    "no_eligible_card_for_required_slot": "There is no eligible card for at least one required lineup slot.",
    "duplicate_athlete_constraint": "The available cards cannot complete this lineup without using one athlete more than once.",
    "maximum_cap_infeasibility": "No complete lineup fits below the contest maximum salary.",
    "minimum_spend_infeasibility": "No complete lineup reaches the contest minimum salary while respecting the other rules.",
    "no_feasible_assignment": "No complete lineup satisfies all contest constraints.",
}


class PlannerAppState:
    def __init__(self, root_dir: Path | str | None = None):
        self.isolated = root_dir is not None
        self.root_dir = Path(__file__).resolve().parent if root_dir is None else Path(root_dir).resolve()
        self.snapshot_store = dff_client.SnapshotStore(self.root_dir / "data" / "snapshots")
        self.plans_dir = self.root_dir / "data" / "saved_plans"
        self.plans_dir.mkdir(parents=True, exist_ok=True)
        self.contests = contest_config.get_default_contests()

        self.overrides: dict[str, float] = self.snapshot_store.load_overrides()
        self.exclusions_path = self.root_dir / "data" / "excluded_players.json"
        self.excluded_athlete_keys = set(json.loads(self.exclusions_path.read_text()) if self.exclusions_path.exists() else [])
        self.snapshot: dff_client.ProjectionsSnapshot | None = self.snapshot_store.load_latest()
        self.roster_cards: list[multi_lineup_planner.OwnedCard] = []
        self.planner_cards: list[multi_lineup_planner.PlannerCard] = []
        self.active_plan: multi_lineup_planner.WeeklyPlan | None = None
        self.salary_source: str = "roster"
        self.live_states: dict[str, live_lineup_manager.PlayerLiveState] = {}
        self.simulated_time: datetime | None = None
        self.last_reallocation_plan: live_lineup_manager.CoordinatedReallocationPlan | None = None
        self.dashboard_schedule: dict[str, Any] | None = None

        self._auto_init()

    def _auto_init(self) -> None:
        # Load local roster if present
        default_rosters = [
            self.root_dir / "data" / "raw" / "My_roster.csv",
        ]
        if not self.isolated:
            default_rosters += [
            self.root_dir.parent / "data" / "raw" / "My_roster.csv",
            self.root_dir.parent / "GameBlazers" / "rosters" / "GB_roster (2).csv",
            ]
        for rpath in default_rosters:
            if rpath.exists():
                try:
                    self.roster_cards = multi_lineup_planner.parse_roster_cards(rpath)
                    break
                except Exception:
                    pass

        # If no snapshot yet, auto-seed from repository projections or run update check
        if self.snapshot is None:
            # Check for backend cheatsheets in GB/Projections
            proj_candidates = sorted(
                ([] if self.isolated else list((self.root_dir.parent / "Projections").glob("DFF_NFL_cheatsheet_*.csv")))
                + list((self.root_dir / "data" / "projections").glob("*.csv")),
                reverse=True,
            )
            for ppath in proj_candidates:
                if ppath.exists():
                    try:
                        with ppath.open("r", encoding="utf-8-sig") as f:
                            snap = dff_client.parse_projections_csv_stream(
                                f, source_name=f"backend_{ppath.name}", user_overrides=self.overrides
                            )
                            self.snapshot_store.save_snapshot(snap)
                            self.snapshot = snap
                            break
                    except Exception:
                        pass

            if self.snapshot is None and not self.isolated:
                try:
                    planner_update_check.run_weekly_update_check(store_dir=str(self.root_dir / "data" / "snapshots"))
                    self.snapshot = self.snapshot_store.load_latest()
                except Exception:
                    pass
        # Auto-load Thursday/live scores if present
        self.load_thursday_scores()
        self.recompute_planner_cards()

    def load_thursday_scores(self) -> None:
        data_dir = self.root_dir / "data"
        scores_candidates = sorted(
            list(data_dir.glob("week*_thursday_scores.csv")),
            key=lambda p: p.name,
            reverse=True,
        )
        if not scores_candidates and (data_dir / "thursday_scores.csv").exists():
            scores_candidates = [data_dir / "thursday_scores.csv"]
        if not self.isolated:
            opt_data = self.root_dir.parent / "lineup_optimizer" / "data"
            scores_candidates.extend(
                sorted(
                    list(opt_data.glob("week*_thursday_scores.csv")),
                    key=lambda p: p.name,
                    reverse=True,
                )
            )
        for sc_path in scores_candidates:
            if sc_path.exists():
                try:
                    with sc_path.open("r", encoding="utf-8-sig") as f:
                        states = live_lineup_manager.parse_live_scores_csv(f)
                        for s in states:
                            self.live_states[s.athlete_key] = s
                    break
                except Exception:
                    pass

    def recompute_planner_cards(self) -> None:
        self.salary_source = "roster"
        if not self.roster_cards:
            self.planner_cards = []
            return
        if not self.snapshot:
            self.planner_cards = multi_lineup_planner.join_cards_with_projections(self.roster_cards, [])
        else:
            self.planner_cards = multi_lineup_planner.join_cards_with_projections(
                self.roster_cards,
                self.snapshot.players,
                salary_source=self.salary_source,
                overrides=self.overrides,
            )
        team_schedule = schedule_manager.build_team_schedule_map(target_time=self.simulated_time)
        for c in self.planner_cards:
            norm_team = normalize_team(c.card.team)
            sched_info = team_schedule.get(norm_team, {})
            if not getattr(c, "kickoff_time", "") and sched_info.get("kickoff_time"):
                c.kickoff_time = sched_info["kickoff_time"]
            if not getattr(c, "game_date", "") and sched_info.get("game_date"):
                c.game_date = sched_info["game_date"]
            if not getattr(c, "opp", "") and sched_info.get("opp"):
                c.opp = sched_info["opp"]

            akey = c.card.athlete_key
            if akey not in self.live_states:
                self.live_states[akey] = live_lineup_manager.PlayerLiveState(
                    athlete_key=akey,
                    player_name=c.card.player_name,
                    team=c.card.team,
                    position=c.card.position,
                    game_status=live_lineup_manager.GAME_STATUS_UPCOMING,
                    remaining_projection=c.raw_projection,
                    remaining_stdev=round(0.35 * c.raw_projection, 2),
                    profile_type="standard",
                    opp=getattr(c, "opp", "") or "",
                    game_date=getattr(c, "game_date", "") or "",
                    kickoff_time=getattr(c, "kickoff_time", "") or "",
                )
            else:
                existing_player = self.live_states[akey]
                if not existing_player.opp and getattr(c, "opp", ""):
                    existing_player.opp = getattr(c, "opp", "")
                if not existing_player.game_date and getattr(c, "game_date", ""):
                    existing_player.game_date = getattr(c, "game_date", "")
                if not existing_player.kickoff_time and getattr(c, "kickoff_time", ""):
                    existing_player.kickoff_time = getattr(c, "kickoff_time", "")

            player = self.live_states[akey]
            if player.game_status in (live_lineup_manager.GAME_STATUS_IN_PROGRESS, live_lineup_manager.GAME_STATUS_FINAL):
                if player.game_status == live_lineup_manager.GAME_STATUS_FINAL:
                    raw_pts = player.final_points
                else:
                    raw_pts = player.live_points + (player.remaining_projection if player.remaining_is_estimate else 0.0)
                c.raw_projection = round(raw_pts, 2)
                c.adjusted_projection = round(raw_pts * c.card.multiplier, 3)
                c.is_matched = True

                is_active = c.card.status.casefold() in ("active", "listed")
                valid_mult = c.card.multiplier > 0
                valid_salary = c.weekly_salary > 0
                valid_pos = c.card.position in multi_lineup_planner.POSITIONS
                not_excluded = akey not in self.excluded_athlete_keys

                if not is_active:
                    c.is_eligible = False
                    c.ineligibility_reason = f"Card status is '{c.card.status}' (Active or Listed required)"
                elif c.card.expires:
                    exp_clean = c.card.expires.strip().split("T")[0]
                    today_clean = datetime.now(timezone.utc).strftime("%Y-%m-%d")
                    if exp_clean and exp_clean != "N/A" and exp_clean < today_clean:
                        c.is_eligible = False
                        c.ineligibility_reason = f"Card expired on {c.card.expires}"
                    elif not valid_mult:
                        c.is_eligible = False
                        c.ineligibility_reason = f"Non-positive multiplier: {c.card.multiplier}"
                elif not valid_mult:
                    c.is_eligible = False
                    c.ineligibility_reason = f"Non-positive multiplier: {c.card.multiplier}"
                elif not valid_salary:
                    c.is_eligible = False
                    c.ineligibility_reason = "Roster salary is zero or negative"
                elif not valid_pos:
                    c.is_eligible = False
                    c.ineligibility_reason = f"Unsupported position: {c.card.position}"
                elif not not_excluded:
                    c.is_eligible = False
                    c.ineligibility_reason = "Excluded by you"
                else:
                    c.is_eligible = True
                    c.ineligibility_reason = None

                if player.opp and not getattr(c, "opp", None):
                    c.opp = player.opp
                if player.game_date and not getattr(c, "game_date", None):
                    c.game_date = player.game_date
                if player.kickoff_time and not getattr(c, "kickoff_time", None):
                    c.kickoff_time = player.kickoff_time
            else:
                if akey in self.excluded_athlete_keys:
                    c.is_eligible = False
                    c.ineligibility_reason = "Excluded by you"
                if not player.is_locked(self.simulated_time) and not player.remaining_is_estimate:
                    player.remaining_projection = c.raw_projection
                    player.remaining_stdev = round(0.35 * c.raw_projection, 2)
        self.last_reallocation_plan = None
        self.sync_active_plan()

    def import_dashboard_schedule(self, season: int, week: int) -> dict[str, Any]:
        # ponytail: explicit local import per server session; durable weekly state belongs with plan persistence.
        default_path = self.root_dir.parent.parent / "NFL_Main" / "nfldashboard" / "props-board.json"
        path = Path(os.environ.get("NFL_DASHBOARD_BOARD_PATH", str(default_path)))
        board = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(board, dict) or str(board.get("season")) != str(season):
            raise ValueError("The dashboard data does not match the selected season.")
        games = board.get("games")
        if not isinstance(games, list) or any(not isinstance(g, dict) for g in games):
            raise ValueError("The dashboard games data is malformed.")
        selected = [g for g in games if g.get("week") == week]
        if not selected:
            raise ValueError("No dashboard games found for this week.")
        kickoffs = {}
        opponents = {}
        for game in selected:
            kickoff = datetime.fromisoformat(str(game.get("kickoff", "")).replace("Z", "+00:00"))
            if kickoff.tzinfo is None:
                raise ValueError("Dashboard kickoff times must include a timezone.")
            away_raw = game.get("away_team", "")
            home_raw = game.get("home_team", "")
            away = normalize_team(away_raw)
            home = normalize_team(home_raw)
            if not away or not home:
                raise ValueError("Dashboard games contain a missing or duplicate team.")
            if away in kickoffs or home in kickoffs:
                raise ValueError("Dashboard games contain a missing or duplicate team.")
            ko_iso = kickoff.astimezone(timezone.utc).isoformat()
            kickoffs[away] = ko_iso
            kickoffs[home] = ko_iso
            opponents[away] = f"@{home}"
            opponents[home] = f"vs {away}"
        pending = []
        matched = 0
        for player in self.live_states.values():
            norm_team = normalize_team(player.team)
            kickoff = kickoffs.get(norm_team)
            if not kickoff:
                continue
            matched += 1
            if norm_team in opponents:
                player.opp = opponents[norm_team]
            if player.kickoff_time:
                if datetime.fromisoformat(player.kickoff_time.replace("Z", "+00:00")) != datetime.fromisoformat(kickoff):
                    raise ValueError(f"Existing kickoff conflicts for {player.player_name}. No times were changed.")
            else:
                pending.append((player, kickoff))
        if not matched:
            raise ValueError("No roster players match this week's dashboard games.")
        # Validate the entire import before touching player state or swap recommendations.
        for player, kickoff in pending:
            player.kickoff_time = kickoff
        for c in self.planner_cards:
            norm_t = normalize_team(c.card.team)
            if norm_t in kickoffs:
                c.kickoff_time = kickoffs[norm_t]
            if norm_t in opponents:
                c.opp = opponents[norm_t]
        if self.active_plan:
            for l in self.active_plan.lineups:
                for s in l.slots:
                    if s.card:
                        norm_t = normalize_team(s.card.card.team)
                        if norm_t in kickoffs:
                            s.card.kickoff_time = kickoffs[norm_t]
                        if norm_t in opponents:
                            s.card.opp = opponents[norm_t]
        self.last_reallocation_plan = None
        self.dashboard_schedule = {
            "season": season, "week": week, "games": len(selected),
            "source": "NFL dashboard props-board.json games",
            "generated_at": board.get("generated_at"),
            "matched_players": matched, "updated_players": len(pending),
            "unmatched_players": len(self.live_states) - matched,
        }
        return self.dashboard_schedule

    def get_missing_inputs_report(self) -> dict[str, Any]:
        missing_kickoffs = [
            p.player_name for p in self.live_states.values()
            if not p.kickoff_time and p.game_status == live_lineup_manager.GAME_STATUS_UPCOMING
        ]
        unverified_scores = []
        if self.active_plan:
            for l in self.active_plan.lineups:
                for s in l.slots:
                    if s.card:
                        p_state = self.live_states.get(s.card.card.athlete_key)
                        if p_state and p_state.game_status in (live_lineup_manager.GAME_STATUS_IN_PROGRESS, live_lineup_manager.GAME_STATUS_FINAL):
                            if p_state.live_points == 0.0 and p_state.final_points == 0.0:
                                unverified_scores.append(f"{s.card.card.player_name} in {l.lineup_id}")
        return {
            "missing_kickoff_times_count": len(missing_kickoffs),
            "missing_kickoff_samples": missing_kickoffs[:5],
            "unverified_scores_count": len(unverified_scores),
            "unverified_score_samples": unverified_scores[:5],
            "thresholds_notice": "Assumed Historical Bayesian Fitted Means (2024 weeks 8-13 sample), not live dynamic contest cutoffs.",
            "distributions_notice": "Player distributions are modeled estimates based on position variance and specified outcome profiles.",
        }

    def sync_active_plan(self) -> None:
        if not self.active_plan:
            return
        self.active_plan.salary_source = "roster"
        self.active_plan.roster_cards = list(self.planner_cards)
        self.active_plan.projection_overrides = dict(self.overrides)
        self.active_plan.excluded_athlete_keys = sorted(self.excluded_athlete_keys)

        card_pool = {c.card.card_id: c for c in self.planner_cards}
        claimed_ids: set[str] = set()
        lineup_conflicts: dict[str, list[str]] = {}

        # Prioritize locked lineups and locked slots when resolving inventory
        all_slots: list[tuple[int, Any, Any, Any]] = []
        for l in self.active_plan.lineups:
            for s in l.slots:
                if s.card:
                    prio = 0
                    if l.is_locked:
                        prio -= 2
                    if s.is_locked:
                        prio -= 1
                    all_slots.append((prio, l, s, s.card))

        all_slots.sort(key=lambda x: x[0])

        for _, l, s, prev_pc in all_slots:
            matched_c = resolve_card_for_slot(prev_pc, card_pool, claimed_ids)
            if matched_c is not None:
                player = self.live_states.get(matched_c.card.athlete_key)
                # A remaining-games CSV may omit started players. Keep only their
                # existing placements usable; the unassigned inventory stays unchanged.
                if (player and player.is_locked(self.simulated_time)
                        and matched_c.card.status.casefold() in ("active", "listed")
                        and matched_c.card.multiplier > 0 and matched_c.weekly_salary > 0
                        and matched_c.card.position in multi_lineup_planner.POSITIONS
                        and (matched_c.is_eligible or matched_c.ineligibility_reason in (
                            "No weekly projection found for athlete", "Projection is zero or negative"))):
                    matched_c = replace(
                        matched_c, raw_projection=player.effective_mean(1.0),
                        adjusted_projection=player.effective_mean(matched_c.card.multiplier),
                        is_eligible=True, ineligibility_reason=None,
                    )
                s.card = matched_c
            else:
                prev_c = prev_pc.card if hasattr(prev_pc, "card") else prev_pc
                pname = getattr(prev_c, "player_name", "Unknown")
                pmult = getattr(prev_c, "multiplier", 1.0)
                s.card = None
                conf_msg = f"Conflict: Slot '{s.slot_name}' player '{pname}' ({pmult}x) is no longer available in current roster inventory."
                lineup_conflicts.setdefault(l.lineup_id, []).append(conf_msg)

        # Track usage and revalidate all lineups
        card_usage: dict[str, list[str]] = {}
        for l in self.active_plan.lineups:
            for s in l.slots:
                if s.card is not None:
                    card_usage.setdefault(s.card.card.card_id, []).append(l.lineup_id)

        for l in self.active_plan.lineups:
            contest = self.contests.get(l.contest_name) or self.active_plan.contests.get(l.contest_name)
            if contest:
                multi_lineup_planner.recalculate_lineup(l, contest, card_usage)
            if l.lineup_id in lineup_conflicts:
                for conf in lineup_conflicts[l.lineup_id]:
                    if conf not in l.validation_errors:
                        l.validation_errors.append(conf)
                l.is_valid = False

        self.active_plan.total_weekly_projection = round(sum(l.total_projection for l in self.active_plan.lineups), 2)
        self.active_plan.total_weekly_estimated_payout = round(
            sum(l.estimated_payout for l in self.active_plan.lineups if l.is_valid), 2
        )


def resolve_card_for_slot(
    card_data: Any,
    card_pool: dict[str, multi_lineup_planner.PlannerCard],
    claimed_ids: set[str],
) -> multi_lineup_planner.PlannerCard | None:
    """Resolve a slot's card from the current planner card pool.

    Supports exact card_id match, or identity matching on athlete_key,
    position, multiplier, and collection group when roster row numbers shift.
    Claims matched card IDs so duplicate copies are allocated cleanly.
    """
    if not card_data:
        return None
    card = card_data.card if hasattr(card_data, "card") else card_data
    cid = card.get("card_id") if isinstance(card, dict) else getattr(card, "card_id", None)
    akey = card.get("athlete_key") if isinstance(card, dict) else getattr(card, "athlete_key", "")
    pos = card.get("position") if isinstance(card, dict) else getattr(card, "position", "")
    try:
        mult = float(card.get("multiplier", 1.0) if isinstance(card, dict) else getattr(card, "multiplier", 1.0))
    except (TypeError, ValueError):
        mult = 1.0
    col_grp = card.get("collection_group") if isinstance(card, dict) else getattr(card, "collection_group", None)

    # 1. Exact card_id match
    if cid and cid in card_pool and cid not in claimed_ids:
        claimed_ids.add(cid)
        return card_pool[cid]

    # 2. Match by athlete_key, position, multiplier, and collection group
    for cand_id, cand in card_pool.items():
        if cand_id in claimed_ids:
            continue
        if (
            cand.card.athlete_key == akey
            and cand.card.position == pos
            and abs(cand.card.multiplier - mult) < 1e-4
            and getattr(cand.card, "collection_group", "Core") == (col_grp or "Core")
        ):
            claimed_ids.add(cand_id)
            return cand

    # 3. Match by athlete_key, position, and multiplier (flexible collection group)
    for cand_id, cand in card_pool.items():
        if cand_id in claimed_ids:
            continue
        if (
            cand.card.athlete_key == akey
            and cand.card.position == pos
            and abs(cand.card.multiplier - mult) < 1e-4
        ):
            claimed_ids.add(cand_id)
            return cand

    return None


STATE = PlannerAppState(root_dir=os.environ.get("PLANNER_INSTANCE_ROOT"))


def allowed_file(filename: str) -> bool:
    return "." in filename and filename.rsplit(".", 1)[1].casefold() in ALLOWED_EXTENSIONS


def _read_upload(upload: Any, label: str) -> tuple[io.StringIO | None, str | None]:
    filename = str(upload.filename or "")
    if not filename:
        return None, f"Choose a {label} CSV file."
    if not allowed_file(filename):
        return None, f"{label.capitalize()} must be a CSV file."
    try:
        content = upload.stream.read().decode("utf-8-sig")
    except UnicodeDecodeError:
        return None, f"{label.capitalize()} could not be decoded as UTF-8. Save it as a UTF-8 CSV and try again."
    return io.StringIO(content), None


def _diagnostics(result: OptimizationResult) -> tuple[list[dict[str, Any]], list[Any]]:
    counts = Counter(diagnostic.reason for diagnostic in result.diagnostics if diagnostic.reason != "matched")
    grouped = [
        {"label": DIAGNOSTIC_LABELS.get(reason, reason.replace("_", " ").capitalize()), "count": counts[reason]}
        for reason in DIAGNOSTIC_LABELS
        if counts.get(reason)
    ]
    details = [diagnostic for diagnostic in result.diagnostics if diagnostic.reason != "matched"]
    return grouped, details


def _render(
    *,
    selected_contest: str = "Spark",
    errors: list[str] | None = None,
    result: OptimizationResult | None = None,
    status_code: int = 200,
):
    diagnostic_counts: list[dict[str, Any]] = []
    diagnostic_details: list[Any] = []
    if result is not None:
        diagnostic_counts, diagnostic_details = _diagnostics(result)
    return render_template(
        "index.html",
        contests=CONTESTS,
        selected_contest=selected_contest,
        errors=errors or [],
        result=result,
        diagnostic_counts=diagnostic_counts,
        diagnostic_details=diagnostic_details,
        infeasible_messages=INFEASIBLE_MESSAGES,
        planner_contests=STATE.contests,
        active_plan=STATE.active_plan,
        snapshot=STATE.snapshot,
        roster_card_count=len(STATE.roster_cards),
        planner_cards=STATE.planner_cards,
        private_preview=STATE.isolated,
    ), status_code


@app.template_filter("currency")
def currency(value: Any) -> str:
    try:
        return f"${float(value):,.0f}"
    except (ValueError, TypeError):
        return "$0"


@app.template_filter("decimal")
def decimal(value: Any) -> str:
    try:
        return f"{float(value):,.2f}"
    except (ValueError, TypeError):
        return "0.00"


@app.template_filter("multiplier")
def multiplier(value: Any) -> str:
    try:
        return f"{float(value):,.2f}x"
    except (ValueError, TypeError):
        return "1.00x"


# ---------------------------------------------------------------------------
# Existing Single-Lineup Optimizer Routes (Preserved for compatibility)
# ---------------------------------------------------------------------------


@app.get("/")
def home():
    return _render()


@app.get("/planner")
def planner_redirect():
    return _render()


@app.post("/optimize")
def optimize():
    selected_contest = request.form.get("contest", "Spark")
    validation_errors: list[str] = []

    if selected_contest not in CONTESTS:
        validation_errors.append("Select a valid contest before optimizing.")

    roster_upload = request.files.get("roster_file")
    projection_upload = request.files.get("projection_file")
    if roster_upload is None:
        validation_errors.append("Choose a roster CSV file.")
    if projection_upload is None:
        validation_errors.append("Choose a projections CSV file.")

    roster_source = projection_source = None
    if roster_upload is not None:
        roster_source, error = _read_upload(roster_upload, "roster")
        if error:
            validation_errors.append(error)
    if projection_upload is not None:
        projection_source, error = _read_upload(projection_upload, "projections")
        if error:
            validation_errors.append(error)

    if validation_errors:
        return _render(selected_contest=selected_contest, errors=validation_errors, status_code=400)

    try:
        result = optimize_lineup(roster_source, projection_source, selected_contest)
    except Exception:
        return _render(
            selected_contest=selected_contest,
            errors=["The uploaded files could not be processed. Check the CSV contents and try again."],
            status_code=400,
        )
    return _render(selected_contest=selected_contest, result=result)


# ---------------------------------------------------------------------------
# Weekly Planner REST API Routes
# ---------------------------------------------------------------------------


@app.get("/api/planner/state")
def api_planner_state():
    matched = [c for c in STATE.planner_cards if c.is_matched]
    eligible = [c for c in STATE.planner_cards if c.is_eligible]
    return jsonify({
        "status": "success",
        "snapshot": STATE.snapshot.to_dict() if STATE.snapshot else None,
        "roster_count": len(STATE.roster_cards),
        "matched_count": len(matched),
        "eligible_count": len(eligible),
        "salary_source": STATE.salary_source,
        "contests": {k: v.to_dict() for k, v in STATE.contests.items()},
        "active_plan": STATE.active_plan.to_dict() if STATE.active_plan else None,
        "overrides_count": len(STATE.overrides),
        "overrides": STATE.overrides,
        "excluded_athlete_keys": sorted(STATE.excluded_athlete_keys),
        "cards": [c.to_dict() for c in STATE.planner_cards],
    })


@app.get("/api/dff/slates")
def api_dff_slates():
    try:
        slates = dff_client.fetch_dff_slates()
        return jsonify({"status": "success", "slates": [s.to_dict() for s in slates]})
    except Exception as exc:
        return jsonify({"status": "error", "message": f"Could not fetch slates: {exc}"}), 500


@app.post("/api/dff/refresh")
def api_dff_refresh():
    data = request.get_json(silent=True) or {}
    slate_id = data.get("slate_id")
    try:
        slates = dff_client.fetch_dff_slates()
        selected = None
        if slate_id:
            selected = next((s for s in slates if s.slate_id == slate_id), None)
        if not selected:
            multi_game = [s for s in slates if s.game_count > 1 and s.showdown_flag == 0]
            selected = multi_game[0] if multi_game else slates[0]

        raw_players = dff_client.fetch_dff_player_details(selected.slate_id)
        if len(raw_players) < 20:
            return jsonify({"status": "error", "message": "Extracted player pool too small"}), 400

        snap = dff_client.parse_dff_players(raw_players, selected.slate_id, selected, STATE.overrides)
        if STATE.snapshot and STATE.snapshot.players:
            new_keys = {p.athlete_key for p in snap.players}
            for prev_p in STATE.snapshot.players:
                if prev_p.athlete_key not in new_keys:
                    p_live = STATE.live_states.get(prev_p.athlete_key)
                    if p_live and p_live.game_status in (live_lineup_manager.GAME_STATUS_IN_PROGRESS, live_lineup_manager.GAME_STATUS_FINAL):
                        snap.players.append(prev_p)
        STATE.snapshot_store.save_snapshot(snap)
        STATE.snapshot = snap
        STATE.recompute_planner_cards()

        # Recalculate plan if active
        if STATE.active_plan:
            # Re-solve or update cards in plan
            card_map = {c.card.card_id: c for c in STATE.planner_cards}
            for l in STATE.active_plan.lineups:
                for s in l.slots:
                    if s.card and s.card.card.card_id in card_map:
                        s.card = card_map[s.card.card.card_id]
                multi_lineup_planner.recalculate_lineup(l, STATE.contests[l.contest_name])
            STATE.active_plan.total_weekly_projection = round(sum(l.total_projection for l in STATE.active_plan.lineups), 2)
            STATE.active_plan.total_weekly_estimated_payout = round(sum(l.estimated_payout for l in STATE.active_plan.lineups), 2)

        return jsonify({
            "status": "success",
            "message": f"Refreshed slate {selected.slate_id} ({snap.offensive_player_count} players)",
            "snapshot": snap.to_dict(),
            "eligible_cards": len([c for c in STATE.planner_cards if c.is_eligible]),
        })
    except Exception as exc:
        return jsonify({"status": "error", "message": str(exc)}), 500


@app.post("/api/projections/upload")
def api_projections_upload():
    upload = request.files.get("projection_file")
    if not upload:
        return jsonify({"status": "error", "message": "No projections file provided"}), 400
    try:
        stream, err = _read_upload(upload, "projections")
        if err:
            return jsonify({"status": "error", "message": err}), 400
        snap = dff_client.parse_projections_csv_stream(stream, source_name=upload.filename or "uploaded_csv", user_overrides=STATE.overrides)
        if STATE.snapshot and STATE.snapshot.players:
            new_keys = {p.athlete_key for p in snap.players}
            for prev_p in STATE.snapshot.players:
                if prev_p.athlete_key not in new_keys:
                    p_live = STATE.live_states.get(prev_p.athlete_key)
                    if p_live and p_live.game_status in (live_lineup_manager.GAME_STATUS_IN_PROGRESS, live_lineup_manager.GAME_STATUS_FINAL):
                        snap.players.append(prev_p)
        STATE.snapshot_store.save_snapshot(snap)
        STATE.snapshot = snap
        STATE.recompute_planner_cards()
        return jsonify({
            "status": "success",
            "message": f"Imported {snap.offensive_player_count} projections from CSV",
            "snapshot": snap.to_dict(),
        })
    except Exception as exc:
        return jsonify({"status": "error", "message": str(exc)}), 400


@app.post("/api/roster/upload")
def api_roster_upload():
    upload = request.files.get("roster_file")
    if not upload:
        return jsonify({"status": "error", "message": "No roster file provided"}), 400
    try:
        stream, err = _read_upload(upload, "roster")
        if err:
            return jsonify({"status": "error", "message": err}), 400
        cards = multi_lineup_planner.parse_roster_cards(stream)
        if not cards:
            raise ValueError("The roster contains no cards.")
        roster_path = STATE.root_dir / "data" / "raw" / "My_roster.csv"
        roster_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = roster_path.with_suffix(".tmp")
        temporary.write_text(stream.getvalue(), encoding="utf-8")
        temporary.replace(roster_path)
        STATE.roster_cards = cards
        STATE.recompute_planner_cards()
        return jsonify({
            "status": "success",
            "message": f"Loaded {len(cards)} roster cards",
            "total_cards": len(cards),
            "eligible_cards": len([c for c in STATE.planner_cards if c.is_eligible]),
        })
    except Exception as exc:
        return jsonify({"status": "error", "message": str(exc)}), 400


@app.post("/api/overrides/update")
def api_overrides_update():
    data = request.get_json(silent=True) or {}
    athlete_key = data.get("athlete_key")
    override_value = data.get("override_value")
    reset = data.get("reset", False)

    if not athlete_key:
        return jsonify({"status": "error", "message": "Missing athlete_key"}), 400

    if reset:
        STATE.overrides.pop(athlete_key, None)
    else:
        try:
            val = float(override_value)
            if val < 0:
                return jsonify({"status": "error", "message": "Override projection must be positive"}), 400
            STATE.overrides[athlete_key] = val
        except (ValueError, TypeError):
            return jsonify({"status": "error", "message": "Invalid override number"}), 400

    STATE.snapshot_store.save_overrides(STATE.overrides)
    STATE.recompute_planner_cards()

    # Update active plan if present
    if STATE.active_plan:
        card_map = {c.card.card_id: c for c in STATE.planner_cards}
        for l in STATE.active_plan.lineups:
            for s in l.slots:
                if s.card and s.card.card.card_id in card_map:
                    s.card = card_map[s.card.card.card_id]
            multi_lineup_planner.recalculate_lineup(l, STATE.contests[l.contest_name])
        STATE.active_plan.total_weekly_projection = round(sum(l.total_projection for l in STATE.active_plan.lineups), 2)
        STATE.active_plan.total_weekly_estimated_payout = round(sum(l.estimated_payout for l in STATE.active_plan.lineups), 2)

    return jsonify({
        "status": "success",
        "overrides": STATE.overrides,
        "active_plan": STATE.active_plan.to_dict() if STATE.active_plan else None,
    })


@app.post("/api/planner/generate")
def api_planner_generate():
    data = request.get_json(silent=True) or {}
    if not isinstance(data, dict):
        return jsonify({"status": "error", "message": "Generation settings must be an object."}), 400
    target_count = data.get("target_count")
    if target_count is not None and (isinstance(target_count, bool) or not isinstance(target_count, int) or target_count < 1):
        return jsonify({"status": "error", "message": "Lineup limit must be a positive integer."}), 400
    salary_source = data.get("salary_source", STATE.salary_source)
    contest_dist = data.get("contest_distribution")
    salary_bounds = data.get("salary_bounds", {})

    # Automatic generation preserves started placements. Manual edits can move them.
    game_locked_placements = _game_locked_placements(STATE.active_plan)

    if salary_source not in ("dff", "roster"):
        return jsonify(status="error", message="Card salaries must come from the roster export."), 400
    if contest_dist is not None:
        if not isinstance(contest_dist, dict) or not contest_dist or any(
            name not in STATE.contests or not STATE.contests[name].enabled or not STATE.contests[name].eligible or
            isinstance(count, bool) or not isinstance(count, int) or count < 0 or count > STATE.contests[name].default_entry_limit
            for name, count in contest_dist.items()
        ) or not any(contest_dist.values()):
            return jsonify(status="error", message="Select at least one contest and use its allowed entry limit."), 400
        if contest_dist.get("Wildfire") and contest_dist.get("Volcano"):
            return jsonify(status="error", message="Wildfire and Volcano refer to the same contest. Select one."), 400
        started_lineups = {lineup_id for lineup_id, _ in game_locked_placements}
        if STATE.active_plan and any(
            (l.is_locked or l.lineup_id in started_lineups or any(s.is_locked for s in l.slots))
            and not contest_dist.get(l.contest_name)
            for l in STATE.active_plan.lineups
        ):
            return jsonify(status="error", message="A lineup with locked or started players belongs to an unselected contest. Select that contest or unlock the placements first."), 400

    # Older pages may still send "dff". Owned cards always use the exported salary.
    STATE.salary_source = "roster"

    # Apply any custom salary bounds from user controls
    if isinstance(salary_bounds, dict):
        for cname, bounds in salary_bounds.items():
            if cname in STATE.contests and isinstance(bounds, dict):
                if "min" in bounds:
                    try:
                        STATE.contests[cname].minimum_salary = int(bounds["min"])
                    except (ValueError, TypeError):
                        pass
                if "max" in bounds:
                    try:
                        STATE.contests[cname].maximum_salary = int(bounds["max"])
                    except (ValueError, TypeError):
                        pass

    STATE.recompute_planner_cards()

    if not STATE.planner_cards or STATE.snapshot is None:
        return jsonify({"status": "error", "message": "Roster or weekly projections are missing."}), 400

    quality_threshold_raw = data.get("quality_threshold", 0.0)
    try:
        quality_threshold = float(quality_threshold_raw)
        if quality_threshold < 0.0 or quality_threshold > 1.0:
            quality_threshold = 0.0
    except (ValueError, TypeError):
        quality_threshold = 0.0

    try:
        current_slate_id = STATE.snapshot.slate_id if STATE.snapshot else "unknown"
        plan = multi_lineup_planner.solve_multi_lineup_allocation(
            cards=STATE.planner_cards,
            contests=STATE.contests,
            target_count=target_count,
            contest_distribution=contest_dist,
            existing_plan=_plan_with_game_locks(STATE.active_plan, game_locked_placements),
            slate_id=current_slate_id,
            salary_source=STATE.salary_source,
            quality_threshold=quality_threshold,
            excluded_athlete_keys=STATE.excluded_athlete_keys,
        )
        STATE.active_plan = plan
        plan.inventory_summary["contest_distribution"] = contest_dist
        count = len(plan.lineups)
        pct_label = f"{int(quality_threshold * 100)}%"
        if count > 0:
            avg_score = round(sum(l.total_projection for l in plan.lineups) / count, 1)
            message = f"Generated {count} competitive lineup{'s' if count != 1 else ''} meeting the {pct_label} quality requirement (averaging {avg_score} projected points)."
        else:
            message = f"No legal lineups met the {pct_label} quality requirement with the current eligible card pool."
            if plan.preserved_existing_lineups:
                message += f" Preserved {len(plan.preserved_existing_lineups)} previous lineup(s) separately."

        stopping_reasons = plan.inventory_summary.get("stopping_reasons", [])
        if stopping_reasons and (target_count is None or count < target_count):
            reasons_str = " ".join(stopping_reasons)
            message += f" Why generation stopped: {reasons_str}"

        if plan.inventory_summary.get("solver_status") == "time_limit_feasible":
            if plan.inventory_summary.get("lineup_count_proven_maximum"):
                message += " Maximum qualifying lineup count is proven for this inventory."
            else:
                message += " Solver reached its time limit; qualifying count is the best found."
        if quality_threshold == 0:
            unused = len({c.card.card_id for c in STATE.planner_cards if c.is_eligible} -
                         {s.card.card.card_id for l in plan.lineups for s in l.slots if s.card})
            message = f"Built {count} lineups. {unused} eligible cards remain available for edits."
            if not plan.inventory_summary.get("lineup_count_proven_maximum"):
                message += " Search time limit reached. More lineups may fit after rearranging cards."
            plan.inventory_summary["stopping_reasons"] = [message]
        STATE.last_reallocation_plan = None
        return jsonify({
            "status": "success",
            "message": message,
            "plan": plan.to_dict(),
        })
    except ValueError as exc:
        return jsonify({"status": "error", "message": str(exc)}), 400
    except Exception as exc:
        return jsonify({"status": "error", "message": f"Optimization failed: {exc}"}), 500


@app.post("/api/planner/swap")
def api_planner_swap():
    data = request.get_json(silent=True) or {}
    lineup_id = data.get("lineup_id")
    slot_name = data.get("slot_name")
    new_card_id = data.get("new_card_id")

    if not STATE.active_plan:
        return jsonify({"status": "error", "message": "No active weekly plan"}), 400

    lineup = next((l for l in STATE.active_plan.lineups if l.lineup_id == lineup_id), None)
    slot = next((s for s in lineup.slots if s.slot_name == slot_name), None) if lineup else None
    if not slot:
        return jsonify(status="error", message="Lineup or slot not found."), 404
    if new_card_id:
        legal = {c["card_id"] for c in _swap_candidates(lineup, slot)}
        if new_card_id not in legal:
            return jsonify(status="error", message="That card is unavailable or would break this lineup's rules."), 400

    success, msg = multi_lineup_planner.swap_card_in_lineup(STATE.active_plan, lineup_id, slot_name, new_card_id)
    if not success:
        return jsonify({"status": "error", "message": msg}), 400

    lineup = next(l for l in STATE.active_plan.lineups if l.lineup_id == lineup_id)
    slot.is_locked = bool(new_card_id)
    STATE.last_reallocation_plan = None
    return jsonify({
        "status": "success",
        "message": msg,
        "lineup": lineup.to_dict(),
        "total_weekly_projection": STATE.active_plan.total_weekly_projection,
        "total_weekly_estimated_payout": STATE.active_plan.total_weekly_estimated_payout,
    })


def _swap_candidates(lineup, slot):
    """Offer cards that keep this lineup legal without requiring an unfinished draft to be complete."""
    used = {s.card.card.card_id: [l.lineup_id] for l in STATE.active_plan.lineups
            for s in l.slots if s.card}
    trial = copy.copy(lineup)
    trial.slots = [copy.copy(s) for s in lineup.slots]
    target = next(s for s in trial.slots if s.slot_name == slot.slot_name)
    # Other excluded players still need edits; they must not block replacing this slot.
    for other in trial.slots:
        if other is not target and other.card:
            other.card = copy.copy(other.card)
            other.card.is_eligible = True
    contest = STATE.contests[lineup.contest_name]
    baseline = set(multi_lineup_planner.blocking_lineup_errors(trial, contest, used))
    result = []
    for card in STATE.planner_cards:
        if not card.is_eligible:
            continue
        target.card = card
        new_errors = [e for e in multi_lineup_planner.blocking_lineup_errors(trial, contest, used) if e not in baseline]
        if not new_errors:
            item = card.to_dict()
            item["projection_delta"] = round(card.adjusted_projection - (slot.card.adjusted_projection if slot.card else 0), 2)
            item["remaining_salary"] = contest.maximum_salary - sum(s.card.weekly_salary for s in trial.slots if s.card)
            result.append(item)
    return sorted(result, key=lambda c: -c["adjusted_projection"])


def _game_locked_card(planner_card) -> bool:
    """True when this player's placement should be preserved by automatic generation."""
    if planner_card is None:
        return False
    live = STATE.live_states.get(planner_card.card.athlete_key)
    return bool(live and live.is_locked(STATE.simulated_time))


def _game_locked_placements(plan) -> set[tuple[str, str]]:
    """(lineup_id, slot_name) placements held by a player whose game has started or finished."""
    locked: set[tuple[str, str]] = set()
    if plan is None:
        return locked
    for lineup in plan.lineups:
        for slot in lineup.slots:
            if slot.card and _game_locked_card(slot.card):
                locked.add((lineup.lineup_id, slot.slot_name))
    return locked


def _plan_with_game_locks(plan, locked_placements: set[tuple[str, str]]):
    """Copy of the plan with game-locked placements marked locked, for the solver only."""
    if plan is None or not locked_placements:
        return plan
    substitute = copy.copy(plan)
    substitute.lineups = [
        multi_lineup_planner.PlannerLineup(
            lineup_id=lineup.lineup_id,
            contest_name=lineup.contest_name,
            slots=[
                multi_lineup_planner.LineupSlotAssignment(
                    slot_name=slot.slot_name,
                    slot_kind=slot.slot_kind,
                    card=slot.card,
                    is_locked=slot.is_locked or (lineup.lineup_id, slot.slot_name) in locked_placements,
                )
                for slot in lineup.slots
            ],
            is_locked=lineup.is_locked,
            contest_benchmark=lineup.contest_benchmark,
            benchmark_percentage=lineup.benchmark_percentage,
            unused_salary_flag=lineup.unused_salary_flag,
            unused_salary_note=lineup.unused_salary_note,
        )
        for lineup in plan.lineups
    ]
    return substitute


@app.get("/api/planner/candidates")
def api_planner_candidates():
    plan = STATE.active_plan
    lineup = next((l for l in plan.lineups if l.lineup_id == request.args.get("lineup_id")), None) if plan else None
    slot = next((s for s in lineup.slots if s.slot_name == request.args.get("slot_name")), None) if lineup else None
    if not slot:
        return jsonify(status="error", message="Lineup or slot not found."), 404
    return jsonify(status="success", cards=_swap_candidates(lineup, slot))


@app.post("/api/planner/exclude")
def api_planner_exclude():
    data = request.get_json(silent=True) or {}
    if not isinstance(data, dict):
        return jsonify(status="error", message="Expected player settings."), 400
    key, excluded = data.get("athlete_key"), data.get("excluded")
    if key not in {c.card.athlete_key for c in STATE.planner_cards} or not isinstance(excluded, bool):
        return jsonify(status="error", message="Choose a roster player and whether to exclude them."), 400
    keys = STATE.excluded_athlete_keys | {key} if excluded else STATE.excluded_athlete_keys - {key}
    temporary = STATE.exclusions_path.with_suffix(".tmp")
    temporary.write_text(json.dumps(sorted(keys)))
    temporary.replace(STATE.exclusions_path)
    STATE.excluded_athlete_keys = keys
    STATE.recompute_planner_cards()
    STATE.last_reallocation_plan = None
    return jsonify(status="success", message="Player excluded. Assigned copies are marked for replacement." if excluded else "Player restored to the pool.")


def _plan_for_manual_entry(data: dict[str, Any], plan):
    """Validate an add request and return (plan, entry_limit, error).

    When no plan exists this builds a manual WeeklyPlan. It never calls the solver:
    the first draft is just an unfilled entry. Existing plans are returned untouched
    on any rejection. Lineups can be added for any eligible contest up to that
    contest's entry limit.
    """
    contest_name = data.get("contest_name")
    if not isinstance(contest_name, str) or contest_name not in STATE.contests:
        return plan, None, "Choose a contest for the new entry."
    contest = STATE.contests[contest_name]
    if not (contest.enabled and contest.eligible):
        return plan, None, f"Contest '{contest_name}' is not open for entry."

    requested = data.get("contest_distribution")
    if requested is not None and not isinstance(requested, dict):
        return plan, None, "Selected contest entries must be an object of entry counts."
    selected: dict[str, int] = {}
    if isinstance(requested, dict):
        for name, count in requested.items():
            cfg = STATE.contests.get(name)
            if cfg is None or not (cfg.enabled and cfg.eligible):
                return plan, None, f"'{name}' is not an available contest."
            if isinstance(count, bool) or not isinstance(count, int) or count < 0:
                return plan, None, "Selected entry counts must be whole numbers."
            if cfg.default_entry_limit is not None and count > cfg.default_entry_limit:
                return plan, None, f"{name} allows at most {cfg.default_entry_limit} entries."
            if count > 0:
                selected[name] = count
        if contest_name not in requested:
            return plan, None, f"'{contest_name}' is not one of the selected contest tiers."
        if selected.get(contest_name, 0) < 1:
            return plan, None, f"Select at least one {contest_name} entry before adding a draft."
        if selected.get("Wildfire") and selected.get("Volcano"):
            return plan, None, "Wildfire and Volcano refer to the same contest. Select one."
        if plan is not None and any(
            count > selected.get(name, 0)
            for name, count in Counter(l.contest_name for l in plan.lineups).items()
        ):
            return plan, None, "Selected limits must include your existing entries. Remove entries before lowering their limits."

    entry_limit = data.get("entry_limit")
    if entry_limit is not None and selected and entry_limit != selected[contest_name]:
        return plan, None, "The entry limit must match the selected contest count."
    if entry_limit is None and contest_name in selected:
        entry_limit = selected[contest_name]

    if entry_limit is not None:
        if isinstance(entry_limit, bool) or not isinstance(entry_limit, int) or entry_limit < 1:
            return plan, None, f"Select at least one {contest_name} entry before adding a draft."
        if contest.default_entry_limit is not None and entry_limit > contest.default_entry_limit:
            return plan, None, f"{contest_name} allows at most {contest.default_entry_limit} entries."

    if plan is None:
        if not selected:
            selected[contest_name] = entry_limit if entry_limit is not None else 1
        now = datetime.now(timezone.utc).isoformat()
        plan = multi_lineup_planner.WeeklyPlan(
            plan_id=f"manual_{int(datetime.now(timezone.utc).timestamp())}",
            name="Manual plan",
            created_at=now,
            updated_at=now,
            slate_id=STATE.snapshot.slate_id if STATE.snapshot else "unknown",
            salary_source=STATE.salary_source,
            contests=STATE.contests,
            lineups=[],
            roster_cards=list(STATE.planner_cards),
            projection_overrides=dict(STATE.overrides),
            excluded_athlete_keys=sorted(STATE.excluded_athlete_keys),
            inventory_summary={"contest_distribution": selected},
        )
    return plan, entry_limit, None


@app.post("/api/planner/edit_lineup")
def api_planner_edit_lineup():
    data = request.get_json(silent=True) or {}
    if not isinstance(data, dict):
        return jsonify(status="error", message="Expected lineup settings."), 400
    action = data.get("action")
    if action not in ("remove", "rebuild", "clear", "add", "optimize_slots", "optimize_all_slots"):
        return jsonify(status="error", message="Choose a valid lineup action."), 400
    plan = STATE.active_plan
    if action == "add":
        # Build or extend a manual plan from the selected contest controls.
        # This never calls the solver; an empty draft is just an unfilled entry.
        plan, entry_limit, error = _plan_for_manual_entry(data, plan)
        if error is not None:
            return jsonify(status="error", message=error), 400
        success, msg, new_lineup = multi_lineup_planner.add_empty_lineup(
            plan, data.get("contest_name"), entry_limit=entry_limit
        )
        if not success:
            return jsonify(status="error", message=msg), 400
        if isinstance(data.get("contest_distribution"), dict):
            plan.inventory_summary["contest_distribution"] = {
                name: count for name, count in data["contest_distribution"].items() if count > 0
            }
        STATE.active_plan = plan
        STATE.sync_active_plan()
        STATE.last_reallocation_plan = None
        return jsonify(status="success", message=msg, lineup=new_lineup.to_dict(), plan=plan.to_dict())

    if not plan:
        return jsonify(status="error", message="No active weekly plan."), 400

    if action == "optimize_all_slots":
        for l in plan.lineups:
            if not l.is_locked:
                multi_lineup_planner.optimize_lineup_slots(l)
                if l.contest_name in STATE.contests:
                    multi_lineup_planner.recalculate_lineup(l, STATE.contests[l.contest_name])
        STATE.sync_active_plan()
        STATE.last_reallocation_plan = None
        return jsonify(status="success", message="Optimized all unlocked lineups for late swap.", plan=plan.to_dict())

    lineup = next((l for l in plan.lineups if l.lineup_id == data.get("lineup_id")), None)
    if not lineup:
        return jsonify(status="error", message="Choose a lineup to edit."), 400

    if action == "optimize_slots":
        if lineup.is_locked:
            return jsonify(status="error", message="Unlock this lineup first."), 400
        multi_lineup_planner.optimize_lineup_slots(lineup)
        if lineup.contest_name in STATE.contests:
            multi_lineup_planner.recalculate_lineup(lineup, STATE.contests[lineup.contest_name])
        STATE.sync_active_plan()
        STATE.last_reallocation_plan = None
        return jsonify(status="success", message=f"Optimized slots for late swap.", lineup=lineup.to_dict(), plan=plan.to_dict())

    if action == "clear":
        slot = next((s for s in lineup.slots if s.slot_name == data.get("slot_name")), None)
        if not slot:
            return jsonify(status="error", message="Choose a slot to clear."), 400
        success, msg = multi_lineup_planner.clear_slot(plan, lineup.lineup_id, slot.slot_name)
        if not success:
            return jsonify(status="error", message=msg), 400
        STATE.sync_active_plan()
        STATE.last_reallocation_plan = None
        return jsonify(status="success", message=msg, lineup=lineup.to_dict(), plan=plan.to_dict())

    if lineup.is_locked:
        return jsonify(status="error", message="Unlock this lineup first."), 400
    if action == "remove":
        plan.lineups.remove(lineup)
    else:
        STATE.sync_active_plan()
        used = {s.card.card.card_id for l in plan.lineups if l is not lineup for s in l.slots if s.card}
        # Game-locked and manually locked slots are both preserved during rebuild.
        fixed: dict[str, str] = {}
        for s in lineup.slots:
            if s.card:
                p_state = STATE.live_states.get(s.card.card.athlete_key)
                game_locked = p_state and p_state.is_locked(STATE.simulated_time)
                if s.is_locked or game_locked:
                    fixed[s.slot_name] = s.card.card.card_id
        # Automatic rebuilds preserve started players; manual edits can reconcile real entries.
        pool = [c for c in STATE.planner_cards if c.card.card_id not in used and
                not (STATE.live_states.get(c.card.athlete_key) and STATE.live_states[c.card.athlete_key].is_locked(STATE.simulated_time))]
        # Add fixed cards back into pool so the solver can keep them in their locked slots.
        fixed_ids = set(fixed.values())
        fixed_card_map = {s.card.card.card_id: s.card for s in lineup.slots
                          if s.card and s.card.card.card_id in fixed_ids}
        pool_ids = {c.card.card_id for c in pool}
        for fid, fc in fixed_card_map.items():
            if fid not in pool_ids:
                pool.append(fc)
        try:
            result = multi_lineup_planner.compute_contest_benchmark(pool, STATE.contests[lineup.contest_name], locked_cards=fixed)
        except ValueError as exc:
            return jsonify(status="error", message=str(exc)), 400
        if not result["cards"]:
            return jsonify(status="error", message="No complete replacement found. Your lineup is unchanged."), 400
        cards = {c.card.card_id: c for c in pool}
        for slot, cid in zip(lineup.slots, result["cards"]):
            slot.card = cards[cid]
        multi_lineup_planner.optimize_lineup_slots(lineup)
        multi_lineup_planner.recalculate_lineup(lineup, STATE.contests[lineup.contest_name])
    STATE.sync_active_plan()
    STATE.last_reallocation_plan = None
    return jsonify(status="success", plan=plan.to_dict())


@app.post("/api/planner/move")
def api_planner_move():
    """Move a card into an empty slot or exchange two occupied slots between entries."""
    data = request.get_json(silent=True) or {}
    if not isinstance(data, dict):
        return jsonify({"status": "error", "message": "Expected move settings."}), 400
    plan = STATE.active_plan
    if not plan:
        return jsonify({"status": "error", "message": "No active weekly plan."}), 400

    source_lineup_id = data.get("source_lineup_id")
    source_slot_name = data.get("source_slot_name")
    destination_lineup_id = data.get("destination_lineup_id")
    destination_slot_name = data.get("destination_slot_name")

    source_lineup = next((l for l in plan.lineups if l.lineup_id == source_lineup_id), None)
    destination_lineup = next((l for l in plan.lineups if l.lineup_id == destination_lineup_id), None)
    if not source_lineup or not destination_lineup:
        return jsonify({"status": "error", "message": "Source or destination lineup not found."}), 404
    source_slot = next((s for s in source_lineup.slots if s.slot_name == source_slot_name), None)
    destination_slot = next((s for s in destination_lineup.slots if s.slot_name == destination_slot_name), None)
    if not source_slot or not destination_slot:
        return jsonify({"status": "error", "message": "Source or destination slot not found."}), 404
    if source_slot.card is None:
        return jsonify({"status": "error", "message": "The source slot is empty."}), 400

    success, msg, affected = multi_lineup_planner.move_or_exchange_card(
        plan, source_lineup_id, source_slot_name, destination_lineup_id, destination_slot_name
    )
    if not success:
        return jsonify({"status": "error", "message": msg}), 400

    STATE.sync_active_plan()
    STATE.last_reallocation_plan = None
    refreshed = [l.to_dict() for l in plan.lineups if l.lineup_id in set(affected)]
    return jsonify({
        "status": "success",
        "message": msg,
        "lineups": refreshed,
        "affected_lineup_ids": affected,
        "plan": plan.to_dict(),
    })


@app.post("/api/planner/toggle_lock")
def api_planner_toggle_lock():
    data = request.get_json(silent=True) or {}
    lineup_id = data.get("lineup_id")
    slot_name = data.get("slot_name")

    if not STATE.active_plan:
        return jsonify({"status": "error", "message": "No active weekly plan"}), 400

    lineup = next((l for l in STATE.active_plan.lineups if l.lineup_id == lineup_id), None)
    if not lineup:
        return jsonify({"status": "error", "message": f"Lineup {lineup_id} not found"}), 404

    if slot_name:
        slot = next((s for s in lineup.slots if s.slot_name == slot_name), None)
        if not slot:
            return jsonify({"status": "error", "message": f"Slot {slot_name} not found"}), 404
        slot.is_locked = not slot.is_locked
    else:
        lineup.is_locked = not lineup.is_locked
        for s in lineup.slots:
            s.is_locked = lineup.is_locked

    return jsonify({"status": "success", "lineup": lineup.to_dict()})


@app.get("/api/planner/export")
def api_planner_export():
    if not STATE.active_plan:
        return "No active weekly plan to export", 400
    csv_text = multi_lineup_planner.export_plan_to_csv(STATE.active_plan)
    now_str = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    filename = f"gameblazers_weekly_lineups_{now_str}.csv"
    return Response(
        csv_text,
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


@app.post("/api/planner/save")
def api_planner_save():
    if not STATE.active_plan:
        return jsonify({"status": "error", "message": "No active plan to save"}), 400
    path = STATE.plans_dir / "latest_plan.json"
    temporary = path.with_suffix(".tmp")
    payload = STATE.active_plan.to_dict()
    payload["live_session"] = {
        "players": [asdict(player) for player in STATE.live_states.values()],
        "dashboard_schedule": STATE.dashboard_schedule,
        "simulated_time": STATE.simulated_time.isoformat() if STATE.simulated_time else None,
    }
    try:
        with temporary.open("w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, allow_nan=False)
        temporary.replace(path)
    except (OSError, ValueError):
        return jsonify({"status": "error", "message": "Could not save. The previous saved plan is unchanged."}), 500
    return jsonify({"status": "success", "message": "Plan, scores, and kickoff times saved."})


@app.post("/api/planner/load")
def api_planner_load():
    path = STATE.plans_dir / "latest_plan.json"
    if not path.exists():
        return jsonify({"status": "error", "message": "No saved plan file found"}), 404
    try:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)

        live_session = data.get("live_session")
        if live_session is not None:
            valid_fields = set(live_lineup_manager.PlayerLiveState.__dataclass_fields__.keys())
            restored_players = {
                player["athlete_key"]: live_lineup_manager.PlayerLiveState(
                    **{k: v for k, v in player.items() if k in valid_fields}
                )
                for player in live_session["players"]
            }
            saved_time = live_session.get("simulated_time")
            restored_time = datetime.fromisoformat(saved_time) if saved_time else None
            STATE.live_states.update(restored_players)
            if restored_time:
                STATE.simulated_time = restored_time

        # Reprice old plans from the current roster, never their saved DFF salaries.
        salary_migrated = data.get("salary_source") != "roster"
        STATE.salary_source = "roster"

        # Restore overrides if saved
        saved_overrides = data.get("projection_overrides")
        if isinstance(saved_overrides, dict):
            STATE.overrides = saved_overrides
            STATE.snapshot_store.save_overrides(STATE.overrides)

        # Restore contest settings if saved
        saved_contests = data.get("contests", {})
        for cname, c_data in saved_contests.items():
            if cname in STATE.contests and isinstance(c_data, dict):
                if "minimum_salary" in c_data:
                    STATE.contests[cname].minimum_salary = int(c_data["minimum_salary"])
                if "maximum_salary" in c_data:
                    STATE.contests[cname].maximum_salary = int(c_data["maximum_salary"])

        STATE.excluded_athlete_keys = set(data.get("excluded_athlete_keys", []))
        temporary = STATE.exclusions_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(sorted(STATE.excluded_athlete_keys)))
        temporary.replace(STATE.exclusions_path)
        STATE.recompute_planner_cards()

        # Reconstruct plan and track missing cards
        card_pool = {c.card.card_id: c for c in STATE.planner_cards}
        claimed_ids: set[str] = set()
        lineup_conflicts: dict[str, list[str]] = {}
        missing_cards_report: list[str] = []
        lineups: list[multi_lineup_planner.PlannerLineup] = []

        # First pass: collect slots with card data and sort by priority (locked first)
        raw_lineups = data.get("lineups", [])
        slots_to_resolve: list[tuple[int, dict[str, Any], dict[str, Any]]] = []
        for l_data in raw_lineups:
            for s_data in l_data.get("slots", []):
                if s_data.get("card"):
                    prio = 0
                    if l_data.get("is_locked"):
                        prio -= 2
                    if s_data.get("is_locked"):
                        prio -= 1
                    slots_to_resolve.append((prio, l_data, s_data))
        slots_to_resolve.sort(key=lambda x: x[0])

        resolved_slot_cards: dict[tuple[str, str], multi_lineup_planner.PlannerCard] = {}
        for _, l_data, s_data in slots_to_resolve:
            card_data = s_data.get("card")
            c = resolve_card_for_slot(card_data, card_pool, claimed_ids)
            lid = l_data.get("lineup_id", "")
            sname = s_data.get("slot_name", "")
            if c:
                resolved_slot_cards[(lid, sname)] = c
            else:
                pname = card_data.get("player_name", card_data.get("card_id", "Unknown"))
                pmult = card_data.get("multiplier", 1.0)
                missing_cards_report.append(f"{pname} (ID: {card_data.get('card_id')}) in {lid}")
                conf_msg = f"Conflict: Slot '{sname}' player '{pname}' ({pmult}x) is no longer available in current roster inventory."
                lineup_conflicts.setdefault(lid, []).append(conf_msg)

        for l_data in raw_lineups:
            slots: list[multi_lineup_planner.LineupSlotAssignment] = []
            lid = l_data.get("lineup_id", "")
            for s_data in l_data.get("slots", []):
                sname = s_data.get("slot_name", "")
                c = resolved_slot_cards.get((lid, sname))
                slots.append(
                    multi_lineup_planner.LineupSlotAssignment(
                        slot_name=sname,
                        slot_kind=s_data["slot_kind"],
                        card=c,
                        is_locked=bool(s_data.get("is_locked", False)),
                    )
                )
            pl = multi_lineup_planner.PlannerLineup(
                lineup_id=lid,
                contest_name=l_data["contest_name"],
                slots=slots,
                is_locked=bool(l_data.get("is_locked", False)),
                contest_benchmark=0.0 if salary_migrated else float(l_data.get("contest_benchmark", 0.0)),
                benchmark_percentage=0.0 if salary_migrated else float(l_data.get("benchmark_percentage", 0.0)),
            )
            lineups.append(pl)

        # Track usage across reconstructed plan and revalidate all
        card_usage: dict[str, list[str]] = {}
        for l in lineups:
            for s in l.slots:
                if s.card is not None:
                    card_usage.setdefault(s.card.card.card_id, []).append(l.lineup_id)

        for l in lineups:
            contest = STATE.contests.get(l.contest_name)
            if contest:
                multi_lineup_planner.recalculate_lineup(l, contest, card_usage)
                if l.contest_benchmark > 0:
                    l.benchmark_percentage = round((l.total_projection / l.contest_benchmark) * 100, 1)
            if l.lineup_id in lineup_conflicts:
                for conf in lineup_conflicts[l.lineup_id]:
                    if conf not in l.validation_errors:
                        l.validation_errors.append(conf)
                l.is_valid = False

        preserved_existing_lineups: list[multi_lineup_planner.PlannerLineup] = []
        for l_data in data.get("preserved_existing_lineups", []):
            p_slots: list[multi_lineup_planner.LineupSlotAssignment] = []
            for s_data in l_data.get("slots", []):
                card_data = s_data.get("card")
                c = resolve_card_for_slot(card_data, card_pool, claimed_ids) if card_data else None
                p_slots.append(
                    multi_lineup_planner.LineupSlotAssignment(
                        slot_name=s_data["slot_name"],
                        slot_kind=s_data["slot_kind"],
                        card=c,
                        is_locked=bool(s_data.get("is_locked", False)),
                    )
                )
            p_lineup = multi_lineup_planner.PlannerLineup(
                lineup_id=l_data["lineup_id"],
                contest_name=l_data["contest_name"],
                slots=p_slots,
                is_locked=bool(l_data.get("is_locked", False)),
                contest_benchmark=0.0 if salary_migrated else float(l_data.get("contest_benchmark", 0.0)),
                benchmark_percentage=0.0 if salary_migrated else float(l_data.get("benchmark_percentage", 0.0)),
            )
            if p_lineup.contest_name in STATE.contests:
                multi_lineup_planner.recalculate_lineup(p_lineup, STATE.contests[p_lineup.contest_name])
            preserved_existing_lineups.append(p_lineup)

        saved_benchmarks = {} if salary_migrated else data.get("contest_benchmarks", {})
        saved_quality_threshold = float(data.get("quality_threshold", 0.90))
        saved_inventory_summary = data.get("inventory_summary", {})
        if salary_migrated:
            saved_inventory_summary = {
                "contest_distribution": saved_inventory_summary.get("contest_distribution"),
                "stopping_reasons": ["Salaries updated from roster. Generate again to optimize with the corrected costs."],
            }

        STATE.active_plan = multi_lineup_planner.WeeklyPlan(
            plan_id=data.get("plan_id", "loaded_plan"),
            name=data.get("name", "Loaded Plan"),
            created_at=data.get("created_at", ""),
            updated_at=datetime.now(timezone.utc).isoformat(),
            slate_id=data.get("slate_id", STATE.snapshot.slate_id if STATE.snapshot else "unknown"),
            salary_source="roster",
            contests=STATE.contests,
            lineups=lineups,
            preserved_existing_lineups=preserved_existing_lineups,
            roster_cards=STATE.planner_cards,
            projection_overrides=STATE.overrides,
            excluded_athlete_keys=data.get("excluded_athlete_keys", []),
            total_weekly_projection=round(sum(l.total_projection for l in lineups), 2),
            total_weekly_estimated_payout=round(sum(l.estimated_payout for l in lineups if l.is_valid), 2),
            inventory_summary=saved_inventory_summary,
            contest_benchmarks=saved_benchmarks,
            quality_threshold=saved_quality_threshold,
        )

        if live_session is not None:
            STATE.live_states = restored_players
            STATE.dashboard_schedule = live_session.get("dashboard_schedule")
            STATE.simulated_time = restored_time
            STATE.recompute_planner_cards()
        STATE.last_reallocation_plan = None

        msg = "Plan loaded successfully."
        if salary_migrated:
            msg += " Salaries updated from your roster export. Existing placements are preserved; check any lineups now over the cap."
        if missing_cards_report:
            msg += f" Note: {len(missing_cards_report)} card(s) were no longer found in current inventory: {', '.join(missing_cards_report[:3])}"

        return jsonify({
            "status": "success",
            "message": msg,
            "plan": STATE.active_plan.to_dict(),
            "missing_cards": missing_cards_report,
        })
    except Exception as exc:
        return jsonify({"status": "error", "message": f"Failed to load plan: {exc}"}), 500


@app.post("/api/planner/reset_week")
def api_planner_reset_week():
    upload = request.files.get("roster_file")
    roster_msg = ""
    if upload:
        try:
            stream, err = _read_upload(upload, "roster")
            if err:
                return jsonify({"status": "error", "message": err}), 400
            cards = multi_lineup_planner.parse_roster_cards(stream)
            if not cards:
                raise ValueError("The roster contains no cards.")
            roster_path = STATE.root_dir / "data" / "raw" / "My_roster.csv"
            roster_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = roster_path.with_suffix(".tmp")
            temporary.write_text(stream.getvalue(), encoding="utf-8")
            temporary.replace(roster_path)
            STATE.roster_cards = cards
            roster_msg = f" Updated roster with {len(cards)} cards."
        except Exception as exc:
            return jsonify({"status": "error", "message": f"Roster upload error: {exc}"}), 400

    # Archive existing plan if one exists
    if STATE.active_plan:
        try:
            now_stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
            archive_path = STATE.plans_dir / f"archived_plan_{now_stamp}.json"
            payload = STATE.active_plan.to_dict()
            payload["live_session"] = {
                "players": [asdict(player) for player in STATE.live_states.values()],
                "dashboard_schedule": STATE.dashboard_schedule,
                "simulated_time": STATE.simulated_time.isoformat() if STATE.simulated_time else None,
            }
            archive_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        except Exception:
            pass

    # Clear plan, reset live states, dashboard schedule, simulated time
    STATE.active_plan = None
    STATE.dashboard_schedule = None
    STATE.simulated_time = None
    STATE.last_reallocation_plan = None

    # Reset live states to clean UPCOMING with current projections, then restore Thursday scores
    STATE.live_states = {}
    STATE.load_thursday_scores()
    STATE.recompute_planner_cards()

    # Clear saved latest_plan.json so reloads or restarts start fresh
    latest_plan_file = STATE.plans_dir / "latest_plan.json"
    if latest_plan_file.exists():
        try:
            latest_plan_file.unlink()
        except OSError:
            pass

    matched = [c for c in STATE.planner_cards if c.is_matched]
    eligible = [c for c in STATE.planner_cards if c.is_eligible]

    return jsonify({
        "status": "success",
        "message": f"Workspace reset for new week.{roster_msg} Ready to generate lineups.",
        "roster_count": len(STATE.roster_cards),
        "matched_count": len(matched),
        "eligible_count": len(eligible),
        "snapshot": STATE.snapshot.to_dict() if STATE.snapshot else None,
        "active_plan": None,
    })


@app.get("/api/check_update")
def api_check_update():
    res = planner_update_check.run_weekly_update_check(store_dir=str(STATE.root_dir / "data" / "snapshots"), persist=True)
    if res.get("status") in ("PROJECTIONS_CHANGED", "NEW_SLATE"):
        new_snap = STATE.snapshot_store.load_latest()
        if new_snap:
            STATE.snapshot = new_snap
            STATE.recompute_planner_cards()
    return jsonify(res)


@app.get("/api/requirements")
def api_requirements():
    reqs = payout_evaluator.get_probabilistic_ranking_requirements()
    return jsonify({"status": "success", "requirements": reqs})


# ---------------------------------------------------------------------------
# Market Listing Advisor REST API Routes
# ---------------------------------------------------------------------------


@app.get("/api/market/advisor")
def api_market_advisor():
    core_only = request.args.get("core_only", "true").lower() != "false"
    try:
        min_premium = float(request.args.get("min_premium", 25.0))
    except ValueError:
        min_premium = 25.0
    try:
        min_spread = float(request.args.get("min_spread", 1.50))
    except ValueError:
        min_spread = 1.50
    force_refresh = request.args.get("force_refresh", "false").lower() == "true"

    cards = [c.to_dict() for c in STATE.roster_cards]
    if not cards:
        roster_path = STATE.root_dir / "data" / "raw" / "My_roster.csv"
        if roster_path.exists():
            try:
                parsed = multi_lineup_planner.parse_roster_cards(io.StringIO(roster_path.read_text(encoding="utf-8")))
                cards = [c.to_dict() for c in parsed]
            except Exception:
                pass

    if not cards:
        return jsonify({
            "status": "error",
            "message": "No roster cards uploaded yet. Upload your roster CSV to analyze market listing opportunities."
        }), 400

    try:
        eval_result = market_advisor.evaluate_roster_opportunities(
            cards,
            core_only=core_only,
            min_premium_pct=min_premium,
            min_spread=min_spread,
            force_refresh=force_refresh,
        )
        return jsonify({
            "status": "success",
            **eval_result,
        })
    except Exception as exc:
        return jsonify({"status": "error", "message": str(exc)}), 500


@app.get("/api/market/player-floor")
def api_market_player_floor():
    player = request.args.get("player")
    if not player:
        return jsonify({"status": "error", "message": "Player name required"}), 400
    mult_str = request.args.get("mult")
    try:
        mult = float(mult_str) if mult_str else None
    except ValueError:
        mult = None
    is_core = request.args.get("is_core", "true").lower() != "false"
    force_refresh = request.args.get("force_refresh", "false").lower() == "true"
    floor_data = market_advisor.get_live_market_floor(
        player, target_multiplier=mult, is_core=is_core, force_refresh=force_refresh
    )
    return jsonify({"status": "success", "data": floor_data})


# ---------------------------------------------------------------------------
# Live Lineup Manager REST API Routes
# ---------------------------------------------------------------------------


@app.post("/api/live/import_dashboard_schedule")
def api_live_import_dashboard_schedule():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify(status="error", message="Choose a season and week."), 400
    season, week = data.get("season"), data.get("week")
    if type(season) is not int or type(week) is not int or not 2000 <= season <= 2100 or not 1 <= week <= 18:
        return jsonify(status="error", message="Use a season from 2000 to 2100 and week from 1 to 18."), 400
    if STATE.simulated_time is not None:
        return jsonify(status="error", message="Exit timeline simulation before importing real kickoff times."), 400
    try:
        schedule = STATE.import_dashboard_schedule(season, week)
    except OSError:
        return jsonify(status="error", message="Dashboard data unavailable. Set NFL_DASHBOARD_BOARD_PATH to its local props-board.json file."), 503
    except (ValueError, TypeError) as error:
        return jsonify(status="error", message=str(error)), 400
    return jsonify(status="success", schedule=schedule)


@app.get("/api/live/state")
def api_live_state():
    promising = []
    lineups_payload = []
    missing_inputs = STATE.get_missing_inputs_report()

    if STATE.active_plan:
        evals = live_lineup_manager.identify_promising_lineups(
            STATE.active_plan, STATE.live_states, STATE.simulated_time
        )
        promising = [ev.to_dict() for ev in evals]

        # Build detailed lineup view with slot breakdown
        lineup_eval_map = {ev.lineup_id: ev for ev in evals}
        for l in STATE.active_plan.lineups:
            ev = lineup_eval_map.get(l.lineup_id)
            contest = STATE.contests.get(l.contest_name) or (STATE.active_plan.contests.get(l.contest_name) if STATE.active_plan else None)
            slots_data = []
            for s in l.slots:
                if s.card:
                    c = s.card
                    p_state = STATE.live_states.get(c.card.athlete_key)
                    is_locked = s.is_locked or l.is_locked or (p_state is not None and p_state.is_locked(STATE.simulated_time))
                    game_status = p_state.game_status if p_state else "UPCOMING"
                    if p_state and p_state.game_status == live_lineup_manager.GAME_STATUS_FINAL:
                        total_pts = p_state.final_points * c.card.multiplier
                    elif p_state and p_state.game_status == live_lineup_manager.GAME_STATUS_IN_PROGRESS:
                        total_pts = p_state.effective_mean(c.card.multiplier)
                    elif p_state:
                        total_pts = p_state.effective_mean(c.card.multiplier)
                    else:
                        total_pts = c.adjusted_projection

                    slots_data.append({
                        "slot_name": s.slot_name,
                        "slot_kind": s.slot_kind,
                        "card_id": c.card.card_id,
                        "player_name": c.card.player_name,
                        "team": c.card.team,
                        "position": c.card.position,
                        "multiplier": c.card.multiplier,
                        "is_locked": is_locked,
                        "game_status": game_status,
                        "total_projected_points": round(total_pts, 2),
                        "opp": (p_state.opp if p_state else "") or getattr(c, "opp", "") or "",
                        "game_date": (p_state.game_date if p_state else "") or getattr(c, "game_date", "") or "",
                        "kickoff_time": (p_state.kickoff_time if p_state else "") or getattr(c, "kickoff_time", "") or "",
                    })
                else:
                    slots_data.append({
                        "slot_name": s.slot_name,
                        "slot_kind": s.slot_kind,
                        "card_id": None,
                        "player_name": "<Empty>",
                        "team": "",
                        "position": "",
                        "multiplier": 1.0,
                        "is_locked": False,
                        "game_status": "UPCOMING",
                        "total_projected_points": 0.0,
                        "opp": "",
                        "game_date": "",
                        "kickoff_time": "",
                    })

            p_cash = ev.p_cash if ev else 0.0
            p_top = ev.p_top if ev else 0.0
            top_tier_name = ev.top_tier_name if ev else "Top Tier"

            lineups_payload.append({
                "lineup_id": l.lineup_id,
                "contest_tier": l.contest_name,
                "contest_name": l.contest_name,
                "current_banked_score": ev.banked_score if ev else 0.0,
                "banked_score": ev.banked_score if ev else 0.0,
                "remaining_expected_points": ev.projected_remaining if ev else 0.0,
                "projected_remaining": ev.projected_remaining if ev else 0.0,
                "projected_total": ev.projected_total if ev else l.total_projection,
                "expected_payout": ev.expected_payout if ev else l.estimated_payout,
                "leverage_status": ev.leverage_status if ev else "On Pace",
                "payout_opportunity_summary": ev.leverage_explanation if ev else "Tracking baseline.",
                "p_cash": round(p_cash, 3),
                "p_first": round(p_top, 3),
                "p_top": round(p_top, 3),
                "top_tier_name": top_tier_name,
                "slots": slots_data,
            })

    # Prepare players array
    players_payload = []
    for k, p in STATE.live_states.items():
        p_dict = p.to_dict()
        p_dict["actual_score"] = round(p.final_points if p.game_status == live_lineup_manager.GAME_STATUS_FINAL else p.live_points, 2)
        p_dict["effective_projection"] = round(p.remaining_projection, 2)
        p_dict["is_locked"] = p.is_locked(STATE.simulated_time)
        p_dict["override_lock"] = None
        players_payload.append(p_dict)
    players_payload.sort(key=lambda x: (x["is_locked"], x["player_name"]))

    # Prepare missing inputs report
    missing_inputs_report = {
        "payout_band_assumptions": missing_inputs.get("thresholds_notice", ""),
        "unverified_actual_scores": missing_inputs.get("unverified_scores_count", 0),
        "missing_kickoff_times": missing_inputs.get("missing_kickoff_times_count", 0),
        "risk_model_notes": missing_inputs.get("distributions_notice", ""),
    }

    # Reallocation plan formatting
    realloc_dict = None
    if STATE.last_reallocation_plan:
        realloc_dict = STATE.last_reallocation_plan.to_dict()
        # Add frontend-compatible aliases to swaps
        for s in realloc_dict.get("swaps", []):
            s["old_card_id"] = s.get("current_card_id")
            s["old_player_name"] = s.get("current_player_name")
            s["payout_delta"] = s.get("expected_payout_delta", 0.0)
            lid = s.get("lineup_id")
            l_obj = next((x for x in STATE.active_plan.lineups if x.lineup_id == lid), None) if STATE.active_plan else None
            s["contest_tier"] = l_obj.contest_name if l_obj else ""

    return jsonify({
        "status": "success",
        "simulated_time": STATE.simulated_time.isoformat() if STATE.simulated_time else None,
        "dashboard_schedule": STATE.dashboard_schedule,
        "promising_lineups": promising,
        "lineups": lineups_payload,
        "player_states": {k: v.to_dict() for k, v in STATE.live_states.items()},
        "players": players_payload,
        "reallocation_plan": realloc_dict,
        "missing_inputs": missing_inputs,
        "missing_inputs_report": missing_inputs_report,
        "active_plan": STATE.active_plan.to_dict() if STATE.active_plan else None,
    })


@app.post("/api/live/simulate_timeline")
def api_live_simulate_timeline():
    data = request.get_json(silent=True) or {}
    preset = data.get("preset", "reset")
    custom_time = data.get("custom_time")

    if custom_time:
        try:
            STATE.simulated_time = datetime.fromisoformat(custom_time)
        except Exception:
            return jsonify({"status": "error", "message": "Invalid custom_time format (ISO 8601 expected)"}), 400
    elif preset == "reset":
        STATE.simulated_time = None
        for p in STATE.live_states.values():
            p.game_status = live_lineup_manager.GAME_STATUS_UPCOMING
            p.live_points = 0.0
            p.final_points = 0.0
    elif preset == "post_thursday":
        STATE.simulated_time = datetime(2026, 9, 11, 2, 0, tzinfo=timezone.utc)
        for p in STATE.live_states.values():
            if p.kickoff_time and "2026-09-10" in p.kickoff_time:
                p.game_status = live_lineup_manager.GAME_STATUS_FINAL
    elif preset == "sunday_early_live":
        STATE.simulated_time = datetime(2026, 9, 13, 18, 30, tzinfo=timezone.utc)
        for p in STATE.live_states.values():
            if p.kickoff_time and "13:00" in p.kickoff_time:
                p.game_status = live_lineup_manager.GAME_STATUS_IN_PROGRESS
    elif preset == "sunday_post_early":
        STATE.simulated_time = datetime(2026, 9, 13, 20, 15, tzinfo=timezone.utc)
        for p in STATE.live_states.values():
            if p.kickoff_time and "13:00" in p.kickoff_time:
                p.game_status = live_lineup_manager.GAME_STATUS_FINAL
    elif preset == "monday_pregame":
        STATE.simulated_time = datetime(2026, 9, 14, 23, 0, tzinfo=timezone.utc)
        for p in STATE.live_states.values():
            if not (p.kickoff_time and "09-14" in p.kickoff_time):
                p.game_status = live_lineup_manager.GAME_STATUS_FINAL

    return jsonify({
        "status": "success",
        "message": f"Simulation set to {preset}",
        "simulated_time": STATE.simulated_time.isoformat() if STATE.simulated_time else None,
    })


@app.post("/api/live/update_player_state")
def api_live_update_player_state():
    data = request.get_json(silent=True) or {}
    athlete_key = data.get("athlete_key")
    if not athlete_key:
        return jsonify({"status": "error", "message": "Missing athlete_key"}), 400

    # --- Validate all numeric fields before touching state ---
    def _parse_finite(raw, field_name):
        try:
            v = float(raw)
        except (ValueError, TypeError):
            raise ValueError(f"{field_name} must be a number.")
        if not math.isfinite(v):
            raise ValueError(f"{field_name} must be finite.")
        return v

    try:
        parsed: dict[str, Any] = {}
        if "game_status" in data:
            gs = str(data["game_status"]).upper()
            if gs not in (live_lineup_manager.GAME_STATUS_UPCOMING, live_lineup_manager.GAME_STATUS_IN_PROGRESS, live_lineup_manager.GAME_STATUS_FINAL):
                return jsonify(status="error", message="game_status must be UPCOMING, IN_PROGRESS, or FINAL."), 400
            parsed["game_status"] = gs
        if "live_points" in data:
            parsed["live_points"] = _parse_finite(data["live_points"], "live_points")
        if "actual_score" in data:
            parsed["actual_score"] = _parse_finite(data["actual_score"], "actual_score")
        if "final_points" in data:
            parsed["final_points"] = _parse_finite(data["final_points"], "final_points")
        if "remaining_projection" in data:
            parsed["remaining_projection"] = _parse_finite(data["remaining_projection"], "remaining_projection")
        if "effective_projection" in data:
            parsed["effective_projection"] = _parse_finite(data["effective_projection"], "effective_projection")
        if "remaining_stdev" in data:
            parsed["remaining_stdev"] = _parse_finite(data["remaining_stdev"], "remaining_stdev")
        if "remaining_is_estimate" in data:
            if not isinstance(data["remaining_is_estimate"], bool):
                return jsonify(status="error", message="remaining_is_estimate must be true or false."), 400
            parsed["remaining_is_estimate"] = data["remaining_is_estimate"]
    except ValueError as exc:
        return jsonify({"status": "error", "message": str(exc)}), 400

    # --- All validation passed; now apply ---
    p_state = STATE.live_states.get(athlete_key)
    if not p_state:
        p_state = live_lineup_manager.PlayerLiveState(
            athlete_key=athlete_key,
            player_name=data.get("player_name", athlete_key),
            team=data.get("team", ""),
            position=data.get("position", ""),
        )
        STATE.live_states[athlete_key] = p_state

    if "game_status" in parsed:
        p_state.game_status = parsed["game_status"]
    if "live_points" in parsed:
        p_state.live_points = parsed["live_points"]
    elif "actual_score" in parsed and p_state.game_status != live_lineup_manager.GAME_STATUS_FINAL:
        p_state.live_points = parsed["actual_score"]
    if "final_points" in parsed:
        p_state.final_points = parsed["final_points"]
    elif "actual_score" in parsed and p_state.game_status == live_lineup_manager.GAME_STATUS_FINAL:
        p_state.final_points = parsed["actual_score"]
    if "remaining_projection" in parsed:
        p_state.remaining_projection = parsed["remaining_projection"]
        p_state.remaining_is_estimate = True
    elif "effective_projection" in parsed:
        p_state.remaining_projection = parsed["effective_projection"]
        p_state.remaining_is_estimate = True
    elif "remaining_is_estimate" in parsed:
        # A cleared estimate stays unknown even though the seeded value is still on hand.
        p_state.remaining_is_estimate = parsed["remaining_is_estimate"]
    if "remaining_stdev" in parsed:
        p_state.remaining_stdev = parsed["remaining_stdev"]
    # Final status: zero remaining projection per rule 9.
    if p_state.game_status == live_lineup_manager.GAME_STATUS_FINAL:
        p_state.remaining_projection = 0.0
        p_state.remaining_stdev = 0.0
        p_state.remaining_is_estimate = False
    if "kickoff_time" in data:
        p_state.kickoff_time = str(data["kickoff_time"])
    if "profile_type" in data:
        p_state.profile_type = str(data["profile_type"]).lower()
    if "game_clock" in data:
        p_state.game_clock = str(data["game_clock"])

    STATE.recompute_planner_cards()
    return jsonify({"status": "success", "player_state": p_state.to_dict()})


@app.post("/api/live/recommend_reallocations")
def api_live_recommend_reallocations():
    if not STATE.active_plan:
        return jsonify({"status": "error", "message": "No active weekly plan entered"}), 400

    try:
        plan = live_lineup_manager.solve_coordinated_reallocation(
            STATE.active_plan, STATE.live_states, STATE.simulated_time
        )
        STATE.last_reallocation_plan = plan
        return jsonify({
            "status": "success",
            "reallocation_plan": plan.to_dict(),
        })
    except Exception as exc:
        return jsonify({"status": "error", "message": f"Optimization failed: {exc}"}), 500


@app.post("/api/live/apply_reallocations")
def api_live_apply_reallocations():
    if not STATE.active_plan:
        return jsonify({"status": "error", "message": "No active weekly plan"}), 400
    if not STATE.last_reallocation_plan:
        return jsonify({"status": "error", "message": "No active reallocation plan to apply"}), 400

    swaps_count = len(STATE.last_reallocation_plan.swaps) if STATE.last_reallocation_plan else 0
    success, msg = live_lineup_manager.apply_coordinated_reallocation(
        STATE.active_plan, STATE.last_reallocation_plan, STATE.live_states, STATE.simulated_time
    )
    if not success:
        return jsonify({"status": "error", "message": msg}), 400

    # Clear last plan once applied
    STATE.last_reallocation_plan = None
    return jsonify({
        "status": "success",
        "message": msg,
        "swaps_applied": swaps_count,
        "active_plan": STATE.active_plan.to_dict(),
    })


@app.post("/api/live/load_scenario")
def api_live_load_scenario():
    data = request.get_json(silent=True) or {}
    scenario_name = data.get("scenario_name") or data.get("scenario") or "thursday_breakout"

    if scenario_name == "thursday_breakout":
        plan, live_states = live_lineup_manager.load_thursday_breakout_scenario()
    elif scenario_name == "monday_player_choice":
        plan, live_states = live_lineup_manager.load_monday_player_choice_scenario()
    elif scenario_name == "monday_first_place":
        plan, live_states = live_lineup_manager.load_monday_first_place_chase_scenario()
    elif scenario_name == "inprogress_chain":
        plan, live_states = live_lineup_manager.load_inprogress_dependent_chain_scenario()
    else:
        return jsonify({"status": "error", "message": f"Unknown scenario '{scenario_name}'"}), 400

    STATE.active_plan = plan
    STATE.live_states = live_states
    STATE.dashboard_schedule = None
    STATE.last_reallocation_plan = None

    evals = live_lineup_manager.identify_promising_lineups(plan, live_states, STATE.simulated_time)
    return jsonify({
        "status": "success",
        "message": f"Loaded scenario: {scenario_name}",
        "active_plan": plan.to_dict(),
        "promising_lineups": [e.to_dict() for e in evals],
    })


@app.post("/api/live/upload_scores_csv")
def api_live_upload_scores_csv():
    upload = request.files.get("scores_file")
    if not upload:
        return jsonify({"status": "error", "message": "No scores CSV provided"}), 400
    try:
        states = live_lineup_manager.parse_live_scores_csv(upload.stream)
        for s in states:
            STATE.live_states[s.athlete_key] = s
        STATE.recompute_planner_cards()
        return jsonify({
            "status": "success",
            "message": f"Updated live state for {len(states)} players",
            "count": len(states),
        })
    except Exception as exc:
        return jsonify({"status": "error", "message": str(exc)}), 400


@app.errorhandler(413)
def request_too_large(_error):
    return _render(errors=["The uploaded files are too large. Please upload smaller CSV files."], status_code=413)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5001))
    # ponytail: one request at a time for a single friend's shared in-memory state.
    app.run(host="127.0.0.1" if STATE.isolated else "0.0.0.0", port=port,
            threaded=not STATE.isolated)
