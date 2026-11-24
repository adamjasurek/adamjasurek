#!/usr/bin/env python3
"""Generate a neo-brutalist GitHub profile card (profile-card.svg).

Dependency-free: Python 3 standard library only. Designed to run in
GitHub Actions (ubuntu-latest) or locally.

Usage:
  METRICS_TOKEN=... python3 scripts/profile_card.py
  python3 scripts/profile_card.py --mock scripts/testdata/mock_profile.json
  python3 scripts/profile_card.py --self-test

Commit counting
---------------
The commit total is the sum of
``contributionsCollection.totalCommitContributions`` over contiguous
calendar-year windows from the account ``createdAt`` to now:

  * first window: createdAt .. Dec 31 of that year (UTC)
  * middle windows: Jan 1 .. Dec 31 of each full year
  * last window: Jan 1 of this year .. now

Each window is at most one year, which is GitHub's GraphQL limit.
``totalCommitContributions`` is GitHub's own "commits" figure. When
METRICS_TOKEN has the ``repo`` scope and can see the user's private
repos, this includes private-repo commits.

We do *not* add ``restrictedContributionsCount``. That field mixes
commits with pull requests, issues, and reviews, so including it
would overstate "commits". If a window reports a non-zero restricted
count (the token could not see a private breakdown), those hidden
contributions are omitted from the total and a warning is printed.

The SVG never lists repository names — only aggregate counts and the
contribution calendar heatmap.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape

LOGIN_DEFAULT = "adamjasurek"
GRAPHQL_URL = "https://api.github.com/graphql"
SCRIPT_DIR = Path(__file__).resolve().parent
ROOT = SCRIPT_DIR.parent
DEFAULT_OUT = ROOT / "profile-card.svg"
NAME_PATHS_FILE = SCRIPT_DIR / "name_paths.json"

# Palette (matches the profile banner)
PAPER = "#FFF8EB"
INK = "#111111"
YELLOW = "#FFD60A"
LIME = "#C6FF4D"
PINK = "#FF4D8D"
# Official GitHub dark-theme contribution graph
GRAPH_BG = "#0d1117"
GRAPH_LABEL = "#7d8590"
LEVEL_COLORS = {
    0: "#161b22",
    1: "#0e4429",
    2: "#006d32",
    3: "#26a641",
    4: "#39d353",
}
LEVEL_FROM_NAME = {
    "NONE": 0,
    "FIRST_QUARTILE": 1,
    "SECOND_QUARTILE": 2,
    "THIRD_QUARTILE": 3,
    "FOURTH_QUARTILE": 4,
}

FONT = "ui-sans-serif, system-ui, -apple-system, 'Segoe UI', Helvetica, Arial, sans-serif"


def load_name_paths() -> dict[str, Any]:
    data = json.loads(NAME_PATHS_FILE.read_text(encoding="utf-8"))
    return data


# ---------------------------------------------------------------------------
# Time windows and copy
# ---------------------------------------------------------------------------

def parse_iso(ts: str) -> datetime:
    ts = ts.strip().replace("Z", "+00:00")
    return datetime.fromisoformat(ts).astimezone(timezone.utc)


def year_windows(created: datetime, now: datetime) -> list[tuple[datetime, datetime]]:
    """Contiguous UTC windows of at most one year from created..now."""
    if now < created:
        raise ValueError("now is before createdAt")
    windows: list[tuple[datetime, datetime]] = []
    start = created
    while start < now:
        end_of_year = datetime(start.year, 12, 31, 23, 59, 59, tzinfo=timezone.utc)
        end = min(end_of_year, now)
        windows.append((start, end))
        if end >= now:
            break
        start = datetime(start.year + 1, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
    return windows


def years_ago_phrase(created: datetime, now: datetime) -> str:
    years = now.year - created.year - (
        (now.month, now.day) < (created.month, created.day)
    )
    if years >= 2:
        return f"Joined GitHub {years} years ago"
    if years == 1:
        return "Joined GitHub 1 year ago"
    months = (now.year - created.year) * 12 + now.month - created.month
    if now.day < created.day:
        months -= 1
    if months <= 0:
        return "Joined GitHub this month"
    if months == 1:
        return "Joined GitHub 1 month ago"
    return f"Joined GitHub {months} months ago"


def format_int(n: int) -> str:
    return f"{n:,}"


def sum_commits(windows: list[dict[str, Any]]) -> tuple[int, int]:
    """Return (commit_total, restricted_total). Commits do not include restricted."""
    commits = 0
    restricted = 0
    for w in windows:
        commits += int(w.get("totalCommitContributions") or 0)
        restricted += int(w.get("restrictedContributionsCount") or 0)
    return commits, restricted


# ---------------------------------------------------------------------------
# GitHub GraphQL
# ---------------------------------------------------------------------------

def _graphql(token: str, query: str, variables: dict[str, Any]) -> dict[str, Any]:
    body = json.dumps({"query": query, "variables": variables}).encode("utf-8")
    req = urllib.request.Request(
        GRAPHQL_URL,
        data=body,
        headers={
            "Authorization": f"bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "adamjasurek-profile-card",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=45) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise SystemExit(f"GitHub GraphQL HTTP {exc.code}: {detail[:500]}") from exc
    if payload.get("errors"):
        raise SystemExit(f"GitHub GraphQL errors: {payload['errors']}")
    return payload["data"]


def fetch_profile(token: str, login: str, now: datetime) -> dict[str, Any]:
    data = _graphql(
        token,
        """
        query($login: String!) {
          user(login: $login) {
            createdAt
            repositories(ownerAffiliations: OWNER) { totalCount }
            contributionsCollection {
              contributionCalendar {
                weeks {
                  contributionDays {
                    date
                    contributionCount
                    contributionLevel
                    weekday
                  }
                }
              }
            }
          }
        }
        """,
        {"login": login},
    )
    user = data["user"]
    if not user:
        raise SystemExit(f"GitHub user {login!r} not found")
    created = parse_iso(user["createdAt"])
    windows = year_windows(created, now)

    alias_parts = []
    variables: dict[str, Any] = {"login": login}
    for i, (start, end) in enumerate(windows):
        alias_parts.append(
            f"c{i}: contributionsCollection(from: $from{i}, to: $to{i})"
            " { totalCommitContributions restrictedContributionsCount }"
        )
        variables[f"from{i}"] = start.strftime("%Y-%m-%dT%H:%M:%SZ")
        variables[f"to{i}"] = end.strftime("%Y-%m-%dT%H:%M:%SZ")
    var_decl = ", ".join(
        ["$login: String!"]
        + [f"$from{i}: DateTime!, $to{i}: DateTime!" for i in range(len(windows))]
    )
    year_data = _graphql(
        token,
        f"query({var_decl}) {{ user(login: $login) {{ {' '.join(alias_parts)} }} }}",
        variables,
    )
    commit_years = []
    for i, (start, end) in enumerate(windows):
        block = year_data["user"][f"c{i}"]
        commit_years.append(
            {
                "from": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "to": end.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "totalCommitContributions": block["totalCommitContributions"],
                "restrictedContributionsCount": block["restrictedContributionsCount"],
            }
        )

    calendar = []
    for week in user["contributionsCollection"]["contributionCalendar"]["weeks"]:
        for day in week["contributionDays"]:
            level = LEVEL_FROM_NAME.get(str(day.get("contributionLevel") or ""), None)
            calendar.append(
                {
                    "date": day["date"],
                    "count": int(day["contributionCount"] or 0),
                    "level": level,
                    "weekday": day.get("weekday"),
                }
            )
    counts = [d["count"] for d in calendar]
    for day in calendar:
        if day["level"] is None:
            day["level"] = level_from_count(day["count"], counts)

    return {
        "login": login,
        "createdAt": user["createdAt"],
        "repositoryCount": int(user["repositories"]["totalCount"]),
        "commitYears": commit_years,
        "calendar": calendar,
    }


def level_from_count(count: int, all_counts: list[int] | None = None) -> int:
    if count <= 0:
        return 0
    nonzero = sorted(c for c in (all_counts or []) if c > 0)
    if len(nonzero) >= 4:
        def quartile(p: float) -> int:
            idx = min(len(nonzero) - 1, max(0, int(round((p / 100) * (len(nonzero) - 1)))))
            return nonzero[idx]
        thresholds = [quartile(25), quartile(50), quartile(75)]
        for i, threshold in enumerate(thresholds, start=1):
            if count <= threshold:
                return i
        return 4
    if count <= 2:
        return 1
    if count <= 5:
        return 2
    if count <= 9:
        return 3
    return 4


def normalize_calendar(days: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Deduplicate by date, fill gaps, pad to a Sunday-Saturday grid."""
    by_date: dict[date, dict[str, Any]] = {}
    counts = [int(d.get("count") or 0) for d in days]
    for item in days:
        dt = date.fromisoformat(item["date"])
        level = item.get("level")
        if level is None:
            level = level_from_count(int(item.get("count") or 0), counts)
        by_date[dt] = {
            "date": dt.isoformat(),
            "count": int(item.get("count") or 0),
            "level": int(level),
        }
    if not by_date:
        return []
    start = min(by_date)
    end = max(by_date)
    start = start - timedelta(days=(start.weekday() + 1) % 7)
    end = end + timedelta(days=(5 - end.weekday()) % 7)
    out = []
    cur = start
    one = timedelta(days=1)
    while cur <= end:
        if cur in by_date:
            out.append(by_date[cur])
        else:
            out.append({"date": cur.isoformat(), "count": 0, "level": 0})
        cur += one
    return out


