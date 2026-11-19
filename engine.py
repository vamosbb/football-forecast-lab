"""Dixon-Coles predictions with conservative, optional recent-goals adjustment."""
import math

def poisson(k, lam):
    return math.exp(-lam) * lam**k / math.factorial(k)

def shrink(v,n,prior,k):
    return (v*n+prior*k)/(n+k) if n+k else prior

def recent_goal_adjustment(team, venue, league):
    """Return conservative form multipliers; never infer missing matches as zero.

    Form is a small correction to the existing attack/defense model, not a
    replacement. Home/away samples are used only with >=3 observations.
    """
    form = team.get('recent_form') or {}
    overall = form.get('overall') or {}
    if overall.get('n', 0) < 5:
        return 1.0, 1.0, 0
    sample = form.get(venue) or {}
    if sample.get('n', 0) < 3:
        sample = overall
    n = sample.get('n', 0)
    gf, ga = sample.get('goals_for_avg'), sample.get('goals_against_avg')
    if not all(isinstance(x, (int, float)) and math.isfinite(x) and x >= 0 for x in (gf, ga)):
        return 1.0, 1.0, 0
    # The venue-specific league mean provides a reference scale. Sample
    # rates are shrunk and capped to avoid extreme changes from 3-5 games.
    baseline = league['avgH'] if venue == 'home' else league['avgA']
    if baseline <= 0:
        return 1.0, 1.0, 0
    shrink_weight = n / (n + 8.0)
    attack_ratio = 1.0 + shrink_weight * (gf / baseline - 1.0)
    conceded_ratio = 1.0 + shrink_weight * (ga / baseline - 1.0)
    return max(.7, min(1.4, attack_ratio)), max(.7, min(1.4, conceded_ratio)), n

def recent_stat(team, field, min_observations=5):
    stat = (team.get('recent_form') or {}).get(field) or {}
    n = stat.get('n', 0)
    value = stat.get('avg')
    if not isinstance(n, int) or n < min_observations:
        return None
    if not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        return None
    return value


def predict(league, home, away, referee=None):
    teams=league['teams']; H=teams[home]; A=teams[away]
    base_h=league['avgH']*H['att']*A['dfn']
    base_a=league['avgA']*A['att']*H['dfn']
    h_att, h_con, hn = recent_goal_adjustment(H, 'home', league)
    a_att, a_con, an = recent_goal_adjustment(A, 'away', league)
    # A modest 25% form overlay preserves the existing calibrated baseline.
    form_weight = 0.25 if hn and an else 0.0
    lh=base_h * (1 + form_weight * (h_att * a_con - 1))
    la=base_a * (1 + form_weight * (a_att * h_con - 1))
    rho=league['rho']
    matrix=[]
    for i in range(10):
        row=[]
        for j in range(10):
            tau={(0,0):1-lh*la*rho,(0,1):1+lh*rho,(1,0):1+la*rho,(1,1):1-rho}.get((i,j),1)
            row.append(poisson(i,lh)*poisson(j,la)*tau)
        matrix.append(row)
    z=sum(map(sum,matrix))
    if z<=0 or any(x<0 for row in matrix for x in row): raise ValueError('Invalid Dixon-Coles parameters')
    matrix=[[x/z for x in row] for row in matrix]
    p1=sum(matrix[i][j] for i in range(10) for j in range(10) if i>j)
    px=sum(matrix[i][i] for i in range(10))
    p2=1-p1-px
    over25=sum(matrix[i][j] for i in range(10) for j in range(10) if i+j>2)
    btts=sum(matrix[i][j] for i in range(1,10) for j in range(1,10))
    ext=league['ext']; ks=ext['ks']; cf=ext['cf']
    hc=H['cr']; ac=A['cr']
    hw=shrink(hc['h'],hc['n']/2,hc['f']*ext['ch']/cf,ks)
    aw=shrink(ac['a'],ac['n']/2,ac['f']*ext['ca']/cf,ks)
    conceded_h=shrink(hc['ag'],hc['n'],cf,ks)
    conceded_a=shrink(ac['ag'],ac['n'],cf,ks)
    corners_h=hw*conceded_a/cf; corners_a=aw*conceded_h/cf
    # Apply a modest recent-match overlay only when both teams have enough
    # independently reported statistics; otherwise keep the established model.
    hcf = recent_stat(H, 'corners_for')
    aca = recent_stat(A, 'corners_against')
    acf = recent_stat(A, 'corners_for')
    hca = recent_stat(H, 'corners_against')
    recent_corners_used = all(v is not None for v in (hcf, aca, acf, hca))
    if recent_corners_used:
        corners_h = 0.75 * corners_h + 0.25 * (hcf + aca) / 2
        corners_a = 0.75 * corners_a + 0.25 * (acf + hca) / 2
    # A referee appointment is independent of whether its historical statistics exist.
    ref_record=league.get('refs',{}).get(referee or '')
    factor=ref_record['f'] if ref_record else 1.0
    def card_rate(team):
        cd=team['cd']; n=cd['n']
        return shrink((cd['y']+cd['r'])/n if n else ext['kc'],n,ext['kc'],ks)*factor
    cards_h=card_rate(H);cards_a=card_rate(A)
    hy, hr = recent_stat(H, 'yellow_cards'), recent_stat(H, 'red_cards')
    ay, ar = recent_stat(A, 'yellow_cards'), recent_stat(A, 'red_cards')
    recent_cards_used = all(v is not None for v in (hy, hr, ay, ar))
    if recent_cards_used:
        cards_h = 0.75 * cards_h + 0.25 * (hy + hr) * factor
        cards_a = 0.75 * cards_a + 0.25 * (ay + ar) * factor
    return {'goals':(lh,la),'1X2':(p1,px,p2),'over2.5':over25,'btts':btts,
            'corners':(corners_h,corners_a),'cards':(cards_h,cards_a),
            'referee_factor':factor,'referee_stats_available':bool(ref_record),
            'recent_corners_used':recent_corners_used,'recent_cards_used':recent_cards_used,
            'recent_form_used':bool(form_weight),'recent_form_matches':(hn,an),
            'scorelines':sorted(((matrix[i][j],f'{i}-{j}') for i in range(10) for j in range(10)),reverse=True)[:5]}

def fair_odds(p): return 1/p if p>0 else math.inf

def expected_value(p,odds): return p*odds-1

def quarter_kelly(p,odds,bankroll):
    return max(0,bankroll*(p*odds-1)/(odds-1)/4) if odds>1 else 0

def poisson_over(lam,line):
    # line ending .5 only: P(X > line)
    return 1-sum(poisson(i,lam) for i in range(int(math.floor(line))+1))
