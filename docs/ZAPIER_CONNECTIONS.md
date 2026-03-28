# ZAPIER CONNECTIONS — CORE AGI Full Autonomy Map

**Goal:** Make CORE truly autonomous — able to perceive, remember, communicate, act, and self-improve
across all dimensions of Ki's work and life.

**How to connect:** https://mcp.zapier.com/mcp/servers/1b142a72-dda7-4341-a414-0a55cc6ee99f/config
For each app below, enable the listed actions. Each becomes a callable `zapier:<app>_<action>` MCP tool.

**Priority tiers:**
- P0 — Core autonomy. CORE is blind without these. Connect first.
- P1 — High value. Enables major capability domains.
- P2 — Nice to have. Expands coverage.

---

## DOMAIN 1 — MEMORY & EXTERNAL KNOWLEDGE BASE

| Priority | App | Actions to Enable | Why |
|---|---|---|---|
| P0 | Notion | Create Page, Update Page, Find Page, Create/Update Database Item, Query Database | Human-readable KB. Milestones, evolution logs, project docs. |
| P1 | Airtable | Create/Update/Find Record, List Records | Structured logging when Sheets is too flat |
| P2 | Mem.ai | Create Note, Search Notes | Personal AI memory layer |

Ki must do: Create Notion workspace with "CORE Sessions" and "CORE Milestones" databases. Connect Notion to Zapier. Share workspace with Zapier integration.

---

## DOMAIN 2 — COMMUNICATION

| Priority | App | Actions to Enable | Why |
|---|---|---|---|
| P0 | Gmail | Send Email, Find Email, Create Draft, Reply to Email | Primary backup alert channel. Telegram is single point of failure. |
| P0 | Slack | Send Channel Message, Send Direct Message, Find Channel | Team/project status updates. Create #core-agi channel. |
| P1 | WhatsApp Business | Send Message | Indonesia-primary messaging. More reliable than email for Ki. |
| P1 | Discord | Send Channel Message | Community notifications |
| P2 | Pushover | Send Notification | Emergency mobile push (bypasses Do Not Disturb) |
| P2 | Twilio | Send SMS | Absolute fallback |

Ki must do: Connect Gmail (OAuth). Create Slack workspace with #core-agi channel. WhatsApp requires Meta Business verification.

---

## DOMAIN 3 — RESEARCH & INFORMATION GATHERING

CORE currently has ZERO external perception. This domain fixes the biggest blind spot.

| Priority | App | Actions to Enable | Why |
|---|---|---|---|
| P0 | Perplexity | Ask Question | Real-time web research without browser. |
| P0 | RSS by Zapier | Find Item in Feed | Monitor blogs, GitHub releases, changelogs automatically. |
| P1 | Feedly | Create Entry, Find Entry | Curated AI/tech news feeds |
| P1 | Pocket | Add Item, Get List | Save articles for later processing |
| P2 | Reddit | Find Post, Create Post | Monitor r/LocalLLaMA, r/MachineLearning |
| P2 | YouTube | Find Video | Find tutorials relevant to current CORE tasks |

Ki must do: Subscribe to key RSS feeds. Get Perplexity API key (paid tier required for Zapier).

---

## DOMAIN 4 — TASK & PROJECT MANAGEMENT

| Priority | App | Actions to Enable | Why |
|---|---|---|---|
| P0 | Todoist | Create Task, Update Task, Complete Task, Find Task | Ki's personal task inbox. CORE queues work here for manual action. |
| P0 | GitHub Issues | Create Issue, Update Issue, Find Issue, Add Comment | CORE logs bugs/tasks directly to repo. |
| P1 | Linear | Create Issue, Update Issue | Engineering-style issue tracking |
| P1 | Trello | Create Card, Update Card, Move Card | Visual board for CORE phases |
| P2 | Asana / ClickUp | Create Task, Update Task | If Ki uses these |

Ki must do: Connect Todoist. Ensure GitHub Zapier integration has write access to pockiesaints7/core-agi.

