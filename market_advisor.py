import json
import math
import os
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

# Path resolution for database
LOCAL_DB = Path(__file__).resolve().parent / "gameblazers.db"
PARENT_DB = Path(__file__).resolve().parent.parent / "market_py" / "gameblazers.db"
CANONICAL_DB = Path("/Users/shyampatel/Desktop/GB/market_py/gameblazers.db")
DEFAULT_DB = LOCAL_DB if LOCAL_DB.exists() else (PARENT_DB if PARENT_DB.exists() else CANONICAL_DB)

API_HOST = os.getenv("GB_API_HOST", "https://api.gameblazers.com")

# Thread-safe in-memory cache for active floor lookups: key -> (timestamp, data)
_FLOOR_CACHE = {}
CACHE_TTL_SECONDS = 900  # 15 minutes


def get_token() -> str | None:
    token = os.getenv("GB_ACCESS_TOKEN")
    if token:
        token = token.strip()
        if token.startswith("Bearer "):
            token = token[7:].strip()
        if token:
            return token
    token_file = Path("~/.gb_token").expanduser()
    if token_file.exists():
        raw = token_file.read_text().strip()
        if raw.startswith("Bearer "):
            raw = raw[7:].strip()
        if raw:
            return raw
    return None


def fetch_player_active_listings(player_name: str, token: str) -> list[dict]:
    params = {
        "Sport": "Nfl",
        "IsUserListings": "false",
        "IsActive": "true",
        "SortCriteria": "ListingCreationDesc",
        "SearchTerm": player_name,
    }
    url = f"{API_HOST}/api/v1/marketplace/getlistings/page/1?{urlencode(params)}"
    request = Request(
        url,
        headers={
            "Accept": "*/*",
            "Authorization": f"Bearer {token}",
            "Origin": "https://app.gameblazers.com",
            "Referer": "https://app.gameblazers.com/",
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/140 Safari/537.36",
        },
    )
    try:
        with urlopen(request, timeout=10) as response:
            data = json.load(response)
            if isinstance(data, list):
                return data
    except Exception:
        pass
    return []


def parse_active_item(item: dict) -> dict:
    listing_id = item.get("id")
    price = item.get("price")
    created_at = item.get("dateCreated")
    exp_date = item.get("expirationDate")

    details = item.get("marketplaceListingItemDetails") or {}
    if isinstance(details, list):
        details = details[0] if details else {}

    pitem = details.get("playerItemDetails") or {}
    pdetails = pitem.get("playerDetails") or {}
    multiplier = pitem.get("multiplier")
    overall = pitem.get("overallRating")
    full_name = pdetails.get("fullName", "")
    team = pdetails.get("team")
    team_abbr = team.get("abbreviation") if isinstance(team, dict) else str(team or "")
    pos = pdetails.get("positions") or pdetails.get("position")
    pos_str = ",".join(pos) if isinstance(pos, list) else str(pos or "")

    pcoll = pitem.get("playerCollectionDetails") or {}
    coll_name = pcoll.get("name") or ""
    is_core = bool(pcoll.get("isCoreCollection", "core" in coll_name.lower()))
    card_exp = pitem.get("expiringDate") or exp_date

    return {
        "listing_id": listing_id,
        "player_name": full_name,
        "price": float(price) if price is not None else None,
        "multiplier": float(multiplier) if multiplier is not None else 1.0,
        "overall": overall,
        "team": team_abbr,
        "position": pos_str,
        "collection_name": coll_name,
        "is_core": is_core,
        "created_at": created_at,
        "expires_at": exp_date,
        "card_expires_at": card_exp,
    }


CANONICAL_CACHE = Path("/Users/shyampatel/Desktop/GB/lineup_optimizer/data/market_floor_cache.json")
CACHE_FILE = CANONICAL_CACHE if CANONICAL_CACHE.parent.exists() else (Path(__file__).resolve().parent / "data" / "market_floor_cache.json")


def _load_disk_cache():
    global _FLOOR_CACHE
    if not _FLOOR_CACHE and CACHE_FILE.exists():
        try:
            raw = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
            now = time.time()
            for k_str, val in raw.items():
                parts = json.loads(k_str)
                key = (parts[0], parts[1], parts[2])
                cache_time = val.get("_cached_at", 0)
                if now - cache_time < CACHE_TTL_SECONDS:
                    _FLOOR_CACHE[key] = (cache_time, val)
        except Exception:
            pass


def _save_disk_cache():
    try:
        CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        dump_data = {}
        for key, (cache_time, val) in _FLOOR_CACHE.items():
            k_str = json.dumps([key[0], key[1], key[2]])
            dump_data[k_str] = {**val, "_cached_at": cache_time}
        temporary = CACHE_FILE.with_suffix(".tmp")
        temporary.write_text(json.dumps(dump_data), encoding="utf-8")
        temporary.replace(CACHE_FILE)
    except Exception:
        pass


