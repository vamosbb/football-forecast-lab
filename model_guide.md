# Football prediction model: data guide

The attached file `football_model_data.json` is the full data behind the model on Khatia's prediction page (top 5 European leagues, season 2026/27). Use it to calculate probabilities for any match in it.

## Structure

- `k`: shrink weight in games (12) for attack/defence.
- `leagues` with keys `en.1` (Premier League), `es.1` (La Liga), `de.1` (Bundesliga), `it.1` (Serie A) and `fr.1` (Ligue 1). Each league has:
  - `avgH`, `avgA`: league average goals for the home side and the away side.
  - `rho`: the Dixon-Coles parameter.
  - `asof`: the date of the table.
  - `teams`: one entry per team:
    - `att`, `dfn`: attack strength and defensive weakness (1.0 = league average).
    - `P`, `F`, `A`: matches played, goals scored, goals conceded.
    - `cd`: `{n: matches, y: yellow cards, r: red cards}`.
    - `cr`: `{n: matches, f: corners won per match, h: at home, a: away, ag: corners conceded per match}`.
    - `city`: `[venue city, latitude, longitude]`.
  - `ext`: league averages for corners and cards:
    - `kc`: cards per team per match.
    - `cf`: corners per team per match.
    - `ch`, `ca`: corners won at home and away.
    - `rbar`: average cards per match across the referee table.
    - `ks`: shrink weight in games (5).
  - `refs`: `{referee: {n: matches, c: cards per match, f: model factor}}`.
- `fixtures`: upcoming matches. Each has:
  - `lg`, `t` (kick-off time, UTC), `h` (home), `a` (away).
  - `o`: [home, draw, away] probability from Sportradar, for comparison.
  - `ref`: the referee, if one has been appointed.
  - `w`: the weather (`hi` high, `lo` low, `pc` chance of rain %, `pt` type of precipitation).

## Formulas (the page uses exactly these)

**Goals.**
- λ_home = avgH × att_home × dfn_away
- λ_away = avgA × att_away × dfn_home
- Score matrix 0–9 × 0–9: Poisson(λ_home, i) × Poisson(λ_away, j) × τ(i, j), then normalised so it sums to 1.
- Dixon-Coles τ:
  - (0,0): 1 − λh·λa·ρ
  - (0,1): 1 + λh·ρ
  - (1,0): 1 + λa·ρ
  - (1,1): 1 − ρ
  - every other score: 1
- 1X2, totals, both teams to score and handicaps all come from this matrix.

**Corners.** shr(v, n, prior, k) = (v·n + prior·k) / (n + k).
- home_won = shr(h, n/2, f·ch/cf, ks)
- away_won = shr(a, n/2, f·ca/cf, ks)
- conceded = shr(ag, n, cf, ks)
- λ_home_corners = home_won(home) × conceded(away) / cf
- λ_away_corners = away_won(away) × conceded(home) / cf
- Each side is Poisson. The total is Poisson(λh + λa).

**Cards** (yellow + red, each card = 1).
- rate = shr((y + r)/n, n, kc, ks)
- λ = rate × referee factor f. Use f = 1 when no referee has been appointed.
- Each side is Poisson.

**Fair odds and stakes.**
- Fair odds = 1 / probability.
- EV = p × odds − 1.
- Stake = bankroll × (p·odds − 1)/(odds − 1) / 4 (quarter Kelly).

## Caveats

- The model does not see injuries, line-ups, rotation or motivation.
- Real corner and card counts vary more than Poisson assumes, so the probabilities for extreme lines are slightly too confident.
- The corners and cards markets have not been tested against last season.
