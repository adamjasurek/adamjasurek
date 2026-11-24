#!/usr/bin/env python3
"""Generate neo-brutalist GitHub profile cards (profile-card.svg and profile-card-dark.svg).

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

The SVG never lists repository names — only aggregate counts
(years on GitHub, repos, and commits). The contribution calendar is
left to GitHub's native profile graph, so this card does not fetch or
draw one.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape

LOGIN_DEFAULT = "adamjasurek"
GRAPHQL_URL = "https://api.github.com/graphql"
SCRIPT_DIR = Path(__file__).resolve().parent
ROOT = SCRIPT_DIR.parent
DEFAULT_OUT = ROOT / "profile-card.svg"
NAME_PATHS_FILE = SCRIPT_DIR / "name_paths.json"

# Palette (matches the profile banner) — used for name plate and stat tiles
PAPER = "#FFF8EB"
INK = "#111111"
YELLOW = "#FFD60A"
LIME = "#C6FF4D"
PINK = "#FF4D8D"

# Card chrome per theme. Shadows match the page: light grey on light,
# near-black on dark. Inner block shadows use the same color.
THEMES = {
    "light": {
        "card_fill": "#FFF8EB",
        "card_stroke": "#111111",
        "card_shadow": "#d0d7de",
    },
    "dark": {
        "card_fill": "#0d1117",
        "card_stroke": "#e6edf3",
        "card_shadow": "#000000",
    },
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

    return {
        "login": login,
        "createdAt": user["createdAt"],
        "repositoryCount": int(user["repositories"]["totalCount"]),
        "commitYears": commit_years,
    }


# ---------------------------------------------------------------------------
# SVG
# ---------------------------------------------------------------------------

def path_els(paths: list[str], fill: str = INK) -> str:
    return "\n".join(f'<path fill="{fill}" d="{d}"/>' for d in paths)


def render_svg(profile: dict[str, Any], now: datetime, theme: str = "light") -> str:
    name = load_name_paths()
    name_paths = name["paths"]
    name_bbox = name["bbox"]

    created = parse_iso(profile["createdAt"])
    repos = int(profile["repositoryCount"])
    commits, restricted = sum_commits(profile.get("commitYears") or [])
    if "commitTotal" in profile and not profile.get("commitYears"):
        commits = int(profile["commitTotal"])
    joined = years_ago_phrase(created, now)

    if theme not in THEMES:
        raise ValueError(f"unknown theme: {theme!r}")
    pal = THEMES[theme]

    # Name plate + three stat tiles; no contribution graph.
    # Top/side padding matches the previous card (24px from the card edge).
    vw, vh = 1200, 262
    card_x, card_y = 12, 12
    card_w, card_h = 1156, 222
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
        f"{joined}. {format_int(repos)} repos. {format_int(commits)} commits."
    )
    parts.append(f'  <title id="title">{escape(title)}</title>')
    parts.append(f'  <desc id="desc">{escape(desc)}</desc>')
    parts.append(
        f'  <rect x="{card_x + shadow}" y="{card_y + shadow}" '
        f'width="{card_w}" height="{card_h}" fill="{pal["card_shadow"]}"/>'
    )
    parts.append(
        f'  <rect x="{card_x}" y="{card_y}" width="{card_w}" height="{card_h}" '
        f'fill="{pal["card_fill"]}" stroke="{pal["card_stroke"]}" stroke-width="8"/>'
    )
    parts.append(
        f'  <rect x="{plate_x + 6}" y="{plate_y + 6}" width="{plate_w}" '
        f'height="{plate_h}" fill="{pal["card_shadow"]}"/>'
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
            f'height="{tile_h}" fill="{pal["card_shadow"]}"/>'
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

    parts.append("</svg>")
    _ = restricted  # reserved for callers via build(); kept out of the SVG
    return "\n".join(parts) + "\n"


# ---------------------------------------------------------------------------
# CLI, mock, tests
# ---------------------------------------------------------------------------

def load_mock(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def build(profile: dict[str, Any], now: datetime | None = None, theme: str = "light") -> tuple[str, dict[str, Any]]:
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
    }
    return render_svg(profile, now, theme=theme), meta


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
    light, meta = build(profile, now=now, theme="light")
    dark, meta_d = build(profile, now=now, theme="dark")
    assert meta == meta_d
    assert meta["repos"] == 30
    assert meta["commits"] == 2155
    assert meta["restricted"] == 0
    assert "2 years ago" in meta["joined"]
    assert "days" not in meta
    for svg in (light, dark):
        assert "Adam Jašúrek" in svg
        assert "2,155" in svg
        assert "2 years ago" in svg
        assert ">30<" in svg or ">30</text>" in svg
        assert YELLOW in svg and LIME in svg and PINK in svg
        assert "<script" not in svg
        assert 'viewBox="0 0 1200 262"' in svg
        assert 'height="262"' in svg
        # No contribution graph, legend, or leftover calendar copy
        assert "Contribution graph" not in svg
        assert ">Less<" not in svg and ">More<" not in svg
        assert ">Mon<" not in svg and ">Wed<" not in svg and ">Fri<" not in svg
        for banned in ("rajwood", "arealmorkov", "dlabanec", "foodtrucky", "plzenskydvur"):
            assert banned.lower() not in svg.lower()
        # Official GitHub contribution greens must not appear
        for green in (
            "#ebedf0", "#9be9a8", "#40c463", "#30a14e", "#216e39",
            "#161b22", "#0e4429", "#006d32", "#26a641", "#39d353",
        ):
            assert green not in svg
    # Theme chrome
    assert THEMES["light"]["card_fill"] in light
    assert THEMES["dark"]["card_fill"] in dark
    # Shadows follow the theme: light grey on light, near-black on dark
    assert THEMES["light"]["card_shadow"] == "#d0d7de"
    assert THEMES["dark"]["card_shadow"] == "#000000"
    assert THEMES["light"]["card_shadow"] in light
    assert THEMES["dark"]["card_shadow"] in dark
    assert THEMES["dark"]["card_shadow"] not in light
    assert "#c9d1d9" not in dark  # previous inverted (light) shadow
    # Count theme shadows: outer card + name plate + 3 tiles
    assert light.count(THEMES["light"]["card_shadow"]) == 5
    assert dark.count('fill="#000000"') == 5
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

    svg_light, meta = build(profile, now=now, theme="light")
    svg_dark, _ = build(profile, now=now, theme="dark")
    if meta["restricted"]:
        print(
            f"warning: {meta['restricted']} restricted (hidden private) contributions "
            f"were reported and are NOT included in the commit total of {meta['commits']}",
            file=sys.stderr,
        )
    out_light = Path(args.output)
    if out_light.suffix:
        out_dark = out_light.with_name(out_light.stem + "-dark" + out_light.suffix)
    else:
        out_dark = Path(str(out_light) + "-dark")
    out_light.write_text(svg_light, encoding="utf-8")
    out_dark.write_text(svg_dark, encoding="utf-8")
    print(f"wrote {out_light} ({out_light.stat().st_size} bytes)", file=sys.stderr)
    print(f"wrote {out_dark} ({out_dark.stat().st_size} bytes)", file=sys.stderr)
    print(json.dumps(meta, default=str), file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
