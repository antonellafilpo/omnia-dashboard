"""
fetch_data.py — weekly Omnia data refresh for the AI Visibility Dashboard

Run locally: python fetch_data.py
Run via GitHub Actions: triggered every Monday at 08:00 UTC

Requires:
  pip install requests python-dotenv

Environment variables:
  OMNIA_API_KEY   — your Omnia API key (set in GitHub repo secrets)
  OMNIA_BASE_URL  — https://app.useomnia.com (default)

──────────────────────────────────────────────────────────────────────────────
WHY THIS VERSION IS DIFFERENT (July 2026 fix)
──────────────────────────────────────────────────────────────────────────────
The previous version called:
    GET /api/v1/brands/{brand}/visibility/aggregates?tags=MOFU
    GET /api/v1/brands/{brand}/visibility/aggregates?tags=TOFU

This silently fails. Omnia's brand-level /visibility/aggregates and
/citations/aggregates endpoints only index L2/L3 tags (e.g. "category-aware",
"fake-products", "competitive", "branded-direct") — they do NOT index the L1
funnel-stage tags "TOFU"/"MOFU"/"BOFU". When you filter by one of those,
the API doesn't error — it silently returns a single fake placeholder row
(Red Points, visibility 0), identical to what you get from a completely
made-up tag string. This was verified directly: querying
tags=MOFU and tags=some-tag-that-does-not-exist return byte-identical
responses. That's why MOFU/TOFU/BOFU-overall have shown "Building baseline"
since launch — the script has been writing 0/None every single week.

THE FIX: instead of asking the brand-level endpoint to filter by MOFU/TOFU/
competitive/branded-direct, we:
  1. Walk every topic for the brand and pull every prompt + its real tags
     (this DOES work — prompt tags are correctly returned by
     /api/v1/topics/{topic}/prompts).
  2. Locally filter that prompt list down to the prompts tagged MOFU, TOFU,
     competitive, or branded-direct.
  3. For each of those prompts individually, call the per-prompt
     /api/v1/prompts/{id}/visibility/aggregates and
     /api/v1/prompts/{id}/citations/aggregates endpoints for the exact
     date window we care about (this is the same method the script already
     used successfully for the category-aware prompt cards).
  4. Average the results to get a funnel-stage mention rate, and merge
     citations to get a top-sources list.

BOFU overall is defined as: average Red Points visibility across every
*actively monitored* prompt tagged "competitive" OR "branded-direct"
(matches the dashboard's own subtitle: "Includes competitive +
branded-direct"). It was never computed before this fix — there was no
code for it at all.
"""

import json
import os
import requests
from datetime import datetime, timedelta, date
from pathlib import Path
from collections import defaultdict

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

KNOWN_COMPETITORS = ["Red Points", "BrandShield", "Corsearch", "MarqVision"]

THEME_LABELS = {
    "category-aware": "BOFU category-aware content",
    "brand-impersonation": "Brand impersonation content",
    "fake-products": "Counterfeit / fake products content",
    "unauthorized-sellers": "Unauthorized seller / gray market content",
    "manual-enforcement": "Manual enforcement / automation content",
    "global-enforcement": "Global enforcement (APAC/China) content",
}

# ── Low-level API helpers ───────────────────────────────────────────────────

def headers():
    return {
        "Authorization": f"Bearer {API_KEY}",
        "Content-Type": "application/json",
    }

def _get(path, params):
    url = f"{BASE_URL}{path}"
    r = requests.get(url, headers=headers(), params=params)
    r.raise_for_status()
    return r.json()

def domain_to_label(domain):
    """Best-effort domain -> readable brand name (e.g. 'corsearch.com' ->
    'Corsearch'). Not guaranteed to match a brand's exact preferred casing
    (e.g. 'zerofox.com' -> 'Zerofox' not 'ZeroFox'), but consistent and
    accurate enough for a data-driven label."""
    core = domain.replace("www.", "").split(".")[0]
    return core.replace("-", " ").title()

