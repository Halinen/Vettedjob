import hashlib
import re
import httpx
from bs4 import BeautifulSoup

from utils import canonical_url


def _stable_id(prefix: str, url: str) -> str:
    """Generate a stable ID from the MD5 of the canonicalized URL."""
    return f"{prefix}_{hashlib.md5(canonical_url(url).encode()).hexdigest()[:12]}"


def _varbi_id(url: str) -> str:
    m = re.search(r'jobID:(\d+)', url)
    if m:
        return f"varbi_{m.group(1)}"
    return f"varbi_{hashlib.md5(canonical_url(url).encode()).hexdigest()[:12]}"


def _match_any(text: str, words: list[str]) -> bool:
    """Whether `text` contains any of the words in `words` (case-insensitive)."""
    text_lower = text.lower()
    return any(w.lower() in text_lower for w in words)


def fetch_varbi(include: list[str], exclude: list[str] = []) -> tuple[list[dict], dict]:
    """Varbi — Swedish university recruitment system RSS (KTH/Uppsala/Lund etc.)"""
    import xml.etree.ElementTree as ET
    feeds = {
        "KTH":             "https://kth.varbi.com/what:rssfeed/",
        "Uppsala":         "https://uu.varbi.com/what:rssfeed/",
        "Lund":            "https://lu.varbi.com/what:rssfeed/",
        "KI":              "https://ki.varbi.com/what:rssfeed/",
        "SU":              "https://su.varbi.com/what:rssfeed/",
        "Umeå":            "https://umu.varbi.com/what:rssfeed/",
        "Luleå (LTU)":     "https://ltu.varbi.com/what:rssfeed/",
        "Mittuniversitet": "https://miun.varbi.com/what:rssfeed/",
        "Karlstad":        "https://kau.varbi.com/what:rssfeed/",
    }
    all_items = []
    for name, feed_url in feeds.items():
        try:
            resp = httpx.get(feed_url, headers={"User-Agent": "Mozilla/5.0"}, timeout=15)
            resp.raise_for_status()
            root = ET.fromstring(resp.text)
            for item in root.findall(".//item"):
                title = item.findtext("title") or ""
                link  = item.findtext("link") or ""
                desc  = item.findtext("description") or ""
                all_items.append({
                    "id":          _varbi_id(link),
                    "title":       title,
                    "company":     name,
                    "description": BeautifulSoup(desc, "html.parser").get_text()[:800],
                    "url":         link,
                    "_text":       (title + " " + desc).lower(),
                })
        except Exception as e:
            print(f"  Varbi {name} failed: {e}")

    fetched = len(all_items)

    if include:
        after_inc = [j for j in all_items if _match_any(j["_text"], include)]
    else:
        after_inc = all_items

    if exclude:
        after_exc = [j for j in after_inc if not _match_any(j["_text"], exclude)]
    else:
        after_exc = after_inc

    for j in after_exc:
        j.pop("_text", None)

    stats = {"fetched": fetched, "after_include": len(after_inc), "after_exclude": len(after_exc)}
    return after_exc, stats


def fetch_liu(include: list[str], exclude: list[str] = []) -> tuple[list[dict], dict]:
    """LiU (Linköping) — own RSS; URLs rebuilt to official detail pages to avoid ReachMee 404"""
    import xml.etree.ElementTree as ET
    from urllib.parse import urlparse as _up, parse_qs as _pqs
    r = httpx.get("https://liu.se/rss/liu-jobs-en.rss",
                  headers={"User-Agent": "Mozilla/5.0"}, timeout=15, follow_redirects=True)
    r.raise_for_status()
    root = ET.fromstring(r.text)

    all_items = []
    for item in root.findall(".//item"):
        title = item.findtext("title") or ""
        link  = item.findtext("link") or ""
        desc  = item.findtext("description") or ""
        rmjob = _pqs(_up(link).query).get("rmjob", [None])[0]

        # Rebuild the official URL using rmjob to bypass the migrated web103.reachmee.com
        if rmjob:
            job_id   = f"liu_{rmjob}"
            safe_url = f"https://liu.se/en/work-at-liu/vacancies/?rmjob={rmjob}"
        else:
            job_id   = f"liu_{hashlib.md5(link.encode()).hexdigest()[:12]}"
            safe_url = link  # keep the original URL when there is no rmjob (rarely happens)

        all_items.append({
            "id":          job_id,
            "title":       title,
            "company":     "LiU",
            "description": BeautifulSoup(desc, "html.parser").get_text()[:800],
            "url":         safe_url,
            "_text":       (title + " " + desc).lower(),
        })

    fetched = len(all_items)

    if include:
        after_inc = [j for j in all_items if _match_any(j["_text"], include)]
    else:
        after_inc = all_items

    if exclude:
        after_exc = [j for j in after_inc if not _match_any(j["_text"], exclude)]
    else:
        after_exc = after_inc

    for j in after_exc:
        j.pop("_text", None)

    stats = {"fetched": fetched, "after_include": len(after_inc), "after_exclude": len(after_exc)}
    return after_exc, stats


