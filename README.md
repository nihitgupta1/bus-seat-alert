# Bus Seat Alert

A Python script that checks redBus listings for selected buses and sends an [ntfy](https://ntfy.sh) notification when few bookable single berths remain.

It uses Playwright to load the results and inspect seat maps. The GitHub Actions workflow runs the checker on a schedule, keeps alert state between runs, and uploads debug screenshots and HTML when a results page cannot be loaded.

## Configuration and alerts

The route, travel dates, operator and bus filters, departure-time cutoff, traveller eligibility, and seat-availability thresholds are configurable constants near the top of `check_seats.py`.

The checker sends an alert when the available qualifying single seats meet the configured threshold. It avoids alerting when no seats are available, and re-arms an alert after availability recovers. Seat counting can account for gender-restricted seats according to the configured traveller type.

## Notifications

The checker sends notifications through ntfy. Set the `NTFY_TOPIC` environment variable to your ntfy topic. Do not commit the topic to the repository.

For GitHub Actions:

1. Create a topic in ntfy and subscribe to it in the ntfy app.
2. In the repository, open **Settings → Secrets and variables → Actions**.
3. Add a repository secret named `NTFY_TOPIC` containing the topic name.
4. Ensure GitHub Actions is enabled for the repository.

The workflow has permission to write `state.json`. Treat the topic name as a secret: anyone who knows it may be able to publish notifications to it.

## Run locally

Requires Python 3.12 (the version used by GitHub Actions) and Google Chrome via Playwright's Chrome channel.

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python -m playwright install chrome
```

Set `NTFY_TOPIC` in your environment, then run:

```bash
python check_seats.py
```

To send a test notification without checking bus listings:

```bash
python check_seats.py --test-notify
```

If `NTFY_TOPIC` is unset, the script prints the notification it would have sent instead.

## GitHub Actions

The workflow is `.github/workflows/seat-check.yml`.

- The workflow runs one check every hour.
- Use **Actions → Check bus seats → Run workflow** for a manual single check. Enable `loop` to repeat checks about every 30 minutes for up to 5 hours and 40 minutes.
- Enable `test_notify` to send a test notification only.
- `state.json` tracks the latest seat counts and whether an alert has already been sent. The workflow commits changes to this file.
- Debug screenshots and page HTML are uploaded as an Actions artifact when available.

The checker depends on redBus's live page structure and seat-map colors; changes to the site can affect detection. Check the Actions logs and debug artifact if a run cannot find results or seats.
