#!/usr/bin/env python3
import os
import json
import time
import requests
import pandas as pd
from pathlib import Path
from datetime import datetime
from jobspy import scrape_jobs

# --- Config from env ---
KEYWORDS_RAW = os.environ.get("JOB_KEYWORDS", "meta ads")
KEYWORDS_LIST = [k.strip() for k in KEYWORDS_RAW.split(",") if k.strip()]
COUNTRY_INDEED = os.environ.get("COUNTRY_INDEED", "india")
IS_REMOTE = os.environ.get("IS_REMOTE", "true").lower() == "true"
RESULTS_PER_KW = int(os.environ.get("RESULTS_PER_KW", "10"))
SLACK_WEBHOOK = os.environ.get("SLACK_WEBHOOK_URL")
MAX_SLACK_JOBS = int(os.environ.get("MAX_SLACK_JOBS", "15"))
HOURS_OLD = int(os.environ.get("HOURS_OLD", "26"))
SEEN_FILE = Path("seen_jobs.json")
SEEN_LIMIT = 5000

# --- Tier-1: title must contain one of these (direct match) ---
RELEVANT_TITLE_TERMS = [
    "meta ads", "facebook ads", "instagram ads", "paid social",
    "google ads", "adwords", "ppc", "paid search", "paid media",
    "performance marketing", "performance marketer",
    "media buyer", "media buying",
    "digital advertising", "biddable",
    "sem specialist", "sem manager", "sem executive",
]

# --- Tier-2: "digital marketing" titles pass only if description mentions one of these ---
PAID_ADS_DESCRIPTION_SIGNALS = [
    "meta ads", "facebook ads", "instagram ads", "paid social",
    "google ads", "adwords", "ppc", "paid search", "paid media",
    "performance marketing", "media buying", "digital advertising",
]

# --- Agency signals in description: exclude if any match ---
AGENCY_DESCRIPTION_SIGNALS = [
    "our clients", "for our clients", "for clients", "client accounts",
    "manage client", "portfolio of clients", "working with clients",
    "client campaigns", "client budgets", "on behalf of clients",
    "multiple clients", "agency environment", "client-facing",
    "client servicing", "handle client", "serve clients",
]

# --- Agency signals in company name: exclude if any match ---
AGENCY_COMPANY_TERMS = [
    " agency", "media agency", "marketing agency", "digital agency",
    "advertising agency", "ad agency", "consultancy", " consulting",
    "media solutions", "marketing solutions",
]


def load_seen() -> set:
    if SEEN_FILE.exists():
        return set(json.loads(SEEN_FILE.read_text()))
    return set()


def save_seen(seen: set) -> None:
    SEEN_FILE.write_text(json.dumps(list(seen)[-SEEN_LIMIT:]))


def is_relevant(job) -> bool:
    title = str(job.get("title", "")).lower()
    description = str(job.get("description", "")).lower()
    company = str(job.get("company", "")).lower()

    tier1_match = any(term in title for term in RELEVANT_TITLE_TERMS)
    is_digital_marketing_title = "digital marketing" in title

    if tier1_match:
        pass  # direct match — proceed to agency check
    elif is_digital_marketing_title:
        # Accept only if description explicitly mentions paid ads work
        if not any(sig in description for sig in PAID_ADS_DESCRIPTION_SIGNALS):
            return False
    else:
        return False  # not a paid ads role

    if any(signal in description for signal in AGENCY_DESCRIPTION_SIGNALS):
        return False

    if any(term in company for term in AGENCY_COMPANY_TERMS):
        return False

    return True


def scrape_keyword(keyword: str, site: str) -> pd.DataFrame:
    try:
        kwargs = dict(
            site_name=[site],
            search_term=keyword,
            is_remote=IS_REMOTE,
            results_wanted=RESULTS_PER_KW,
            hours_old=HOURS_OLD,
        )
        if site == "indeed":
            kwargs["country_indeed"] = COUNTRY_INDEED
        df = scrape_jobs(**kwargs)
        print(f"  [{site}] '{keyword}': {len(df)} raw")
        return df
    except Exception as e:
        print(f"  [{site}] '{keyword}': failed — {e}")
        return pd.DataFrame()


def build_slack_payload(jobs_df, total_raw: int) -> dict:
    date_str = datetime.now().strftime("%b %d, %Y")
    total = len(jobs_df)
    extra = max(0, total - MAX_SLACK_JOBS)

    blocks = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": f"Paid Ads Job Alerts — {date_str}"},
        },
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": (
                        f"*{total}* relevant brand jobs found "
                        f"(filtered from {total_raw} raw) · "
                        f"Remote · {COUNTRY_INDEED.title()} Indeed"
                    ),
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
        date_posted = job.get("date_posted")
        posted_str = f" · Posted {date_posted}" if pd.notna(date_posted) and date_posted else ""

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
                    "text": f"*{title_text}*\n{company} · {location}{salary}{posted_str}",
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


def send_to_slack(jobs_df, total_raw: int) -> None:
    if not SLACK_WEBHOOK:
        print("SLACK_WEBHOOK_URL not set — skipping Slack")
        return

    payload = build_slack_payload(jobs_df, total_raw)
    resp = requests.post(SLACK_WEBHOOK, json=payload, timeout=10)
    if resp.status_code == 200:
        print(f"Slack: sent {min(len(jobs_df), MAX_SLACK_JOBS)} jobs")
    else:
        print(f"Slack error {resp.status_code}: {resp.text}")


def main() -> None:
    if not SEEN_FILE.exists():
        SEEN_FILE.write_text("[]")

    print(
        f"[{datetime.now():%Y-%m-%d %H:%M}] "
        f"keywords={len(KEYWORDS_LIST)} · country='{COUNTRY_INDEED}' · remote={IS_REMOTE}"
    )
    print(f"Keywords: {', '.join(KEYWORDS_LIST)}")

    # Scrape every keyword × site with a small delay to avoid rate limits
    all_frames = []
    for keyword in KEYWORDS_LIST:
        for site in ["indeed", "naukri"]:
            df = scrape_keyword(keyword, site)
            all_frames.append(df)
            time.sleep(1)

    if not any(not f.empty for f in all_frames):
        print("No jobs returned from any keyword/site")
        return

    # Combine and deduplicate by job ID
    combined = pd.concat([f for f in all_frames if not f.empty], ignore_index=True)
    combined = combined.drop_duplicates(subset=["id"]).dropna(subset=["id"])
    print(f"\nTotal raw (deduped): {len(combined)}")

    # Apply relevance + agency filters
    filtered = combined[combined.apply(is_relevant, axis=1)].copy()
    print(f"After relevance + agency filter: {len(filtered)}")

    if filtered.empty:
        print("No matching jobs after filtering")
        return

    # Deduplicate against seen
    seen = load_seen()
    new_jobs = filtered[~filtered["id"].isin(seen)].copy()
    print(f"New (unseen): {len(new_jobs)}")

    if new_jobs.empty:
        print("No new jobs today")
        return

    send_to_slack(new_jobs, total_raw=len(combined))

    seen.update(new_jobs["id"].astype(str).tolist())
    save_seen(seen)
    print("seen_jobs.json updated")


if __name__ == "__main__":
    main()
