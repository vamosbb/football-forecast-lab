"""Chronological, leakage-resistant diagnostic on stored recent match histories.

Uses only earlier games from the same JSON window. This is NOT a validation of
production engine.predict: its fitted league/team parameters are not timestamped.
Run: python backtest.py
"""
import csv
import json
import math
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA = ROOT / 'football_model_data.json'


def poisson_probs(rate, cap=16):
    rate = max(.05, min(8.0, rate))
    return [math.exp(-rate) * rate ** k / math.factorial(k) for k in range(cap)]


def over_prob(rate, line):
    return max(0., min(1., 1 - sum(poisson_probs(rate, int(line) + 1))))


def one_x_two(h, a):
    hp, ap = poisson_probs(h), poisson_probs(a)
    p1 = sum(hp[i] * ap[j] for i in range(16) for j in range(16) if i > j)
    px = sum(hp[i] * ap[i] for i in range(16))
    p2 = sum(hp[i] * ap[j] for i in range(16) for j in range(16) if i < j)
    z = p1 + px + p2
    return [p1 / z, px / z, p2 / z]


def mean(values, fallback):
    return sum(values) / len(values) if values else fallback


def main():
    data = json.loads(DATA.read_text(encoding='utf-8'))
    rows = []
    for lg, league in data['leagues'].items():
        # Each match is present twice, once per team. Deduplicate by ESPN event ID.
        games = defaultdict(dict)
        for team, info in league['teams'].items():
            for game in (info.get('recent_form') or {}).get('matches', []):
                eid = str(game['id'])
                games[eid][team] = game
        ordered = []
        for eid, sides in games.items():
            if len(sides) != 2:
                continue
            home = [(t, g) for t, g in sides.items() if g.get('venue') == 'home']
            away = [(t, g) for t, g in sides.items() if g.get('venue') == 'away']
            if len(home) != 1 or len(away) != 1:
                continue
            ht, hg = home[0]
            at, ag = away[0]
            if hg.get('goals_for') != ag.get('goals_against') or hg.get('goals_against') != ag.get('goals_for'):
                continue
            ordered.append((hg['date'], eid, ht, at, hg, ag))
        ordered.sort()
        prior = defaultdict(list)
        for date, eid, ht, at, hg, ag in ordered:
            hprev, aprev = prior[ht], prior[at]
            # At least three previous games for EACH side; never use current game.
            if len(hprev) >= 3 and len(aprev) >= 3:
                hgf = mean([g['goals_for'] for g in hprev], league['avgH'])
                aga = mean([g['goals_against'] for g in aprev], league['avgH'])
                agf = mean([g['goals_for'] for g in aprev], league['avgA'])
                hga = mean([g['goals_against'] for g in hprev], league['avgA'])
                # Fixed prior league averages are only fallbacks, not fitted forecasts.
                eh = max(.1, min(5., (hgf + aga) / 2))
                ea = max(.1, min(5., (agf + hga) / 2))
                probs = one_x_two(eh, ea)
                outcome = 0 if hg['goals_for'] > ag['goals_for'] else (1 if hg['goals_for'] == ag['goals_for'] else 2)
                total_goals = hg['goals_for'] + ag['goals_for']
                p_over = over_prob(eh + ea, 2.5)
                record = dict(league=lg, date=date, event_id=eid, home=ht, away=at,
                              home_prior=len(hprev), away_prior=len(aprev),
                              actual_home_goals=hg['goals_for'], actual_away_goals=ag['goals_for'],
                              p_home=round(probs[0], 6), p_draw=round(probs[1], 6), p_away=round(probs[2], 6),
                              logloss=-math.log(max(1e-12, probs[outcome])),
                              brier=sum((p - (i == outcome)) ** 2 for i, p in enumerate(probs)),
                              p_goals_over_2_5=round(p_over, 6), goals_over_2_5=int(total_goals > 2.5),
                              goals_brier=(p_over - (total_goals > 2.5)) ** 2,
                              p_corners_over_8_5='', corners_over_8_5='', corners_brier='',
                              p_cards_over_4_5='', cards_over_4_5='', cards_brier='')
                hc = [g['corners_for'] for g in hprev if g.get('corners_for') is not None]
                ac = [g['corners_for'] for g in aprev if g.get('corners_for') is not None]
                if len(hc) >= 3 and len(ac) >= 3 and hg.get('corners_for') is not None and ag.get('corners_for') is not None:
                    pc = over_prob(mean(hc, 0) + mean(ac, 0), 8.5)
                    truth = int(hg['corners_for'] + ag['corners_for'] > 8.5)
                    record.update(p_corners_over_8_5=round(pc, 6), corners_over_8_5=truth, corners_brier=(pc-truth)**2)
                def card_values(gs):
                    return [g['yellow_cards'] + g['red_cards'] for g in gs if g.get('yellow_cards') is not None and g.get('red_cards') is not None]
                hcards, acards = card_values(hprev), card_values(aprev)
                if len(hcards) >= 3 and len(acards) >= 3 and all(g.get('yellow_cards') is not None and g.get('red_cards') is not None for g in (hg, ag)):
                    pc = over_prob(mean(hcards, 0) + mean(acards, 0), 4.5)
                    truth = int(sum(g['yellow_cards'] + g['red_cards'] for g in (hg, ag)) > 4.5)
                    record.update(p_cards_over_4_5=round(pc, 6), cards_over_4_5=truth, cards_brier=(pc-truth)**2)
                rows.append(record)
            prior[ht].append(hg)
            prior[at].append(ag)
    if not rows:
        raise SystemExit('No eligible matches: need paired games and at least 3 earlier games per team.')
    output = ROOT / 'backtest_results.csv'
    with output.open('w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    def metrics(items):
        def metric(field):
            vals = [r[field] for r in items if isinstance(r[field], (float, int))]
            return {'n': len(vals), 'mean': round(sum(vals)/len(vals), 4) if vals else None}
        return {'matches': len(items), '1x2_logloss': metric('logloss'), '1x2_brier': metric('brier'),
                'goals_over_2_5_brier': metric('goals_brier'),
                'corners_over_8_5_brier': metric('corners_brier'),
                'cards_over_4_5_brier': metric('cards_brier')}
    summary = {'method': 'Chronological rolling-window baseline; NOT a production-engine backtest',
               'warning': 'Limited to last ten matches per team; small early-game samples. Poisson independence; no fitted parameter snapshots.',
               'overall': metrics(rows),
               'leagues': {lg: metrics([r for r in rows if r['league'] == lg]) for lg in data['leagues']}}
    (ROOT / 'backtest_report.json').write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding='utf-8')
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    print('Saved:', output.name, 'and backtest_report.json')

if __name__ == '__main__':
    main()
