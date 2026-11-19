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
ESPN_LEAGUES = {'en.1': 'eng.1', 'es.1': 'esp.1', 'de.1': 'ger.1', 'it.1': 'ita.1', 'fr.1': 'fra.1'}
UTC = dt.timezone.utc


def norm(value):
    value = unicodedata.normalize('NFKD', str(value or '').casefold())
    value = ''.join(c for c in value if not unicodedata.combining(c))
    return ' '.join(re.findall(r'[a-z0-9]+', value))


# ESPN display names mapped to the exact team keys in football_model_data.json.
# Never create a team or invent its statistics when the name is unknown.
ALIASES = {
    'en.1': {
        'manchester city': 'Manchester City FC', 'manchester united': 'Manchester United FC',
        'tottenham hotspur': 'Tottenham Hotspur FC', 'brighton hove albion': 'Brighton & Hove Albion FC',
        'bournemouth': 'AFC Bournemouth', 'afc bournemouth': 'AFC Bournemouth',
        'hull city': 'Hull City AFC', 'sunderland': 'Sunderland AFC',
        'coventry city': 'Coventry City FC', 'ipswich town': 'Ipswich Town FC',
        'nottingham forest': 'Nottingham Forest FC', 'newcastle united': 'Newcastle United FC',
        'leeds united': 'Leeds United FC', 'crystal palace': 'Crystal Palace FC',
        'aston villa': 'Aston Villa FC', 'west ham': None,
        'man city': 'Manchester City FC', 'man united': 'Manchester United FC',
        'spurs': 'Tottenham Hotspur FC',
    },
    'es.1': {
        'atletico madrid': 'Club Atlético de Madrid', 'real betis': 'Real Betis Balompié',
        'real sociedad': 'Real Sociedad de Fútbol', 'deportivo la coruna': 'RC Deportivo La Coruña',
        'racing santander': 'Real Racing Club de Santander', 'celta vigo': 'RC Celta de Vigo',
        'alaves': 'Deportivo Alavés', 'espanyol': 'RCD Espanyol de Barcelona',
        'rayo vallecano': 'Rayo Vallecano de Madrid', 'osasuna': 'CA Osasuna',
        'malaga': 'Málaga CF', 'barcelona': 'FC Barcelona', 'real madrid': 'Real Madrid CF',
        'deportivo': 'RC Deportivo La Coruña',
    },
    'de.1': {
        'bayern munich': 'FC Bayern München', 'bayern munchen': 'FC Bayern München',
        'bayer leverkusen': 'Bayer 04 Leverkusen', 'werder bremen': 'SV Werder Bremen',
        'hoffenheim': 'TSG 1899 Hoffenheim', 'mainz': '1. FSV Mainz 05',
        'mainz 05': '1. FSV Mainz 05', 'paderborn': 'SC Paderborn 07',
        'cologne': '1. FC Köln', 'koln': '1. FC Köln',
        'union berlin': '1. FC Union Berlin', 'schalke': 'FC Schalke 04',
        'monchengladbach': 'Borussia Mönchengladbach', 'gladbach': 'Borussia Mönchengladbach',
        'elversberg': 'SV 07 Elversberg', 'freiburg': 'SC Freiburg',
        'augsburg': 'FC Augsburg', 'stuttgart': 'VfB Stuttgart',
        'fc cologne': '1. FC Köln', 'eintracht frankfurt': 'Eintracht Frankfurt',
        'hamburg sv': 'Hamburger SV', 'tsg hoffenheim': 'TSG 1899 Hoffenheim',
        'sv elversberg': 'SV 07 Elversberg', 'sc freiburg': 'SC Freiburg',
    },
    'it.1': {
        'lazio': 'SS Lazio', 'inter': 'FC Internazionale Milano',
        'internazionale': 'FC Internazionale Milano', 'roma': 'AS Roma',
        'milan': 'AC Milan', 'frosinone': 'Frosinone Calcio',
        'juventus': 'Juventus FC', 'bologna': 'Bologna FC 1909',
        'cagliari': 'Cagliari Calcio', 'napoli': 'SSC Napoli',
        'torino': 'Torino FC', 'atalanta': 'Atalanta BC',
        'genoa': 'Genoa CFC', 'lecce': 'US Lecce',
        'fiorentina': 'ACF Fiorentina', 'parma': 'Parma Calcio 1913',
        'sassuolo': 'US Sassuolo Calcio', 'monza': 'AC Monza',
        'venezia': 'Venezia FC', 'udinese': 'Udinese Calcio',
        'como': 'Como 1907',
    },
    'fr.1': {
        'psg': 'Paris Saint-Germain FC', 'paris saint germain': 'Paris Saint-Germain FC',
        'lille': 'Lille OSC', 'marseille': 'Olympique de Marseille',
        'lyon': 'Olympique Lyonnais', 'nice': 'OGC Nice',
        'monaco': 'AS Monaco FC', 'rennes': 'Stade Rennais FC 1901',
        'brest': 'Stade Brestois 29', 'lorient': 'FC Lorient',
        'lens': 'Racing Club de Lens', 'toulouse': 'Toulouse FC',
        'strasbourg': 'RC Strasbourg Alsace', 'auxerre': 'AJ Auxerre',
        'le mans': 'Le Mans FC', 'angers': 'Angers SCO',
        'troyes': 'ES Troyes AC', 'le havre': 'Le Havre AC',
        'stade rennais': 'Stade Rennais FC 1901',
    },
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
    # Strip only common football-club designators. Match ONLY if unique.
    stop = {'fc', 'cf', 'afc', 'sc', 'ac', 'as', 'ss', 'rc', 'rcd', 'ca',
            'ud', 'us', 'sv', 'vfb', 'tsg', 'ogc', 'osc', 'bc', 'cfc', 'sco'}
    def simplified(value):
        parts = norm(value).split()
        return ' '.join(p for p in parts if p not in stop)
    clean = simplified(name)
    matches = [k for k in known if simplified(k) == clean]
    if len(matches) == 1:
        return matches[0]
    # Fuzzy match remains deliberately conservative to avoid wrong statistics.
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
    """Try ESPN scoreboard without query params first, then date-filtered calls.

    The public ESPN endpoint is undocumented and can reject parameters with 400.
    A no-parameter response may cover only a limited fixture window; it must
    never be presented as proof of complete 14-day coverage.
    """
    url = BASE.format(league=ESPN_LEAGUES.get(league, league))
    headers = {'Accept': 'application/json', 'User-Agent': 'Mozilla/5.0'}
    events_by_id = {}
    errors = []
    successful_dates = 0

    def ingest(response, label):
        response.raise_for_status()
        body = response.json()
        if not isinstance(body, dict) or not isinstance(body.get('events'), list):
            raise ValueError(f'ESPN missing events: {label}')
        for event in body['events']:
            if isinstance(event, dict):
                key = str(event.get('id') or (event.get('date'), len(events_by_id)))
                events_by_id[key] = event

    # The bare endpoint is the least restrictive diagnostic request.
    try:
        ingest(session.get(url, timeout=20, headers=headers), 'unfiltered')
    except (requests.RequestException, ValueError) as exc:
        status = getattr(getattr(exc, 'response', None), 'status_code', None)
        errors.append(f'unfiltered: {type(exc).__name__} HTTP {status or "unknown"}')

    day = start
    while day <= end:
        try:
            ingest(session.get(url, params={'dates': day.strftime('%Y%m%d')},
                               timeout=20, headers=headers), str(day))
            successful_dates += 1
        except (requests.RequestException, ValueError) as exc:
            status = getattr(getattr(exc, 'response', None), 'status_code', None)
            errors.append(f'{day}: {type(exc).__name__} HTTP {status or "unknown"}')
            # Repeated HTTP 400 is likely a rejected query format, not a
            # day-specific problem. Avoid 14 identical requests.
            if status == 400:
                break
        day += dt.timedelta(days=1)

    if not events_by_id and successful_dates == 0:
        raise RuntimeError('; '.join(errors[:3]) or 'No ESPN data returned')
    return list(events_by_id.values()), successful_dates, errors


def enrich_completed(session, league, event):
    """Best-effort ESPN match summary for corners/cards/officials."""
    eid = event.get('id')
    if not eid:
        return event
    url = ('https://site.api.espn.com/apis/site/v2/sports/soccer/'
           + ESPN_LEAGUES.get(league, league) + '/summary')
    try:
        response = session.get(url, params={'event': str(eid)}, timeout=12,
                               headers={'Accept': 'application/json', 'User-Agent': 'Mozilla/5.0'})
        response.raise_for_status()
        body = response.json()
        # Header can contain appointed officials; keep original if absent.
        header = body.get('header') or {}
        comps = header.get('competitions') or []
        if comps and comps[0].get('officials'):
            event.setdefault('competitions', [{}])[0]['officials'] = comps[0]['officials']
        # Boxscore teams may contain numeric team-level stats.
        teams = (body.get('boxscore') or {}).get('teams') or []
        sides = (event.get('competitions') or [{}])[0].get('competitors') or []
        for side in sides:
            sid = str(side.get('team', {}).get('id', ''))
            for item in teams:
                if sid and str(item.get('team', {}).get('id', '')) == sid and item.get('statistics'):
                    side['statistics'] = item['statistics']
                    break
    except (requests.RequestException, ValueError, KeyError, TypeError):
        pass  # Unavailable stats are never treated as zero.
    return event


def stat_value(side, *keys):
    """Only accept a real ESPN reported number; missing is NOT zero."""
    wanted = {norm(k).replace(' ', '') for k in keys}
    for stat in side.get('statistics') or []:
        label = norm(stat.get('name') or stat.get('label') or stat.get('abbreviation')).replace(' ', '')
        if label in wanted:
            raw = stat.get('value', stat.get('displayValue'))
            try:
                return float(str(raw).replace(',', ''))
            except (ValueError, TypeError):
                return None
    return None


def update_finished(data, league, event, seen, warnings):
    """Incrementally apply only verified post-baseline final results, once per event."""
    event_id = str(event.get('id') or '')
    if not event_id or event_id in seen:
        return False, False, False
    lg = data['leagues'][league]
    state = (event.get('status') or {}).get('type') or {}
    if state.get('completed') is not True and state.get('name') not in ('STATUS_FINAL', 'STATUS_FULL_TIME'):
        return False, False, False
    try:
        event_date = parse_time(event['date']).date()
        baseline = dt.date.fromisoformat(data.setdefault('sync_baseline_dates', {}).setdefault(league, lg['asof']))
        if event_date <= baseline:
            return False, False, False
        comp = (event.get('competitions') or [])[0]
        sides = comp.get('competitors') or []
        home = next(x for x in sides if x.get('homeAway') == 'home')
        away = next(x for x in sides if x.get('homeAway') == 'away')
        h = match_team(home.get('team', {}).get('displayName'), league, lg['teams'])
        a = match_team(away.get('team', {}).get('displayName'), league, lg['teams'])
        if not h or not a or h == a:
            return False, False, False
        # Score must be a final integer, never inferred from an absent field.
        hs, aas = home.get('score'), away.get('score')
        if isinstance(hs, dict): hs = hs.get('value', hs.get('displayValue'))
        if isinstance(aas, dict): aas = aas.get('value', aas.get('displayValue'))
        hg, ag = int(hs), int(aas)
        if hg < 0 or ag < 0 or str(hs).strip() not in (str(hg), f'{hg}.0') or str(aas).strip() not in (str(ag), f'{ag}.0'):
            return False, False, False
    except (KeyError, ValueError, TypeError, IndexError, StopIteration):
        warnings.append(f'{league}: incomplete final score event {event_id}')
        return False, False, False
    # Goals are authoritative from final scoreboard. Existing team P/F/A
    # are a historical baseline; event IDs prevent duplicate additions.
    for team, gf, ga in ((h, hg, ag), (a, ag, hg)):
        t = lg['teams'][team]
        t['P'] = int(t.get('P', 0)) + 1
        t['F'] = int(t.get('F', 0)) + gf
        t['A'] = int(t.get('A', 0)) + ga
        # Preserve original prior shrinkage; do not fabricate missing games.
        k = max(1, int(data.get('k', 12)))
        avg = max(.1, (float(lg['avgH']) + float(lg['avgA'])) / 2)
        t['att'] = round((t['F'] + k * avg * float(t.get('pa', 1))) / (t['P'] + k) / avg, 4)
        t['dfn'] = round((t['A'] + k * avg * float(t.get('pd', 1))) / (t['P'] + k) / avg, 4)
    lg['asof'] = max(lg['asof'], event_date.isoformat())
    # ESPN may provide corners/cards in competitor statistics; never replace
    # previous averages with missing values or an assumed zero.
    corners = [stat_value(x, 'cornerKicks', 'corners', 'corner kicks') for x in (home, away)]
    yellows = [stat_value(x, 'yellowCards', 'yellow cards') for x in (home, away)]
    reds = [stat_value(x, 'redCards', 'red cards') for x in (home, away)]
    corners_done = all(v is not None for v in corners)
    cards_done = all(v is not None for v in yellows) and all(v is not None for v in reds)
    for i, team in enumerate((h, a)):
        t = lg['teams'][team]
        if corners_done:
            cr = t.setdefault('cr', {})
            n = int(cr.get('n', 0))
            cr['f'] = round((float(cr.get('f', 0)) * n + corners[i]) / (n + 1), 3)
            cr['ag'] = round((float(cr.get('ag', 0)) * n + corners[1-i]) / (n + 1), 3)
            cr['n'] = n + 1
            # Separate home/away sample counts, initialized conservatively.
            homekey = 'h' if i == 0 else 'a'
            countkey = '_home_n' if i == 0 else '_away_n'
            oldn = int(cr.get(countkey, n // 2))
            cr[homekey] = round((float(cr.get(homekey, 0)) * oldn + corners[i]) / (oldn + 1), 3)
            cr[countkey] = oldn + 1
        if cards_done:
            cd = t.setdefault('cd', {})
            cd['n'] = int(cd.get('n', 0)) + 1
            cd['y'] = int(cd.get('y', 0)) + int(yellows[i])
            cd['r'] = int(cd.get('r', 0)) + int(reds[i])
    # Update historical referee average only with an actual named referee and
    # observed card totals; new referee entries are allowed with n=1.
    if cards_done:
        name = event_referee(event)
        if name:
            refs = lg.setdefault('refs', {})
            key = match_referee(name, refs) or name
            ref = refs.setdefault(key, {'n': 0, 'c': 0, 'f': 1.0})
            n = int(ref.get('n', 0))
            count = sum(yellows) + sum(reds)
            ref['c'] = round((float(ref.get('c', 0)) * n + count) / (n + 1), 3)
            ref['n'] = n + 1
            ref['f'] = round((ref['c'] * ref['n'] + 5 * float(lg.get('ext', {}).get('rbar', 4))) /
                             (ref['n'] + 5) / max(.1, float(lg.get('ext', {}).get('rbar', 4))), 3)
    seen.add(event_id)
    return True, corners_done, cards_done


def collect_recent_matches(session, league, league_data, now, min_matches=10, max_lookback_days=210):
    """Collect actual latest N completed games per team, not N calendar days.

    Fetch older match days in weekly blocks until all modeled teams have N
    observations or the lookback limit is reached. Do not invent missing stats.
    """
    url = BASE.format(league=ESPN_LEAGUES.get(league, league))
    headers = {'Accept': 'application/json', 'User-Agent': 'Mozilla/5.0'}
    by_team = {team: [] for team in league_data['teams']}
    observed = set()
    errors = []
    today = now.date()

    def consume(events):
        for ev in events:
            eid = str(ev.get('id') or '')
            if not eid or eid in observed:
                continue
            observed.add(eid)
            status = (ev.get('status') or {}).get('type') or {}
            if status.get('completed') is not True and status.get('name') not in ('STATUS_FINAL', 'STATUS_FULL_TIME'):
                continue
            try:
                kickoff = parse_time(ev['date'])
                if kickoff > now:
                    continue
                comp = (ev.get('competitions') or [])[0]
                sides = comp.get('competitors') or []
                home = next(x for x in sides if x.get('homeAway') == 'home')
                away = next(x for x in sides if x.get('homeAway') == 'away')
                h = match_team(home.get('team', {}).get('displayName') or home.get('team', {}).get('name'), league, league_data['teams'])
                a = match_team(away.get('team', {}).get('displayName') or away.get('team', {}).get('name'), league, league_data['teams'])
                if not h or not a or h == a:
                    continue
                def score(side):
                    val = side.get('score')
                    if isinstance(val, dict):
                        val = val.get('value', val.get('displayValue'))
                    number = int(val)
                    if number < 0 or str(val).strip() not in (str(number), f'{number}.0'):
                        raise ValueError('invalid score')
                    return number
                hg, ag = score(home), score(away)
                # Store verifiable raw results. Enrichment is done for the
                # selected ten only, reducing unnecessary summary requests.
                for team, opponent, gf, ga, venue in ((h,a,hg,ag,'home'), (a,h,ag,hg,'away')):
                    by_team[team].append({'id': eid, 'date': kickoff.strftime('%Y-%m-%dT%H:%M:%SZ'),
                                          'opponent': opponent, 'venue': venue,
                                          'goals_for': gf, 'goals_against': ga})
            except (KeyError, IndexError, TypeError, ValueError, StopIteration):
                continue

    # Use weekly date ranges if ESPN accepts them. If not, fall back to
    # individual days. Stop when every team has at least ten actual games.
    for offset in range(0, max_lookback_days, 7):
        if all(len(v) >= min_matches for v in by_team.values()):
            break
        end = today - dt.timedelta(days=offset)
        start = max(today - dt.timedelta(days=max_lookback_days-1), end - dt.timedelta(days=6))
        range_param = f'{start:%Y%m%d}-{end:%Y%m%d}'
        try:
            r = session.get(url, params={'dates': range_param}, timeout=20, headers=headers)
            r.raise_for_status()
            body = r.json()
            if not isinstance(body.get('events'), list):
                raise ValueError('No events list')
            consume(body['events'])
        except (requests.RequestException, ValueError):
            day = end
            while day >= start:
                try:
                    r = session.get(url, params={'dates': f'{day:%Y%m%d}'}, timeout=20, headers=headers)
                    r.raise_for_status()
                    body = r.json()
                    if isinstance(body.get('events'), list):
                        consume(body['events'])
                except (requests.RequestException, ValueError) as exc:
                    errors.append(f'{day}: {type(exc).__name__}')
                day -= dt.timedelta(days=1)

    # Sort and truncate to exactly the last N matches per team.
    for team, games in by_team.items():
        games.sort(key=lambda g: g['date'], reverse=True)
        by_team[team] = games[:min_matches]
    return by_team, errors


def apply_recent_form(data, league, history):
    """Keep recent-10 form separate from existing season baseline aggregates."""
    team_data = data['leagues'][league]['teams']
    for team, games in history.items():
        if not games:
            continue
        # Do NOT overwrite P/F/A, att/dfn or historical corners/cards:
        # their existing baselines can already include these fixtures.
        home = [g for g in games if g['venue'] == 'home']
        away = [g for g in games if g['venue'] == 'away']
        def summary(rows):
            n = len(rows)
            return {'n': n,
                    'goals_for_avg': round(sum(g['goals_for'] for g in rows) / n, 3) if n else None,
                    'goals_against_avg': round(sum(g['goals_against'] for g in rows) / n, 3) if n else None}
        team_data[team]['recent_form'] = {
            'target': 10, 'count': len(games), 'complete': len(games) >= 10,
            'overall': summary(games), 'home': summary(home), 'away': summary(away),
            'matches': games,
            'source': 'ESPN completed match scores',
        }


def sync(path=DATA, days=14, network=True):
    path = Path(path)
    original = json.loads(path.read_text(encoding='utf-8'))
    data = json.loads(json.dumps(original))
    now = dt.datetime.now(UTC)
    # Include completed matches from the previous week; future-only windows cannot update statistics.
    start, end = (now - dt.timedelta(days=7)).date(), (now + dt.timedelta(days=days)).date()
    warnings, status_by_league, matched_events = [], {}, {}
    overrides = load_overrides()
    existing = data.get('fixtures', [])
    seen = set(str(x) for x in data.get('processed_espn_event_ids', []))
    goals_updated = corners_updated = cards_updated = 0
    recent_teams_complete = recent_teams_partial = 0
    history_errors = 0

    if network:
        with requests.Session() as session:
            for league, league_data in data['leagues'].items():
                try:
                    events, covered_days, fetch_errors = fetch(session, league, start, end)
                    status_by_league[league] = 'ok' if covered_days == (end - start).days + 1 else 'partial'
                    if fetch_errors:
                        warnings.append(f"{league}: partial ESPN coverage ({covered_days} dated days); " + "; ".join(fetch_errors[:2]))
                except (requests.RequestException, ValueError, TypeError, RuntimeError) as exc:
                    status_by_league[league] = 'failed'
                    status = getattr(getattr(exc, 'response', None), 'status_code', None)
                    warnings.append(f'{league}: ESPN fetch failed ({type(exc).__name__}, HTTP {status or "unknown"})')
                    continue
                # Rebuild recent form from the last ten *matches* per team.
                # This is separate from incremental post-baseline updates.
                history, recent_errors = collect_recent_matches(session, league, league_data, now)
                history_errors += len(recent_errors)
                apply_recent_form(data, league, history)
                recent_teams_complete += sum(len(games) >= 10 for games in history.values())
                recent_teams_partial += sum(0 < len(games) < 10 for games in history.values())
                if recent_errors:
                    warnings.append(f'{league}: {len(recent_errors)} historical date requests failed')
                for event in events:
                    state = (event.get('status') or {}).get('type') or {}
                    if (state.get('completed') is True or state.get('name') in ('STATUS_FINAL', 'STATUS_FULL_TIME')):
                        eid = str(event.get('id') or '')
                        if eid and eid not in seen:
                            event = enrich_completed(session, league, event)
                    g, c, ca = update_finished(data, league, event, seen, warnings)
                    goals_updated += int(g)
                    corners_updated += int(c)
                    cards_updated += int(ca)
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
        if event is None and status_by_league.get(league) in ('ok', 'partial'):
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
        else:
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

    data['processed_espn_event_ids'] = sorted(seen)
    output.sort(key=lambda x: x.get('t', ''))
    data['fixtures'] = output
    if any(v in ('ok', 'partial') for v in status_by_league.values()):
        data['updated'] = now.strftime('%Y-%m-%dT%H:%M:%SZ')
    data['sync_meta'] = {
        'last_attempt': now.strftime('%Y-%m-%dT%H:%M:%SZ'), 'source': 'ESPN public scoreboard (unofficial)',
        'league_fetch_status': status_by_league, 'warnings': warnings[:250],
        'warnings_count': len(warnings), 'events_matched': updated_existing,
        'fixtures_added': added, 'referees_verified_this_run': verified,
        'team_statistics_refreshed': goals_updated > 0,
        'completed_matches_applied': goals_updated,
        'matches_with_corners_updated': corners_updated,
        'matches_with_cards_updated': cards_updated,
        'recent_form_target_per_team': 10, 'recent_form_teams_complete': recent_teams_complete,
        'recent_form_teams_partial': recent_teams_partial, 'history_request_errors': history_errors,
        'note': 'Recent form is calculated from up to ten completed matches per team (up to 210 days), separately from baseline aggregates. Corners/cards are NOT backfilled by this recent-form collector.'
    }
    if network and not any(v in ('ok', 'partial') for v in status_by_league.values()):
        raise RuntimeError('All ESPN leagues failed; original data file preserved. ' + '; '.join(warnings[:5]))
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
              'league_fetch_status': status_by_league,
              'goals_updated': goals_updated, 'corners_updated': corners_updated,
              'cards_updated': cards_updated, 'recent_teams_complete': recent_teams_complete,
              'recent_teams_partial': recent_teams_partial, 'history_request_errors': history_errors}
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
