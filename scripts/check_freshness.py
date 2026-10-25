#!/usr/bin/env python3
"""Fail when the aviation data has not been refreshed recently.

Finds the most recent successful run of the data-update workflow through the
GitHub REST API and exits non-zero when it is older than STALE_HOURS. The
failure message names the last success and lists any runs of that workflow
that are still waiting, pending, queued or in progress, since those are the
likely blockers.

Environment:
  GITHUB_TOKEN       token with actions: read (required for private repos and
                     to avoid low anonymous rate limits)
  GITHUB_REPOSITORY  owner/repo (required)
  GITHUB_API_URL     defaults to https://api.github.com
  WORKFLOW_FILE      defaults to update-data.yml
  BRANCH             defaults to main
  STALE_HOURS        defaults to 12

Standard library only.
"""

import json
import os
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

DEFAULT_STALE_HOURS = 12.0
ACTIVE_STATUSES = ("waiting", "pending", "queued", "in_progress")

FRESH = "FRESH"
STALE = "STALE"
ERROR = "ERROR"


def parse_timestamp(value):
    """Parse a GitHub ISO 8601 timestamp such as 2026-09-29T19:34:09Z."""
    if not isinstance(value, str) or not value:
        raise ValueError(f"invalid timestamp: {value!r}")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def format_timestamp(moment):
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _runs_from_payload(payload):
    if not isinstance(payload, dict):
        raise ValueError("API payload is not a JSON object")
    runs = payload.get("workflow_runs")
    if not isinstance(runs, list):
        raise ValueError("API payload has no workflow_runs list")
    return [run for run in runs if isinstance(run, dict)]


def _completed_at(run):
    return parse_timestamp(run.get("updated_at") or run.get("created_at"))


def _describe_run(run):
    parts = [f"run {run.get('id', '?')}", f"status {run.get('status', '?')}"]
    if run.get("head_branch"):
        parts.append(f"branch {run['head_branch']}")
    if run.get("created_at"):
        parts.append(f"created {run['created_at']}")
    if run.get("html_url"):
        parts.append(run["html_url"])
    return ", ".join(parts)


def evaluate(success_payload, active_payloads, now, stale_hours):
    """Decide whether the data is fresh.

    success_payload: a "list workflow runs" API response filtered to
        successful runs.
    active_payloads: "list workflow runs" API responses for runs that have
        not finished (waiting, pending, queued, in progress).
    now: timezone-aware datetime.
    stale_hours: threshold in hours.

    Returns (verdict, message), where verdict is FRESH, STALE or ERROR.
    """
    threshold = timedelta(hours=stale_hours)

    try:
        successes = _runs_from_payload(success_payload)
        last_success = None
        for run in successes:
            finished = _completed_at(run)
            if last_success is None or finished > last_success[0]:
                last_success = (finished, run)
    except (TypeError, ValueError) as exc:
        return ERROR, f"ERROR: could not read successful runs: {exc}"

    blockers = []
    blocker_errors = []
    seen = set()
    for payload in active_payloads:
        try:
            runs = _runs_from_payload(payload)
        except ValueError as exc:
            blocker_errors.append(str(exc))
            continue
        for run in runs:
            key = run.get("id")
            if key is not None and key in seen:
                continue
            seen.add(key)
            blockers.append(run)
    blockers.sort(key=lambda run: str(run.get("created_at") or ""))

    if last_success is None:
        lines = ["STALE: no successful data-update run was found."]
    else:
        finished, run = last_success
        age = now - finished
        age_hours = age.total_seconds() / 3600
        summary = (
            f"last successful run {run.get('id', '?')} finished "
            f"{format_timestamp(finished)} ({age_hours:.1f}h ago, "
            f"threshold {stale_hours:g}h)"
        )
        if age <= threshold:
            return FRESH, f"FRESH: {summary}."
        lines = [f"STALE: {summary}."]

    if blockers:
        lines.append("Unfinished data-update runs that may be blocking updates:")
        lines.extend(f"- {_describe_run(run)}" for run in blockers)
    else:
        lines.append("No data-update runs are currently waiting, pending, queued or in progress.")
    if blocker_errors:
        lines.append("Could not read some unfinished-run lists: " + "; ".join(blocker_errors))

    return STALE, "\n".join(lines)


def fetch_json(url, token):
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "aviation-data-freshness-check",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def runs_url(api_url, repository, workflow, **params):
    query = urllib.parse.urlencode(params)
    return (
        f"{api_url.rstrip('/')}/repos/{repository}/actions/workflows/"
        f"{urllib.parse.quote(workflow)}/runs?{query}"
    )


def main():
    repository = os.environ.get("GITHUB_REPOSITORY")
    if not repository:
        print("ERROR: GITHUB_REPOSITORY is not set.", file=sys.stderr)
        return 2
    token = os.environ.get("GITHUB_TOKEN", "")
    api_url = os.environ.get("GITHUB_API_URL", "https://api.github.com")
    workflow = os.environ.get("WORKFLOW_FILE", "update-data.yml")
    branch = os.environ.get("BRANCH", "main")
    try:
        stale_hours = float(os.environ.get("STALE_HOURS") or DEFAULT_STALE_HOURS)
    except ValueError:
        print("ERROR: STALE_HOURS must be a number.", file=sys.stderr)
        return 2

    try:
        success_payload = fetch_json(
            runs_url(api_url, repository, workflow,
                     branch=branch, status="success", per_page=1),
            token,
        )
        active_payloads = [
            fetch_json(
                runs_url(api_url, repository, workflow, status=status, per_page=50),
                token,
            )
            for status in ACTIVE_STATUSES
        ]
    except (OSError, ValueError) as exc:
        print(f"ERROR: GitHub API request failed: {exc}", file=sys.stderr)
        return 2

    verdict, message = evaluate(
        success_payload, active_payloads, datetime.now(timezone.utc), stale_hours
    )
    if verdict == FRESH:
        print(message)
        return 0
    print(f"::error title=Aviation data is stale::{message.splitlines()[0]}")
    print(message)
    return 1


if __name__ == "__main__":
    sys.exit(main())