---

## DOMAIN 5 — FINANCE & ECONOMICS

| Priority | App | Actions to Enable | Why |
|---|---|---|---|
| P0 | Google Sheets | Create Row, Update Row, Find Row, Create Spreadsheet | Log all CORE infra costs |
| P1 | Wave | Create Transaction, Find Transaction | Free accounting. CORE logs all expenses. |
| P1 | Wise | Find Transfer, Get Balance | Monitor Ki's Wise balance (Indonesia ops) |
| P1 | Stripe | Find Customer, Find Payment | Monitor revenue if Ki sells anything |
| P2 | Midtrans (via webhook) | Monitor payments | Indonesian payment gateway — most relevant locally |

Ki must do: Create Google Sheet "CORE Cost Tracker" with columns: Date, Service, Cost (USD), Notes.

---

## DOMAIN 6 — INFRASTRUCTURE MONITORING & ALERTING

| Priority | App | Actions to Enable | Why |
|---|---|---|---|
| P0 | PagerDuty | Create Incident, Resolve Incident, Get Incident | CORE creates/resolves incidents for itself. |
| P0 | Better Uptime | Create Monitor, Create Heartbeat | Ping Railway endpoint every 5 min. Alert if down. |
| P1 | Datadog | Create Event, Create Monitor | Metrics and alerting for Railway |
| P1 | UptimeRobot | Create Monitor | Free alternative to Better Uptime |
| P2 | Sentry | Create Issue, Find Issue | Error tracking for core.py exceptions |

Ki must do: Create free PagerDuty account with Ki's email/phone as escalation target. Set up Better Uptime on https://core-agi-production.up.railway.app/health (every 5 min).

---

## DOMAIN 7 — FILES, DOCUMENTS & STORAGE

| Priority | App | Actions to Enable | Why |
|---|---|---|---|
| P0 | Google Drive | Upload File, Create Folder, Find File, Copy File, Create File from Text | Store CORE outputs humans can access |
| P0 | Google Docs | Create Document, Append Text, Find Document | Readable session reports and logs |
| P1 | Google Sheets | Create Spreadsheet, Create/Update Row | Structured data outputs |
| P1 | Dropbox | Upload File, Find File | Backup storage |
| P2 | OneDrive | Upload File | Windows-native backup for Ki |

Note: Google Drive already partially connected. Expand to include write actions.

Ki must do: Create "CORE AGI" folder in Google Drive with subfolders: Sessions/, Reports/, Evolutions/, Artifacts/

---

## DOMAIN 8 — CODE, DEPLOYMENT & DEVOPS

| Priority | App | Actions to Enable | Why |
|---|---|---|---|
| P0 | GitHub | Create Issue, Create/Update File, Create Branch, Create PR, Push Files | CORE self-modifies. Backup write path when gh_search_replace fails. |
| P1 | Vercel | Create Deployment, Find Deployment | If frontend/edge functions added to CORE |
| P1 | Cloudflare (via webhook) | Deploy Worker, Purge Cache | Manage vault worker deployments |
| P2 | Docker Hub | Find Image, Get Tag | Monitor base image updates affecting Railway |

Note: GitHub MCP already in Claude Desktop. Zapier GitHub = redundancy/backup path only.

---

## DOMAIN 9 — SOCIAL & WEB PRESENCE

| Priority | App | Actions to Enable | Why |
|---|---|---|---|
| P1 | Twitter/X | Create Tweet, Find Tweet, Send DM | Post CORE milestones, monitor mentions |
| P1 | LinkedIn | Create Post, Find Connection | Share progress professionally |
| P2 | Instagram | Create Post | Visual updates |
| P2 | Medium | Create Post | Long-form CORE development blog |
| P2 | Substack (via webhook) | Send newsletter | Progress newsletter to followers |

Ki must do: Connect Twitter/X and LinkedIn. Define posting rules: what CORE can post autonomously vs draft-for-review.

---

## DOMAIN 10 — SCHEDULING & TIME

