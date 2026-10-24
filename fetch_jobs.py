"""
Daily job fetcher.

Pulls listings posted in the last ~24h from job boards, applies cheap keyword
filters, removes anything already seen, and writes today's new listings to
data/. Claude picks the file up afterwards for scoring and the digest.

No AI calls happen here. Only secret needed: RAPIDAPI_KEY (Adzuna optional).
"""

from __future__ import annotations

import gzip
import html
import json
import os
import re
import sys
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

import time

import requests

import config

BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "data"
DAILY_DIR = DATA_DIR / "daily"
RUNS_DIR = DATA_DIR / "runs"       # one immutable file per run, never overwritten
RAW_DIR = DATA_DIR / "raw"         # gzipped raw JSearch responses, one per run
SEEN_FILE = DATA_DIR / "seen_jobs.json"
LATEST_FILE = DATA_DIR / "latest.json"

HEADERS = {"User-Agent": "Mozilla/5.0 (personal job digest; contact via GitHub)"}
NOW = datetime.now(timezone.utc)
CUTOFF = NOW - timedelta(hours=config.MAX_AGE_HOURS)

RAW_FILE = DATA_DIR / "raw_jsearch_latest.json"
REPLAY = "--replay" in sys.argv   # re-filter the last saved JSearch responses; no API calls
RAW_RESPONSES: dict[str, list] = {}

RAPIDAPI_KEY = os.environ.get("RAPIDAPI_KEY", "").strip()
ADZUNA_APP_ID = os.environ.get("ADZUNA_APP_ID", "").strip()
ADZUNA_APP_KEY = os.environ.get("ADZUNA_APP_KEY", "").strip()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def log(msg: str) -> None:
    print(f"[fetch] {msg}", flush=True)


def clean_text(s: str | None, limit: int = 4000) -> str:
    if not s:
        return ""
    s = html.unescape(re.sub(r"<[^>]+>", " ", s))
    s = re.sub(r"\s+", " ", s).strip()
    return s[:limit]


def _word_re(words: list[str]) -> re.Pattern:
    return re.compile(r"\b(" + "|".join(re.escape(w) for w in words) + r")\b", re.I)


_TITLE_EXCLUDE_RE = _word_re(config.TITLE_EXCLUDE)


def title_ok(title: str) -> bool:
    t = title.lower()
    if not any(k in t for k in config.TITLE_INCLUDE):
        return False
    return not _TITLE_EXCLUDE_RE.search(title)


def location_ok(location: str, is_remote: bool) -> bool:
    loc = (location or "").lower()
    if any(k in loc for k in config.GTA_KEYWORDS):
        return True
    remote = is_remote or any(k in loc for k in config.REMOTE_KEYWORDS)
    if not remote:
        return False
    # Strip the remote markers; what's left is the region (may be empty).
    region = re.sub(r"\b(remote|work from home|distributed)\b|[()\[\],;:|/-]", " ", loc)
    region = re.sub(r"\s+", " ", region).strip()
    if not region:
        return True
    return bool(re.search(r"\b(" + "|".join(re.escape(r) for r in config.REMOTE_ALLOWED_REGIONS) + r")\b", region))


def fresh(posted: datetime | None) -> bool:
    # Unknown dates are kept; the source query is already date-limited.
    return posted is None or posted >= CUTOFF


def parse_dt(value) -> datetime | None:
    if value in (None, ""):
        return None
    try:
        if isinstance(value, (int, float)):
            return datetime.fromtimestamp(value, tz=timezone.utc)
        s = str(value).strip()
        if s.isdigit():
            return datetime.fromtimestamp(int(s), tz=timezone.utc)
        try:
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        except ValueError:
            dt = parsedate_to_datetime(s)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def make_job(*, id, source, title, company, location, url, posted, remote,
             description="", salary="", publisher="") -> dict:
    return {
        "id": id,
        "source": source,
        "publisher": publisher or source,
        "title": (title or "").strip(),
        "company": (company or "").strip(),
        "location": (location or "").strip(),
        "remote": bool(remote),
        "url": url,
        "posted": posted.isoformat() if posted else None,
        "salary": salary,
        "description": clean_text(description),
    }


DIAG: dict[str, dict] = {}


def diag(name: str) -> dict:
    return DIAG.setdefault(name, {"returned": 0, "kept": 0, "error": None,
                                  "rejected_by": {}, "rejected_samples": []})


