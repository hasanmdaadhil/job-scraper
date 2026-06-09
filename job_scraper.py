#!/usr/bin/env python3
import os
import re
import json
import time
import requests
import pandas as pd
from pathlib import Path
from datetime import datetime
from jobspy import scrape_jobs

# --- Config from env ---
KEYWORDS_RAW     = os.environ.get("JOB_KEYWORDS", "meta ads")
KEYWORDS_LIST    = [k.strip() for k in KEYWORDS_RAW.split(",") if k.strip()]
COUNTRY_INDEED   = os.environ.get("COUNTRY_INDEED", "india")
IS_REMOTE        = os.environ.get("IS_REMOTE", "true").lower() == "true"
RESULTS_PER_KW   = int(os.environ.get("RESULTS_PER_KW", "10"))
SLACK_WEBHOOK    = os.environ.get("SLACK_WEBHOOK_URL")
MAX_SLACK_JOBS   = int(os.environ.get("MAX_SLACK_JOBS", "15"))
HOURS_OLD        = int(os.environ.get("HOURS_OLD", "48"))
NOTION_TOKEN     = os.environ.get("NOTION_TOKEN")
OPENROUTER_KEY   = os.environ.get("OPENROUTER_API_KEY")
NOTION_DS_ID     = "362597e1da6880c9b922000b20b90b02"
SEEN_FILE        = Path("seen_jobs.json")
SEEN_LIMIT       = 5000
LLM_MODEL        = "anthropic/claude-3-5-haiku"

# ── Candidate profile used in the LLM prompt ────────────────────────────────
CANDIDATE_PROFILE = """
You are filtering job listings for a paid digital advertising specialist based in India.

Candidate profile:
- Core expertise: Meta Ads (Facebook/Instagram Ads) and Google Ads
- Seeking: Remote, part-time / freelance / contract work
- Target employers: Brands that sell their OWN products or services
  (D2C, e-commerce, SaaS, retail brands, healthcare brands, hospitality, etc.)

ACCEPT a job ONLY if:
  • Running Meta Ads and/or Google Ads is the PRIMARY responsibility
  • The employer is a brand (not an agency managing client accounts)
  • The role is remote / work-from-home

REJECT if ANY of the following apply:
  • Agency job — the company manages ads for multiple external clients
  • Intern / fresher / trainee / management trainee / junior (0–1 yr exp)
  • Paid ads is SECONDARY — main job is SEO, social media content, video editing,
    content creation, graphic design, or general digital marketing
  • Sales / BDE / business development / inside sales role
  • Role requires Hindi language proficiency
  • Amazon marketplace / Amazon PPC (not Google or Meta)
  • In-person, on-site, or relocation required
  • Manufacturing context "PPC" (production planning, garments, inventory control)
  • Company name is blank, "nan", or clearly a fake/placeholder listing
  • Role is for a developer, web designer, or primarily technical position
""".strip()

