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
NOTION_TOKEN = os.environ.get("NOTION_TOKEN")
NOTION_DS_ID = "362597e1da6880c9b922000b20b90b02"  # Part Time Job Application data source
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


NOTION_PAGE_URL = "https://www.notion.so/362597e1da6880ae99bcf1b119f8ddaf"


def build_slack_summary(notion_count: int, total_raw: int, filtered: int) -> dict:
    date_str = datetime.now().strftime("%b %d, %Y")
    blocks = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": f"📋 Daily Job Report — {date_str}"},
        },
        {"type": "divider"},
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*Jobs added to Notion*\n✅ {notion_count}"},
                {"type": "mrkdwn", "text": f"*After filters*\n🔍 {filtered} of {total_raw} raw"},
            ],
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*Keywords:* {', '.join(KEYWORDS_LIST)}\n*Source:* {COUNTRY_INDEED.title()} Indeed · Remote only",
            },
        },
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "View in Notion →"},
                    "url": NOTION_PAGE_URL,
                    "style": "primary",
                }
            ],
        },
    ]
    return {"blocks": blocks}


def send_to_slack(notion_count: int, total_raw: int, filtered: int) -> None:
    if not SLACK_WEBHOOK:
        print("SLACK_WEBHOOK_URL not set — skipping Slack")
        return
    payload = build_slack_summary(notion_count, total_raw, filtered)
    resp = requests.post(SLACK_WEBHOOK, json=payload, timeout=10)
    if resp.status_code == 200:
        print(f"Slack: summary sent ({notion_count} jobs)")
    else:
        print(f"Slack error {resp.status_code}: {resp.text}")


def add_to_notion(job) -> bool:
    if not NOTION_TOKEN:
        return False

    title = str(job.get("title", "N/A"))[:100]
    company = str(job.get("company", "N/A"))
    job_url = str(job.get("job_url", "")).strip() or None

    salary_str = ""
    min_amt, max_amt = job.get("min_amount"), job.get("max_amount")
    if pd.notna(min_amt) and pd.notna(max_amt) and min_amt and max_amt:
        currency = job.get("currency", "")
        salary_str = f"{currency}{int(min_amt)}–{int(max_amt)}"

    props = {
        "Company": {"title": [{"text": {"content": company}}]},
        "Role": {"select": {"name": title}},
        "URL": {"url": job_url},
        "Status": {"select": {"name": "New"}},
        "Date Applied": {"date": {"start": datetime.now().strftime("%Y-%m-%d")}},
    }
    if salary_str:
        props["Salary"] = {"rich_text": [{"text": {"content": salary_str}}]}

    resp = requests.post(
        "https://api.notion.com/v1/pages",
        headers={
            "Authorization": f"Bearer {NOTION_TOKEN}",
            "Content-Type": "application/json",
            "Notion-Version": "2025-09-03",
        },
        json={
            "parent": {"type": "data_source_id", "data_source_id": NOTION_DS_ID},
            "properties": props,
        },
        timeout=10,
    )
    if resp.status_code != 200:
        print(f"  Notion error {resp.status_code}: {resp.text[:200]}")
    return resp.status_code == 200



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

    notion_count = sum(add_to_notion(job) for _, job in new_jobs.iterrows())
    print(f"Notion: added {notion_count}/{len(new_jobs)} rows")

    send_to_slack(notion_count=notion_count, total_raw=len(combined), filtered=len(filtered))

    seen.update(new_jobs["id"].astype(str).tolist())
    save_seen(seen)
    print("seen_jobs.json updated")


if __name__ == "__main__":
    main()
