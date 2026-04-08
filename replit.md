# Workspace

## Overview

This Replit workspace is the development environment for the **Greater Richmond VA Motivated Seller Lead Scraper** — a fully automated pipeline that scrapes, scores, and publishes real estate distress leads across 5 jurisdictions.

---

## Richmond VA Lead Scraper

### GitHub Repo
`sailakshmi01/richmond-city-va`

### Architecture
- **Scraper**: `scraper/fetch.py` — Python 3.11 + Playwright + Requests
- **Dashboard**: `dashboard/index.html` — GitHub Pages static site
- **Data outputs**: `dashboard/records.json`, `data/records.json`, `data/leads.csv`
- **Automation**: GitHub Actions (`.github/workflows/scrape.yml`) — runs daily at 3 AM ET

### Jurisdictions Covered
| Code | Name | Status |
|---|---|---|
| 760 | Richmond City | ✅ GIS REST + OCIS |
| 087 | Henrico County | 🔄 OCIS only |
| 041 | Chesterfield County | 🔄 OCIS only |
| 085 | Hanover County | 🔄 OCIS only |
| 075 | Goochland County | 🔄 OCIS only |

### Data Sources
1. **Richmond City GIS** (free REST, no auth) — property transfers with Foreclosure/Special Financing tags
2. **Richmond City Surplus** (free REST) — city surplus/tax-sale properties
3. **OCIS Playwright** — Virginia circuit court civil cases (all 5 courts): foreclosure suits, judgments, IRS liens
4. **ACT DataScout Playwright** — Richmond City land instruments (Lis Pendens, Mechanic Liens)

### Lead Types & Scores
| Type | Score |
|---|---|
| Lis Pendens | 90 |
| Foreclosure / Forced Sale | 85 |
| Foreclosure Civil Suit | 83 |
| IRS Tax Lien | 78 |
| Federal Tax Lien | 75 |
| Judgment | 72 |
| Probate | 68 |
| Mechanic Lien | 65 |
| HOA Lien | 60 |
| Surplus Property | 55 |
| Special Financing | 50 |

+10 pts if sale price < 70% of assessed value (max 100)

---

## GitHub Push Helper

**Secret required**: `GITHUB_PERSONAL_ACCESS_TOKEN` — stored in Replit Secrets
- Scopes needed: `repo` + `workflow`

### Push commands (run from workspace root)
```bash
# Push everything (workflows + scraper + data)
python3 scraper/github_push.py all

# Push only data outputs
python3 scraper/github_push.py outputs

# Push specific files
python3 scraper/github_push.py dashboard/records.json data/leads.csv

# Push workflow files (requires workflow scope)
python3 scraper/github_push.py .github/workflows/scrape.yml
```

### Or from Python
```python
from scraper.github_push import push_files, push_standard_outputs, push_all
push_all("feat: my change description")
```

---

## Key Files
| File | Purpose |
|---|---|
| `scraper/fetch.py` | Main scraper — all sources, scoring, export |
| `scraper/github_push.py` | GitHub file pusher (uses PAT secret) |
| `scraper/test_ocis.py` | OCIS diagnostic — logs all API responses + screenshots |
| `scraper/requirements.txt` | Python deps: requests, bs4, lxml, playwright |
| `.github/workflows/scrape.yml` | Daily scrape + GitHub Pages deploy |
| `.github/workflows/test-ocis.yml` | Manual trigger to test OCIS Playwright scraper |
| `dashboard/index.html` | GitHub Pages dashboard |
| `dashboard/records.json` | Lead data served to dashboard |
| `data/leads.csv` | GHL CRM export |

---

## OCIS Technical Notes
- **URL**: `eapps.courts.state.va.us/ocis` — Angular SPA, cannot be scraped via HTTP
- **Terms button ID**: `#acceptTerms` (confirmed from JS bundle analysis)
- **REST API**: `/ocis-rest/api/public/search` — POST with `{courtLevels, selectedCourts, searchBy, searchString, divisions}`
- **Auth mechanism**: Session cookie set by clicking #acceptTerms; `page.evaluate()` fetch calls inherit it automatically
- **Court FIPS4 codes**: `760C` (Richmond City), `087C` (Henrico), `041C` (Chesterfield), `085C` (Hanover), `075C` (Goochland)
- **Cannot run locally** — Playwright requires system libs only available in CI (GitHub Actions ubuntu-22.04)

---

## pnpm Monorepo (pre-existing)

- **Monorepo tool**: pnpm workspaces
- **Node.js version**: 24
- **Package manager**: pnpm
- **TypeScript version**: 5.9
- **API framework**: Express 5
- **Database**: PostgreSQL + Drizzle ORM