def reject_reason(job: dict, posted: datetime | None) -> str | None:
    if not job["title"] or not job["url"]:
        return "missing title/url"
    if any(x in job["company"].lower() for x in config.EXCLUDE_COMPANIES):
        return "aggregator"
    if not title_ok(job["title"]):
        return "title"
    if not location_ok(job["location"], job["remote"]):
        return "location"
    if not fresh(posted):
        return "too old"
    return None


def keep(job: dict, posted: datetime | None, d: dict | None = None) -> bool:
    reason = reject_reason(job, posted)
    if d is not None:
        d["returned"] += 1
        if reason is None:
            d["kept"] += 1
        else:
            d["rejected_by"][reason] = d["rejected_by"].get(reason, 0) + 1
            if len(d["rejected_samples"]) < 8:
                d["rejected_samples"].append(
                    f"[{reason}] {job['title']} | {job['company']} | {job['location']} | {job['posted']}")
    return reason is None


# ---------------------------------------------------------------------------
# Sources
# ---------------------------------------------------------------------------
def _jsearch_items(body) -> list[dict]:
    """v1 returns {"data": [...]}; v2 may nest the list. Find the job list."""
    if isinstance(body, list):
        return [x for x in body if isinstance(x, dict)]
    data = body.get("data", body)
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]
    if isinstance(data, dict):
        for k in ("jobs", "data", "results", "items"):
            if isinstance(data.get(k), list):
                return [x for x in data[k] if isinstance(x, dict)]
    return []


def _shape(body) -> dict:
    """Record the response layout so field mapping can be checked from the output file."""
    top = list(body.keys())[:15] if isinstance(body, dict) else type(body).__name__
    data = body.get("data") if isinstance(body, dict) else None
    data_keys = list(data.keys())[:15] if isinstance(data, dict) else type(data).__name__
    items = _jsearch_items(body)
    params = body.get("parameters") if isinstance(body, dict) else None
    return {"top_keys": top, "data_keys": data_keys, "parameters": params,
            "job_keys": list(items[0].keys())[:40] if items else []}


def fetch_jsearch() -> list[dict]:
    saved = json.loads(RAW_FILE.read_text()) if REPLAY and RAW_FILE.exists() else {}
    if REPLAY:
        log(f"Replay mode: using saved responses from {RAW_FILE.name}")
    if not REPLAY and not RAPIDAPI_KEY:
        diag("jsearch")["error"] = "skipped: RAPIDAPI_KEY secret not set"
        log("JSearch skipped: RAPIDAPI_KEY not set")
        return []
    out = []
    for query, country, remote_only, pages in config.JSEARCH_QUERIES:
        d = diag(f"jsearch: {query} ({country}{', remote' if remote_only else ''})")
        items, cursor = [], None
        key = f"{query}|{country}|{remote_only}"
        if REPLAY:
            items = saved.get(key, [])
            pages = 0
        for page in range(pages):
            params = {
                "query": query,
                "country": country,
                "date_posted": config.JSEARCH_DATE_POSTED,
                "employment_types": "FULLTIME",
            }
            if remote_only:
                params["work_from_home"] = "true"
            if cursor:
                params["cursor"] = cursor
            try:
                r = requests.get(
                    config.JSEARCH_URL,
                    headers={"x-rapidapi-key": RAPIDAPI_KEY,
                             "x-rapidapi-host": "jsearch.p.rapidapi.com"},
                    params=params, timeout=120,
                )
                if r.status_code != 200:
                    raise RuntimeError(f"HTTP {r.status_code}: {r.text[:300]}")
                body = r.json()
                if body.get("status") not in (None, "OK"):
                    d["error"] = f"API status {body.get('status')}: {str(body)[:300]}"
                batch = _jsearch_items(body)
                items.extend(batch)
                RAW_RESPONSES.setdefault(key, []).extend(batch)
                d["response_shape"] = _shape(body)
                d.setdefault("pages", []).append(len(batch))
                data = body.get("data") if isinstance(body, dict) else None
                cursor = data.get("cursor") if isinstance(data, dict) else None
            except Exception as e:
                d["error"] = str(e)[:400]
                log(f"JSearch '{query}' ({country}) page {page + 1} failed: {e}")
                break
            if not cursor or not batch:
                break
        kept = 0
        for j in items:
            loc = j.get("job_location") or ", ".join(
                p for p in (j.get("job_city"), j.get("job_state"), j.get("job_country")) if p)
            remote = bool(j.get("job_is_remote")) or "remote" in (j.get("job_title") or "").lower()
            country_name = {"CA": "Canada", "US": "United States"}.get((j.get("job_country") or "").upper())
            if country_name and country_name.lower() not in loc.lower():
                loc = f"{loc}, {country_name}" if loc else country_name
            if remote and "remote" not in loc.lower():
                loc = f"{loc} (Remote)".strip()
            posted = parse_dt(j.get("job_posted_at_datetime_utc") or j.get("job_posted_at_timestamp"))
            sal = j.get("job_salary_string") or ""
            if not sal and (j.get("job_min_salary") or j.get("job_max_salary")):
                sal = f"{j.get('job_min_salary') or '?'}-{j.get('job_max_salary') or '?'} " \
                      f"{j.get('job_salary_currency') or ''} {j.get('job_salary_period') or ''}".strip()
            job = make_job(
                id=f"jsearch:{j.get('job_id')}", source="jsearch",
                publisher=j.get("job_publisher") or "", title=j.get("job_title"),
                company=j.get("employer_name"), location=loc,
                url=j.get("job_apply_link") or j.get("job_google_link"),
                posted=posted, remote=remote, description=j.get("job_description"),
                salary=sal,
            )
            if keep(job, posted, d):
                out.append(job)
                kept += 1
        log(f"JSearch '{query}' ({country}): {len(items)} returned, {kept} kept")
    return out