_CTH_VALIDATOR = "a72aeedd63ec10de71e46f8d91d0d57c"
_CTH_BASE      = "https://web103.reachmee.com/ext/I003/304"
_CTH_PAGE      = "https://www.chalmers.se/en/about-chalmers/work-with-us/vacancies/"


def fetch_chalmers(include: list[str], exclude: list[str] = []) -> tuple[list[dict], dict]:
    """Chalmers University of Technology — scrape the list and details via the official iframe.

    Both the listing page and detail pages are extracted through the ReachMee iframe
    within the playwright session, requiring no separate HTTP requests and no Referer header.
    Only items that pass filtering navigate to detail pages, reducing the number of requests.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("  Chalmers skipped: playwright not installed")
        return [], {"fetched": 0, "after_include": 0, "after_exclude": 0}

    all_items = []

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page = browser.new_page()
            page.goto(_CTH_PAGE, timeout=30000)
            page.wait_for_load_state("networkidle", timeout=20000)
            # Accept the cookie popup (if present)
            try:
                page.click("#CybotCookiebotDialogBodyLevelButtonLevelOptinAllowAll",
                           timeout=5000)
                page.wait_for_timeout(1500)
            except Exception:
                pass
            # Extract job links from the ReachMee iframe
            rmf = next((f for f in page.frames if "reachmee" in f.url), None)
            if rmf is None:
                print("  Chalmers: ReachMee iframe not found, skipping")
                browser.close()
                return [], {"fetched": 0, "after_include": 0, "after_exclude": 0}

            links = rmf.query_selector_all("a[href*='job_id']")
            for a in links:
                title = a.inner_text().strip()
                href  = a.get_attribute("href") or ""
                m = re.search(r"job_id=(\d+)", href)
                if not title or not m:
                    continue
                job_id = m.group(1)
                all_items.append({
                    "id":          f"cth_{job_id}",
                    "title":       title,
                    "company":     "Chalmers University of Technology",
                    "description": "",
                    "url":         f"{_CTH_PAGE}?rmpage=job&rmjob={job_id}&rmlang=UK",
                    "_job_id":     job_id,
                })

            fetched = len(all_items)

            # Filter (only the title is needed)
            after_inc = [j for j in all_items if _match_any(j["title"], include)] \
                        if include else all_items
            after_exc = [j for j in after_inc if not _match_any(j["title"], exclude)] \
                        if exclude else after_inc

            # Navigate to each detail page within the iframe, processing only items that passed filtering
            for j in after_exc:
                job_id = j.pop("_job_id")
                detail_url = (f"{_CTH_BASE}/job?site=5&lang=UK"
                              f"&validator={_CTH_VALIDATOR}&job_id={job_id}")
                try:
                    rmf.goto(detail_url, timeout=10000)
                    paras = rmf.query_selector_all("p")
                    desc = " ".join(
                        p.inner_text().strip() for p in paras if p.inner_text().strip()
                    )
                    j["description"] = re.sub(r"\s{2,}", " ", desc)[:800].strip()
                except Exception:
                    j["description"] = ""

            # Clean up the temporary field on items that are not returned
            for j in all_items:
                j.pop("_job_id", None)

            browser.close()

            stats = {"fetched": fetched,
                     "after_include": len(after_inc),
                     "after_exclude": len(after_exc)}
            return after_exc, stats

    except Exception as e:
        print(f"  Chalmers playwright failed: {e}")
        return [], {"fetched": 0, "after_include": 0, "after_exclude": 0}


def fetch_af_jobs(include: list[str], exclude: list[str] = [], max_results: int = 50) -> tuple[list[dict], dict]:
    """Arbetsförmedlingen JobSearch API — the include list is joined into a query string"""
    keywords = " ".join(include)
    url = "https://jobsearch.api.jobtechdev.se/search"
    resp = httpx.get(url, params={"q": keywords, "limit": max_results},
                     headers={"accept": "application/json"}, timeout=15)
    resp.raise_for_status()
    jobs = [
        {
            "id":          f"af_{h['id']}",
            "title":       h.get("headline", ""),
            "company":     h.get("employer", {}).get("name", "Unknown"),
            "description": h.get("description", {}).get("text", "")[:1000],
            "url":         h.get("webpage_url") or
                           f"https://arbetsformedlingen.se/platsbanken/annonser/{h['id']}"
        }
        for h in resp.json().get("hits", [])
    ]
    fetched = len(jobs)
    after_include = fetched  # the server already filters by keywords

    if exclude:
        jobs = [j for j in jobs
                if not _match_any(j["title"] + " " + j["description"], exclude)]

    stats = {"fetched": fetched, "after_include": after_include, "after_exclude": len(jobs)}
    return jobs, stats


def fetch_jobspy(include: list[str], exclude: list[str] = [], max_results: int = 20) -> tuple[list[dict], dict]:
    """JobSpy — aggregates Indeed + Google Jobs. `include` is joined into a query.

    Location/country come from config.json -> jobspy {location, country}, so this
    source works for any region without code changes. Defaults to a global Google
    Jobs search when unset.
    """
    from jobspy import scrape_jobs
    try:
        from utils import load_config
        _jc = load_config().get("jobspy", {})
    except Exception:
        _jc = {}
    location = _jc.get("location", "")
    country = _jc.get("country", "")
    hours_old = _jc.get("hours_old", 168)  # 7 days
    try:
        from utils import load_config
        remote_only = bool(load_config().get("remote_only", False))
    except Exception:
        remote_only = False
    keywords = " ".join(include)
    # Indeed needs a real country (it routes to that country's site); "worldwide"
    # returns nothing. Default to "usa" when unset rather than an invalid value.
    country_indeed = country or "usa"
    # Google Jobs reads the region from the query text, so fold location/remote in.
    google_term = keywords
    if remote_only:
        google_term = f"remote {google_term}"
    if location:
        google_term = f"{google_term} {location}"
    df = scrape_jobs(
        site_name=["indeed", "google"],
        search_term=keywords,
        google_search_term=google_term,
        location=location or None,
        results_wanted=max_results,
        country_indeed=country_indeed,
        hours_old=hours_old,
        is_remote=remote_only or None,
    )
    if df is None or df.empty:
        return [], {"fetched": 0, "after_include": 0, "after_exclude": 0}

    jobs = []
    for _, row in df.iterrows():
        uid = str(row.get("job_url", "") or row.get("id", ""))
        # jobspy exposes an is_remote column; carry it through so the pool/filter
        # can trust it rather than guessing from the description text.
        is_remote = row.get("is_remote")
        jobs.append({
            "id":          _stable_id("spy", uid),
            "title":       str(row.get("title", "")),
            "company":     str(row.get("company", "Unknown")),
            "description": str(row.get("description", ""))[:1000],
            "url":         str(row.get("job_url", "")),
            "is_remote":   bool(is_remote) if is_remote is not None else None,
        })

    fetched = len(jobs)
    after_include = fetched

    if exclude:
        jobs = [j for j in jobs
                if not _match_any(j["title"] + " " + j["description"], exclude)]

    stats = {"fetched": fetched, "after_include": after_include, "after_exclude": len(jobs)}
    return jobs, stats


def fetch_euraxess(include: list[str], exclude: list[str] = [],
                   max_results: int = 50) -> tuple[list[dict], dict]:
    """EURAXESS — EU research jobs platform; scrape the search page with httpx + BS4."""
    keywords = " ".join(include) if include else ""
    url = "https://euraxess.ec.europa.eu/jobs/search"
    params = {"keyword": keywords, "country": "Sweden"}
    try:
        resp = httpx.get(url, params=params,
                         headers={"User-Agent": "Mozilla/5.0"},
                         timeout=20, follow_redirects=True)
        resp.raise_for_status()
    except Exception as e:
        print(f"  EURAXESS fetch failed [{keywords}]: {e}")
        return [], {"fetched": 0, "after_include": 0, "after_exclude": 0}

    soup = BeautifulSoup(resp.text, "html.parser")
    job_links = [a for a in soup.select("a[href]")
                 if str(a.get("href", "")).startswith("/jobs/")
                 and "search" not in str(a.get("href", ""))]

    jobs = []
    seen = set()
    for a in job_links[:max_results]:
        href = str(a.get("href", ""))
        if href in seen:
            continue
        seen.add(href)
        title = a.get_text().strip()
        if not title or len(title) < 5:
            continue
        full_url = f"https://euraxess.ec.europa.eu{href}"
        job_id = href.rstrip("/").split("/")[-1]
        jobs.append({
            "id":          f"euraxess_{job_id}",
            "title":       title[:200],
            "company":     "EURAXESS",
            "description": "",
            "url":         full_url,
        })

    fetched = len(jobs)
    if exclude:
        jobs = [j for j in jobs if not _match_any(j["title"], exclude)]
    stats = {"fetched": fetched, "after_include": fetched, "after_exclude": len(jobs)}
    return jobs, stats


def fetch_academic_positions(include: list[str], exclude: list[str] = [],
                              max_results: int = 30) -> tuple[list[dict], dict]:
    """Academic Positions — Nordic academic recruitment platform; playwright bypasses Cloudflare."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("  Academic Positions skipped: playwright not installed")
        return [], {"fetched": 0, "after_include": 0, "after_exclude": 0}

    keywords = "+".join(include) if include else "phd"
    search_url = (f"https://academicpositions.com/jobs?keywords={keywords}"
                  f"&country=sweden&pageSize={max_results}")
    jobs = []
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page = browser.new_page()
            page.goto(search_url, timeout=30000)
            page.wait_for_load_state("networkidle", timeout=20000)

            cards = page.query_selector_all("article, .job-card, [class*='job']")
            for card in cards[:max_results]:
                title_el = card.query_selector("h2, h3, .title, a")
                link_el  = card.query_selector("a[href]")
                if not title_el or not link_el:
                    continue
                title = title_el.inner_text().strip()
                href  = link_el.get_attribute("href") or ""
                if not title or not href:
                    continue
                full_url = href if href.startswith("http") else f"https://academicpositions.com{href}"
                jobs.append({
                    "id":          f"ap_{hashlib.md5(canonical_url(full_url).encode()).hexdigest()[:12]}",
                    "title":       title[:200],
                    "company":     "Academic Positions",
                    "description": "",
                    "url":         full_url,
                })
            browser.close()
    except Exception as e:
        print(f"  Academic Positions fetch failed: {e}")

    fetched = len(jobs)
    if exclude:
        jobs = [j for j in jobs if not _match_any(j["title"], exclude)]
    stats = {"fetched": fetched, "after_include": fetched, "after_exclude": len(jobs)}
    return jobs, stats


