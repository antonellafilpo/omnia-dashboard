"""
fetch_data.py — weekly Omnia data refresh for the AI Visibility Dashboard

Run locally: python fetch_data.py
Run via GitHub Actions: triggered every Monday at 08:00 UTC

Requires:
  pip install requests python-dotenv

Environment variables:
  OMNIA_API_KEY   — your Omnia API key (set in GitHub repo secrets)
  OMNIA_BASE_URL  — https://app.useomnia.com (default)

The script fetches:
  - Visibility aggregates filtered by tag for BOFU category-aware, MOFU, TOFU
  - Per-prompt visibility for each category-aware prompt
  - Appends to the weekly/monthly arrays in data.json
"""

import json
import os
import requests
from datetime import datetime, timedelta, date
from pathlib import Path

# ── Config ────────────────────────────────────────────────────────────────────

BRAND_ID      = "03adaaca-5265-404e-b4b1-bbaea0ce73f9"
BASE_URL      = os.getenv("OMNIA_BASE_URL", "https://app.useomnia.com")
API_KEY       = os.getenv("OMNIA_API_KEY", "")
DATA_FILE     = Path(__file__).parent / "data.json"

# Category-aware prompt UUIDs — update if prompts change
CATEGORY_AWARE_PROMPTS = [
    {"id": "019dbfa7-4c78-76d5-b112-14c6e53eab4a", "text": "Which brand protection platforms offer flat-fee pricing with unlimited takedowns?", "modifier": "narrative-grounding"},
    {"id": "71610839-020c-47da-8442-76f8558f7203", "text": "Which brand protection software offers unlimited takedowns at a flat fee?", "modifier": "narrative-grounding"},
    {"id": "21320a03-93d6-430b-b593-9439ae680875", "text": "Which brand protection platforms offer unlimited enforcement?", "modifier": "narrative-grounding"},
    {"id": "e22d85fa-d94e-4e56-a66a-65545e59e642", "text": "Which brand protection platforms use smart rules to automate detection and enforcement?", "modifier": None},
    {"id": "fcfacef9-cd42-42d2-a7db-68efaf210f67", "text": "Which brand protection platforms use predictive analytics for enforcement and risk prioritization?", "modifier": None},
    {"id": "481068ff-2beb-4c1b-93eb-c10a158cf591", "text": "Which brand protection platforms offer a fully managed service model rather than self-serve?", "modifier": None},
    {"id": "f17fde22-246e-4f92-ad33-6bdb9e1923ee", "text": "Which brand protection platforms offer zero-cost litigation and revenue recovery programs?", "modifier": "narrative-grounding"},
    {"id": "9402c055-eeab-445f-8531-f2448fb8c479", "text": "What is the best brand protection software for enterprise-level companies?", "modifier": "icp-specific"},
    {"id": "ec1232d5-2197-46c3-a0e0-581bd3c58eef", "text": "What brand protection platforms provide coverage across marketplaces, social media, websites, and ads?", "modifier": None},
    {"id": "a26e64a4-bbac-49fb-b572-f039c698b43b", "text": "Which brand protection software is best for stopping gray market and parallel imports?", "modifier": None},
    {"id": "82e4b8f7-7813-4824-8f48-e1da6739d1de", "text": "Which brand protection platforms offer API integrations with marketplaces and ecommerce systems?", "modifier": None},
    {"id": "019a9cdb-9169-7109-aa8e-4626f11fe6e6", "text": "Which brand protection solution covers the most channels?", "modifier": None},
    {"id": "df1d953d-57df-46b1-9e8b-4f3575dcd483", "text": "Which brand protection platforms train their AI models on large proprietary datasets?", "modifier": "narrative-grounding"},
    {"id": "8bf7ebd2-b226-4ec8-a35d-932e886af527", "text": "What are the benefits of using AI-driven solutions for online brand protection?", "modifier": None},
    {"id": "e510094e-7acc-42f8-b297-27d40d949cb0", "text": "Which AI-powered platforms help verify product authenticity and detect counterfeits online?", "modifier": None},
    {"id": "019dbfa7-6a1f-7334-bf97-697716947bde", "text": "Which brand protection platforms have the fastest takedown times?", "modifier": None},
]