def fetch_remoteok() -> list[dict]:
    if not config.ENABLE_REMOTEOK:
        return []
    d = diag("remoteok")
    try:
        r = requests.get("https://remoteok.com/api", headers=HEADERS, timeout=30)
        r.raise_for_status()
        items = [x for x in r.json() if isinstance(x, dict) and x.get("id")]
    except Exception as e:
        d["error"] = str(e)[:400]
        log(f"RemoteOK failed: {e}")
        return []
    out = []
    for j in items:
        posted = parse_dt(j.get("epoch") or j.get("date"))
        loc = j.get("location") or "Remote"
        if "remote" not in loc.lower():
            loc = f"{loc} (Remote)"
        sal = ""
        if j.get("salary_min") or j.get("salary_max"):
            sal = f"{j.get('salary_min') or '?'}-{j.get('salary_max') or '?'} USD year"
        job = make_job(
            id=f"remoteok:{j.get('id')}", source="remoteok", title=j.get("position"),
            company=j.get("company"), location=loc, url=j.get("url") or j.get("apply_url"),
            posted=posted, remote=True, description=j.get("description"), salary=sal,
        )
        if keep(job, posted, d):
            out.append(job)
    log(f"RemoteOK: {len(items)} returned, {len(out)} kept")
    return out


def fetch_wwr() -> list[dict]:
    if not config.ENABLE_WWR:
        return []
    d = diag("weworkremotely")
    items = []
    for url in config.WWR_FEEDS:
        try:
            r = requests.get(url, headers=HEADERS, timeout=30)
            r.raise_for_status()
            root = ET.fromstring(r.content)
            items.extend(root.findall("./channel/item"))
        except Exception as e:
            d["error"] = f"{url}: {str(e)[:300]}"
            log(f"We Work Remotely feed failed ({url}): {e}")
    out = []
    for it in items:
        raw_title = (it.findtext("title") or "").strip()
        company, _, title = raw_title.partition(":")
        if not title:
            company, title = "", raw_title
        link = (it.findtext("link") or "").strip()
        region = (it.findtext("region") or "Anywhere").strip()
        posted = parse_dt(it.findtext("pubDate"))
        job = make_job(
            id=f"wwr:{link}", source="weworkremotely", title=title.strip(),
            company=company.strip(), location=f"{region} (Remote)", url=link,
            posted=posted, remote=True, description=it.findtext("description"),
        )
        if keep(job, posted, d):
            out.append(job)
    log(f"We Work Remotely: {len(items)} returned, {len(out)} kept")
    return out


