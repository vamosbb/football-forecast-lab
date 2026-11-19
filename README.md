# Football Forecast Lab

Independent five-league football prediction application based on the supplied `football_model_data.json` and `model_guide.md`.

## Run locally

```bash
python -m pip install -r requirements.txt
streamlit run app.py
```

## Update data

```bash
python sync.py
```

The sync script attempts to read ESPN's publicly reachable scoreboard endpoints (which are **not guaranteed to be a supported official API**). It matches teams conservatively, reads referee appointments only when ESPN explicitly supplies them, and retains existing referee names if the endpoint lacks an appointment. It does **not** automatically scrape Flashscore. If a referee is shown on Flashscore or an official league page, enter a **verified** appointment in `referee_overrides.csv` with league code, exact JSON home and away team names, full referee name, and source description. The CSV takes priority over ESPN. Do not add unverified appointments.

Example CSV row (illustrative only; not an actual appointment):

`en.1,Example Home FC,Example Away FC,Example Referee,official league appointment page`

No unsupported live cards/corners/standings update is attempted. The existing figures are retained, and `asof` is not artificially advanced. The app displays the source data's actual date. A `.bak` copy is written before each successful sync. The script removes expired fixtures based on UTC kickoff; postponements are handled only when ESPN supplies a matching event and updated kickoff. A production implementation should also use fixture IDs and an official results feed to distinguish postponements from completed matches.

### Referee status logic

- `appointed`: verified CSV or explicit ESPN referee field.
- `previously_recorded_unverified`: name in original JSON, but not verified in current sync.
- `not_confirmed`: no referee appointment available. **This is not a claim that no referee has been appointed.**
- Missing historical referee stats: display referee name and use factor `1`, per existing model logic.

### Automation

For scheduled updates, run `python sync.py` using your server cron or a GitHub Actions scheduled workflow. A Streamlit frontend alone cannot guarantee background refreshes. ESPN availability, rate limits, terms and any data licensing should be reviewed before production deployment. Flashscore may require a licensed provider or a separately maintained verified appointments feed.

### Model limitations

The supplied formulas are preserved: Dixon–Coles-adjusted 0–9 Poisson score matrix, shrinkage/Poisson corners and cards, fair odds, EV and quarter Kelly. Weather and Sportradar comparison values are displayed only as existing data; they do not modify probabilities. No claims of live verification are made.
