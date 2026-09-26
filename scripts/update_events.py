name: Refresh NorCal Live Music Events

on:
  schedule:
    # 9:00 UTC = 1:00 AM Pacific (PDT, UTC-8 in winter / UTC-7 in summer)
    # Monday/Wednesday/Friday only — not daily. This project queries two
    # states (CA + NV) statewide and keeps only a fraction of what comes
    # back, so a daily cron would exceed Jambase's free-tier 1,000
    # calls/month limit. 3x/week keeps calls comfortably under that cap
    # (see scripts/update_events.py's DAYS_AHEAD comment for the other
    # half of this budget).
    - cron: '0 9 * * 1,3,5'
  workflow_dispatch:

jobs:
  update:
    runs-on: ubuntu-latest

    permissions:
      contents: write

    steps:
      - name: Checkout repository
        uses: actions/checkout@v4

      - name: Set up Python 3.12
        uses: actions/setup-python@v5
        with:
          python-version: '3.12'

      - name: Install dependencies
        run: pip install requests

      - name: Fetch events and write events.js
        env:
          JAMBASE_API_KEY: ${{ secrets.JAMBASE_API_KEY }}
        run: python scripts/update_events.py

      - name: Commit and push if events.js changed
        run: |
          git config user.name  "github-actions[bot]"
          git config user.email "github-actions[bot]@users.noreply.github.com"
          git add events.js
          if git diff --staged --quiet; then
            echo "No new events — events.js unchanged, skipping commit."
          else
            DATESTAMP=$(date -u +'%Y-%m-%d')
            git commit -m "chore: refresh NorCal live music events for ${DATESTAMP}"
            git push
          fi
