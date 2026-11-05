# AGENTS.md

This project ships agent context: skills under `.agents/skills/` and MCP servers in `.mcp.json`. Load them before writing code.

## Project

React + TypeScript (Vite) multi-modal oncology patient record ("Rayfin Patient 360") backed by **Project Rayfin**.
Fifteen real TCGA patients; for each, the clinical record, tumour genome, diagnostic H&E slide (OpenSeadragon deep
zoom with an AI lymphocyte overlay) and CT/MRI (NiiVue, with AI segmentations) on one page.
Data lives in three Fabric Lakehouses (`clinical_lakehouse`, `genomics_lakehouse`, `imaging_lakehouse`); a fourth,
`patient360`, contains only OneLake shortcuts. Functions query the `patient360` SQL analytics endpoint as the app
identity (cross-silo T-SQL) and sign read-only, per-patient OneLake SAS links so the browser streams pixels straight
from the silos. Tumour board notes are the only app-owned data (`TumorBoardNote` entity). Production auth is Fabric
Entra SSO; `rayfin dev` signs in locally with the Rayfin CLI login (dev-only dynamic import in `src/main.tsx`).

## Repo map

- `src/` – React UI
- `src/components/ui/` – Radix-based UI components (shadcn)
- `src/components/` – App components (AppShell, Panel, Doc, AuthPage, Wordmark, ThemeToggle, Scrubber)
- `src/components/patient/` – Timeline (vertical lifetime timeline) and TimelineDetail (an item in a tab), Imaging
  (PathologyPanel, RadiologyPanel), Genomics, Clinical (registry, report, notes), PrimaryCare (MeasurementsPanel,
  PrimaryCareGlance), ReportPdf
- `src/hooks/` – React hooks (useCohort / usePatient, useTheme, AuthContext)
- `src/lib/` – `api.ts` (function types and calls, SAS file URLs), `record.ts` (tabs, mark kinds per tab, `tabFor`),
  `format.ts`, `clinical.ts` (observation vocabulary,
  reference ranges, score interpretation, drug classes), `timelineLink.ts` (`openInTimeline`: any section can select
  a timeline mark by id, or by kind and title nearest a date)
- `src/pages/` – Patients, PatientView, DataMap, Sources, AuthCallback
- `pipeline/` – Python data pipeline: cohort, downloads (`download_sources.py`: TCGA-Reports, GDC PanImmune files,
  Synthea jar; `download_imaging.py`: GDC slides, IDC CT/MR/SEG/TIL), clinical and genomics fetch (GDC, cBioPortal),
  Synthea background, slide tiling, radiology, tables, OneLake upload and shortcuts (IDs from `fabric.env` via
  `fabric_config.py`), `summaries.json`, `run_all.py` (README steps 5–6 in order)