| Priority | App | Actions to Enable | Why |
|---|---|---|---|
| P0 | Google Calendar | Create Event, Find Event, Update Event, Delete Event | Schedule maintenance windows, review sessions |
| P1 | Calendly | Find Event Type, Get Scheduled Events | Understand Ki's availability |
| P2 | World Time API (via webhook) | Get Current Time | CORE is timezone-aware (Jakarta = UTC+7) |

Ki must do: Connect Google Calendar to Zapier. Create "CORE AGI" calendar to isolate CORE-created events.

---

## DOMAIN 11 — COMMERCE & MONETIZATION

| Priority | App | Actions to Enable | Why |
|---|---|---|---|
| P1 | Stripe | Create Customer, Find Payment, Create Price | Monitor revenue if Ki sells anything |
| P1 | Gumroad | Find Sale, Find Product | Simple digital product sales |
| P1 | Shopify | Find Order, Create/Update Product | E-commerce if Ki builds a store |
| P2 | Midtrans (via webhook) | Monitor payments | Indonesian payment gateway |
| P2 | Lemon Squeezy | Find Order, Create License | SaaS-style digital products |

Ki must do: Connect whichever payment processor Ki uses. For Midtrans: use Webhooks by Zapier.

---

## DOMAIN 12 — AI TOOLS & MODEL ROUTING

| Priority | App | Actions to Enable | Why |
|---|---|---|---|
| P0 | OpenAI | Send Prompt, Generate Image | Fallback reasoning + image gen when Claude can't |
| P1 | Google AI Studio / Gemini | Send Prompt, Understand Document | Multi-model verification. Gemini for long docs. |
| P2 | ElevenLabs | Generate Speech | Audio output — read CORE summaries aloud |
| P2 | Stability AI | Generate Image | Alternative image generation |

Note: Groq already integrated natively in core.py — skip Zapier for Groq.

Ki must do: Get OpenAI API key. Get Gemini API key (free tier available). Set image generation budget.

---

## DOMAIN 13 — IOT & PHYSICAL WORLD

| Priority | App | Actions to Enable | Why |
|---|---|---|---|
| P1 | Home Assistant (via webhook) | Trigger automation, Get sensor state | Control home devices, physical environment |
| P1 | IFTTT (via webhook) | Trigger applet | Bridge to any smart home device |
| P2 | Philips Hue | Control Light | Flash lights as physical CORE alert |
| P2 | Oura Ring (via webhook) | Get sleep/health data | CORE monitors Ki's biometrics |
| P2 | Strava | Find Activity | Monitor Ki's physical activity |

Ki must do: Set up Home Assistant or IFTTT as smart home bridge. Connect Oura/Strava if Ki uses them.

---

## DOMAIN 14 — CRM & PEOPLE INTELLIGENCE

| Priority | App | Actions to Enable | Why |
|---|---|---|---|
| P1 | HubSpot | Create/Update/Find Contact, Create Deal | CRM for Ki's professional relationships |
| P1 | Airtable | Create Record, Find Record | Simple contact/relationship database |
| P2 | Google Contacts | Create/Update/Find Contact | Native contact sync |
| P2 | Clay | Find Person, Enrich Person | Deep contact enrichment |

Ki must do: Connect HubSpot free CRM (or Airtable as simpler alternative).

---

## DOMAIN 15 — WEBHOOKS & ESCAPE HATCH (Most powerful)

| Priority | App | Actions to Enable | Why |
|---|---|---|---|
| P0 | Webhooks by Zapier | POST, GET, PUT | Call ANY URL. Railway /patch endpoint. Custom services. Anything. |
| P0 | Code by Zapier | Run Python, Run JavaScript | Execute code inside Zapier. Transform data. Call APIs. |
| P1 | Email by Zapier | Send Email | Generic email without needing Gmail auth |
| P2 | Storage by Zapier | Get Value, Set Value | Simple key-value store inside Zapier |
| P2 | Formatter by Zapier | Format Date/Number/Text | Data transformation before sending anywhere |