def get_live_market_floor(
    player_name: str,
    target_multiplier: float | None = None,
    is_core: bool | None = None,
    token: str | None = None,
    force_refresh: bool = False,
) -> dict:
    global _FLOOR_CACHE
    now = time.time()
    _load_disk_cache()
    cache_key = (player_name.strip().lower(), target_multiplier, is_core)

    if not force_refresh and cache_key in _FLOOR_CACHE:
        cache_time, cached_val = _FLOOR_CACHE[cache_key]
        if now - cache_time < CACHE_TTL_SECONDS:
            return {**cached_val, "cached": True}

    if not token:
        token = get_token()

    if not token:
        return {
            "player_name": player_name,
            "cheapest_for_multiplier": None,
            "cheapest_overall": None,
            "total_active_listings": 0,
            "cached": False,
            "error": "No token",
        }

    raw_items = fetch_player_active_listings(player_name, token)
    parsed = [parse_active_item(x) for x in raw_items]

    # Filter by exact player name
    exact = [x for x in parsed if x["player_name"].lower() == player_name.lower() and x["price"] is not None]
    if not exact:
        tokens = set(player_name.lower().replace("'", "").replace(".", "").split())
        exact = [
            x for x in parsed
            if tokens.issubset(set(x["player_name"].lower().replace("'", "").replace(".", "").split()))
            and x["price"] is not None
        ]

    if is_core is not None:
        exact = [x for x in exact if x.get("is_core") == is_core]

    by_mult = {}
    for comp in exact:
        m = comp["multiplier"]
        p = comp["price"]
        if m not in by_mult or p < by_mult[m]["price"]:
            by_mult[m] = comp

    mult_comp = by_mult.get(target_multiplier) if target_multiplier is not None else None
    overall_cheapest = min(exact, key=lambda x: x["price"]) if exact else None

    result = {
        "player_name": player_name,
        "target_multiplier": target_multiplier,
        "total_active_listings": len(exact),
        "cheapest_for_multiplier": mult_comp["price"] if mult_comp else None,
        "cheapest_overall": overall_cheapest["price"] if overall_cheapest else None,
        "cached": False,
    }

    _FLOOR_CACHE[cache_key] = (now, result)
    _save_disk_cache()
    return result