def compute_theme_content_ranking(theme_key, prompt_ids, start_date, end_date, top_n_display=4):
    """Builds ONE ranked list of domains by citation share for a theme, so
    'leader', 'our_rank', 'gap', and 'what_wins' are guaranteed to agree
    with each other — no more mixing a visibility-based leader with an
    unrelated citations-based content list. If Red Points leads, our own
    domain naturally lands at #1. If we're 'Rank 2', our own URL is
    guaranteed to be the #2 entry in what_wins, because it's the same list."""
    domain_totals = defaultdict(int)
    domain_best = {}
    for pid in prompt_ids:
        for c in get_prompt_citations(pid, start_date, end_date, top_n=25):
            domain = c.get("domain", "")
            if not domain:
                continue
            cites = c.get("totalCitations", 0)
            domain_totals[domain] += cites
            if domain not in domain_best or cites > domain_best[domain]["totalCitations"]:
                domain_best[domain] = {"title": c.get("title") or domain, "url": c.get("url", ""), "totalCitations": cites}

    ranking = sorted(domain_totals.items(), key=lambda kv: kv[1], reverse=True)
    total_all = sum(domain_totals.values())

    our_index = next((i for i, (d, _) in enumerate(ranking) if d == "redpoints.com"), None)
    our_rank = our_index + 1 if our_index is not None else None
    our_score = round((domain_totals.get("redpoints.com", 0) / total_all) * 100) if total_all else 0

    if ranking:
        leader_domain, leader_citations = ranking[0]
        leader_label = "Red Points" if leader_domain == "redpoints.com" else domain_to_label(leader_domain)
        leader_score = round((leader_citations / total_all) * 100) if total_all else 0
    else:
        leader_label, leader_score = None, None

    gap_val = (our_score - leader_score) if ranking else None
    gap_str = (f"+{gap_val}pp" if gap_val > 0 else f"{gap_val}pp") if gap_val is not None else "—"
    status = (
        "leading" if gap_val is not None and gap_val >= 0 else
        "close" if gap_val is not None and gap_val >= -15 else
        "gap" if gap_val is not None else "pending"
    )

    what_wins = []
    for i, (domain, _) in enumerate(ranking[:top_n_display]):
        entry = domain_best[domain]
        what_wins.append({
            "label": entry["title"], "url": entry["url"],
            "type": "ok" if domain == "redpoints.com" else ("warn" if i == 0 else "neutral"),
        })
    # If our own domain didn't make the displayed slice, append it anyway so
    # "our_rank" is never a number the person can't actually see evidence for.
    if our_index is not None and our_index >= top_n_display:
        entry = domain_best["redpoints.com"]
        what_wins.append({"label": f"{entry['title']} (our Rank {our_rank})", "url": entry["url"], "type": "ok"})
    elif our_index is None:
        what_wins.append({"label": "Red Points has no cited page for this theme yet", "url": "", "type": "neutral"})

    return {
        "theme": theme_key, "content": THEME_LABELS.get(theme_key, theme_key),
        "leader": leader_label, "leader_score": leader_score,
        "our_rank": f"Rank {our_rank}" if our_rank else "Not cited",
        "gap": gap_str, "status": status, "what_wins": what_wins,
        "_gap_val": gap_val if gap_val is not None else -9999,
    }

def get_visibility_by_tag(tag, start_date, end_date, top_n=10):
    """Brand-level visibility filtered by tag. Only reliable for L2/L3 tags
    (e.g. 'category-aware', 'fake-products') — do NOT use for TOFU/MOFU/BOFU,
    'competitive', or 'branded-direct'. See module docstring."""
    data = _get(f"/api/v1/brands/{BRAND_ID}/visibility/aggregates", {
        "tags": tag, "startDate": start_date, "endDate": end_date,
        "sortBy": "visibility", "sortDirection": "desc", "pageSize": top_n,
    })
    aggregates = data.get("data", {}).get("aggregates", [])
    rp = next((a for a in aggregates if a.get("relationship") == "owned"), None)
    return round(rp["visibility"] * 100) if rp and rp.get("visibility") is not None else None