# ── Keyword filter (fallback when no OPENROUTER_API_KEY) ────────────────────
RELEVANT_TITLE_TERMS = [
    "meta ads", "facebook ads", "instagram ads", "paid social",
    "google ads", "adwords", "ppc", "paid search", "paid media",
    "performance marketing", "performance marketer",
    "media buyer", "media buying", "digital advertising", "biddable",
    "sem specialist", "sem manager", "sem executive",
]
PAID_ADS_DESC_SIGNALS = [
    "meta ads", "facebook ads", "instagram ads", "paid social",
    "google ads", "adwords", "ppc", "paid search", "paid media",
    "performance marketing", "media buying", "digital advertising",
]
EXCLUDED_TITLE_TERMS = [
    "intern", "internship", "fresher", "freshers", "trainee", "management trainee",
    "junior", "jr.", "entry level", "trainer", "faculty", "professor",
    "inside sales", "business advisor", "business development", "business analyst",
    "growth consultant", "video editor", "graphic designer", "content writer",
    "copywriter", "web developer", "web designer", "full stack",
    "production planning", "ppc engineer", "ppc manager - garment",
    "ppc manager - production",
]
MANUFACTURING_PPC_SIGNALS = [
    "production planning", "inventory", "garments", "textile", "manufacturing",
    "procurement", "supply chain", "warehouse",
]
SEO_PRIMARY_SIGNALS = [
    "seo specialist", "seo manager", "search engine optimization",
    "on-page seo", "off-page seo", "link building", "keyword research for seo",
    "organic traffic", "seo audit",
]
DESCRIPTION_HARD_EXCLUSIONS = [
    "hindi mandatory", "hindi required", "must know hindi", "fluent in hindi",
    "hindi speaking", "hindi language required", "proficiency in hindi",
    "amazon ppc", "amazon ads", "amazon advertising", "amazon seller",
    "amazon marketplace", "amazon dsp",
    "work from office", "must be present", "on-site only", "office only",
    "relocate to", "relocation required",
]
AGENCY_DESCRIPTION_SIGNALS = [
    "our clients", "for our clients", "for clients", "client accounts",
    "manage client", "portfolio of clients", "working with clients",
    "client campaigns", "client budgets", "on behalf of clients",
    "multiple clients", "agency environment", "client-facing",
    "client servicing", "handle client", "serve clients",
]
AGENCY_COMPANY_TERMS = [
    " agency", "media agency", "marketing agency", "digital agency",
    "advertising agency", "ad agency", "consultancy", " consulting",
    "media solutions", "marketing solutions",
    "offshore marketers", "webconsult", "web consult", "brandclever",
    "vsplash", "elevate media",
]


def keyword_filter(job) -> bool:
    """Rule-based relevance filter (used as fallback when LLM is unavailable)."""
    title   = str(job.get("title", "")).lower()
    desc    = str(job.get("description", "")).lower()
    company = str(job.get("company", "")).lower().strip()

    if not company or company in ("nan", "none", "n/a", ""):
        return False
    if any(t in title for t in EXCLUDED_TITLE_TERMS):
        return False
    if "ppc" in title and any(s in desc for s in MANUFACTURING_PPC_SIGNALS):
        return False
    if any(s in desc for s in DESCRIPTION_HARD_EXCLUSIONS):
        return False

    tier1 = any(t in title for t in RELEVANT_TITLE_TERMS)
    is_dm = "digital marketing" in title

    if tier1:
        pass
    elif is_dm:
        if not any(s in desc for s in PAID_ADS_DESC_SIGNALS):
            return False
        if any(s in desc for s in SEO_PRIMARY_SIGNALS):
            return False
    else:
        return False

    if any(s in desc for s in AGENCY_DESCRIPTION_SIGNALS):
        return False
    if any(t in company for t in AGENCY_COMPANY_TERMS):
        return False

    return True


# ── LLM filter ───────────────────────────────────────────────────────────────

def _parse_llm_json(raw: str) -> list:
    """Extract JSON array from LLM response, tolerating markdown fences."""
    raw = raw.strip()
    # Strip ```json ... ``` fences if present
    raw = re.sub(r"^```(?:json)?\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)
    return json.loads(raw)


def llm_batch_filter(jobs_df: pd.DataFrame) -> pd.DataFrame:
    """
    Send all jobs to the LLM in one call. Returns the subset the LLM ACCEPTs.
    Each job gets a numeric index used as its LLM-side ID to keep the prompt short.
    """
    if jobs_df.empty:
        return jobs_df

    # Build compact job list (index → job info)
    job_list = []
    for i, (_, job) in enumerate(jobs_df.iterrows()):
        desc_snippet = str(job.get("description", ""))[:500].replace("\n", " ")
        job_list.append({
            "idx": i,
            "title": str(job.get("title", ""))[:120],
            "company": str(job.get("company", ""))[:80],
            "description": desc_snippet,
        })

    prompt = (
        f"{CANDIDATE_PROFILE}\n\n"
        "Evaluate each job below. "
        "Return ONLY a JSON array — no explanation, no markdown fences:\n"
        '[{"idx": 0, "verdict": "ACCEPT", "reason": "..."}, ...]\n\n'
        f"Jobs:\n{json.dumps(job_list, indent=2)}"
    )

    try:
        resp = requests.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {OPENROUTER_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": LLM_MODEL,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0,
            },
            timeout=90,
        )
        resp.raise_for_status()
        content = resp.json()["choices"][0]["message"]["content"]
        verdicts = _parse_llm_json(content)
    except Exception as e:
        print(f"  LLM filter failed ({e}) — falling back to keyword filter")
        return jobs_df[jobs_df.apply(keyword_filter, axis=1)].copy()

    # Log each verdict
    accepted_idx = set()
    for v in verdicts:
        idx     = v.get("idx")
        verdict = v.get("verdict", "").upper()
        reason  = v.get("reason", "")
        icon    = "✅" if verdict == "ACCEPT" else "❌"
        title   = job_list[idx]["title"] if idx is not None and idx < len(job_list) else "?"
        print(f"  {icon} [{verdict}] {title[:60]} — {reason}")
        if verdict == "ACCEPT" and idx is not None:
            accepted_idx.add(idx)

    # Rebuild DataFrame preserving original index
    accepted_rows = [
        row for i, (_, row) in enumerate(jobs_df.iterrows())
        if i in accepted_idx
    ]
    if not accepted_rows:
        return pd.DataFrame(columns=jobs_df.columns)
    return pd.DataFrame(accepted_rows)


