"""
Search settings for Setareh's daily fetcher. Edit this file to change what gets pulled.

The fetcher is deliberately "dumb": it casts a reasonably wide net using cheap
keyword rules. Fit scoring, judgment and the digest happen in Claude afterwards,
so prefer letting a borderline role through over filtering it out here.

Targets: coordinator / specialist / associate roles in marketing, marketing
operations, CRM, client onboarding, operations, customer success, revenue or
sales operations, content and social media; project coordinator; project
manager only for marketing, digital, creative, client, content or campaign work.
Location: GTA on-site or hybrid, or remote within Canada.
"""

# ---------------------------------------------------------------------------
# JSearch (Google for Jobs -> LinkedIn, Indeed, Glassdoor, ZipRecruiter, company sites)
#
# Quota: each page = 1 request on the v2 endpoint. 4 queries x 1 page = 4/day
# ~= 120/month on the free BASIC plan (200/month), leaving ~80 for re-runs.
# ---------------------------------------------------------------------------
JSEARCH_URL = "https://jsearch.p.rapidapi.com/search-v2"
JSEARCH_DATE_POSTED = "3days"   # today | 3days | week | month | all
                                # Seen-job tracking means a role is only ever reported once.

JSEARCH_QUERIES = [
    # (query, country code, remote_only, pages)
    ("marketing coordinator OR marketing specialist in Toronto, ON", "ca", False, 1),
    ("CRM coordinator OR marketing operations coordinator OR client onboarding specialist "
     "in Toronto, ON", "ca", False, 1),
    ("operations coordinator OR project coordinator in Toronto, ON", "ca", False, 1),
    # Remote within Canada. "remote" goes in the query text rather than the
    # work_from_home flag, which returns worldwide spam; the location filter
    # below drops anything that isn't Canada-remote or GTA.
    ("remote marketing coordinator OR remote CRM coordinator Canada", "ca", False, 1),
]

# ---------------------------------------------------------------------------
# Free sources (no key). Set to False to disable.
# ---------------------------------------------------------------------------
ENABLE_REMOTEOK = False    # Off: the free API returns ~100 mixed (mostly dev, US) jobs with
                           # no category filter, so almost nothing would match her titles.
ENABLE_WWR = True          # We Work Remotely RSS feeds below
ENABLE_ADZUNA = True       # only runs if ADZUNA_APP_ID / ADZUNA_APP_KEY secrets exist
ENABLE_BUILTIN = True      # Built In Toronto search pages (page 1 only; robots.txt blocks ?page=)

WWR_FEEDS = [
    "https://weworkremotely.com/categories/remote-sales-and-marketing-jobs.rss",
    "https://weworkremotely.com/categories/remote-customer-support-jobs.rss",
]

# (url, default location label). Toronto, hybrid + in-office, last 1 day.
# Never add &page= - Built In's robots.txt disallows it. Both categories can run
# past one page on busy days; only page 1 is read.
BUILTIN_SEARCHES = [
    ("https://builtintoronto.com/jobs/hybrid/office/marketing"
     "?daysSinceUpdated=1&city=Toronto&state=Ontario&country=CAN&allLocations=true", "Toronto, ON"),
    ("https://builtintoronto.com/jobs/hybrid/office/operations"
     "?daysSinceUpdated=1&city=Toronto&state=Ontario&country=CAN&allLocations=true", "Toronto, ON"),
]
# Cap on job links considered per run across both searches. Card titles are
# filtered first, so only matching roles get a detail-page fetch (1s apart).
BUILTIN_MAX_JOB_PAGES = 60

ADZUNA_QUERIES = [
    # (what, where) - Canada endpoint
    ("marketing coordinator", "Toronto"),
    ("crm coordinator", "Toronto"),
    ("operations coordinator", "Toronto"),
    ("project coordinator", "Toronto"),
]

# Only keep listings posted within this many hours (applies to all sources).
MAX_AGE_HOURS = 168  # 7 days; seen-job tracking stops repeats.

# ---------------------------------------------------------------------------
# Title filters
# TITLE_INCLUDE: case-insensitive substring match (any one is enough).
# TITLE_EXCLUDE: whole-word match (any one drops the title).
# ---------------------------------------------------------------------------
_FUNCTIONS = [
    "marketing", "digital marketing", "marketing operations", "marketing automation",
    "crm", "hubspot", "onboarding", "client onboarding", "customer onboarding",
    "operations", "customer success", "client success", "revenue operations", "revops",
    "sales operations", "content", "social media", "campaign",
]
_LEVELS = ["coordinator", "specialist", "associate"]

TITLE_INCLUDE = (
    # "Marketing Coordinator", "CRM Specialist", "Operations Associate", ...
    [f"{fn} {lvl}" for fn in _FUNCTIONS for lvl in _LEVELS]
    # "Coordinator, Marketing", "Specialist - CRM", ...
    + [f"{lvl}{sep}{fn}" for fn in _FUNCTIONS for lvl in _LEVELS
       for sep in (", ", " - ", " – ", " of ")]
    + ["project coordinator", "coordinator, projects"]
    # Project manager only with a marketing / digital / creative / client /
    # content / campaign qualifier ("project manager" on its own is not listed).
    + [f"{q} project manager" for q in
       ("marketing", "digital", "creative", "client", "content", "campaign")]
    + [f"project manager{sep}{q}" for q in
       ("marketing", "digital", "creative", "client", "content", "campaign")
       for sep in (", ", " - ", " – ", " (")]
)

TITLE_EXCLUDE = [
    # Manager-and-above
    "director", "vp", "vice president", "head of", "chief", "marketing manager",
    "senior project manager", "sr project manager", "sr. project manager",
    # Internships
    "intern", "internship", "co-op", "coop",
    # Engineering / construction / IT / infrastructure
    "engineer", "engineering", "construction", "it", "infrastructure",
]

# ---------------------------------------------------------------------------
# Location filters
# A job passes if it's in the GTA, OR it's remote and not restricted to a
# region outside Canada.
# ---------------------------------------------------------------------------
GTA_KEYWORDS = [
    "toronto", "north york", "scarborough", "etobicoke", "mississauga",
    "brampton", "caledon", "markham", "vaughan", "concord", "woodbridge",
    "richmond hill", "thornhill", "aurora", "newmarket", "stouffville", "king city",
    "oakville", "burlington", "milton", "halton hills", "georgetown",
    "pickering", "ajax", "whitby", "oshawa", "gta",
]

REMOTE_KEYWORDS = ["remote", "anywhere", "work from home", "distributed"]

# A remote role passes only if its location is one of these (or has no specific
# country). US-only, Europe, India, etc. are dropped.
REMOTE_ALLOWED_REGIONS = [
    "canada", "north america", "americas", "anywhere", "worldwide", "global",
    "ontario", "quebec", "british columbia", "alberta", "manitoba", "saskatchewan",
    "nova scotia", "new brunswick",
]

# Reposting/spam aggregators that list themselves as the employer
# (matched against the company name, case-insensitive substring).
EXCLUDE_COMPANIES = [
    "remote quest jobs", "jzone", "jobshives", "wfhsprint", "remotical", "jobgether",
    "remotepromsp",
]

# State file pruning: forget job IDs older than this many days.
SEEN_RETENTION_DAYS = 60