THEME_TAGS = [
    "category-aware",
    "global-enforcement",
    "manual-enforcement",
    "unauthorized-sellers",
    "fake-products",
    "brand-impersonation",
]

# ── API helpers ───────────────────────────────────────────────────────────────

def headers():
    return {
        "Authorization": f"Bearer {API_KEY}",
        "Content-Type": "application/json",
    }

def get_visibility_by_tag(tag, start_date, end_date, top_n=10):
    """Get visibility aggregates filtered by tag, return Red Points visibility %."""
    url = f"{BASE_URL}/api/v1/brands/{BRAND_ID}/visibility/aggregates"
    params = {
        "tags": tag,
        "startDate": start_date,
        "endDate": end_date,
        "sortBy": "visibility",
        "sortDirection": "desc",
        "pageSize": top_n,
    }
    r = requests.get(url, headers=headers(), params=params)
    r.raise_for_status()
    data = r.json()
    aggregates = data.get("data", {}).get("aggregates", [])

    rp = next((a for a in aggregates if a.get("relationship") == "owned"), None)
    return round(rp["visibility"] * 100) if rp else None

def get_prompt_visibility(prompt_id, start_date, end_date, top_n=3):
    """Get top entities mentioned for a specific prompt."""
    url = f"{BASE_URL}/api/v1/prompts/{prompt_id}/visibility/aggregates"
    params = {
        "startDate": start_date,
        "endDate": end_date,
        "sortBy": "visibility",
        "sortDirection": "desc",
        "pageSize": top_n,
    }
    r = requests.get(url, headers=headers(), params=params)
    r.raise_for_status()
    data = r.json()
    aggregates = data.get("data", {}).get("aggregates", [])
    result = []
    rp_rank = None
    for i, a in enumerate(aggregates):
        is_owned = a.get("relationship") == "owned"
        if is_owned:
            rp_rank = i + 1
        result.append({
            "name": a["brand"].strip(),
            "visibility": round(a["visibility"] * 100),
            "owned": is_owned,
        })
    return result, rp_rank

def get_competitors_bofu(start_date, end_date):
    """Get top competitors on category-aware prompts."""
    url = f"{BASE_URL}/api/v1/brands/{BRAND_ID}/visibility/aggregates"
    params = {
        "tags": "category-aware",
        "startDate": start_date,
        "endDate": end_date,
        "sortBy": "visibility",
        "sortDirection": "desc",
        "pageSize": 10,
    }
    r = requests.get(url, headers=headers(), params=params)
    r.raise_for_status()
    data = r.json()
    aggregates = data.get("data", {}).get("aggregates", [])
    # Return top 4 (RP + 3 competitors)
    known = ["Red Points", "BrandShield", "Corsearch", "MarqVision"]
    result = []
    for name in known:
        match = next((a for a in aggregates if a["brand"].strip() == name), None)
        if match:
            result.append({
                "name": name,
                "visibility": round(match["visibility"] * 100),
                "owned": match.get("relationship") == "owned",
            })
    return result

# ── Date helpers ──────────────────────────────────────────────────────────────

def this_week_range():
    today = date.today()
    monday = today - timedelta(days=today.weekday())
    sunday = monday + timedelta(days=6)
    return monday.isoformat(), sunday.isoformat()

def this_month_range():
    today = date.today()
    start = date(today.year, today.month, 1)
    return start.isoformat(), today.isoformat()

def week_label(start):
    return datetime.fromisoformat(start).strftime("%-d %b %Y")

