# FORENSIGHT AI · PROJECT NIGHTFALL

First-round digital-forensics investigation prototype. The Vercel version is a static frontend with bundled fictional PROJECT NIGHTFALL data; it does not call a backend, use environment variables, or make network requests.

## Build and preview

From the `forensight` project directory:

```sh
npm install
npm run build
```

The deployable static site is `dist/`. Preview it locally with any static file server, for example `npx serve dist`.

## Deploy on Vercel

The Git repository contains this project in the `forensight/` directory. In Vercel set:

- Root Directory: `forensight`
- Framework Preset: Other
- Build Command: `npm run build`
- Output Directory: `dist`
- Install Command: `npm install`
- Environment variables: none

Deploy the Git repository; Vercel builds and serves only the static output. No Python functions are created.

## Prototype interactions

- Dashboard totals and evidence explorer use the bundled 361-record case export.
- Search, type filters, pagination, artifact details and bundled SHA-256 checks run in the browser.
- Timeline filters and incident-window selection operate on the local records.
- Relationship graph and GPS coordinate plot are derived from those records.
- Copilot questions use local, case-grounded demo retrieval and cite artifact IDs; there is no live LLM.

The Python backend remains available for separate local development under `backend/`; it is not used by this static deployment. `scripts/generate-demo-data.py` is a one-time exporter from the local synthetic input under `data/nightfall/`. Build/deploy does not require Python or the source dataset because the checked-in browser bundle is `frontend/demo-data.js`.
