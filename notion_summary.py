#!/usr/bin/env python3
"""
notion_summary.py — Query Notion Part Times database and send a daily
status-breakdown summary to Slack at 10:00 AM IST.
"""
import os
import requests
from collections import Counter
from datetime import datetime

NOTION_TOKEN    = os.environ.get("NOTION_TOKEN")
SLACK_WEBHOOK   = os.environ.get("SLACK_WEBHOOK_URL")
NOTION_DB_ID    = "37e597e1da6880f38e03e9a18fda164b"  # Part Time Jobs v2
NOTION_PAGE_URL = "https://www.notion.so/hasanmdaadhil/37e597e1da6880f38e03e9a18fda164b"

STATUS_EMOJI = {
    "New":          "🆕",
    "Applied":      "📤",
    "Interview":    "🎯",
    "Offer":        "🎉",
    "Not Applied":  "⏭️",
    "Rejected":     "❌",
}


def query_all_pages() -> list:
    """Fetch all pages from the v2 database using the standard query endpoint."""
    if not NOTION_TOKEN:
        print("NOTION_TOKEN not set — aborting")
        return []

    headers = {
        "Authorization": f"Bearer {NOTION_TOKEN}",
        "Content-Type": "application/json",
        "Notion-Version": "2022-06-28",
    }

    pages, cursor = [], None
    while True:
        body: dict = {"page_size": 100}
        if cursor:
            body["start_cursor"] = cursor

        resp = requests.post(
            f"https://api.notion.com/v1/databases/{NOTION_DB_ID}/query",
            headers=headers,
            json=body,
            timeout=30,
        )

        if resp.status_code != 200:
            print(f"Notion query error {resp.status_code}: {resp.text[:300]}")
            return []

        data = resp.json()
        pages.extend(data.get("results", []))

        if not data.get("has_more"):
            break
        cursor = data.get("next_cursor")

    return pages


def get_status(page: dict) -> str:
    """Extract the Status select value from a Notion page."""
    select = page.get("properties", {}).get("Status", {}).get("select")
    return select.get("name", "Unknown") if select else "Unknown"


def build_slack_payload(status_counts: Counter, total: int) -> dict:
    date_str = datetime.now().strftime("%b %d, %Y")

    lines = []
    for status, count in status_counts.most_common():
        emoji = STATUS_EMOJI.get(status, "•")
        lines.append(f"{emoji} *{status}*: {count}")

    breakdown = "\n".join(lines) if lines else "_No jobs found_"

    blocks = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": f"📊 Job Tracker — {date_str}"},
        },
        {"type": "divider"},
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*Total Jobs*\n{total}"},
                {"type": "mrkdwn", "text": f"*Statuses*\n{len(status_counts)} types"},
            ],
        },
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": breakdown},
        },
        {"type": "divider"},
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Open Notion →"},
                    "url": NOTION_PAGE_URL,
                    "style": "primary",
                }
            ],
        },
    ]
    return {"blocks": blocks}


def main() -> None:
    print(f"[{datetime.now():%Y-%m-%d %H:%M}] Fetching Notion job tracker summary…")

    pages = query_all_pages()
    if not pages:
        print("No pages returned — skipping Slack")
        return

    status_counts = Counter(get_status(p) for p in pages)
    total = len(pages)

    print(f"Total jobs: {total}")
    for status, count in status_counts.most_common():
        print(f"  {status}: {count}")

    if not SLACK_WEBHOOK:
        print("SLACK_WEBHOOK_URL not set — skipping Slack")
        return

    payload = build_slack_payload(status_counts, total)
    resp = requests.post(SLACK_WEBHOOK, json=payload, timeout=10)
    if resp.status_code == 200:
        print("Slack summary sent ✅")
    else:
        print(f"Slack error {resp.status_code}: {resp.text}")


if __name__ == "__main__":
    main()