def get_prompt_visibility(prompt_id, start_date, end_date, top_n=5):
    # NOTE: Omnia's API requires pageSize >= 5. The original script passed
    # top_n=3 here, which is BELOW that minimum and returns 400 Bad Request
    # every time. It was silently swallowed by a try/except in main(), so
    # this per-prompt detail refresh has likely been failing every week
    # without anyone noticing. Fixed by raising the default to 5 and
    # trimming to 3 for display at the call site instead.
    """Top entities mentioned for a specific prompt, within a date window."""
    data = _get(f"/api/v1/prompts/{prompt_id}/visibility/aggregates", {
        "startDate": start_date, "endDate": end_date,
        "sortBy": "visibility", "sortDirection": "desc", "pageSize": top_n,
    })
    aggregates = data.get("data", {}).get("aggregates", [])
    result, rp_rank = [], None
    for i, a in enumerate(aggregates):
        is_owned = a.get("relationship") == "owned"
        if is_owned:
            rp_rank = i + 1
        result.append({"name": a["brand"].strip(), "visibility": round(a["visibility"] * 100), "owned": is_owned})
    return result, rp_rank

def get_prompt_rp_visibility(prompt_id, start_date, end_date):
    """Just Red Points' own visibility (0-1) for one prompt in a date window.
    Returns 0.0 if RP wasn't mentioned at all (not None) — a real absence,
    not missing data — and None only if the API call itself returned nothing
    usable (no monitoring data yet for that window)."""
    data = _get(f"/api/v1/prompts/{prompt_id}/visibility/aggregates", {
        "startDate": start_date, "endDate": end_date,
        "sortBy": "visibility", "sortDirection": "desc", "pageSize": 25,
    })
    aggregates = data.get("data", {}).get("aggregates", [])
    if not aggregates:
        return None
    rp = next((a for a in aggregates if a.get("relationship") == "owned"), None)
    return rp["visibility"] if rp and rp.get("visibility") is not None else 0.0

def get_prompt_citations(prompt_id, start_date, end_date, top_n=10):
    data = _get(f"/api/v1/prompts/{prompt_id}/citations/aggregates", {
        "startDate": start_date, "endDate": end_date,
        "sortBy": "total_citations", "sortDirection": "desc", "pageSize": top_n,
    })
    return data.get("data", {}).get("aggregates", [])

def get_competitors_bofu(start_date, end_date):
    """Top competitors on category-aware prompts (this tag DOES work at brand level)."""
    data = _get(f"/api/v1/brands/{BRAND_ID}/visibility/aggregates", {
        "tags": "category-aware", "startDate": start_date, "endDate": end_date,
        "sortBy": "visibility", "sortDirection": "desc", "pageSize": 10,
    })
    aggregates = data.get("data", {}).get("aggregates", [])
    result = []
    for name in KNOWN_COMPETITORS:
        match = next((a for a in aggregates if a["brand"].strip() == name), None)
        if match:
            result.append({"name": name, "visibility": round(match["visibility"] * 100), "owned": match.get("relationship") == "owned"})
    return result, aggregates

# ── Prompt discovery (the actual fix) ───────────────────────────────────────

def get_all_prompts():
    """Walk every topic for the brand and return every prompt with its real
    tags + isMonitoringActive flag. This is the only reliable way to find
    which prompts are tagged MOFU / TOFU / competitive / branded-direct,
    since the brand-level aggregate endpoints can't filter on those tags."""
    all_prompts = []
    page = 1
    while True:
        data = _get(f"/api/v1/brands/{BRAND_ID}/topics", {"page": page, "pageSize": 100})
        topics = data.get("data", {}).get("topics", [])
        if not topics:
            break
        for topic in topics:
            tpage = 1
            while True:
                tdata = _get(f"/api/v1/topics/{topic['id']}/prompts", {"page": tpage, "pageSize": 100})
                prompts = tdata.get("data", {}).get("prompts", [])
                all_prompts.extend(prompts)
                pagination = tdata.get("pagination", {})
                if tpage * pagination.get("pageSize", 100) >= pagination.get("totalItems", 0):
                    break
                tpage += 1
        pagination = data.get("pagination", {})
        if page * pagination.get("pageSize", 100) >= pagination.get("totalItems", 0):
            break
        page += 1
    return all_prompts