def fetch_adzuna() -> list[dict]:
    if not (config.ENABLE_ADZUNA and ADZUNA_APP_ID and ADZUNA_APP_KEY):
        if config.ENABLE_ADZUNA:
            log("Adzuna skipped: ADZUNA_APP_ID / ADZUNA_APP_KEY not set")
        return []
    out = []
    for what, where in config.ADZUNA_QUERIES:
        params = {
            "app_id": ADZUNA_APP_ID, "app_key": ADZUNA_APP_KEY,
            "what": what, "where": where, "max_days_old": 2,
            "results_per_page": 50, "sort_by": "date", "content-type": "application/json",
        }
        d = diag(f"adzuna: {what} ({where})")
        try:
            r = requests.get("https://api.adzuna.com/v1/api/jobs/ca/search/1",
                             params=params, headers=HEADERS, timeout=30)
            r.raise_for_status()
            items = r.json().get("results", []) or []
        except Exception as e:
            d["error"] = str(e)[:400]
            log(f"Adzuna '{what}' failed: {e}")
            continue
        kept = 0
        for j in items:
            posted = parse_dt(j.get("created"))
            sal = ""
            if j.get("salary_min") or j.get("salary_max"):
                sal = f"{j.get('salary_min') or '?'}-{j.get('salary_max') or '?'} CAD year"
            job = make_job(
                id=f"adzuna:{j.get('id')}", source="adzuna",
                title=j.get("title"), company=(j.get("company") or {}).get("display_name"),
                location=(j.get("location") or {}).get("display_name", ""),
                url=j.get("redirect_url"), posted=posted, remote=False,
                description=j.get("description"), salary=sal,
            )
            if keep(job, posted, d):
                out.append(job)
                kept += 1
        log(f"Adzuna '{what}' in {where}: {len(items)} returned, {kept} kept")
    return out


BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "en-CA,en;q=0.9",
}
RAW_BUILTIN_DIR = DATA_DIR / "raw_builtin"


def _meta(html_text: str, prop: str) -> str:
    m = re.search(r'<meta[^>]+(?:property|name)="' + re.escape(prop) + r'"[^>]+content="([^"]*)"', html_text, re.I) \
        or re.search(r'<meta[^>]+content="([^"]*)"[^>]+(?:property|name)="' + re.escape(prop) + r'"', html_text, re.I)
    return html.unescape(m.group(1)).strip() if m else ""


def _jsonld_jobposting(html_text: str) -> dict:
    for block in re.findall(r'<script[^>]+application/ld\+json[^>]*>(.*?)</script>', html_text, re.S | re.I):
        try:
            data = json.loads(block.strip())
        except Exception:
            continue
        for obj in (data if isinstance(data, list) else data.get("@graph", [data]) if isinstance(data, dict) else []):
            if isinstance(obj, dict) and obj.get("@type") == "JobPosting":
                return obj
    return {}


def _parse_builtin_job(html_text: str) -> dict:
    """Best-effort: JSON-LD if present, else page title / meta tags / visible text."""
    jp = _jsonld_jobposting(html_text)
    if jp:
        org = jp.get("hiringOrganization") or {}
        loc = jp.get("jobLocation") or {}
        if isinstance(loc, list):
            loc = loc[0] if loc else {}
        addr = (loc.get("address") or {}) if isinstance(loc, dict) else {}
        return {
            "title": jp.get("title", ""),
            "company": org.get("name", "") if isinstance(org, dict) else str(org),
            "location": ", ".join(x for x in (addr.get("addressLocality"), addr.get("addressRegion")) if x),
            "remote": "TELECOMMUTE" in str(jp.get("jobLocationType", "")).upper(),
            "posted": parse_dt(jp.get("datePosted")),
            "description": jp.get("description", ""),
        }
    page_title = html.unescape((re.search(r"<title[^>]*>(.*?)</title>", html_text, re.S | re.I) or [None, ""])[1]).strip()
    og = _meta(html_text, "og:title") or page_title
    title, company = og, ""
    # Typical patterns: "Title - Company | Built In Toronto", "Title at Company | Built In"
    core = re.split(r"\s*\|\s*Built\s*In", og, flags=re.I)[0]
    m = re.match(r"(.+?)\s+(?:-|–|—|at)\s+(.+)$", core)
    if m:
        title, company = m.group(1).strip(), m.group(2).strip()
    body = re.sub(r"<(script|style|noscript)[^>]*>.*?</\1>", " ", html_text, flags=re.S | re.I)
    return {"title": title, "company": company, "location": "", "remote": False, "posted": None,
            "description": clean_text(body), "meta_description": _meta(html_text, "description")}


