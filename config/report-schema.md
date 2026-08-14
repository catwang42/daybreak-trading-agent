# Report Schema — every /daily-scan output MUST follow this order
## reports/<date>/daily-brief.md
1. **Market Overview** — indices, sector performance, breadth, VIX (1 short paragraph + table)
2. **Macro & Events Today** — economic calendar, notable earnings
3. **Sector Opportunity Map** — where momentum/rotation is, tied to preferences.md sectors
4. **Shortlist** — table: ticker | sector | why it surfaced | signal-bundle summary (M3+) | quick rating
5. **Deep Analysis** (M2+) — link per ticker to deep/<ticker>.md
6. **Options Candidates** (M4+) — table: ticker | strategy (CSP/CC) | strike | exp | delta | premium | annualized yield | earnings flag
7. **Degraded Sources** — list any missing/failed data sources, or "none"
8. **Disclaimer footer** (mandatory, verbatim): "Automated research output for personal study. Not financial advice. Paper trading only. Verify all data before acting."
## reports/<date>/deep/<ticker>.md (per ticker, M2+)
1. Verdict: **Buy / Overweight / Hold / Underweight / Sell** + soft price target + confidence (L/M/H)
2. Analyst summaries (fundamentals / technical / news / sentiment — max 1 paragraph each)
3. Bull vs Bear — strongest argument each side + arbiter's resolution
4. Trade proposal (trader)
5. Risk review — aggressive/conservative/neutral takes + judge's ruling
6. Options view (M4+)
7. Data sources used + timestamps
## journal/journal.jsonl (append one line per recommendation)
{"date":"","ticker":"","verdict":"","target":null,"confidence":"","options":null,"signal_sources":[],"report":"reports/<date>/deep/<ticker>.md","outcome_7d":null,"outcome_30d":null}
