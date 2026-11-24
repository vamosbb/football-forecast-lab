"""Save pre-match engine forecasts and score them once completed results enter the data JSON.

Only forecasts recorded before kickoff are scored; no retroactive reconstruction.
"""
import csv
import json
from datetime import datetime, timezone
from pathlib import Path

from engine import predict, poisson_over

ROOT = Path(__file__).resolve().parent
DATA = ROOT / 'football_model_data.json'
LEDGER = ROOT / 'forecast_history.json'
REPORT = ROOT / 'forecast_accuracy.json'


def parse_time(s):
    return datetime.fromisoformat(s.replace('Z', '+00:00'))


def main():
    now = datetime.now(timezone.utc)
    data = json.loads(DATA.read_text(encoding='utf-8'))
    ledger = json.loads(LEDGER.read_text(encoding='utf-8')) if LEDGER.exists() else {'forecasts': []}
    entries = ledger['forecasts']
    seen = {(x['league'], x['event_id']) for x in entries}
    new, scored = 0, 0

    # Completed results must agree from both teams' ESPN history records.
    completed = {}
    for lg, league in data['leagues'].items():
        by_id = {}
        for team, info in league['teams'].items():
            for game in (info.get('recent_form') or {}).get('matches', []):
                by_id.setdefault(str(game.get('id')), []).append((team, game))
        for eid, sides in by_id.items():
            if len(sides) != 2:
                continue
            homes = [(t, g) for t, g in sides if g.get('venue') == 'home']
            aways = [(t, g) for t, g in sides if g.get('venue') == 'away']
            if len(homes) != 1 or len(aways) != 1:
                continue
            ht, hg = homes[0]
            at, ag = aways[0]
            if hg.get('goals_for') != ag.get('goals_against') or hg.get('goals_against') != ag.get('goals_for'):
                continue
            completed[(lg, eid)] = (ht, at, hg, ag)

    for row in entries:
        if row.get('result') is not None:
            continue
        match = completed.get((row['league'], row['event_id']))
        if not match:
            continue
        ht, at, hg, ag = match
        if ht != row['home'] or at != row['away']:
            continue
        home_goals, away_goals = hg['goals_for'], ag['goals_for']
        outcome = 0 if home_goals > away_goals else (1 if home_goals == away_goals else 2)
        probs = row['p_1x2']
        row['result'] = {
            'home_goals': home_goals, 'away_goals': away_goals,
            'outcome': ['1', 'X', '2'][outcome],
            'logloss': round(-__import__('math').log(max(1e-12, probs[outcome])), 6),
            'brier': round(sum((p - int(i == outcome)) ** 2 for i, p in enumerate(probs)), 6),
            'goals_over_2_5_brier': round((row['p_goals_over_2_5'] - int(home_goals + away_goals > 2.5)) ** 2, 6),
        }
        for label, field, threshold in [('corners', 'corners_for', 8.5), ('cards', None, 4.5)]:
            if field:
                values = [hg.get(field), ag.get(field)]
            else:
                values = [g.get('yellow_cards') + g.get('red_cards') if g.get('yellow_cards') is not None and g.get('red_cards') is not None else None for g in (hg, ag)]
            if row.get(f'p_{label}_over') is not None and all(v is not None for v in values):
                row['result'][f'{label}_brier'] = round((row[f'p_{label}_over'] - int(sum(values) > threshold)) ** 2, 6)
        scored += 1

    for f in data.get('fixtures', []):
        lg, eid = f.get('lg'), str(f.get('espn_event_id') or '')
        if not lg or not eid or (lg, eid) in seen or not f.get('t'):
            continue
        kickoff = parse_time(f['t'])
        if kickoff <= now:
            continue
        league = data['leagues'].get(lg)
        if not league or f.get('h') not in league['teams'] or f.get('a') not in league['teams']:
            continue
        referee = f.get('ref') if f.get('ref_status') == 'appointed' else None
        try:
            p = predict(league, f['h'], f['a'], referee)
        except (ValueError, KeyError, TypeError, OverflowError) as exc:
            print('Skipping prediction:', lg, eid, str(exc))
            continue
        entries.append({
            'league': lg, 'event_id': eid, 'kickoff': f['t'],
            'recorded_at': now.isoformat(), 'home': f['h'], 'away': f['a'],
            'p_1x2': [round(float(x), 8) for x in p['1X2']],
            'p_goals_over_2_5': round(float(p['over2.5']), 8),
            'p_corners_over': round(float(poisson_over(sum(p['corners']), 8.5)), 8) if lg != 'uefa.champions' or all(league['teams'][team].get('ucl_corners_ready') for team in (f['h'], f['a'])) else None,
            'p_cards_over': round(float(poisson_over(sum(p['cards']), 4.5)), 8) if lg != 'uefa.champions' or all(league['teams'][team].get('ucl_cards_ready') for team in (f['h'], f['a'])) else None,
            'result': None,
        })
        seen.add((lg, eid))
        new += 1

    ledger['forecasts'] = entries
    LEDGER.write_text(json.dumps(ledger, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    finished = [x for x in entries if x.get('result') is not None]
    def avg(key):
        values = [x['result'][key] for x in finished if key in x['result']]
        return {'n': len(values), 'mean': round(sum(values) / len(values), 5) if values else None}
    report = {
        'note': 'Only pre-kickoff saved production-engine predictions. Results require both teams in recent match histories; coverage may be incomplete.',
        'recorded': len(entries), 'completed': len(finished), 'pending': len(entries) - len(finished),
        '1x2_logloss': avg('logloss'), '1x2_brier': avg('brier'),
        'goals_over_2_5_brier': avg('goals_over_2_5_brier'),
        'corners_over_8_5_brier': avg('corners_brier'),
        'cards_over_4_5_brier': avg('cards_brier'),
    }
    REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({'new_predictions': new, 'newly_scored': scored, **report}, ensure_ascii=False))


if __name__ == '__main__':
    main()