def apply_filters(jobs_df: pd.DataFrame) -> pd.DataFrame:
    """Route to LLM filter if available, otherwise use keyword filter."""
    if OPENROUTER_KEY:
        print("Using LLM filter (OpenRouter)…")
        return llm_batch_filter(jobs_df)
    else:
        print("Using keyword filter (no OPENROUTER_API_KEY set)…")
        return jobs_df[jobs_df.apply(keyword_filter, axis=1)].copy()


# ── Seen-jobs cache ──────────────────────────────────────────────────────────

def load_seen() -> set:
    if SEEN_FILE.exists():
        return set(json.loads(SEEN_FILE.read_text()))
    return set()


def save_seen(seen: set) -> None:
    SEEN_FILE.write_text(json.dumps(list(seen)[-SEEN_LIMIT:]))


# ── Scraping ─────────────────────────────────────────────────────────────────

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


# ── Slack ─────────────────────────────────────────────────────────────────────

NOTION_PAGE_URL = "https://www.notion.so/362597e1da6880ae99bcf1b119f8ddaf"


def build_slack_summary(notion_count: int, total_raw: int, filtered: int) -> dict:
    date_str = datetime.now().strftime("%b %d, %Y")
    mode     = "LLM" if OPENROUTER_KEY else "keyword"
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
                {"type": "mrkdwn", "text": f"*After {mode} filter*\n🔍 {filtered} of {total_raw} raw"},
            ],
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f"*Keywords:* {', '.join(KEYWORDS_LIST)}\n"
                    f"*Source:* {COUNTRY_INDEED.title()} Indeed · Remote only"
                ),
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


# ── Notion ────────────────────────────────────────────────────────────────────

def add_to_notion(job) -> bool:
    if not NOTION_TOKEN:
        return False

    title   = str(job.get("title", "N/A"))[:100]
    company = str(job.get("company", "N/A"))
    job_url = str(job.get("job_url", "")).strip() or None

    salary_str = ""
    min_amt, max_amt = job.get("min_amount"), job.get("max_amount")
    if pd.notna(min_amt) and pd.notna(max_amt) and min_amt and max_amt:
        currency   = job.get("currency", "")
        salary_str = f"{currency}{int(min_amt)}–{int(max_amt)}"

    props = {
        "Company":      {"title": [{"text": {"content": company}}]},
        "Role":         {"select": {"name": title}},
        "URL":          {"url": job_url},
        "Status":       {"select": {"name": "New"}},
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


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    if not SEEN_FILE.exists():
        SEEN_FILE.write_text("[]")

    print(
        f"[{datetime.now():%Y-%m-%d %H:%M}] "
        f"keywords={len(KEYWORDS_LIST)} · country='{COUNTRY_INDEED}' · remote={IS_REMOTE}"
    )
    print(f"Keywords: {', '.join(KEYWORDS_LIST)}")

    # Scrape every keyword × site
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

    # Apply LLM or keyword filter
    filtered = apply_filters(combined)
    print(f"After filter: {len(filtered)}")

    if filtered.empty:
        print("No matching jobs after filtering")
        return

    # Deduplicate against seen
    seen     = load_seen()
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