def fetch_teamtailor(company_name: str, career_url: str,
                     include: list[str] = [],
                     exclude: list[str] = []) -> tuple[list[dict], dict]:
    """Generic Teamtailor ATS fetch function (playwright, JS rendering).
    Known companies using Teamtailor: Excillum, Qamcom
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print(f"  {company_name} skipped: playwright not installed")
        return [], {"fetched": 0, "after_include": 0, "after_exclude": 0}

    jobs = []
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page = browser.new_page()
            page.goto(career_url, wait_until="networkidle", timeout=30000)
            try:
                page.wait_for_selector("a[href*='/jobs/']", timeout=15000)
            except Exception:
                pass

            cards = page.query_selector_all("a[href*='/jobs/']")
            seen_urls: set[str] = set()
            for card in cards:
                href = card.get_attribute("href") or ""
                if not href or href in seen_urls or href.count("/") < 3:
                    continue
                seen_urls.add(href)
                full_url = (href if href.startswith("http")
                            else f"https://{career_url.split('/')[2]}{href}")
                title = card.inner_text().strip().split("\n")[0][:200]
                if not title:
                    continue
                jobs.append({
                    "id":          _stable_id("tt", full_url),
                    "title":       title,
                    "company":     company_name,
                    "description": "",
                    "url":         full_url,
                })
            browser.close()
    except Exception as e:
        print(f"  Teamtailor fetch failed [{company_name}]: {e}")

    fetched = len(jobs)
    if include:
        jobs = [j for j in jobs if _match_any(j["title"], include)]
    after_inc = len(jobs)
    if exclude:
        jobs = [j for j in jobs if not _match_any(j["title"], exclude)]
    stats = {"fetched": fetched, "after_include": after_inc, "after_exclude": len(jobs)}
    return jobs, stats


def fetch_excillum(include: list[str], exclude: list[str] = []) -> tuple[list[dict], dict]:
    """Excillum — X-ray tubes / microwave technology, Stockholm"""
    return fetch_teamtailor("Excillum", "https://career.excillum.com/jobs", include, exclude)


def fetch_qamcom(include: list[str], exclude: list[str] = []) -> tuple[list[dict], dict]:
    """Qamcom — radar / RF / signal processing, Gothenburg & Stockholm"""
    return fetch_teamtailor("Qamcom", "https://career.qamcom.com/jobs", include, exclude)


def fetch_workday(company_name: str, tenant: str, career_path: str,
                  include: list[str] = [], exclude: list[str] = [],
                  wd_number: int = 3, max_results: int = 20) -> tuple[list[dict], dict]:
    """Generic Workday ATS fetch function (httpx JSON API).
    Known config: ABB(tenant=abb, path=External_Career_Page, wd=3)
    """
    api_url = (f"https://{tenant}.wd{wd_number}.myworkdayjobs.com"
               f"/wday/cxs/{tenant}/{career_path}/jobs")
    payload = {
        "appliedFacets": {},
        "limit": max_results,
        "offset": 0,
        "searchText": " ".join(include) if include else "",
    }
    headers = {"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"}
    try:
        resp = httpx.post(api_url, json=payload, headers=headers, timeout=20)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        print(f"  Workday fetch failed [{company_name}]: {e}")
        return [], {"fetched": 0, "after_include": 0, "after_exclude": 0}

    jobs = []
    for item in data.get("jobPostings", []):
        ext = item.get("externalPath", "")
        full_url = (f"https://{tenant}.wd{wd_number}.myworkdayjobs.com"
                    f"/en-US/{career_path}{ext}")
        title = item.get("title", "")
        if not title:
            continue
        jobs.append({
            "id":          _stable_id("wd", full_url),
            "title":       title,
            "company":     company_name,
            "description": item.get("locationsText", ""),
            "url":         full_url,
        })

    fetched = len(jobs)
    if exclude:
        jobs = [j for j in jobs if not _match_any(j["title"] + " " + j["description"], exclude)]
    stats = {"fetched": fetched, "after_include": fetched, "after_exclude": len(jobs)}
    return jobs, stats


def fetch_abb(include: list[str], exclude: list[str] = [],
              max_results: int = 20) -> tuple[list[dict], dict]:
    """ABB — electrical / automation / industrial systems, multiple Swedish cities (Workday API, client-side Sweden filter)"""
    jobs, stats = fetch_workday("ABB", "abb", "External_Career_Page",
                                include, exclude, max_results=max_results)
    # Workday has no server-side geo filtering; keep Swedish positions client-side
    sweden = [j for j in jobs
              if "sweden" in j.get("description", "").lower()
              or "vaesteras" in j.get("description", "").lower()
              or "sweden" in j.get("url", "").lower()]
    stats["after_exclude"] = len(sweden)
    return sweden, stats


def fetch_atlas_copco(include: list[str], exclude: list[str] = [],
                      max_results: int = 20) -> tuple[list[dict], dict]:
    """Atlas Copco — industrial machinery / vacuum / gas technology (Algolia API).
    Does not use Workday; the official site uses Algolia for job search.
    App-Id: 9AX0H7NCCX, Index: GROUP_EN_dateDesc
    """
    _ALGOLIA_APP_ID = "9AX0H7NCCX"
    _ALGOLIA_API_KEY = "4415f5d1228e3b2da6ac78d10c41e93c"
    keywords = " ".join(include) if include else ""
    payload = {"requests": [{
        "indexName": "GROUP_EN_dateDesc",
        "params": (
            "filters=(data.tagsTranslated%3A%22Job%20vacancy%22"
            "%20AND%20data.country%3ASweden)"
            f"&hitsPerPage={max_results}&query={keywords.replace(' ', '%20')}"
        ),
    }]}
    try:
        resp = httpx.post(
            f"https://{_ALGOLIA_APP_ID.lower()}-dsn.algolia.net/1/indexes/*/queries",
            headers={
                "x-algolia-application-id": _ALGOLIA_APP_ID,
                "x-algolia-api-key": _ALGOLIA_API_KEY,
                "Content-Type": "application/json",
            },
            json=payload, timeout=15,
        )
        resp.raise_for_status()
        hits = resp.json().get("results", [{}])[0].get("hits", [])
    except Exception as e:
        print(f"  Atlas Copco (Algolia) fetch failed: {e}")
        return [], {"fetched": 0, "after_include": 0, "after_exclude": 0}

    jobs = []
    for h in hits:
        d = h.get("data", {})
        title = d.get("title", "")
        ext   = d.get("externalPath", "")
        job_id = str(d.get("jobID", ""))
        if not title or not ext:
            continue
        jobs.append({
            "id":          f"ac_{job_id}" if job_id else _stable_id("ac", ext),
            "title":       title,
            "company":     "Atlas Copco",
            "description": d.get("description", "")[:800],
            "url":         ext,
        })

    fetched = len(jobs)
    if exclude:
        jobs = [j for j in jobs if not _match_any(j["title"] + " " + j["description"], exclude)]
    stats = {"fetched": fetched, "after_include": fetched, "after_exclude": len(jobs)}
    return jobs, stats


def fetch_englishjobs(include: list[str], exclude: list[str] = [],
                      max_results: int = 20) -> tuple[list[dict], dict]:
    """EnglishJobs.se — aggregator for international job seekers (visa-friendly), httpx + BS4."""
    keywords = " ".join(include)
    url = f"https://www.englishjobs.se/?s={keywords.replace(' ', '+')}"
    try:
        resp = httpx.get(url, headers={"User-Agent": "Mozilla/5.0"},
                         timeout=15, follow_redirects=True)
        resp.raise_for_status()
    except Exception as e:
        print(f"  EnglishJobs fetch failed [{keywords}]: {e}")
        return [], {"fetched": 0, "after_include": 0, "after_exclude": 0}

    soup = BeautifulSoup(resp.text, "html.parser")
    jobs = []
    for card in soup.select("article")[:max_results]:
        title_el = card.select_one("h2, h3, .entry-title")
        link_el  = card.select_one("a[href]")
        if not title_el or not link_el:
            continue
        title = title_el.get_text(strip=True)
        href  = str(link_el.get("href", ""))
        if not title or not href or not href.startswith("http"):
            continue
        jobs.append({
            "id":          _stable_id("ej", href),
            "title":       title[:200],
            "company":     "EnglishJobs.se",
            "description": "",
            "url":         href,
        })

    fetched = len(jobs)
    if exclude:
        jobs = [j for j in jobs if not _match_any(j["title"], exclude)]
    stats = {"fetched": fetched, "after_include": fetched, "after_exclude": len(jobs)}
    return jobs, stats


def fetch_rise(include: list[str], exclude: list[str] = []) -> tuple[list[dict], dict]:
    """RISE Research Institutes of Sweden — materials / industry / energy, playwright (JS rendering)."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("  RISE skipped: playwright not installed")
        return [], {"fetched": 0, "after_include": 0, "after_exclude": 0}

    url = "https://www.ri.se/en/about-rise/work-with-us/open-job-positions"
    jobs = []
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page = browser.new_page()
            page.goto(url, timeout=30000)
            page.wait_for_load_state("networkidle", timeout=20000)

            links = page.query_selector_all("a[href*='/job'], a[href*='/position'], a[href*='/vacancy']")
            if not links:
                # Try generic content links
                links = page.query_selector_all("article a, .job-listing a, .views-row a")

            seen: set[str] = set()
            for a in links:
                href  = a.get_attribute("href") or ""
                title = a.inner_text().strip()
                if not href or not title or len(title) < 5 or href in seen:
                    continue
                seen.add(href)
                full_url = href if href.startswith("http") else f"https://www.ri.se{href}"
                jobs.append({
                    "id":          _stable_id("rise", full_url),
                    "title":       title[:200],
                    "company":     "RISE Research Institutes of Sweden",
                    "description": "",
                    "url":         full_url,
                })
            browser.close()
    except Exception as e:
        print(f"  RISE fetch failed: {e}")

    fetched = len(jobs)
    if include:
        jobs = [j for j in jobs if _match_any(j["title"], include)]
    after_inc = len(jobs)
    if exclude:
        jobs = [j for j in jobs if not _match_any(j["title"], exclude)]
    stats = {"fetched": fetched, "after_include": after_inc, "after_exclude": len(jobs)}
    return jobs, stats


