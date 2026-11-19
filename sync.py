"""Conservative ESPN fixture and referee sync. Public endpoints may change; no official API guarantee."""
import argparse,copy,csv,datetime as dt,difflib,json,os,re,tempfile
from pathlib import Path
import requests

ROOT=Path(__file__).parent
DATA=ROOT/'football_model_data.json'
OVERRIDES=ROOT/'referee_overrides.csv'
BASE='https://site.api.espn.com/apis/site/v2/sports/soccer/{league}/scoreboard'

def norm(s):
    s=(s or '').casefold();s=re.sub(r'[^\w ]',' ',s);return ' '.join(s.split())

def team_match(name,known):
    target=norm(name)
    exact=[k for k in known if norm(k)==target]
    if exact:return exact[0]
    # Avoid risky fuzzy matching: only extremely close, unambiguous matches.
    scored=sorted(((difflib.SequenceMatcher(None,target,norm(k)).ratio(),k) for k in known),reverse=True)
    if scored and scored[0][0]>=0.91 and (len(scored)==1 or scored[0][0]-scored[1][0]>=0.08):return scored[0][1]
    return None

def ref_match(name,known):
    if not name:return None
    exact=team_match(name,known)
    if exact:return exact
    # Supports 'Brooks J.' -> 'John Brooks' only if unique.
    parts=norm(name).split()
    if len(parts)==2:
        matches=[k for k in known if (norm(k).split()[-1]==parts[0] and norm(k).split()[0].startswith(parts[1])) or (norm(k).split()[-1]==parts[1] and norm(k).split()[0].startswith(parts[0]))]
        if len(matches)==1:return matches[0]
    return None

def overrides():
    out={}
    if OVERRIDES.exists():
        with OVERRIDES.open(encoding='utf-8-sig',newline='') as f:
            for row in csv.DictReader(f):
                if row.get('league') and row.get('home') and row.get('away') and row.get('referee') and row.get('verified_source'):
                    out[(row['league'],norm(row['home']),norm(row['away']))]=row
    return out

def event_ref(event):
    # ESPN event schemas differ across competitions; read only explicit referee fields.
    comps=event.get('competitions') or []
    if not comps:return None
    for ref in comps[0].get('officials') or []:
        name=ref.get('displayName') or ref.get('fullName') or ref.get('name')
        if name:return name
    return None

def fetch_events(league,date,session):
    resp=session.get(BASE.format(league=league),params={'dates':date.strftime('%Y%m%d'),'limit':100},timeout=15)
    resp.raise_for_status();return resp.json().get('events',[])

def sync(path=DATA,days=8,network=True):
    with open(path,encoding='utf-8') as f: original=json.load(f)
    data=copy.deepcopy(original); now=dt.datetime.now(dt.timezone.utc)
    notes=[]; ov=overrides(); event_index={}
    if network:
        with requests.Session() as session:
            for lg in data['leagues']:
                for day in range(days+1):
                    date=(now+dt.timedelta(days=day)).date()
                    try: events=fetch_events(lg,date,session)
                    except (requests.RequestException,ValueError) as e:
                        notes.append(f'{lg} {date}: ESPN unavailable ({type(e).__name__})');continue
                    for ev in events:
                        comp=(ev.get('competitions') or [{}])[0]
                        sides=comp.get('competitors') or []
                        home=next((x for x in sides if x.get('homeAway')=='home'),None)
                        away=next((x for x in sides if x.get('homeAway')=='away'),None)
                        if not home or not away:continue
                        ht=team_match(home.get('team',{}).get('displayName',''),data['leagues'][lg]['teams'])
                        at=team_match(away.get('team',{}).get('displayName',''),data['leagues'][lg]['teams'])
                        if ht and at:event_index[(lg,ht,at)]=ev
                        else:notes.append(f'Unmatched ESPN teams {lg}: {home.get("team",{}).get("displayName")} / {away.get("team",{}).get("displayName")}')
    for fx in data.get('fixtures',[]):
        key=(fx['lg'],fx['h'],fx['a']); ev=event_index.get(key)
        ref_source=None; raw_ref=None
        override=ov.get((fx['lg'],norm(fx['h']),norm(fx['a'])))
        if override:
            raw_ref=override['referee'];ref_source=override['verified_source']
        elif ev:
            raw_ref=event_ref(ev)
            if raw_ref:ref_source='ESPN event officials'
        if raw_ref:
            match=ref_match(raw_ref,data['leagues'][fx['lg']].get('refs',{}))
            fx['ref']=match or raw_ref
            fx['ref_status']='appointed'
            fx['ref_source']=ref_source
            fx['ref_stats_available']=bool(match)
        elif fx.get('ref'):
            # Keep last known appointment; don't erase it on a missing response.
            fx['ref_status']='previously_recorded_unverified'
            fx.setdefault('ref_source','existing JSON')
        else:
            fx['ref_status']='not_confirmed'
            fx['ref_source']=None
        if ev:
            state=ev.get('status',{}).get('type',{}).get('name','')
            if state in ('STATUS_FINAL','STATUS_FULL_TIME'):
                fx['_completed']=True
            # Kickoff changes are only applied for confirmed matching ESPN events.
            if ev.get('date') and not fx.get('_completed'):
                fx['t']=ev['date'].replace('+00:00','Z')
    data['fixtures']=[f for f in data['fixtures'] if not f.pop('_completed',False) and dt.datetime.fromisoformat(f['t'].replace('Z','+00:00'))>=now]
    data['updated']=now.strftime('%Y-%m-%dT%H:%M:%SZ')
    data['sync_meta']={'last_attempt':data['updated'],'sources':['ESPN scoreboard (best effort)','verified referee CSV'], 'warnings':notes[:100]}
    # Safe atomic replace, with backup; never destroy original on error.
    backup=Path(str(path)+'.bak');backup.write_text(json.dumps(original,ensure_ascii=False,indent=2),encoding='utf-8')
    fd,tmp=tempfile.mkstemp(dir=str(Path(path).parent),suffix='.json')
    try:
        with os.fdopen(fd,'w',encoding='utf-8') as f:json.dump(data,f,ensure_ascii=False,indent=2)
        os.replace(tmp,path)
    finally:
        if os.path.exists(tmp):os.unlink(tmp)
    return {'fixtures':len(data['fixtures']),'warnings':len(notes),'referees_verified':sum(f.get('ref_status')=='appointed' for f in data['fixtures'])}

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--offline',action='store_true');args=parser.parse_args()
    print(sync(network=not args.offline))