def prompts_with_any_tag(all_prompts, tags, active_only=True):
    tagset = set(tags)
    out = []
    for p in all_prompts:
        if active_only and not p.get("isMonitoringActive"):
            continue
        if tagset.intersection(p.get("tags") or []):
            out.append(p)
    return out

def compute_funnel_rate(prompt_ids, start_date, end_date):
    """Average Red Points visibility across a list of prompt IDs for one
    date window. Returns None only if none of the prompts had any data at
    all for that window (e.g. too new)."""
    values = []
    for pid in prompt_ids:
        v = get_prompt_rp_visibility(pid, start_date, end_date)
        if v is not None:
            values.append(v)
    if not values:
        return None
    return round((sum(values) / len(values)) * 100)

def compute_top_citations(prompt_ids, start_date, end_date, top_n=4):
    """Merge citations across a list of prompts into a single top-N source
    list, summing total_citations for the same URL."""
    merged = defaultdict(lambda: {"totalCitations": 0, "domain": "", "title": "", "url": ""})
    for pid in prompt_ids:
        for c in get_prompt_citations(pid, start_date, end_date, top_n=10):
            key = c.get("url") or c.get("domain")
            merged[key]["totalCitations"] += c.get("totalCitations", 0)
            merged[key]["domain"] = c.get("domain", "")
            merged[key]["title"] = c.get("title", c.get("domain", ""))
            merged[key]["url"] = c.get("url", "")
    ranked = sorted(merged.values(), key=lambda x: x["totalCitations"], reverse=True)[:top_n]
    return [
        {"rank": i + 1, "title": c["title"], "url": c["url"], "domain": c["domain"]}
        for i, c in enumerate(ranked)
    ]

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

    print("Discovering all prompts + real tags across every topic (this replaces the broken tag filter)…")
    all_prompts = get_all_prompts()
    mofu_prompts = prompts_with_any_tag(all_prompts, ["MOFU"])
    tofu_prompts = prompts_with_any_tag(all_prompts, ["TOFU"])
    bofu_overall_prompts = prompts_with_any_tag(all_prompts, ["competitive", "branded-direct"])
    mofu_ids = [p["id"] for p in mofu_prompts]
    tofu_ids = [p["id"] for p in tofu_prompts]
    bofu_overall_ids = [p["id"] for p in bofu_overall_prompts]
    print(f"  Found {len(mofu_ids)} active MOFU prompts, {len(tofu_ids)} active TOFU prompts, "
          f"{len(bofu_overall_ids)} active competitive/branded-direct prompts")

    # ── Weekly ──────────────────────────────────────────────────────────────
    w_start, w_end = this_week_range()
    w_label = "Week of " + week_label(w_start)
    print(f"Fetching weekly data ({w_label})…")

    w_ca   = get_visibility_by_tag("category-aware", w_start, w_end)
    w_mofu = compute_funnel_rate(mofu_ids, w_start, w_end)
    w_tofu = compute_funnel_rate(tofu_ids, w_start, w_end)
    w_bofu_overall = compute_funnel_rate(bofu_overall_ids, w_start, w_end)
    w_comps, w_ca_raw = get_competitors_bofu(w_start, w_end)

    prev_ca_vals = [w.get("bofu_ca") for w in data["weekly"][-3:] if w.get("bofu_ca") is not None]
    if w_ca is not None:
        prev_ca_vals.append(w_ca)
    avg4 = round(sum(prev_ca_vals) / len(prev_ca_vals)) if prev_ca_vals else None

    weekly_entry = {
        "period": w_label, "bofu_ca": w_ca, "bofu_ca_avg4": avg4,
        "mofu": w_mofu, "tofu": w_tofu, "bofu_overall": w_bofu_overall,
        "competitors_bofu": w_comps,
    }
    existing_periods = [w["period"] for w in data["weekly"]]
    if w_label not in existing_periods:
        data["weekly"].append(weekly_entry)
    else:
        for i, w in enumerate(data["weekly"]):
            if w["period"] == w_label:
                data["weekly"][i] = weekly_entry
    print(f"  BOFU-CA={w_ca}%  MOFU={w_mofu}%  TOFU={w_tofu}%  BOFU-overall={w_bofu_overall}%")

    # ── Monthly ─────────────────────────────────────────────────────────────
    m_start, m_end = this_month_range()
    m_label = month_label(m_start)
    print(f"Fetching monthly data ({m_label})…")

    m_ca   = get_visibility_by_tag("category-aware", m_start, m_end)
    m_mofu = compute_funnel_rate(mofu_ids, m_start, m_end)
    m_tofu = compute_funnel_rate(tofu_ids, m_start, m_end)
    m_bofu_overall = compute_funnel_rate(bofu_overall_ids, m_start, m_end)
    m_comps, _ = get_competitors_bofu(m_start, m_end)

    prev_m_vals = [m.get("bofu_ca") for m in data["monthly"][-2:] if m.get("bofu_ca") is not None]
    if m_ca is not None:
        prev_m_vals.append(m_ca)
    m_avg = round(sum(prev_m_vals) / len(prev_m_vals)) if prev_m_vals else None

    monthly_entry = {
        "period": m_label, "bofu_ca": m_ca, "bofu_ca_avg4": m_avg,
        "mofu": m_mofu, "tofu": m_tofu, "bofu_overall": m_bofu_overall,
        "competitors_bofu": m_comps,
    }
    existing_months = [m["period"] for m in data["monthly"]]
    if m_label not in existing_months:
        data["monthly"].append(monthly_entry)
    else:
        for i, m in enumerate(data["monthly"]):
            if m["period"] == m_label:
                data["monthly"][i] = monthly_entry

    # ── Theme visibility (unchanged — this tag filter already works) ────────
    print("Fetching theme visibility…")
    theme_results = []
    for tag in THEME_TAGS:
        vis = get_visibility_by_tag(tag, w_start, w_end)
        if vis is not None:
            status = "leading" if vis >= 60 else "gap" if vis < 30 else "close"
            theme_results.append({"name": tag, "visibility": vis, "status": status})
    if theme_results:
        data["themes"] = theme_results

    # ── Per-theme prompt detail (NEW — this was never automated before) ─────
    # The theme bars above (data["themes"]) already refreshed correctly, but
    # the drill-down detail underneath each bar — data["theme_prompts"] — was
    # hand-seeded once and never touched by this script. That's why it could
    # drift out of sync with the bar's own percentage. We already have
    # `all_prompts` from the MOFU/TOFU/BOFU-overall discovery step above, so
    # we reuse it here instead of making extra topic-listing calls.
    print("Fetching per-theme prompt detail…")
    DRILLDOWN_THEMES = [t for t in THEME_TAGS if t != "category-aware"]
    new_theme_prompts = {}
    theme_prompt_ids_map = {}
    for theme in DRILLDOWN_THEMES:
        theme_prompt_list = [p for p in all_prompts if theme in (p.get("tags") or []) and p.get("isMonitoringActive")]
        theme_prompt_ids_map[theme] = [p["id"] for p in theme_prompt_list]
        entries = []
        for p in theme_prompt_list:
            stage = next((s for s in ("TOFU", "MOFU", "BOFU") if s in (p.get("tags") or [])), None)
            try:
                mentions, rp_rank = get_prompt_visibility(p["id"], w_start, w_end)
                entries.append({
                    "text": p["query"], "stage": stage, "rp_rank": rp_rank,
                    "top_mentions": mentions[:3],
                })
            except Exception as e:
                print(f"  Error on theme={theme} prompt={p['query'][:40]}: {e}")
        new_theme_prompts[theme] = entries
        print(f"  {theme}: refreshed {len(entries)} prompts")
    data["theme_prompts"] = new_theme_prompts


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
                "text": p["text"], "modifier": p.get("modifier"), "rp_rank": rp_rank,
                "rp_visibility": rp["visibility"] if rp else None, "top_mentions": mentions[:3],
            })
        except Exception as e:
            print(f"  Error on {p['text'][:40]}: {e}")
            updated_prompts.append(p)
    data["category_aware_prompts"] = updated_prompts

    # ── Content priorities (NEW — was 100% hand-typed and frozen before) ────
    # Built from the exact same theme_prompts / category_aware_prompts data
    # just refreshed above — no separate API calls needed for the numbers.
    # We rank every theme by how far behind the leading competitor Red
    # Points is, and surface the 5 biggest gaps. "what_wins" is a real,
    # live list of the content currently being cited for that theme's
    # prompts (via citations aggregates) — not an editorial judgment.
    print("Generating content priorities…")
    all_theme_ids = dict(theme_prompt_ids_map)
    all_theme_ids["category-aware"] = [cp["id"] for cp in CATEGORY_AWARE_PROMPTS]

    theme_stages = {}
    for theme in DRILLDOWN_THEMES:
        theme_stages[theme] = sorted({p["stage"] for p in new_theme_prompts.get(theme, []) if p.get("stage")})
    theme_stages["category-aware"] = ["BOFU"]

    priorities = []
    for theme in THEME_TAGS:
        ids = all_theme_ids.get(theme, [])
        if not ids:
            continue
        row = compute_theme_content_ranking(theme, ids, w_start, w_end)
        row["stages"] = theme_stages.get(theme, [])
        priorities.append(row)

    priorities.sort(key=lambda r: r["_gap_val"])
    priorities = priorities[:5]
    for i, row in enumerate(priorities):
        row["priority"] = i + 1
        del row["_gap_val"]
    data["content_priorities"] = priorities
    print(f"  Generated {len(priorities)} content priorities (biggest gap: "
          f"{priorities[0]['content'] if priorities else 'n/a'})")

    # ── Citation sources — now refreshed automatically every run ────────────
    print("Refreshing citation sources…")
    ca_leader = max(w_ca_raw, key=lambda a: a.get("visibility") or 0) if w_ca_raw else None
    ca_subtitle = (
        f"{ca_leader['brand'].strip()} leads {round(ca_leader['visibility'] * 100)}%"
        if ca_leader else "Data building"
    )
    data["citation_sources"] = {
        "bofu_ca": {
            "label": "BOFU category-aware", "subtitle": ca_subtitle, "stage": "ca",
            "sources": [
                {"rank": i + 1, "title": c.get("title", c["domain"]), "url": c.get("url", ""), "domain": c["domain"]}
                for i, c in enumerate(sorted(
                    _get(f"/api/v1/brands/{BRAND_ID}/citations/aggregates", {
                        "tags": "category-aware", "startDate": w_start, "endDate": w_end,
                        "sortBy": "total_citations", "sortDirection": "desc", "pageSize": 10,
                    }).get("data", {}).get("aggregates", []),
                    key=lambda x: x.get("totalCitations", 0), reverse=True
                )[:4])
            ],
        },
        "bofu_overall": {
            "label": "BOFU overall", "subtitle": "Includes competitive + branded-direct", "stage": "bofu",
            "sources": compute_top_citations(bofu_overall_ids, w_start, w_end, top_n=4),
        },
        "mofu": {
            "label": "MOFU sources",
            "subtitle": f"Live — {len(mofu_ids)} monitored prompts" if mofu_ids else "Data building",
            "stage": "mofu",
            "sources": compute_top_citations(mofu_ids, w_start, w_end, top_n=4),
        },
        "tofu": {
            "label": "TOFU sources",
            "subtitle": f"Live — {len(tofu_ids)} monitored prompts" if tofu_ids else "Data building",
            "stage": "tofu",
            "sources": compute_top_citations(tofu_ids, w_start, w_end, top_n=4),
        },
    }

    data["lastUpdated"] = date.today().isoformat()

    with open(DATA_FILE, "w") as f:
        json.dump(data, f, indent=2)
    print(f"\nDone. data.json updated ({data['lastUpdated']})")

if __name__ == "__main__":
    main()