Ki must do: Enable "Webhooks by Zapier" — no account needed, just enable it in config. "Code by Zapier" requires paid Zapier plan.

---

## DOMAIN 16 — HEALTH, WELLBEING & LIFE

| Priority | App | Actions to Enable | Why |
|---|---|---|---|
| P1 | Strava | Find Activity, Create Activity | Monitor exercise — understand Ki's energy level |
| P1 | Oura Ring | Get Daily Summary | Sleep score, HRV, readiness — CORE adjusts session intensity |
| P1 | MyFitnessPal | Find Food Log | Nutrition awareness |
| P2 | Daylio (via webhook) | Log mood | Mood tracking — CORE adapts tone/approach |
| P2 | Apple Health (via Shortcuts+webhook) | Get health data | iOS health data bridge |

Ki must do: Connect Strava or Oura to Zapier (if Ki uses these). Set up Apple Shortcuts → Webhook bridge for iOS Health.

---

## DOMAIN 17 — MEDIA, CONTENT & LEARNING

| Priority | App | Actions to Enable | Why |
|---|---|---|---|
| P1 | YouTube | Find Video, Find Channel | Find tutorials relevant to current CORE task |
| P1 | Spotify | Find Playlist, Create Playlist | Curate focus playlists based on CORE work mode |
| P1 | Pocket | Add Item, Retrieve List | Save articles for later — CORE queues reading material |
| P2 | Readwise | Create Highlight, Get Highlights | Surface relevant knowledge from Ki's reading history |
| P2 | Kindle (via webhook) | Get highlights | Import book highlights into CORE KB |

Ki must do: Connect Spotify. Connect Pocket or Readwise if Ki uses these.

---

## PRIORITY CONNECTION ORDER

### Week 1 — P0 (8 things, fix biggest blind spots)
1. Gmail — backup alert channel (Connect Google account to Zapier)
2. Notion — external knowledge base (Create workspace + databases first)
3. Todoist — human-facing task queue for Ki
4. Perplexity — real-time research (Get API key)
5. Better Uptime — Railway self-monitoring (Create free account)
6. Webhooks by Zapier — escape hatch (Just enable it, no account needed)
7. Google Calendar — time awareness (Connect Google account)
8. Google Drive write actions — expand existing connection (upload/create/folder)

### Week 2 — P1
9. Slack (#core-agi channel)
10. GitHub write actions (expand existing connection)
11. Google Sheets write actions (Create Row, Update Row)
12. PagerDuty (incident management)
13. OpenAI (multi-model fallback)
14. Google Docs write actions
15. Twitter/X (milestone announcements)

### Week 3+ — P2 (Life integration)
Strava, Oura, WhatsApp Business, HubSpot, Spotify, Readwise, Home Assistant

---

## CAPABILITY UNLOCKED AFTER ALL CONNECTIONS

| Capability | Before Zapier | After Zapier |
|---|---|---|
| Alert Ki | Telegram only | + Gmail + Slack + WhatsApp |
| External memory | Supabase only | + Notion + Google Docs |
| Self-monitoring | Manual | PagerDuty + Better Uptime auto-alerts |
| Research | Claude training only | + Perplexity + RSS real-time |
| Schedule work | None | Google Calendar events |
| Track costs | None | Sheets + Wave ledger |
| Manage tasks | task_queue table | + Todoist + Linear + GitHub Issues |
| Other AI models | Groq only | + OpenAI + Gemini + ElevenLabs |
| Post updates | None | Twitter/X + LinkedIn + Discord |
| Sense environment | None | Oura + Strava + Home Assistant |
| Reach any app | Only core-agi tools | + 8,000 apps via Zapier |

---

Owner: REINVAGNAR (Ki)
Config URL: https://mcp.zapier.com/mcp/servers/1b142a72-dda7-4341-a414-0a55cc6ee99f/config
See also: docs/ZAPIER_MCP.md — usage patterns, tool conventions, decision tree
Last updated: 2026-03-12