def fetch_swerim(include: list[str], exclude: list[str] = []) -> tuple[list[dict], dict]:
    """Swerim — metal materials research institute (steel / alloys / surface treatment), playwright (JS rendering)."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("  Swerim skipped: playwright not installed")
        return [], {"fetched": 0, "after_include": 0, "after_exclude": 0}

    url = "https://www.swerim.se/en/career/vacant-jobs-and-master-degree-projects"
    jobs = []
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page = browser.new_page()
            page.goto(url, timeout=30000)
            page.wait_for_load_state("networkidle", timeout=20000)

            links = page.query_selector_all("a[href*='/job'], a[href*='/career/'], a[href*='/position']")
            if not links:
                links = page.query_selector_all("article a, .job a, li a")

            seen: set[str] = set()
            for a in links:
                href  = a.get_attribute("href") or ""
                title = a.inner_text().strip()
                if not href or not title or len(title) < 5 or len(title) > 200 or href in seen:
                    continue
                # Filter out navigation links
                if any(kw in href for kw in ["/en/career/vacant", "/areas-", "/about-", "/research-"]):
                    continue
                seen.add(href)
                full_url = href if href.startswith("http") else f"https://www.swerim.se{href}"
                jobs.append({
                    "id":          _stable_id("swerim", full_url),
                    "title":       title[:200],
                    "company":     "Swerim",
                    "description": "",
                    "url":         full_url,
                })
            browser.close()
    except Exception as e:
        print(f"  Swerim fetch failed: {e}")

    fetched = len(jobs)
    if include:
        jobs = [j for j in jobs if _match_any(j["title"], include)]
    after_inc = len(jobs)
    if exclude:
        jobs = [j for j in jobs if not _match_any(j["title"], exclude)]
    stats = {"fetched": fetched, "after_include": after_inc, "after_exclude": len(jobs)}
    return jobs, stats


# ─────────────────────────────────────────────
# Function registry — fetch_jobs.py looks up the corresponding function via the source field
# ─────────────────────────────────────────────
SOURCE_REGISTRY = {
    "varbi":              fetch_varbi,
    "liu":                fetch_liu,
    "chalmers":           fetch_chalmers,
    "af":                 fetch_af_jobs,
    "jobspy":             fetch_jobspy,
    "euraxess":           fetch_euraxess,
    "academic_positions": fetch_academic_positions,
    "excillum":           fetch_excillum,
    "qamcom":             fetch_qamcom,
    "abb":                fetch_abb,
    "atlas_copco":        fetch_atlas_copco,
    "englishjobs":        fetch_englishjobs,
    "rise":               fetch_rise,
    "swerim":             fetch_swerim,
}
