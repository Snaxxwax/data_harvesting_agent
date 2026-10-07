"""Fail the build on a tool/source contract change instead of silently losing diagnostics."""

import ast
import importlib.metadata
import sys
from pathlib import Path

root = Path(sys.argv[1])
name, version = sys.argv[2:4]
actual = importlib.metadata.distribution(name).version
if actual != version:
    raise SystemExit(f"unsupported {name} build: {actual}")


def patch(rel, edits):
    file = root / rel
    text = file.read_text()
    for before, after in edits:
        if text.count(before) != 1:
            raise SystemExit(f"{rel}: structured hook anchor changed: {before[:70]}")
        text = text.replace(before, after)
    ast.parse(text)
    file.write_text(text)


if name == "maigret":
    patch(
        "checking.py",
        [
            (
                "                    all_results.update([result])",
                "                    all_results.update([result])\n                    from harvest_native import maigret_result\n                    maigret_result(result)",
            )
        ],
    )
    patch(
        "maigret.py",
        [
            (
                "    site_data = get_top_sites_for_id(args.id_type)",
                "    site_data = get_top_sites_for_id(args.id_type)\n    from harvest_native import maigret_plan\n    maigret_plan(site_data, db_file)",
            )
        ],
    )
elif name == "ghunt":
    patch(
        "modules/email.py",
        [
            (
                "    ghunt_creds = await auth.load_and_auth(as_client)",
                "    from harvest_native import ghunt_result\n    for check in ('authentication', 'profile', 'play_games_search', 'play_games_detail', 'maps_stats', 'calendar'):\n        ghunt_result(check, 'unfinished', reason='scheduled sub-check')\n    try:\n        ghunt_creds = await auth.load_and_auth(as_client)\n    except Exception:\n        ghunt_result('authentication', 'blocked', reason='credential authentication failed')\n        raise\n    ghunt_result('authentication', 'found', reason='credential authentication succeeded')",
            ),
            (
                "    if not is_found:",
                "    from harvest_native import ghunt_result\n    ghunt_result('profile', 'found' if is_found else 'absent', target if is_found else None)\n    if not is_found:\n        for check in ('play_games_search', 'play_games_detail', 'maps_stats', 'calendar'):\n            ghunt_result(check, 'skipped', reason='profile absent; dependent check inapplicable')",
            ),
            (
                "    if player_results:",
                "    ghunt_result('play_games_search', 'found' if player_results else 'absent', player_results)\n    if not player_results:\n        ghunt_result('play_games_detail', 'skipped', reason='no player candidate')\n    if player_results:",
            ),
            (
                "        playgames.output(player)",
                "        ghunt_result('play_games_detail', 'found' if player else 'errored', player)\n        playgames.output(player)",
            ),
            (
                "    gmaps.output(err, stats, target.personId)",
                "    ghunt_result('maps_stats', 'errored' if err else ('found' if stats else 'absent'), stats, 'native Maps error' if err else '')\n    gmaps.output(err, stats, target.personId)",
            ),
            (
                "    if cal_found:",
                "    ghunt_result('calendar', 'found' if cal_found else 'absent', {'details': calendar, 'events': calendar_events} if cal_found else None)\n    if cal_found:",
            ),
        ],
    )
else:
    raise SystemExit("unknown tool")
