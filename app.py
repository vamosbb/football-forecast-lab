import json,datetime as dt
from pathlib import Path
import streamlit as st
from engine import predict,fair_odds,poisson_over,expected_value,quarter_kelly

st.set_page_config(page_title='Football Forecast Lab',page_icon='⚽',layout='wide')
st.title('⚽ Football Forecast Lab')
st.caption('Five leagues • Dixon–Coles goals • Corners • Cards • Transparent data freshness')
path=Path(__file__).parent/'football_model_data.json'
with path.open(encoding='utf-8') as f:data=json.load(f)
league_names={'en.1':'Premier League','es.1':'La Liga','de.1':'Bundesliga','it.1':'Serie A','fr.1':'Ligue 1'}
with st.sidebar:
    st.header('Settings')
    lg=st.selectbox('League',list(data['leagues']),format_func=lambda k:league_names.get(k,k))
    league=data['leagues'][lg]
    fixtures=[f for f in data.get('fixtures',[]) if f['lg']==lg]
    selection=st.radio('Match selection',['Upcoming fixtures','Custom matchup'])
    if selection=='Upcoming fixtures' and fixtures:
        idx=st.selectbox('Fixture',range(len(fixtures)),format_func=lambda i:f"{fixtures[i]['t'][:16]}  {fixtures[i]['h']} – {fixtures[i]['a']}")
        fx=fixtures[idx]; home=fx['h'];away=fx['a'];ref=fx.get('ref')
    else:
        home=st.selectbox('Home team',list(league['teams']))
        away=st.selectbox('Away team',[x for x in league['teams'] if x!=home])
        ref=st.text_input('Referee (optional)','').strip() or None
        fx=None
    bankroll=st.number_input('Bankroll',min_value=0.0,value=100.0,step=10.0)
    bookmaker=st.number_input('Bookmaker home-win odds',min_value=1.01,value=2.00,step=0.05)

if home==away:st.error('Teams must differ');st.stop()
r=predict(league,home,away,ref)
st.subheader(f'{home}  vs  {away}')
for label, team in [('Home', home), ('Away', away)]:
    form = league['teams'][team].get('recent_form') or {}
    count = form.get('count', 0)
    corners_n = (form.get('corners_for') or {}).get('n', 0)
    cards_n = (form.get('yellow_cards') or {}).get('n', 0)
    st.caption(f'{label} recent data: {count}/10 results | corners {corners_n}/10 | cards {cards_n}/10')
if fx:
    st.caption(f"Kickoff (UTC): {fx['t']} | Source dataset: {league.get('asof','unknown')} | Last file update: {data.get('updated','unknown')}")
    status=fx.get('ref_status') or ('previously_recorded_unverified' if ref else 'not_confirmed')
    st.info(f"Referee: **{ref or 'Not confirmed'}** | Status: **{status}** | Source: {fx.get('ref_source','existing dataset')}")
else:st.info(f"Referee: **{ref or 'Not confirmed'}**")
if ref and not r['referee_stats_available']:
    st.warning('Referee name is retained, but historical statistics are unavailable. Neutral factor 1.0 is applied; this does NOT mean the referee is unappointed.')
cols=st.columns(3)
for col,label,p in zip(cols,['Home win','Draw','Away win'],r['1X2']):
    col.metric(label,f'{p:.1%}',f'Fair odds {fair_odds(p):.2f}',delta_color='off')
st.divider()
c1,c2,c3=st.columns(3)
with c1:
    st.subheader('Goals')
    st.write(f"Expected goals: **{r['goals'][0]:.2f} – {r['goals'][1]:.2f}**")
    st.write(f"Over 2.5: **{r['over2.5']:.1%}**")
    st.write(f"Both teams score: **{r['btts']:.1%}**")
    st.write('Most likely scores:')
    for p,s in r['scorelines']:st.write(f'{s}: {p:.1%}')
with c2:
    st.subheader('Corners')
    h,a=r['corners'];st.write(f'Expected: **{h:.2f} – {a:.2f}**')
    for line in [7.5,8.5,9.5,10.5]:st.write(f'Over {line}: **{poisson_over(h+a,line):.1%}**')
with c3:
    st.subheader('Cards')
    h,a=r['cards'];st.write(f'Expected: **{h:.2f} – {a:.2f}**')
    st.write(f"Referee factor: **{r['referee_factor']:.3f}**")
    for line in [3.5,4.5,5.5]:st.write(f'Over {line}: **{poisson_over(h+a,line):.1%}**')
st.caption(f"Recent-match adjustment: corners {'used' if r['recent_corners_used'] else 'not used (insufficient verified observations)'}; cards {'used' if r['recent_cards_used'] else 'not used (insufficient verified observations)'}")
st.divider()
st.subheader('Value calculator — home win')
p=r['1X2'][0];ev=expected_value(p,bookmaker)
st.write(f'Fair odds: **{fair_odds(p):.2f}** | Bookmaker odds: **{bookmaker:.2f}** | EV: **{ev:+.1%}** | Quarter Kelly stake: **{quarter_kelly(p,bookmaker,bankroll):.2f}**')
st.caption('These are model estimates, not guarantees. The model does not account for injuries, lineups, rotation or motivation. Corners/cards extreme lines may be overconfident. Referee source verification is separate from statistics availability.')
with st.expander('Raw match model data'):
    st.json({'fixture':fx,'forecast':r})