- `scripts/cloud_load.py` – optional: runs `run_all.py` on a temporary Azure VM (managed identity, no public inbound
  ports; `cloud_load.sh` is the VM's job). Resource-group tags record the workspace and principal before the grant;
  cleanup verifies the recorded workspace role is gone before deleting the group. Requires workspace Admin.
- `scripts/setup_fabric.py` – finds or creates the workspace and four Lakehouses; writes `fabric.env`,
  `.env.production.local` (`VITE_FABRIC_WORKSPACE_*`, the header link) and the `<fabric-config>` block in
  `function_app.ts`. That block is committed only with zero GUIDs
- `src/services/` – Service layer (ServiceContainer, Rayfin services, auth services)
- `rayfin/rayfin.yml` – Rayfin configuration (auth, data, functions, static hosting)
- `rayfin/functions/` – Functions (`getCohort`, `getPatient`, `getDataMap`)
- `rayfin/data/` – Entities (export from `rayfin/data/schema.ts`)
- `local/` – Local mode (`npm run dev:local`, `.env.localdata`, `VITE_LOCAL_DATA=1`): a Vite plugin
  (`local/plugin.ts`) serves `/local-api/*` and `/local-files/<silo>/*`. `local/api.ts` runs the three functions'
  queries in DuckDB over `work/tables/<silo>/*.parquet` (as `dbo.<table>` views); keep them in step with
  `function_app.ts`. Files come from `work/`, notes from `work/local/notes.json`, and there is no sign-in
  (`LocalDataAuthService`, no RayfinClient). `src/lib/localMode.ts` holds the build-time `LOCAL_DATA` flag; production
  builds are unaffected

## UI (Radix-based shadcn/ui)

This project uses **Radix-based UI components** styled with **Tailwind CSS v4**.

- UI components live in `src/components/ui/`
- Utility helper lives in `src/lib/utils.ts` (`cn`)
- Add a component: `npx shadcn@latest add <component>`

Key config/files:

- Global styles + Tailwind import: `src/styles/theme.css`
- Path alias `@/*` → `src/*`: `tsconfig.json`
- Vite plugins + alias: `vite.config.ts`
- shadcn registry/config: `components.json`

## Architecture

### Service Layer

- **ServiceContainer**: Singleton that initializes and provides access to services; auto-detects local vs. production and selects the appropriate auth service
- **RayfinClientService**: Manages the RayfinClient singleton instance
- **lib/api.ts**: Calls `getCohort`, `getPatient`, `getDataMap`; builds signed OneLake URLs with `fileUrl`
- **useCohort / usePatient**: cohort context plus a per-session patient cache (prefetched on hover)

### Auth Service Layer

- **RayfinAuthService**: Fabric Entra authentication operations (production)
- **RayfinFabricAuthService**: Delegates to `@microsoft/rayfin-auth-provider-fabric` SDK for Entra flows
- **MockAuthService**: Local development mock authentication (email/password via local backend)
- **AuthContext**: React context for auth state management

### Routing

- `/auth` – Public auth page (Fabric sign-in or mock sign-in; redirects to `/` if authenticated)
- `/auth/callback` – Fabric Entra OAuth callback
- `/` – Cohort overview; `/p/<TCGA barcode>?tab=<tab>&e=<mark id>` – patient record (timeline left, tabs right)
- `/data` – Where the data lives (live inventory and shortcut list)
- `/sources` – Sources, licences, method

### Key files

- `rayfin/functions/src/function_app.ts` – Fabric IDs in the `<fabric-config>` block (written by `setup_fabric.py`);
  SQL over the `patient360` endpoint (database = Lakehouse **name**), per-patient
  SAS, Fabric API shortcut listing, query provenance
- `src/components/patient/Imaging.tsx` – OpenSeadragon viewer (OneLake tile source, TIL overlay as a single image placed
  in viewport units, tour) and NiiVue viewer (CT presets, AI label maps with LUT, timepoints)
- `src/pages/PatientView.tsx` – the record, in chapters: `ChapterRule` (2 px orange rule, pill at the start,
  Hide/Show action at the end) for *Timeline* (the chart, indented under its rule; controls portalled into the record
  header) and *Details* (folder tabs on a plain rule; with the timeline on top this
  rule is also the resize handle). The timeline has no background; the lane and time column under the pointer are
  shaded. `RecordHead`, the timeline (on top by default, or left; `p360.timelineLayout`,
  height and width in localStorage, one resizable splitter for both; collapsed to a strip with
  `p360.timelineCollapsed`, size animated by CSS except while dragging), `TabBar` and `TabPane`. The DOM is the same in
  both layouts so the timeline keeps its zoom when flipped. The record scrolls as one page (`#patient-scroll` is the `<main>` in `Patients.tsx`); the tab bar is sticky, and
  with the timeline on the left the timeline column is sticky and a screen tall. The tab and selected mark are URL state (`?tab=&e=`);
  timeline clicks and tab switches push history, stepping within a tab replaces it. `tabFor` keeps the current tab
  when it can show the mark (a visit stays in Measurements). Each tab defaults to its latest item and remembers the
  last one. Pathology, Radiology and Report stay mounted once opened (hidden), and a `resize` event is sent on tab
  or layout change. `openInTimeline` requests are resolved here. Notes are loaded once (`useNotes`) and drawn as marks
- `src/components/patient/Timeline.tsx` – `buildModel` turns the bundle into marks in five lanes (`visits`,
  `problems`, `cancer`, `diagnostics`, `notes`; each lane has per-row `sizes`). Ids such as `v:<encounter_id>`,
  `c:<condition>` (a `band`), `m:<medicine>:<span>` (an `inset` bar inside its condition's row when its `reason`
  matches a kept condition), `t:<treatment index>`, `r:<study uid>`, `slide` / `sl:<slide_id>`, `report`, `genome`;
  `noteMarks` adds `n:<note id>` at the note's `eventId`. Conditions and medicines are coloured by `bodySystem()` in
  `src/lib/clinical.ts` (`--sys-*` tokens in `atlas.css`). `LifeTimeline` computes a time axis and a cross axis and
  maps them to x/y for `orientation` (`top` or `left`); piecewise time scale (life before diagnosis compressed), zoom
  window `{f0, f1}` over the base scale, gliding zoom changes, `lens` fading marks outside the open tab. Controls
  (zoom − slider + and factor, one layout toggle, Key pop-over, collapse) are portalled into the record header
  (`controlsHost`), so the timeline has no toolbar.
  Lane sizes are multipliers on each lane's natural size (`p360.laneSizes`), changed by dragging the grips beside
  the lane names (the next lane gives the space) or double-clicking a name. `collapsed` draws one thin row per lane.
  The date pill (and a 10 px hit area around the selection line, `.tl-pick-line`) is a picker; hovering
  either sets `pickHover` (pill lifts, line thickens). `detents` (one per visible mark, same-day marks fanned 8 px apart, 5 px
  collapsed) and `startPick`/`land` select the nearest notch while dragging, calling `onSelect(id, stepping)` (the
  record pushes history for the first notch and replaces it after). The wheel zooms; shift + wheel pans.
  Conditions are solid round-ended lines (medications a thinner line beneath); names go just before
  their line (conditions in their colour), after it when there is no room; label size follows the row height (at least 10.5 px); `labelSpecs` joins names that
  start together ("Pemetrexed + Cisplatin"), places conditions first and tries the other end of the line before
  dropping a name that would collide. In the
  horizontal layout the years sit on top under the ages
- Timeline performance: pointer moves and wheel events are handled once per animation frame; the marks/labels and the
  lanes/grid/axis are memoised layers (`marksLayer`, `frameLayer`), so hover and the picker only re-render overlays.
  The picker draws the pill and line from local state and tells the record once per frame inside `startTransition`;
  `body.tl-picking` turns off entrance animations while it moves. In `PatientView`, `TabPane`/`IndexRow` are memoised
  and their callbacks stable (`go` reads `setSearchParams` and `select` reads the tab through refs). Keep new props
  to these stable. `npm run preview:local` serves the production build from `work/` for profiling
- `src/components/patient/TimelineDetail.tsx` – an item in a tab (visit, condition, medicine, vaccine, cancer events,
  treatment, scan, slide, genome, tests): Earlier and Later step through the tab's items; cross-links select other
  marks by id
- `src/components/patient/ReportPdf.tsx` – draws the scanned report with PDF.js (never an iframe, which Fabric and
  managed browsers block). Decoders and standard fonts are served from `/pdfjs/` by the `pdfjsAssets` plugin in
  `vite.config.ts`. `warmPdfJs()` loads the code, a shared worker and those files into memory when a record opens,
  because the static host's sign-in expires after about an hour and later lazy fetches would get 401
- `src/components/Scrubber.tsx` – the one slider for stepping through instances (a tab's items by date, report pages;
  the slide and scan viewers' own sliders are off inside the record); items carry a 0..1 position
- `src/components/patient/PrimaryCare.tsx` – synthetic primary care: `MeasurementsPanel` (trend small multiples with
  shared hover and the selected visit pinned, visits per year) and `PrimaryCareGlance` (problem list, medicines,
  allergies, lifestyle and screening, immunisations; in Summary)
- `src/components/patient/Genomics.tsx` – canvas genome plot (copy number + mutation rainfall), cohort percentiles
- `pipeline/download_imaging.py --tissue-slides` and `build_slides.py --tissue` add the frozen sections (TS/BS) under
  `slides/<id>/<section>/`, inside the per-patient SAS scope; `pathology_slide` has one row per slide with `role`
  (`diagnostic` or `frozen`) and `sort_order`, so cross-silo joins must filter `role = 'diagnostic'`
- `pipeline/tumour_board_notes.json` – one illustrative case-review note per patient, seeded into `TumorBoardNote` as
  the signed-in user with author `Tumour board (illustrative)`; the UI labels them. `TumorBoardNote.eventId`
  (optional) anchors a note to a timeline mark; notes without it sit at diagnosis
- `pipeline/build_tables.py` – shapes everything into typed tables per silo; `pipeline/upload_onelake.py` writes Delta
  with delta-rs, uploads Files with azcopy and creates the shortcuts

## Data notes

- Lakehouse SQL analytics endpoints expose strings as `varchar(8000)`: long report text is stored in parts and cohort
  distributions as quantiles.
- Function typegen cannot express tuple types; keep function return types to objects and arrays.
- IDC DICOM SEGs do not always share the CT's pixel layout (several store rows posterior to anterior);
  `read_seg` in `pipeline/build_radiology.py` places every labelled pixel through the SEG frame's own position,
  orientation and spacing. Check a new segmentation by the mean HU under each label.
- Anything synthetic (names, day/month of diagnosis, Synthea primary-care history) must stay labelled in the UI.
- Data sources must allow commercial reuse (NIH GDC open access, CC0, CC BY, CC BY-SA): no NC or ND data. The Thorsson
  2018 supplementary table is CC BY-NC-ND, so immune values come from the study's GDC data files
  (`work/sources/panimmune/`); immune subtypes and neoantigens are deliberately absent. IDC queries filter on
  `license_short_name`; GDC queries on `access = open`. No data is committed: everything downloads into `work/`.
- Synthea items (observation, condition, medication, immunization, allergy, procedure, careplan) carry `encounter_id`,
  which links them to their visit in the detail panel. Reference-range flags are adult ranges and are suppressed under
  18.

## Environment

- `.env` – Default Fabric API configuration
- `.env.local` – Local overrides (git-ignored, generated by `rayfin dev`)
- `rayfin/.env` – Runtime + deployment env (managed by Rayfin CLI)
- `rayfin/.deployments.json` – Registry of all Fabric workspace deployments (managed by Rayfin CLI)
- `fabric.env`, `.env.production.local` – Fabric IDs written by `scripts/setup_fabric.py` (git-ignored)

## Code conventions

- TypeScript strict mode; prefer small functional React components.
- Keep UI logic in `src/`; keep data model code in `rayfin/data/`.
- Services follow the singleton pattern via `ServiceContainer`.
- `ServiceContainer` auto-selects `MockAuthService` for localhost backends and `RayfinAuthService` for production.

## Generated files (do not edit)

- `rayfin/.temp/` – Rayfin/DAB config generation output
- `dist/` – Vite build output
- `.env.local` – Local environment overrides (git-ignored, script-managed)
