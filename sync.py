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
ESPN_LEAGUES = {'en.1': 'eng.1', 'es.1': 'esp.1', 'de.1': 'ger.1', 'it.1': 'ita.1', 'fr.1': 'fra.1', 'uefa.champions': 'uefa.champions'}
UTC = dt.timezone.utc
FOOTBALL_DATA_COMPETITIONS = {'en.1': 'PL', 'es.1': 'PD', 'de.1': 'BL1', 'it.1': 'SA', 'fr.1': 'FL1', 'uefa.champions': 'CL'}


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
    """Select only an explicitly identified match referee; never guess an official.

    ESPN may list assistant, VAR and fourth officials. An untyped official
    is ambiguous, even if it is the first entry.
    """
    for comp in event.get('competitions') or []:
        for official in comp.get('officials') or []:
            position = official.get('position')
            if isinstance(position, dict):
                role_text = position.get('name') or position.get('displayName') or ''
            else:
                role_text = position or ''
            typ = official.get('type')
            if isinstance(typ, dict):
                typ = typ.get('name') or typ.get('displayName') or ''
            role = norm(str(role_text or typ or ''))
            if not role or any(x in role for x in ('assistant', 'video', 'fourth', 'var', 'linesman')):
                continue
            if role not in ('referee', 'main referee', 'head referee', 'center referee', 'centre referee'):
                continue
            name = official.get('displayName') or official.get('fullName') or official.get('name')
            if name:
                return name
    return None



def enrich_upcoming_referee(session, league, event):
    """Check ESPN event summary for an explicitly identified main referee.

    An absent or untyped official is never treated as a confirmed appointment.
    """
    if event_referee(event) or not event.get('id'):
        return event, False
    url = ('https://site.api.espn.com/apis/site/v2/sports/soccer/'
           + ESPN_LEAGUES.get(league, league) + '/summary')
    try:
        response = session.get(url, params={'event': str(event['id'])}, timeout=12,
                               headers={'Accept': 'application/json', 'User-Agent': 'Mozilla/5.0'})
        response.raise_for_status()
        body = response.json()
        candidates = []
        header = body.get('header') or {}
        candidates.extend(header.get('competitions') or [])
        candidates.extend(body.get('competitions') or [])
        # Some ESPN responses expose officials outside the header.
        if isinstance(body.get('officials'), list):
            candidates.append({'officials': body['officials']})
        for comp in candidates:
            if not isinstance(comp, dict):
                continue
            probe = {'competitions': [{'officials': comp.get('officials') or []}]}
            if event_referee(probe):
                event.setdefault('competitions', [{}])[0]['officials'] = comp['officials']
                return event, True
    except (requests.RequestException, ValueError, TypeError, KeyError):
        pass
    return event, False