def fetch_builtin() -> list[dict]:
    if not config.ENABLE_BUILTIN:
        return []
    RAW_BUILTIN_DIR.mkdir(parents=True, exist_ok=True)
    out, job_links = [], {}
    for i, (url, default_loc) in enumerate(config.BUILTIN_SEARCHES):
        d = diag(f"builtin search {i + 1}")
        try:
            r = requests.get(url, headers=BROWSER_HEADERS, timeout=40)
            d["http_status"] = r.status_code
            if r.status_code != 200:
                raise RuntimeError(f"HTTP {r.status_code}: {r.text[:200]}")
            page = r.text
            (RAW_BUILTIN_DIR / f"search_{i + 1}.html").write_text(page)
        except Exception as e:
            d["error"] = str(e)[:400]
            log(f"Built In search {i + 1} failed: {e}")
            continue
        links = re.findall(r'href="(?:https://builtintoronto\.com)?(/job/[^"/?#]+/(\d+))[^"]*"[^>]*>(.*?)</a>', page, re.S)
        for path, jid, anchor in links:
            text = clean_text(anchor, 200)
            if jid not in job_links or (text and not job_links[jid][1]):
                job_links[jid] = (path, text, default_loc, "remote" in url)
        if re.search(r"[?&]page=2\b", page):
            d["error"] = "more than one page of results; only page 1 read (robots.txt)"
        d["links_found"] = len({j for _, j, _ in links})
        log(f"Built In search {i + 1}: {d['links_found']} job links")

    d = diag("builtin job pages")
    for n, (jid, (path, anchor_title, default_loc, is_remote)) in enumerate(job_links.items()):
        if n >= config.BUILTIN_MAX_JOB_PAGES:
            break
        # Cheap pre-filter on the card title before fetching the detail page.
        if anchor_title and not title_ok(anchor_title):
            d["returned"] += 1
            d["rejected_by"]["title"] = d["rejected_by"].get("title", 0) + 1
            if len(d["rejected_samples"]) < 8:
                d["rejected_samples"].append(f"[title] {anchor_title}")
            continue
        time.sleep(1)
        job_url = f"https://builtintoronto.com{path}"
        try:
            r = requests.get(job_url, headers=BROWSER_HEADERS, timeout=40)
            if r.status_code != 200:
                raise RuntimeError(f"HTTP {r.status_code}")
            if n < 2:
                (RAW_BUILTIN_DIR / f"job_{jid}.html").write_text(r.text)
            info = _parse_builtin_job(r.text)
        except Exception as e:
            d["error"] = f"{job_url}: {str(e)[:200]}"
            info = {"title": anchor_title, "company": "", "location": "", "remote": False,
                    "posted": None, "description": ""}
        title = info["title"] or anchor_title
        loc = info["location"] or default_loc
        remote = info["remote"] or is_remote
        if remote and "remote" not in loc.lower():
            loc = f"{loc} (Remote)"
        job = make_job(id=f"builtin:{jid}", source="builtin", publisher="Built In",
                       title=title, company=info["company"], location=loc, url=job_url,
                       posted=info["posted"], remote=remote, description=info["description"])
        if keep(job, info["posted"], d):
            out.append(job)
    log(f"Built In: {len(job_links)} links, {len(out)} kept")
    return out


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------
def norm_key(job: dict) -> str:
    """Cross-source key so the same role on LinkedIn and Indeed shows once."""
    def n(s): return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()
    return f"key:{n(job['company'])}|{n(job['title'])}"


def load_seen() -> dict[str, str]:
    if SEEN_FILE.exists():
        data = json.loads(SEEN_FILE.read_text())
        return data.get("seen", {}) if isinstance(data.get("seen"), dict) else {}
    return {}


def save_seen(seen: dict[str, str]) -> None:
    cutoff = (NOW - timedelta(days=config.SEEN_RETENTION_DAYS)).date().isoformat()
    seen = {k: v for k, v in seen.items() if v >= cutoff}
    SEEN_FILE.write_text(json.dumps({"seen": dict(sorted(seen.items()))}, indent=1))