def compute_fair_value(
    conn: sqlite3.Connection,
    player_name: str,
    multiplier: float | None = None,
    is_core: bool | None = None,
    card_expires_at: str | None = None,
    half_life_days: float = 14.0,
    min_comps: int = 3,
) -> dict:
    now_utc = datetime.now(timezone.utc)
    query = """
        SELECT price, multiplier, overall_rating, player_position, sold_at, card_expires_at
        FROM marketplace_sales_v2
        WHERE LOWER(player_name) = LOWER(?) AND price > 0 AND sold_at IS NOT NULL
    """
    params = [player_name.strip()]

    if is_core is True:
        query += " AND collection_name LIKE '%Core%'"
    elif is_core is False:
        query += " AND collection_name NOT LIKE '%Core%'"

    query += " ORDER BY sold_at DESC LIMIT 200"

    cursor = conn.execute(query, params)
    rows = cursor.fetchall()

    if not rows and is_core is not None:
        # Fall back to all collections if no specific collection comps exist
        cursor = conn.execute(
            """
            SELECT price, multiplier, overall_rating, player_position, sold_at, card_expires_at
            FROM marketplace_sales_v2
            WHERE LOWER(player_name) = LOWER(?) AND price > 0 AND sold_at IS NOT NULL
            ORDER BY sold_at DESC LIMIT 200
            """,
            (player_name.strip(),),
        )
        rows = cursor.fetchall()

    if not rows:
        return {
            "player_name": player_name,
            "multiplier": multiplier,
            "fair_value": None,
            "comps_count": 0,
            "confidence": "None",
        }

    sales = []
    for price, mult, overall, pos, sold_at_str, exp_at_str in rows:
        try:
            sold_dt = datetime.fromisoformat(sold_at_str.replace("Z", "+00:00"))
            days_ago = max(0.0, (now_utc - sold_dt).total_seconds() / 86400.0)
            sales.append({
                "price": float(price),
                "multiplier": float(mult) if mult else 1.0,
                "days_ago": days_ago,
            })
        except Exception:
            continue

    if not sales:
        return {
            "player_name": player_name,
            "multiplier": multiplier,
            "fair_value": None,
            "comps_count": 0,
            "confidence": "None",
        }

    target_mult = float(multiplier) if multiplier is not None else 1.0
    exact_matches = [s for s in sales if abs(s["multiplier"] - target_mult) < 0.05]

    if len(exact_matches) >= min_comps:
        comp_pool = exact_matches
        confidence = "High" if len(exact_matches) >= 10 else "Medium"
    else:
        comp_pool = []
        for s in sales:
            scaled_price = s["price"] * (target_mult / s["multiplier"]) if s["multiplier"] > 0 else s["price"]
            comp_pool.append({
                **s,
                "price": scaled_price,
            })
        confidence = "Medium" if len(comp_pool) >= 10 else "Low"

    prices = sorted([s["price"] for s in comp_pool])
    if len(prices) >= 5:
        q1 = prices[len(prices) // 4]
        q3 = prices[(len(prices) * 3) // 4]
        iqr = q3 - q1
        lower_bound = max(0.50, q1 - 1.5 * iqr)
        upper_bound = q3 + 1.5 * iqr
        filtered_comps = [s for s in comp_pool if lower_bound <= s["price"] <= upper_bound]
        if not filtered_comps:
            filtered_comps = comp_pool
    else:
        filtered_comps = comp_pool

    decay_constant = half_life_days / math.log(2)
    weights = [math.exp(-s["days_ago"] / decay_constant) for s in filtered_comps]
    total_weight = sum(weights)

    if total_weight > 0:
        norm_weights = [w / total_weight for w in weights]
        weighted_val = sum(s["price"] * w for s, w in zip(filtered_comps, norm_weights))
    else:
        weighted_val = sum(s["price"] for s in filtered_comps) / len(filtered_comps)

    fair_price = round(max(0.50, weighted_val), 2)
    return {
        "player_name": player_name,
        "multiplier": multiplier,
        "fair_value": fair_price,
        "comps_count": len(filtered_comps),
        "confidence": confidence,
    }


def recommend_undercut_price(floor_price: float | None, fair_val: float | None) -> float:
    if floor_price is None or floor_price <= 0:
        return round(fair_val * 1.4, 2) if fair_val else 5.00

    if floor_price >= 20.0:
        target = floor_price - 2.00
    elif floor_price >= 10.0:
        target = floor_price - 1.00
    elif floor_price >= 5.0:
        target = floor_price - 0.50
    elif floor_price >= 2.0:
        target = floor_price - 0.25
    else:
        target = floor_price - 0.10

    if fair_val and target < fair_val:
        target = max(fair_val, floor_price - 0.05)
    return round(target, 2)


def evaluate_roster_opportunities(
    roster_rows: list[dict],
    token: str | None = None,
    db_path: Path = DEFAULT_DB,
    core_only: bool = True,
    min_premium_pct: float = 25.0,
    min_spread: float = 1.50,
    force_refresh: bool = False,
    delay: float = 0.25,
) -> dict:
    """
    Evaluates uploaded roster cards for premium listing opportunities.
    Returns categorized recommendations, duplicates, and summary metrics.
    """
    if not token:
        token = get_token()

    tradeable_cards = [
        r for r in roster_rows
        if str(r.get("Tradeable", r.get("tradeable", "yes"))).strip().lower() in ("yes", "true", "1")
    ]

    if core_only:
        tradeable_cards = [
            r for r in tradeable_cards
            if "core" in str(r.get("Collection", r.get("collection", ""))).lower()
        ]

    # Group by (player_name, multiplier, collection)
    groups = {}
    for r in tradeable_cards:
        p_name = r.get("Player") or r.get("player_name") or ""
        p_name = p_name.strip()
        if not p_name:
            continue

        try:
            mult = float(r.get("Multiplier") or r.get("multiplier") or 1.0)
        except ValueError:
            mult = 1.0

        coll = str(r.get("Collection") or r.get("collection") or "")
        is_core = "core" in coll.lower()
        is_franchise = str(r.get("Franchise", r.get("franchise", "no"))).strip().lower() in ("yes", "true", "1")
        expires = str(r.get("Expires") or r.get("expires") or "N/A")
        team = str(r.get("Team") or r.get("team") or "")
        pos = str(r.get("Positions") or r.get("position") or "")
        salary = r.get("Salary") or r.get("salary") or 0

        key = (p_name.lower(), mult, is_core)
        if key not in groups:
            groups[key] = {
                "player_name": p_name,
                "multiplier": mult,
                "collection": coll,
                "is_core": is_core,
                "is_franchise": is_franchise,
                "expires": expires,
                "team": team,
                "pos": pos,
                "salary": salary,
                "copies_owned": 1,
            }
        else:
            groups[key]["copies_owned"] += 1
            if is_franchise:
                groups[key]["is_franchise"] = True

    unique_cards = list(groups.values())
    conn = sqlite3.connect(db_path) if db_path.exists() else None
    results = []

    try:
        for idx, item in enumerate(unique_cards):
            p_name = item["player_name"]
            mult = item["multiplier"]
            is_core = item["is_core"]
            card_exp = item["expires"]
            copies = item["copies_owned"]

            # Historical fair price
            if conn:
                fv_data = compute_fair_value(
                    conn,
                    p_name,
                    multiplier=mult,
                    is_core=is_core,
                    card_expires_at=card_exp if card_exp != "N/A" else None,
                )
                fair_val = fv_data.get("fair_value")
                comps_count = fv_data.get("comps_count", 0)
                conf = fv_data.get("confidence", "None")
            else:
                fair_val = None
                comps_count = 0
                conf = "None"

            # Live market floor
            bin_data = get_live_market_floor(
                p_name,
                target_multiplier=mult,
                is_core=is_core,
                token=token,
                force_refresh=force_refresh,
            )
            floor_for_mult = bin_data.get("cheapest_for_multiplier")
            floor_overall = bin_data.get("cheapest_overall")
            active_count = bin_data.get("total_active_listings", 0)

            status = "HOLD"
            recommended_price = None
            premium_dollars = 0.0
            premium_pct = 0.0

            if active_count == 0:
                status = "MONOPOLY"
                recommended_price = round((fair_val * 1.5) if fair_val else 10.0, 2)
                premium_dollars = (recommended_price - fair_val) if fair_val else 0.0
                premium_pct = 50.0
            elif floor_for_mult is not None and fair_val and fair_val > 0:
                premium_dollars = floor_for_mult - fair_val
                premium_pct = (premium_dollars / fair_val) * 100.0

                if premium_pct >= min_premium_pct and premium_dollars >= min_spread:
                    status = "PREMIUM_SELL"
                    recommended_price = recommend_undercut_price(floor_for_mult, fair_val)
                elif floor_for_mult > fair_val:
                    status = "MODEST_PREMIUM"
                    recommended_price = recommend_undercut_price(floor_for_mult, fair_val)
                else:
                    status = "UNDERSOLD"
            elif floor_overall is not None and fair_val and fair_val > 0:
                if floor_overall > fair_val * 1.3:
                    status = "PREMIUM_SELL"
                    recommended_price = recommend_undercut_price(floor_overall, fair_val)
                    premium_dollars = floor_overall - fair_val
                    premium_pct = (premium_dollars / fair_val) * 100.0

            net_proceeds = round(recommended_price * 0.95, 2) if recommended_price else None
            net_profit_over_fair = round(net_proceeds - fair_val, 2) if (net_proceeds and fair_val) else None

            results.append({
                "player": p_name,
                "team": item["team"],
                "pos": item["pos"],
                "multiplier": mult,
                "collection": item["collection"],
                "is_franchise": item["is_franchise"],
                "expires": card_exp,
                "copies_owned": copies,
                "is_surplus": copies >= 2,
                "fair_value": fair_val,
                "comps_count": comps_count,
                "confidence": conf,
                "floor_price": floor_for_mult,
                "overall_floor": floor_overall,
                "active_market_count": active_count,
                "status": status,
                "recommended_price": recommended_price,
                "net_proceeds": net_proceeds,
                "net_profit_over_fair": net_profit_over_fair,
                "premium_dollars": round(premium_dollars, 2),
                "premium_pct": round(premium_pct, 1),
            })

            # Small delay only when not cached
            if not bin_data.get("cached"):
                time.sleep(delay)

    finally:
        if conn:
            conn.close()

    # Sort opportunities: Surplus Premium first, then Top Premiums, then Monopoly, then rest
    def sort_key(x):
        priority = 4
        if x["status"] == "PREMIUM_SELL" and x["is_surplus"]:
            priority = 0
        elif x["status"] == "PREMIUM_SELL":
            priority = 1
        elif x["status"] == "MONOPOLY":
            priority = 2
        elif x["status"] == "MODEST_PREMIUM":
            priority = 3
        return (priority, -x["premium_dollars"])

    results.sort(key=sort_key)

    premium_opps = [r for r in results if r["status"] == "PREMIUM_SELL"]
    surplus_opps = [r for r in results if r["status"] == "PREMIUM_SELL" and r["is_surplus"]]
    monopoly_opps = [r for r in results if r["status"] == "MONOPOLY"]

    total_potential_profit = sum(r["net_profit_over_fair"] or 0 for r in premium_opps)

    return {
        "summary": {
            "total_tradeable_cards": len(tradeable_cards),
            "unique_cards_evaluated": len(unique_cards),
            "premium_opportunities": len(premium_opps),
            "surplus_opportunities": len(surplus_opps),
            "monopoly_opportunities": len(monopoly_opps),
            "estimated_potential_profit": round(total_potential_profit, 2),
            "evaluated_at": datetime.now(timezone.utc).isoformat(),
        },
        "items": results,
    }