def football_data_referees(session, fixtures, now, warnings):
    """Supplement ESPN fixtures with explicitly appointed main referees.

    Match on league, both known teams and kickoff time; never use fuzzy
    referee assignments or infer a referee from an untyped official.
    """
    token = os.environ.get('FOOTBALL_DATA_API_KEY', '').strip()
    stats = {'requests': 0, 'matched': 0, 'appointed': 0, 'matches_with_referees': 0, 'referee_types': {}, 'referee_names_present': 0}
    if not token:
        warnings.append('Football-Data.org: FOOTBALL_DATA_API_KEY not configured')
        return stats
    for league, code in FOOTBALL_DATA_COMPETITIONS.items():
        candidates = [(key, ev) for key, ev in fixtures.items()
                      if key[0] == league and not event_referee(ev)
                      and now <= parse_time(ev['date']) <= now + dt.timedelta(days=14)]
        if not candidates:
            continue
        url = f'https://api.football-data.org/v4/competitions/{code}/matches'
        params = {'dateFrom': now.date().isoformat(),
                  'dateTo': (now + dt.timedelta(days=15)).date().isoformat()}
        stats['requests'] += 1
        try:
            response = session.get(url, params=params,
                                   headers={'X-Auth-Token': token, 'Accept': 'application/json'},
                                   timeout=20)
            response.raise_for_status()
            matches = response.json().get('matches')
            if not isinstance(matches, list):
                raise ValueError('missing matches list')
        except (requests.RequestException, ValueError, TypeError) as exc:
            status = getattr(getattr(exc, 'response', None), 'status_code', None)
            warnings.append(f'Football-Data.org {code}: {type(exc).__name__} HTTP {status or "unknown"}')
            continue
        for match in matches:
            if not isinstance(match, dict) or not match.get('utcDate'):
                continue
            home = match.get('homeTeam') or {}
            away = match.get('awayTeam') or {}
            known = [key for key, _ in candidates]
            names = {team for key in known for team in key[1:3]}
            def mapped(side):
                possibilities = [match_team(side.get(field), league, names)
                                 for field in ('name', 'shortName', 'tla') if side.get(field)]
                possibilities = [name for name in possibilities if name]
                return possibilities[0] if possibilities and len(set(possibilities)) == 1 else None
            h, a = mapped(home), mapped(away)
            if not h or not a:
                continue
            try:
                kickoff = parse_time(match['utcDate'])
            except (ValueError, TypeError):
                continue
            matches_found = [(key, ev) for key, ev in candidates
                             if key[1:3] == (h, a)
                             and abs((parse_time(ev['date']) - kickoff).total_seconds()) <= 36 * 3600]
            if len(matches_found) != 1:
                continue
            key, event = matches_found[0]
            stats['matched'] += 1
            officials = match.get('referees') or []
            if isinstance(officials, list) and officials:
                stats['matches_with_referees'] += 1
                for official in officials:
                    if isinstance(official, dict):
                        role = str(official.get('type') or '(missing)')
                        stats['referee_types'][role] = stats['referee_types'].get(role, 0) + 1
                        if isinstance(official.get('name'), str) and official['name'].strip():
                            stats['referee_names_present'] += 1
            main = [r for r in officials if isinstance(r, dict)
                    and norm(r.get('type')) in ('referee', 'main referee')
                    and isinstance(r.get('name'), str) and r['name'].strip()]
            if len(main) != 1:
                continue
            if event_referee(event):
                continue
            event.setdefault('competitions', [{}])[0]['officials'] = [
                {'displayName': main[0]['name'].strip(), 'position': 'referee'}]
            event['_ref_source'] = 'Football-Data.org match referees'
            stats['appointed'] += 1
    return stats


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




def collect_recent_details(session, league, history, cache, max_new_requests=35):
    """Enrich the last ten games per team, caching match-level stats across runs.

    A missing statistic stays None; zero is accepted only when ESPN reports zero.
    """
    league_cache = cache.setdefault(league, {})
    event_ids = list(dict.fromkeys(g['id'] for games in history.values() for g in games))
    # Prefer matches shared by more modeled teams and newest match dates.
    event_ids.sort(reverse=True)
    requested = 0
    errors = 0
    corner_stat_labels = set()
    for eid in event_ids:
        cached = league_cache.get(eid)
        # Revisit old cached summaries once when the corner parser is upgraded.
        # Otherwise a previously cached None would remain None forever.
        if cached is not None and cached.get('_corner_parser_version') == 2:
            continue
        if requested >= max_new_requests:
            break
        requested += 1
        url = ('https://site.api.espn.com/apis/site/v2/sports/soccer/'
               + ESPN_LEAGUES.get(league, league) + '/summary')
        try:
            resp = session.get(url, params={'event': eid}, timeout=12,
                               headers={'Accept': 'application/json', 'User-Agent': 'Mozilla/5.0'})
            resp.raise_for_status()
            body = resp.json()
            teams = (body.get('boxscore') or {}).get('teams') or []
            by_id = {}
            for side in teams:
                for stat in side.get('statistics') or []:
                    if isinstance(stat, dict):
                        label = str(stat.get('name') or stat.get('label') or stat.get('abbreviation') or '')
                        if 'corner' in label.lower():
                            corner_stat_labels.add(label)
                tid = str((side.get('team') or {}).get('id') or '')
                if not tid:
                    continue
                by_id[tid] = {
                    'corners': stat_value(side, 'cornerKicks', 'corners', 'corner kicks',
                                           'wonCorners', 'cornersWon', 'cornerKicksWon',
                                           'totalCorners', 'corner kicks won'),
                    'yellow': stat_value(side, 'yellowCards', 'yellow cards'),
                    'red': stat_value(side, 'redCards', 'red cards'),
                }
            # Identify teams using ESPN IDs from summary header or boxscore.
            header = body.get('header') or {}
            comps = header.get('competitions') or []
            sides = comps[0].get('competitors') or [] if comps else []
            entry = {'by_team': {}, 'referee': None, '_corner_parser_version': 2}
            for side in sides:
                tid = str((side.get('team') or {}).get('id') or '')
                name = (side.get('team') or {}).get('displayName') or (side.get('team') or {}).get('name')
                mapped = match_team(name, league, history)
                if mapped and tid in by_id:
                    entry['by_team'][mapped] = by_id[tid]
            for official in (comps[0].get('officials') or []) if comps else []:
                name = official.get('displayName') or official.get('fullName') or official.get('name')
                if name:
                    entry['referee'] = name
                    break
            # Cache even empty summaries to avoid endless retries of unsupported data.
            league_cache[eid] = entry
        except (requests.RequestException, ValueError, TypeError, KeyError):
            errors += 1
    # Attach verified per-team data, and opponent's corners when both exist.
    for team, games in history.items():
        for game in games:
            record = league_cache.get(game['id'], {}).get('by_team', {})
            own = record.get(team) or {}
            opp = record.get(game['opponent']) or {}
            if own.get('corners') is not None:
                game['corners_for'] = own['corners']
            if opp.get('corners') is not None:
                game['corners_against'] = opp['corners']
            if own.get('yellow') is not None:
                game['yellow_cards'] = own['yellow']
            if own.get('red') is not None:
                game['red_cards'] = own['red']
    if requested:
        print('ESPN corner statistic labels observed:', sorted(corner_stat_labels)[:25])
        print('ESPN detail summaries with team data:',
              sum(bool(league_cache.get(eid, {}).get('by_team')) for eid in event_ids))
    return requested, errors


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
        def stat_summary(rows, field):
            values = [g[field] for g in rows if isinstance(g.get(field), (int, float))]
            return {'n': len(values), 'avg': round(sum(values) / len(values), 3) if values else None}
        team_data[team]['recent_form'] = {
            'target': 10, 'count': len(games), 'complete': len(games) >= 10,
            'overall': summary(games), 'home': summary(home), 'away': summary(away),
            'matches': games,
            'corners_for': stat_summary(games, 'corners_for'),
            'corners_against': stat_summary(games, 'corners_against'),
            'yellow_cards': stat_summary(games, 'yellow_cards'),
            'red_cards': stat_summary(games, 'red_cards'),
            'source': 'ESPN completed match scores and available summary statistics',
        }