def main() -> int:
    DAILY_DIR.mkdir(parents=True, exist_ok=True)
    seen = load_seen()
    today = NOW.date().isoformat()

    # The workflow has several backup cron slots because GitHub often starts
    # scheduled runs hours late. Only the first scheduled run of the day does
    # anything; later ones exit here, so JSearch credits are spent once a day.
    # Manual "Run workflow" runs always go ahead.
    if not REPLAY and os.environ.get("GITHUB_EVENT_NAME") == "schedule":
        # Cron is UTC-only. The workflow fires at 05:31 and 06:31 UTC; whichever
        # one is before 1:00 AM Toronto time (the 05:31 slot in winter) exits,
        # so the day's fetch starts at 1:31 AM Toronto year-round.
        from zoneinfo import ZoneInfo
        local = NOW.astimezone(ZoneInfo("America/Toronto"))
        if local.hour < 1:
            log(f"{local:%H:%M} Toronto is before 1:00 AM; waiting for the next slot.")
            return 0
        if (DAILY_DIR / f"{today}.json").exists():
            log(f"Already fetched today ({today}); backup slot exiting without API calls.")
            return 0

    if REPLAY:
        # Free sources are cheap, so re-fetch them; only JSearch is replayed.
        seen = {}
    sources = {
        "jsearch": fetch_jsearch,
        "remoteok": fetch_remoteok,
        "weworkremotely": fetch_wwr,
        "adzuna": fetch_adzuna,
        "builtin": fetch_builtin,
    }
    stats, all_jobs = {}, []
    for name, fn in sources.items():
        jobs = fn()
        stats[name] = len(jobs)
        all_jobs.extend(jobs)

    new_jobs = []
    for job in all_jobs:
        k = norm_key(job)
        if job["id"] in seen or k in seen:
            continue
        seen[job["id"]] = today
        seen[k] = today
        new_jobs.append(job)

    daily_file = DAILY_DIR / f"{today}.json"
    if REPLAY:
        out = DATA_DIR / "replay.json"
        out.write_text(json.dumps({"diagnostics": DIAG, "jobs": new_jobs}, indent=1))
        log(f"Replay done: {len(new_jobs)} kept -> {out.relative_to(BASE_DIR)} (seen list untouched)")
        return 0
    run_id = NOW.strftime("%Y-%m-%dT%H%M%SZ")
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    RAW_DIR.mkdir(parents=True, exist_ok=True)

    # Raw JSearch responses: latest copy for --replay, plus a gzipped archive per run.
    RAW_FILE.write_text(json.dumps(RAW_RESPONSES))
    (RAW_DIR / f"{run_id}.json.gz").write_bytes(
        gzip.compress(json.dumps(RAW_RESPONSES).encode()))

    # 1) This run's own file. Written once, never overwritten or merged.
    new_jobs.sort(key=lambda j: j["posted"] or "", reverse=True)
    run_payload = {
        "run_id": run_id,
        "run_at": NOW.isoformat(),
        "date": today,
        "source_counts_after_filters": stats,
        "new_count": len(new_jobs),
        "diagnostics": DIAG,
        "jobs": new_jobs,
    }
    run_file = RUNS_DIR / f"{run_id}.json"
    if run_file.exists():
        raise SystemExit(f"Refusing to overwrite existing run file {run_file.name}")
    run_file.write_text(json.dumps(run_payload, indent=1))

    # 2) The day's file = union of every run that day. Earlier jobs are always
    #    kept as-is (never re-filtered), so nothing already marked seen can vanish.
    day_jobs, day_runs = {}, []
    if daily_file.exists():
        prior = json.loads(daily_file.read_text())
        day_runs = prior.get("runs") or ([prior["run_at"]] if prior.get("run_at") else [])
        for j in prior.get("jobs", []):
            day_jobs[j["id"]] = j
    for j in new_jobs:
        day_jobs.setdefault(j["id"], j)
    day_runs.append(run_id)
    jobs_sorted = sorted(day_jobs.values(), key=lambda j: j["posted"] or "", reverse=True)
    daily_payload = {
        "run_at": NOW.isoformat(),          # time of the most recent run that day
        "date": today,
        "runs": day_runs,
        "source_counts_after_filters": stats,
        "new_count": len(jobs_sorted),
        "diagnostics": DIAG,
        "jobs": jobs_sorted,
    }
    daily_file.write_text(json.dumps(daily_payload, indent=1))

    # 3) latest.json = pointer copy of this run only (convenience, not a record).
    LATEST_FILE.write_text(json.dumps(run_payload, indent=1))

    save_seen(seen)
    log(f"Done: {len(all_jobs)} matched filters, {len(new_jobs)} new -> "
        f"data/runs/{run_id}.json (day total {len(jobs_sorted)} in data/daily/{today}.json)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
