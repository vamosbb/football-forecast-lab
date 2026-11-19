"""Conservative ESPN fixture sync. Never treats missing information as verified."""
import argparse
import csv
import datetime as dt
import difflib
import json
import os
import re
import tempfile
import unicodedata
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent
DATA = ROOT / 'football_model_data.json'
OVERRIDES = ROOT / 'referee_overrides.csv'
BASE = 'https://site.api.espn.com/apis/site/v2/sports/soccer/{league}/scoreboard'
UTC = dt.timezone.utc


def norm(value):
    value = unicodedata.normalize('NFKD', str(value or '').casefold())
    value = ''.join(c for c in value if not unicodedata.combining(c))
    return ' '.join(re.findall(r'[a-z0-9]+', value))


ALIASES = {
    'de.1': {'bayern munich': 'FC Bayern München', 'bayern munchen': 'FC Bayern München',
             'fc cologne': '1. FC Köln', 'koln': '1. FC Köln', 'cologne': '1. FC Köln'},
    'en.1': {'man city': 'Manchester City FC', 'man united': 'Manchester United FC',
             'brighton': 'Brighton & Hove Albion FC', 'spurs': 'Tottenham Hotspur FC'},
    'es.1': {'atletico madrid': 'Club Atlético de Madrid', 'barcelona': 'FC Barcelona',
             'real madrid': 'Real Madrid CF'},
}


def match_team(name, league, known):
    target = norm(name)
    if not target:
        return None
    exact = [k for k in known if norm(k) == target]
    if len(exact) == 1:
        return exact[0]
    alias = ALIASES.get(league, {}).get(target)
    if alias in known:
        return alias
    # High threshold, unambiguous only; never silently guess a team.
    scored = sorted(((difflib.SequenceMatcher(None, target, norm(k)).ratio(), k)
                     for k in known), reverse=True)
    if scored and scored[0][0] >= .94 and (len(scored) == 1 or scored[0][0] - scored[1][0] >= .09):
        return scored[0][1]
    return None


def match_referee(name, known):
    target = norm(name)
    exact = [k for k in known if norm(k) == target]
    if len(exact) == 1:
        return exact[0]
    parts = target.split()
    if len(parts) == 2:
        matches = []
        for k in known:
            kp = norm(k).split()
            if len(kp) >= 2 and ((kp[-1] == parts[0] and kp[0].startswith(parts[1])) or
                                  (kp[-1] == parts[1] and kp[0].startswith(parts[0]))):
                matches.append(k)
        if len(matches) == 1:
            return matches[0]
    return None


def load_overrides():
    result = {}
    if not OVERRIDES.exists():
        return result
    with OVERRIDES.open(encoding='utf-8-sig', newline='') as stream:
        for row in csv.DictReader(stream):
            if not all(row.get(k, '').strip() for k in ('league', 'home', 'away', 'referee', 'verified_source')):
                continue
            # Date is required to prevent an old appointment being reused in a later match.
            if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', row.get('date', '')):
                continue
            key = (row['league'].strip(), norm(row['home']), norm(row['away']), row['date'])
            result[key] = row
    return result


def event_referee(event):
    for comp in event.get('competitions') or []:
        for official in comp.get('officials') or []:
            role = norm(official.get('position', {}).get('name', '') if isinstance(official.get('position'), dict)
                        else official.get('position', '') or official.get('type', ''))
            if role and any(x in role for x in ('assistant', 'video', 'fourth', 'var')):
                continue
            name = official.get('displayName') or official.get('fullName') or official.get('name')
            if name:
                return name
    return None


def parse_time(value):
    return dt.datetime.fromisoformat(value.replace('Z', '+00:00')).astimezone(UTC)


def fetch(session, league, start, end):
    # One request per league instead of 9/day; logs actual HTTP status on failure.
    response = session.get(BASE.format(league=league), params={
        'dates': f'{start:%Y%m%d}-{end:%Y%m%d}', 'limit': 500
    }, timeout=20, headers={'Accept': 'application/json', 'User-Agent': 'FootballForecastLab/1.1'})
    response.raise_for_status()
    body = response.json()
    if not isinstance(body.get('events'), list):
        raise ValueError('ESPN response has no events list')
    return body['events']


