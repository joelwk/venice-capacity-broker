---
name: GCE health check root redirect
description: GET / returning a redirect (307) silently fails the GCE promote-step health probe, causing a 45-min timeout
---

# GCE Health Check: Root Route Must Return 200

## The Rule
`GET /` must return HTTP 200 in the broker API. Any redirect at `/` (307, 302, etc.) causes the GCE/Replit deployment promote-step health probe to fail silently — the probe does not follow redirects.

**Why:** Replit's GCE deployment infrastructure sends `GET /` as a startup probe before promoting a new container to live. If it doesn't receive a 200, the probe fails. A 307 redirect looks like a working app but the probe rejects it. The symptom is a 45-minute timeout with zero runtime logs captured (because the probe never succeeds, so the deployment system never considers the container healthy).

**How to apply:** `apps/broker_api/app.py` `_setup_static_routes()` — the `/` route must return `FileResponse` (200) not `RedirectResponse` (307). The buy.html file is served directly at `/`. The `/buy.html` alias still works for direct links.
