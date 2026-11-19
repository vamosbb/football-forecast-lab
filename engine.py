"""Exact core formulas from model_guide.md. No external dependencies."""
import math

def poisson(k, lam):
    return math.exp(-lam) * lam**k / math.factorial(k)

def shrink(v,n,prior,k):
    return (v*n+prior*k)/(n+k) if n+k else prior

def predict(league, home, away, referee=None):
    teams=league['teams']; H=teams[home]; A=teams[away]
    lh=league['avgH']*H['att']*A['dfn']; la=league['avgA']*A['att']*H['dfn']; rho=league['rho']
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
    # A referee appointment is independent of whether its historical statistics exist.
    ref_record=league.get('refs',{}).get(referee or '')
    factor=ref_record['f'] if ref_record else 1.0
    def card_rate(team):
        cd=team['cd']; n=cd['n']
        return shrink((cd['y']+cd['r'])/n if n else ext['kc'],n,ext['kc'],ks)*factor
    cards_h=card_rate(H);cards_a=card_rate(A)
    return {'goals':(lh,la),'1X2':(p1,px,p2),'over2.5':over25,'btts':btts,
            'corners':(corners_h,corners_a),'cards':(cards_h,cards_a),
            'referee_factor':factor,'referee_stats_available':bool(ref_record),
            'scorelines':sorted(((matrix[i][j],f'{i}-{j}') for i in range(10) for j in range(10)),reverse=True)[:5]}

def fair_odds(p): return 1/p if p>0 else math.inf

def expected_value(p,odds): return p*odds-1

def quarter_kelly(p,odds,bankroll):
    return max(0,bankroll*(p*odds-1)/(odds-1)/4) if odds>1 else 0

def poisson_over(lam,line):
    # line ending .5 only: P(X > line)
    return 1-sum(poisson(i,lam) for i in range(int(math.floor(line))+1))