# ---------------------------------------------------------------------------
# SVG
# ---------------------------------------------------------------------------

def path_els(paths: list[str], fill: str = INK) -> str:
    return "\n".join(f'<path fill="{fill}" d="{d}"/>' for d in paths)


def render_svg(profile: dict[str, Any], now: datetime) -> str:
    name = load_name_paths()
    name_paths = name["paths"]
    name_bbox = name["bbox"]

    created = parse_iso(profile["createdAt"])
    repos = int(profile["repositoryCount"])
    commits, restricted = sum_commits(profile.get("commitYears") or [])
    if "commitTotal" in profile and not profile.get("commitYears"):
        commits = int(profile["commitTotal"])
    joined = years_ago_phrase(created, now)
    calendar = normalize_calendar(profile.get("calendar") or [])

    vw, vh = 1200, 476
    card_x, card_y = 12, 12
    card_w, card_h = 1156, 440
    shadow = 16

    plate_x, plate_y = 40, 36
    plate_w, plate_h = 1100, 78
    bx0, by0, bx1, by1 = name_bbox
    inner_top = plate_y + 2
    inner_bot = plate_y + plate_h - 2
    glyph_h = by1 - by0
    pad = ((inner_bot - inner_top) - glyph_h) / 2
    name_cx = plate_x + 22
    name_baseline = inner_top + pad - by0

    tile_y = 132
    tile_h = 78
    tile_gap = 16
    tiles_x = 40
    tiles_w = 1100
    tile_w = (tiles_w - 2 * tile_gap) / 3

    # Dark graph panel, aligned with the tiles above
    panel_x, panel_w = 40.0, 1100.0
    panel_y = 222.0
    cal_plot_x = panel_x + 40
    cal_plot_y = panel_y + 28
    weeks_n = max(1, len(calendar) // 7)
    plot_right = panel_x + panel_w - 20
    plot_w = plot_right - cal_plot_x
    pitch = plot_w / weeks_n
    gap = 3.0
    cell = pitch - gap
    if cell < 10:
        cell = 10.0
        gap = max(1.5, pitch - cell)
    cal_h = 7 * pitch - gap
    legend_y = cal_plot_y + cal_h + 22
    panel_h = (legend_y + 16) - panel_y

    weeks: list[list[dict[str, Any]]] = [
        calendar[i:i + 7] for i in range(0, len(calendar), 7)
    ]

    month_labels: list[tuple[float, str]] = []
    last_m = None
    last_x = -999.0
    for wi, week in enumerate(weeks):
        for day in week:
            dt = date.fromisoformat(day["date"])
            if last_m is None or dt.month != last_m:
                x = cal_plot_x + wi * pitch
                if last_m is None or x - last_x >= 32:
                    month_labels.append((x, dt.strftime("%b")))
                    last_x = x
                last_m = dt.month
                break

    tiles = [
        (LIME, "Joined GitHub", joined.replace("Joined GitHub ", "")),
        (YELLOW, "repos", format_int(repos)),
        (PINK, "commits", format_int(commits)),
    ]

    parts: list[str] = []
    parts.append('<?xml version="1.0" encoding="UTF-8"?>')
    parts.append(
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{vw}" height="{vh}" '
        f'viewBox="0 0 {vw} {vh}" role="img" aria-labelledby="title desc">'
    )
    title = "Adam Jašúrek on GitHub"
    desc = (
        f"{joined}. {format_int(repos)} repos. {format_int(commits)} commits. "
        "Contribution graph for the last 12 months."
    )
    parts.append(f'  <title id="title">{escape(title)}</title>')
    parts.append(f'  <desc id="desc">{escape(desc)}</desc>')
    parts.append(
        f'  <rect x="{card_x + shadow}" y="{card_y + shadow}" '
        f'width="{card_w}" height="{card_h}" fill="{INK}"/>'
    )
    parts.append(
        f'  <rect x="{card_x}" y="{card_y}" width="{card_w}" height="{card_h}" '
        f'fill="{PAPER}" stroke="{INK}" stroke-width="8"/>'
    )
    parts.append(
        f'  <rect x="{plate_x + 6}" y="{plate_y + 6}" width="{plate_w}" '
        f'height="{plate_h}" fill="{INK}"/>'
    )
    parts.append(
        f'  <rect x="{plate_x}" y="{plate_y}" width="{plate_w}" height="{plate_h}" '
        f'fill="{YELLOW}" stroke="{INK}" stroke-width="4"/>'
    )
    parts.append(f'  <g transform="translate({name_cx:.2f}, {name_baseline:.2f})">')
    parts.append(path_els(name_paths))
    parts.append("  </g>")

    for i, (color, caption, value) in enumerate(tiles):
        tx = tiles_x + i * (tile_w + tile_gap)
        parts.append(
            f'  <rect x="{tx + 5:.2f}" y="{tile_y + 5}" width="{tile_w:.2f}" '
            f'height="{tile_h}" fill="{INK}"/>'
        )
        parts.append(
            f'  <rect x="{tx:.2f}" y="{tile_y}" width="{tile_w:.2f}" height="{tile_h}" '
            f'fill="{PAPER}" stroke="{INK}" stroke-width="4"/>'
        )
        parts.append(
            f'  <rect x="{tx:.2f}" y="{tile_y}" width="{tile_w:.2f}" height="10" '
            f'fill="{color}" stroke="{INK}" stroke-width="4"/>'
        )
        if i == 0:
            parts.append(
                f'  <text x="{tx + 18:.2f}" y="{tile_y + 30}" font-family="{FONT}" '
                f'font-size="13" font-weight="600" fill="{INK}">{escape(caption)}</text>'
            )
            parts.append(
                f'  <text x="{tx + 18:.2f}" y="{tile_y + 58}" font-family="{FONT}" '
                f'font-size="24" font-weight="800" fill="{INK}">{escape(value)}</text>'
            )
        else:
            parts.append(
                f'  <text x="{tx + 18:.2f}" y="{tile_y + 42}" font-family="{FONT}" '
                f'font-size="32" font-weight="800" fill="{INK}">{escape(value)}</text>'
            )
            parts.append(
                f'  <text x="{tx + 18:.2f}" y="{tile_y + 64}" font-family="{FONT}" '
                f'font-size="13" font-weight="600" fill="{INK}">{escape(caption)}</text>'
            )

    parts.append(
        f'  <rect x="{panel_x + 5:.2f}" y="{panel_y + 5:.2f}" width="{panel_w:.2f}" '
        f'height="{panel_h:.2f}" fill="{INK}"/>'
    )
    parts.append(
        f'  <rect x="{panel_x:.2f}" y="{panel_y:.2f}" width="{panel_w:.2f}" '
        f'height="{panel_h:.2f}" fill="{GRAPH_BG}" stroke="{INK}" stroke-width="4"/>'
    )

    for x, lab in month_labels:
        parts.append(
            f'  <text x="{x:.2f}" y="{cal_plot_y - 10}" font-family="{FONT}" '
            f'font-size="12" font-weight="600" fill="{GRAPH_LABEL}">{lab}</text>'
        )
    for di, lab in ((1, "Mon"), (3, "Wed"), (5, "Fri")):
        y = cal_plot_y + di * pitch + cell * 0.8
        parts.append(
            f'  <text x="{cal_plot_x - 10}" y="{y:.2f}" text-anchor="end" '
            f'font-family="{FONT}" font-size="11" font-weight="600" fill="{GRAPH_LABEL}">{lab}</text>'
        )

    today = now.date()
    for wi, week in enumerate(weeks):
        for di, day in enumerate(week):
            x = cal_plot_x + wi * pitch
            y = cal_plot_y + di * pitch
            fill = LEVEL_COLORS.get(int(day["level"]), LEVEL_COLORS[0])
            if date.fromisoformat(day["date"]) > today:
                fill = LEVEL_COLORS[0]
            parts.append(
                f'  <rect x="{x:.2f}" y="{y:.2f}" width="{cell:.2f}" height="{cell:.2f}" rx="2" ry="2" fill="{fill}"/>'
            )

    lx = cal_plot_x
    sw, sg = 12, 4
    parts.append(
        f'  <text x="{lx}" y="{legend_y:.2f}" font-family="{FONT}" font-size="11" '
        f'font-weight="600" fill="{GRAPH_LABEL}">Less</text>'
    )
    for i in range(5):
        parts.append(
            f'  <rect x="{lx + 36 + i * (sw + sg):.2f}" y="{legend_y - 11:.2f}" '
            f'width="{sw}" height="{sw}" rx="2" ry="2" fill="{LEVEL_COLORS[i]}"/>'
        )
    parts.append(
        f'  <text x="{lx + 36 + 5 * (sw + sg) + 4:.2f}" y="{legend_y:.2f}" '
        f'font-family="{FONT}" font-size="11" font-weight="600" fill="{GRAPH_LABEL}">More</text>'
    )
    parts.append("</svg>")
    _ = restricted  # reserved for callers via build(); kept out of the SVG
    return "\n".join(parts) + "\n"


# ---------------------------------------------------------------------------
# CLI, mock, tests
# ---------------------------------------------------------------------------

def load_mock(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def build(profile: dict[str, Any], now: datetime | None = None) -> tuple[str, dict[str, Any]]:
    now = now or datetime.now(timezone.utc)
    commits, restricted = sum_commits(profile.get("commitYears") or [])
    if "commitTotal" in profile and not profile.get("commitYears"):
        commits = int(profile["commitTotal"])
    meta = {
        "createdAt": profile["createdAt"],
        "repos": int(profile["repositoryCount"]),
        "commits": commits,
        "restricted": restricted,
        "joined": years_ago_phrase(parse_iso(profile["createdAt"]), now),
        "days": len(profile.get("calendar") or []),
    }
    return render_svg(profile, now), meta


def self_test() -> None:
    created = parse_iso("2024-05-07T10:34:37Z")
    now = parse_iso("2026-11-24T10:00:00Z")
    wins = year_windows(created, now)
    assert len(wins) == 3, wins
    assert wins[0][0] == created
    assert wins[0][1].year == 2024 and wins[0][1].month == 12
    assert wins[1][0] == datetime(2025, 1, 1, tzinfo=timezone.utc)
    assert wins[1][1].year == 2025 and wins[1][1].month == 12
    assert wins[2][0] == datetime(2026, 1, 1, tzinfo=timezone.utc)
    assert wins[2][1] == now
    for i in range(len(wins) - 1):
        assert wins[i][1] < wins[i + 1][0]
    assert years_ago_phrase(created, now) == "Joined GitHub 2 years ago"
    assert years_ago_phrase(parse_iso("2025-11-24T00:00:00Z"), now) == "Joined GitHub 1 year ago"
    assert years_ago_phrase(parse_iso("2026-09-24T00:00:00Z"), now) == "Joined GitHub 2 months ago"

    commits, restricted = sum_commits(
        [
            {"totalCommitContributions": 100, "restrictedContributionsCount": 50},
            {"totalCommitContributions": 200, "restrictedContributionsCount": 7},
        ]
    )
    assert commits == 300, "restricted counts must not be added to commits"
    assert restricted == 57

    fixture_path = SCRIPT_DIR / "testdata" / "mock_profile.json"
    profile = load_mock(fixture_path)
    svg, meta = build(profile, now=now)
    assert meta["repos"] == 30
    assert meta["commits"] == 2155
    assert meta["restricted"] == 0
    assert "2 years ago" in meta["joined"]
    assert "Adam Jašúrek" in svg
    assert "2,155" in svg
    assert "2 years ago" in svg
    assert ">30<" in svg or ">30</text>" in svg
    assert YELLOW in svg and LIME in svg and PINK in svg
    assert GRAPH_BG in svg
    assert LEVEL_COLORS[0] == "#161b22"
    assert LEVEL_COLORS[4] == "#39d353"
    assert all(c in svg for c in LEVEL_COLORS.values())
    assert GRAPH_LABEL in svg
    for banned in ("rajwood", "arealmorkov", "dlabanec", "foodtrucky", "plzenskydvur"):
        assert banned.lower() not in svg.lower()
    assert "<script" not in svg
    days = normalize_calendar(profile["calendar"])
    assert len(days) % 7 == 0
    assert date.fromisoformat(days[0]["date"]).weekday() == 6
    assert date.fromisoformat(days[-1]["date"]).weekday() == 5
    print("self-test ok:", meta)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Generate a neo-brutalist GitHub profile card SVG."
    )
    parser.add_argument(
        "--mock",
        nargs="?",
        const=str(SCRIPT_DIR / "testdata" / "mock_profile.json"),
        help="Load a mock JSON profile instead of calling the API",
    )
    parser.add_argument("--output", "-o", default=str(DEFAULT_OUT))
    parser.add_argument("--login", default=os.environ.get("GITHUB_LOGIN", LOGIN_DEFAULT))
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--dump-json", help="Write the fetched/mock profile JSON to this path")
    args = parser.parse_args(argv)

    if args.self_test:
        self_test()
        return 0

    now = datetime.now(timezone.utc)
    if args.mock:
        profile = load_mock(Path(args.mock))
        print(f"loaded mock profile from {args.mock}", file=sys.stderr)
    else:
        token = os.environ.get("METRICS_TOKEN") or os.environ.get("GITHUB_TOKEN")
        if not token:
            raise SystemExit("Set METRICS_TOKEN (or GITHUB_TOKEN) or pass --mock")
        profile = fetch_profile(token, args.login, now)

    if args.dump_json:
        Path(args.dump_json).write_text(json.dumps(profile, indent=2), encoding="utf-8")

    svg, meta = build(profile, now=now)
    if meta["restricted"]:
        print(
            f"warning: {meta['restricted']} restricted (hidden private) contributions "
            f"were reported and are NOT included in the commit total of {meta['commits']}",
            file=sys.stderr,
        )
    out = Path(args.output)
    out.write_text(svg, encoding="utf-8")
    print(f"wrote {out} ({out.stat().st_size} bytes)", file=sys.stderr)
    print(json.dumps(meta, default=str), file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