def sync(path=DATA, days=14, network=True):
    path = Path(path)
    original = json.loads(path.read_text(encoding='utf-8'))
    data = json.loads(json.dumps(original))
    now = dt.datetime.now(UTC)
    start, end = now.date(), (now + dt.timedelta(days=days)).date()
    warnings, status_by_league, matched_events = [], {}, {}
    overrides = load_overrides()
    existing = data.get('fixtures', [])

    if network:
        with requests.Session() as session:
            for league, league_data in data['leagues'].items():
                try:
                    events = fetch(session, league, start, end)
                    status_by_league[league] = 'ok'
                except (requests.RequestException, ValueError, TypeError) as exc:
                    status_by_league[league] = 'failed'
                    status = getattr(getattr(exc, 'response', None), 'status_code', None)
                    warnings.append(f'{league}: ESPN fetch failed ({type(exc).__name__}, HTTP {status or "unknown"})')
                    continue
                for event in events:
                    try:
                        competition = (event.get('competitions') or [])[0]
                        sides = competition.get('competitors') or []
                        home = next((x for x in sides if x.get('homeAway') == 'home'), None)
                        away = next((x for x in sides if x.get('homeAway') == 'away'), None)
                        if not home or not away:
                            continue
                        h_name = home.get('team', {}).get('displayName') or home.get('team', {}).get('name')
                        a_name = away.get('team', {}).get('displayName') or away.get('team', {}).get('name')
                        h = match_team(h_name, league, league_data['teams'])
                        a = match_team(a_name, league, league_data['teams'])
                        if not h or not a:
                            warnings.append(f'{league}: unmatched teams {h_name!r} / {a_name!r}')
                            continue
                        kickoff = parse_time(event['date'])
                        if not start <= kickoff.date() <= end:
                            continue
                        key = (league, h, a, kickoff.date().isoformat())
                        matched_events[key] = event
                    except (KeyError, IndexError, TypeError, ValueError) as exc:
                        warnings.append(f'{league}: malformed event {event.get("id", "?")}: {type(exc).__name__}')

    verified = 0
    updated_existing = 0
    added = 0
    output = []
    for fixture in existing:
        fixture = dict(fixture)
        league = fixture['lg']
        try:
            kickoff = parse_time(fixture['t'])
        except (KeyError, ValueError, TypeError):
            warnings.append(f'{league}: invalid fixture kickoff: {fixture.get("t")}')
            output.append(fixture)
            continue
        key = (league, fixture['h'], fixture['a'], kickoff.date().isoformat())
        event = matched_events.pop(key, None)
        # Rescheduled games: only match within 48h, same league/teams, uniquely.
        if event is None and status_by_league.get(league) == 'ok':
            options = [(k, ev) for k, ev in matched_events.items()
                       if k[:3] == key[:3] and abs((parse_time(ev['date']) - kickoff).total_seconds()) <= 48 * 3600]
            if len(options) == 1:
                other_key, event = options[0]
                matched_events.pop(other_key)
        override = overrides.get(key)
        raw_ref = override['referee'] if override else event_referee(event) if event else None
        if raw_ref:
            known = data['leagues'][league].get('refs', {})
            matched_ref = match_referee(raw_ref, known)
            fixture['ref'] = matched_ref or raw_ref
            fixture['ref_status'] = 'appointed'
            fixture['ref_source'] = override['verified_source'] if override else 'ESPN event officials'
            fixture['ref_stats_available'] = bool(matched_ref)
            verified += 1
        elif fixture.get('ref_status') != 'appointed':
            # Legacy refs are not verified; never label them confirmed.
            fixture['ref_status'] = 'previously_recorded_unverified' if fixture.get('ref') else 'not_confirmed'
            fixture['ref_source'] = fixture.get('ref_source') or ('existing JSON' if fixture.get('ref') else None)
            fixture['ref_stats_available'] = False
        if event:
            updated_existing += 1
            fixture['espn_event_id'] = str(event.get('id', ''))
            state = (event.get('status') or {}).get('type') or {}
            if state.get('completed') is True or state.get('name') in ('STATUS_FINAL', 'STATUS_FULL_TIME'):
                continue
            fixture['t'] = parse_time(event['date']).strftime('%Y-%m-%dT%H:%M:%SZ')
        # Never delete old fixtures just because ESPN is unreachable. Drop only past ones.
        if parse_time(fixture['t']) >= now:
            output.append(fixture)

    # New ESPN events are appended only for teams that exist in model data.
    for (league, home, away, date), event in matched_events.items():
        state = (event.get('status') or {}).get('type') or {}
        if state.get('completed') is True or state.get('name') in ('STATUS_FINAL', 'STATUS_FULL_TIME'):
            continue
        kickoff = parse_time(event['date'])
        if kickoff < now:
            continue
        raw_ref = event_referee(event)
        known = data['leagues'][league].get('refs', {})
        matched_ref = match_referee(raw_ref, known) if raw_ref else None
        fixture = {'lg': league, 't': kickoff.strftime('%Y-%m-%dT%H:%M:%SZ'),
                   'h': home, 'a': away, 'espn_event_id': str(event.get('id', '')),
                   'ref_status': 'appointed' if raw_ref else 'not_confirmed',
                   'ref_source': 'ESPN event officials' if raw_ref else None,
                   'ref_stats_available': bool(matched_ref)}
        if raw_ref:
            fixture['ref'] = matched_ref or raw_ref
            verified += 1
        output.append(fixture)
        added += 1

    output.sort(key=lambda x: x.get('t', ''))
    data['fixtures'] = output
    data['updated'] = now.strftime('%Y-%m-%dT%H:%M:%SZ')
    data['sync_meta'] = {
        'last_attempt': data['updated'], 'source': 'ESPN public scoreboard (unofficial)',
        'league_fetch_status': status_by_league, 'warnings': warnings[:250],
        'warnings_count': len(warnings), 'events_matched': updated_existing,
        'fixtures_added': added, 'referees_verified_this_run': verified,
        'team_statistics_refreshed': False,
        'note': 'Goal, corner, card and referee historical rates are NOT refreshed by this script.'
    }
    # Atomic write. No ever-growing backup committed to Git.
    fd, tmp = tempfile.mkstemp(prefix='.football-data-', suffix='.json', dir=str(path.parent))
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.write('\n')
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    result = {'fixtures': len(output), 'added': added, 'matched': updated_existing,
              'warnings': len(warnings), 'referees_verified': verified,
              'league_fetch_status': status_by_league}
    print(json.dumps(result, ensure_ascii=False))
    for warning in warnings[:15]:
        print('WARNING:', warning)
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--offline', action='store_true')
    parser.add_argument('--days', type=int, default=14)
    args = parser.parse_args()
    sync(days=max(1, min(args.days, 30)), network=not args.offline)