def month_label(start):
    return datetime.fromisoformat(start).strftime("%b %Y")

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    print("Loading existing data.json…")
    with open(DATA_FILE) as f:
        data = json.load(f)

    # Weekly
    w_start, w_end = this_week_range()
    w_label = "Week of " + week_label(w_start)
    print(f"Fetching weekly data ({w_label})…")

    w_ca   = get_visibility_by_tag("category-aware", w_start, w_end)
    w_mofu = get_visibility_by_tag("MOFU", w_start, w_end)
    w_tofu = get_visibility_by_tag("TOFU", w_start, w_end)
    w_comps = get_competitors_bofu(w_start, w_end)

    # 4-week average for category-aware
    prev_ca_vals = [w.get("bofu_ca") for w in data["weekly"][-3:] if w.get("bofu_ca") is not None]
    if w_ca is not None:
        prev_ca_vals.append(w_ca)
    avg4 = round(sum(prev_ca_vals) / len(prev_ca_vals)) if prev_ca_vals else None

    weekly_entry = {
        "period": w_label,
        "bofu_ca": w_ca,
        "bofu_ca_avg4": avg4,
        "mofu": w_mofu,
        "tofu": w_tofu,
        "competitors_bofu": w_comps,
    }

    # Avoid duplicate week entries
    existing_periods = [w["period"] for w in data["weekly"]]
    if w_label not in existing_periods:
        data["weekly"].append(weekly_entry)
        print(f"  Added weekly entry: BOFU={w_ca}%, MOFU={w_mofu}%, TOFU={w_tofu}%")
    else:
        # Update existing
        for i, w in enumerate(data["weekly"]):
            if w["period"] == w_label:
                data["weekly"][i] = weekly_entry
        print(f"  Updated weekly entry: BOFU={w_ca}%, MOFU={w_mofu}%, TOFU={w_tofu}%")

    # Monthly
    m_start, m_end = this_month_range()
    m_label = month_label(m_start)
    print(f"Fetching monthly data ({m_label})…")

    m_ca   = get_visibility_by_tag("category-aware", m_start, m_end)
    m_mofu = get_visibility_by_tag("MOFU", m_start, m_end)
    m_tofu = get_visibility_by_tag("TOFU", m_start, m_end)
    m_comps = get_competitors_bofu(m_start, m_end)

    prev_m_vals = [m.get("bofu_ca") for m in data["monthly"][-2:] if m.get("bofu_ca") is not None]
    if m_ca is not None:
        prev_m_vals.append(m_ca)
    m_avg = round(sum(prev_m_vals) / len(prev_m_vals)) if prev_m_vals else None

    monthly_entry = {
        "period": m_label,
        "bofu_ca": m_ca,
        "bofu_ca_avg4": m_avg,
        "mofu": m_mofu,
        "tofu": m_tofu,
        "competitors_bofu": m_comps,
    }

    existing_months = [m["period"] for m in data["monthly"]]
    if m_label not in existing_months:
        data["monthly"].append(monthly_entry)
    else:
        for i, m in enumerate(data["monthly"]):
            if m["period"] == m_label:
                data["monthly"][i] = monthly_entry

    # Theme visibility
    print("Fetching theme visibility…")
    theme_results = []
    for tag in THEME_TAGS:
        vis = get_visibility_by_tag(tag, w_start, w_end)
        if vis is not None:
            status = "leading" if vis >= 60 else "gap" if vis < 30 else "close"
            theme_results.append({"name": tag, "visibility": vis, "status": status})
    if theme_results:
        data["themes"] = theme_results

    # Per-prompt visibility for category-aware prompts
    print("Fetching per-prompt visibility…")
    updated_prompts = []
    for p in data.get("category_aware_prompts", []):
        match = next((cp for cp in CATEGORY_AWARE_PROMPTS if p["text"] == cp["text"]), None)
        if not match:
            updated_prompts.append(p)
            continue
        try:
            mentions, rp_rank = get_prompt_visibility(match["id"], w_start, w_end)
            rp = next((m for m in mentions if m["owned"]), None)
            updated_prompts.append({
                "text": p["text"],
                "modifier": p.get("modifier"),
                "rp_rank": rp_rank,
                "rp_visibility": rp["visibility"] if rp else None,
                "top_mentions": mentions[:3],
            })
            print(f"  {p['text'][:60]}… RP rank={rp_rank}")
        except Exception as e:
            print(f"  Error on {p['text'][:40]}: {e}")
            updated_prompts.append(p)

    data["category_aware_prompts"] = updated_prompts
    data["lastUpdated"] = date.today().isoformat()

    with open(DATA_FILE, "w") as f:
        json.dump(data, f, indent=2)
    print(f"\nDone. data.json updated ({data['lastUpdated']})")

if __name__ == "__main__":
    main()
