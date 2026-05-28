#!/usr/bin/env python3
import os
import json
import requests
import pandas as pd
from pathlib import Path
from datetime import datetime
from jobspy import scrape_jobs

KEYWORDS = os.environ.get("JOB_KEYWORDS", "software engineer")
LOCATION = os.environ.get("JOB_LOCATION", "")
RESULTS_PER_SITE = int(os.environ.get("RESULTS_PER_SITE", "25"))
SLACK_WEBHOOK = os.environ.get("SLACK_WEBHOOK_URL")
MAX_SLACK_JOBS = int(os.environ.get("MAX_SLACK_JOBS", "10"))
SEEN_FILE = Path("seen_jobs.json")
SEEN_LIMIT = 2000


def load_seen() -> set:
    if SEEN_FILE.exists():
        return set(json.loads(SEEN_FILE.read_text()))
    return set()


def save_seen(seen: set) -> None:
    trimmed = list(seen)[-SEEN_LIMIT:]
    SEEN_FILE.write_text(json.dumps(trimmed))


def build_slack_payload(jobs_df) -> dict:
    date_str = datetime.now().strftime("%b %d, %Y")
    total = len(jobs_df)
    extra = max(0, total - MAX_SLACK_JOBS)

    blocks = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": f"Job Alerts — {date_str}"},
        },
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": f"*{total}* new listings for *{KEYWORDS}*{' in ' + LOCATION if LOCATION else ''}",
                }
            ],
        },
        {"type": "divider"},
    ]

    for _, job in jobs_df.head(MAX_SLACK_JOBS).iterrows():
        title = str(job.get("title", "N/A"))
        company = str(job.get("company", "N/A"))
        location = str(job.get("location", "")).strip() or "Remote"
        url = str(job.get("job_url", "")).strip()
        site = str(job.get("site", "")).capitalize()

        salary = ""
        min_amt, max_amt = job.get("min_amount"), job.get("max_amount")
        if pd.notna(min_amt) and pd.notna(max_amt) and min_amt and max_amt:
            currency = job.get("currency", "")
            salary = f" · {currency}{int(min_amt)}–{int(max_amt)}"

        title_text = f"<{url}|{title}>" if url else title
        blocks.append(
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*{title_text}*\n{company} · {location}{salary} · _{site}_",
                },
            }
        )

    if extra > 0:
        blocks.append(
            {
                "type": "context",
                "elements": [{"type": "mrkdwn", "text": f"_+{extra} more jobs not shown_"}],
            }
        )

    return {"blocks": blocks}


def send_to_slack(jobs_df) -> None:
    if not SLACK_WEBHOOK:
        print("SLACK_WEBHOOK_URL not set — skipping Slack")
        return

    payload = build_slack_payload(jobs_df)
    resp = requests.post(SLACK_WEBHOOK, json=payload, timeout=10)
    if resp.status_code == 200:
        print(f"Slack: sent {min(len(jobs_df), MAX_SLACK_JOBS)} jobs")
    else:
        print(f"Slack error {resp.status_code}: {resp.text}")


def scrape_site(site: str) -> pd.DataFrame:
    try:
        df = scrape_jobs(
            site_name=[site],
            search_term=KEYWORDS,
            location=LOCATION if LOCATION else None,
            results_wanted=RESULTS_PER_SITE,
            hours_old=26,
        )
        print(f"  {site}: {len(df)} jobs")
        return df
    except Exception as e:
        print(f"  {site}: failed — {e}")
        return pd.DataFrame()


def main() -> None:
    # Ensure file exists so git can always track it
    if not SEEN_FILE.exists():
        SEEN_FILE.write_text("[]")

    print(f"[{datetime.now():%Y-%m-%d %H:%M}] keywords='{KEYWORDS}' location='{LOCATION or 'any'}'")

    frames = [scrape_site(s) for s in ["indeed", "naukri"]]
    jobs = pd.concat([f for f in frames if not f.empty], ignore_index=True) if any(not f.empty for f in frames) else pd.DataFrame()

    print(f"Fetched: {len(jobs)} jobs total")

    if jobs.empty:
        print("No jobs returned from any site")
        return

    seen = load_seen()
    new_jobs = jobs[~jobs["id"].isin(seen)].dropna(subset=["id"]).copy()

    print(f"New (unseen): {len(new_jobs)}")

    if new_jobs.empty:
        print("No new jobs today")
        return

    send_to_slack(new_jobs)

    seen.update(new_jobs["id"].astype(str).tolist())
    save_seen(seen)
    print("seen_jobs.json updated")


if __name__ == "__main__":
    main()
