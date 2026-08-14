from __future__ import annotations

import io
import os
from collections import Counter
from typing import Any

from flask import Flask, render_template, request

from optimizer_core import CONTESTS, OptimizationResult, optimize_lineup


app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 2 * 1024 * 1024

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
    ), status_code


@app.template_filter("currency")
def currency(value: Any) -> str:
    return f"${value:,.0f}"


@app.template_filter("decimal")
def decimal(value: Any) -> str:
    return f"{value:,.2f}"


@app.template_filter("multiplier")
def multiplier(value: Any) -> str:
    return f"{value:,.2f}x"


@app.get("/")
def home():
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


@app.errorhandler(413)
def request_too_large(_error):
    return _render(errors=["The uploaded files are too large. Please upload smaller CSV files."], status_code=413)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