def bootstrap_ucl(session, data, now, warnings):
    lg = 'uefa.champions'
    url = BASE.format(league=lg)
    events = {}
    headers = {'Accept': 'application/json', 'User-Agent': 'Mozilla/5.0'}
    successful = 0
    failures = []
    def load(params):
        nonlocal successful
        r = session.get(url, params=params, headers=headers, timeout=20)
        r.raise_for_status()
        rows = r.json().get('events')
        if not isinstance(rows, list):
            raise ValueError('missing events list')
        successful += 1
        for e in rows:
            if e.get('id'): events[str(e['id'])] = e
    def diagnostic(exc):
        response = getattr(exc, 'response', None)
        return f'{type(exc).__name__}: HTTP {getattr(response, "status_code", "unknown")} {str(exc)[:180]}'
    # Match the working domestic-league request format; no limit parameter.
    for offset in range(-14, 365, 7):
        end = now.date() - dt.timedelta(days=offset)
        start = end - dt.timedelta(days=6)
        try:
            load({'dates': f'{start:%Y%m%d}-{end:%Y%m%d}'})
        except (requests.RequestException, ValueError, KeyError, TypeError) as exc:
            if not failures:
                print('UCL range request:', diagnostic(exc))
            failures.append(diagnostic(exc))
            for i in range(7):
                day = start + dt.timedelta(days=i)
                try:
                    load({'dates': f'{day:%Y%m%d}'})
                except (requests.RequestException, ValueError, KeyError, TypeError) as daily_exc:
                    failures.append(diagnostic(daily_exc))
                    if i == 0:
                        print('UCL daily request:', day, diagnostic(daily_exc))
                    if getattr(getattr(daily_exc, 'response', None), 'status_code', None) in (400, 401, 403, 404, 429):
                        break
        if successful == 0 and len(failures) >= 6:
            break
    print('UCL history:', json.dumps({'successful_requests': successful, 'events': len(events), 'failed_requests': len(failures)}))
    if failures:
        warnings.append(f'UCL history: {len(failures)} failed requests; {failures[0]}')
    if not successful:
        return False
    names = set(data.get('leagues', {}).get(lg, {}).get('teams', {}))
    active = set()
    for e in events.values():
        try:
            kickoff = parse_time(e['date'])
            sides = e['competitions'][0]['competitors']
            pair = {x['team']['displayName'] for x in sides}
            if len(pair) != 2: continue
            names.update(pair)
            if now - dt.timedelta(days=100) <= kickoff <= now + dt.timedelta(days=28): active.update(pair)
        except (KeyError, IndexError, ValueError, TypeError): continue
    if not active: return False
    history = {name: [] for name in names}
    for eid, e in events.items():
        try:
            state = e['status']['type']
            if state.get('name') not in ('STATUS_FINAL', 'STATUS_FULL_TIME'): continue
            if any(x in str(state).lower() for x in ('extra time', 'penalt', 'shootout', 'aet')): continue
            kickoff = parse_time(e['date'])
            if kickoff >= now: continue
            sides = e['competitions'][0]['competitors']
            h = next(x for x in sides if x['homeAway']=='home'); a = next(x for x in sides if x['homeAway']=='away')
            hn, an = h['team']['displayName'], a['team']['displayName']
            hg, ag = int(h['score']), int(a['score'])
            for t,o,gf,ga,v in ((hn,an,hg,ag,'home'),(an,hn,ag,hg,'away')):
                history[t].append(dict(id=eid,date=kickoff.strftime('%Y-%m-%dT%H:%M:%SZ'),opponent=o,venue=v,goals_for=gf,goals_against=ga))
        except (KeyError, IndexError, ValueError, TypeError, StopIteration): continue
    for t in history: history[t].sort(key=lambda g:g['date'],reverse=True)
    rows = [g for games in history.values() for g in games if g['venue']=='home']
    if not rows: return False
    avgH = max(.1,sum(g['goals_for'] for g in rows)/len(rows)); avgA=max(.1,sum(g['goals_against'] for g in rows)/len(rows)); avg=(avgH+avgA)/2
    recent = {t: games[:10] for t,games in history.items()}
    collect_recent_details(session,lg,recent,data.setdefault('recent_details_cache',{}),max_new_requests=120)
    # Competition averages only from observed statistics, never invented zeros.
    allgames=[g for games in recent.values() for g in games]
    def mean(values): return sum(values)/len(values) if values else None
    cf=mean([g['corners_for'] for g in allgames if 'corners_for' in g]); ch=mean([g['corners_for'] for g in allgames if g['venue']=='home' and 'corners_for' in g]); ca=mean([g['corners_for'] for g in allgames if g['venue']=='away' and 'corners_for' in g]); kc=mean([g['yellow_cards']+g['red_cards'] for g in allgames if 'yellow_cards' in g and 'red_cards' in g])
    teams={}
    for t in active:
        games=history[t]; n=len(games); gf=sum(g['goals_for'] for g in games); ga=sum(g['goals_against'] for g in games)
        if not n: continue
        rr=recent[t]; cr=[g for g in rr if 'corners_for' in g and 'corners_against' in g]; cd=[g for g in rr if 'yellow_cards' in g and 'red_cards' in g]
        # Require actual team observations before exposing either market.
        teams[t]={'P':n,'F':gf,'A':ga,'att':(gf+12*avg)/(n+12)/avg,'dfn':(ga+12*avg)/(n+12)/avg,'cr':{'n':len(cr),'f':mean([g['corners_for'] for g in cr]) or 0,'ag':mean([g['corners_against'] for g in cr]) or 0,'h':mean([g['corners_for'] for g in cr if g['venue']=='home']) if any(g['venue']=='home' for g in cr) else (cf or 1),'a':mean([g['corners_for'] for g in cr if g['venue']=='away']) if any(g['venue']=='away' for g in cr) else (cf or 1)},'cd':{'n':len(cd),'y':sum(g['yellow_cards'] for g in cd),'r':sum(g['red_cards'] for g in cd)},'ucl_corners_ready':len(cr)>=3 and bool(cf and ch and ca),'ucl_cards_ready':len(cd)>=3 and kc is not None}
    if len(teams)<2: return False
    data['leagues'][lg]={'teams':teams,'avgH':avgH,'avgA':avgA,'rho':0.0,'asof':now.date().isoformat(),'refs':{},'ext':{'ks':8,'cf':cf or 1,'ch':ch or cf or 1,'ca':ca or cf or 1,'kc':kc or 0,'rbar':2*(kc or 0)}}
    apply_recent_form(data,lg,{t:recent[t] for t in teams})
    print("UCL coverage:", json.dumps({"teams": len(teams), "corners_ready": sum(t["ucl_corners_ready"] for t in teams.values()), "cards_ready": sum(t["ucl_cards_ready"] for t in teams.values())}))
    return True
