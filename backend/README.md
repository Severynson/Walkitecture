# Walkitecture backend

FastAPI service that turns DWG/DXF floor plans into the canonical building JSON, applies
**validated, allowlisted edits** with undo history, and interprets natural-language edit
requests with OpenAI (with a deterministic rule-based fallback).

| Concern | Implementation |
| --- | --- |
| API | FastAPI, camelCase JSON, OpenAPI docs at `/docs` |
| Storage | PostgreSQL 17: raw uploads as `bytea`, building model as `JSONB`, edit history table |
| DWG → DXF | LibreDWG `dwg2dxf` (compiled into the Docker image) |
| DXF parsing | ezdxf |
| Edit interpretation | OpenAI structured outputs (`OPENAI_MODEL`, default `gpt-5-mini`); rule-based fallback |
| Migrations | Alembic (run automatically on container start) |

## Run it

```bash
# from the repository root
docker compose up --build          # API: http://localhost:8000/docs  Postgres: localhost:5433
```

To enable OpenAI, put `OPENAI_API_KEY=sk-...` in a top-level `.env` file (git-ignored) before
`docker compose up`. Without a key, `/api/interpret-edit` uses the rule-based interpreter.

### Local development (without the backend container)

```bash
docker compose up -d db
cd backend
cp .env.example .env               # points at localhost:5433
uv sync
uv run alembic upgrade head
uv run uvicorn app.main:app --reload
```

DWG uploads need `dwg2dxf` on your `PATH` locally (`DWG2DXF_PATH`); DXF uploads work anywhere.

### Tests

```bash
cd backend
uv run pytest            # API tests need the compose database; they are skipped if it is down
uv run ruff check . && uv run ruff format --check .
```

## Typical flow

```bash
# 1. Upload (DWG is converted to DXF immediately) and parse
curl -F file=@plan.dwg "http://localhost:8000/api/plans?parse=true"
#    ...or create the built-in sample plan:
curl -X POST http://localhost:8000/api/plans/demo

# 2. Fetch the scene
curl http://localhost:8000/api/plans/$PLAN/scene

# 3. Edit directly (buttons/sliders) ...
curl -X POST http://localhost:8000/api/plans/$PLAN/edits -H 'content-type: application/json' -d '{
  "command": {"operation": "resize_selected_window", "targetId": "window_1", "parameters": {"factor": 1.5}},
  "baseVersion": 1, "source": "ui"}'

# ... or through natural language (apply=true applies it if it validates)
curl -X POST http://localhost:8000/api/interpret-edit -H 'content-type: application/json' -d '{
  "text": "Make the selected window 50% wider", "selectedId": "window_1", "planId": "'$PLAN'", "apply": true}'

# 4. Undo
curl -X POST http://localhost:8000/api/plans/$PLAN/undo
```

## Endpoints

| Method & path | Purpose |
| --- | --- |
| `GET /api/health` | DB status, whether `dwg2dxf` is available, active interpreter |
| `POST /api/plans` | Upload `.dwg`/`.dxf` (multipart `file`; `?parse=true` to parse immediately) |
| `POST /api/plans/demo` | Create and parse the built-in sample apartment |
| `GET /api/plans` · `GET/DELETE /api/plans/{id}` | List, inspect, delete plans |
| `POST /api/plans/{id}/parse` | Extract and normalize geometry (body: optional parse options). Clears edit history |
| `GET /api/plans/{id}/scene` | Canonical building JSON + `version` |
| `PUT /api/plans/{id}/scene` | Replace the scene with a hand-authored model (validated, undoable) |
| `GET /api/plans/{id}/file?kind=original\|dxf` | Download the upload or the converted DXF |
| `POST /api/plans/{id}/edits[?dryRun=true]` | Apply (or preview) one allowlisted edit |
| `GET /api/plans/{id}/edits` | Edit history |
| `POST /api/plans/{id}/undo` | Revert the latest edit |
| `POST /api/interpret-edit` | Text + `selectedId` + `planId` (or inline `model`) → typed command, validation result, optional apply |

### Edit operations (the allowlist)

| `operation` | `targetId` | `parameters` |
| --- | --- | --- |
| `resize_selected_window` | window ID (a wall with exactly one window also works) | `factor` *or* `width`, plus optional `height`, `sillHeight` |
| `resize_windows` | — | `factor` |
| `set_ceiling_height` | — | `height` (2.1–6.0 m) |
| `move_selected_wall` | wall ID (free-standing walls only) | `offset`: meters along the wall's left-hand normal |

Windows resize around their center and shift to stay inside the wall when possible. Every edit
response includes `affectedIds` (walls/openings whose meshes the renderer should rebuild; `building`
means global properties changed) and the new `version`. Send `baseVersion` to get `409` instead of
silently overwriting a newer model. Rejected edits return `422` with machine-readable `problems`.

`/api/interpret-edit` accepts an optional `cameraForward: [x, y]` (plan coordinates) so that
"move this wall back 30 cm" can be resolved; without it, directional words trigger a clarification.

## Drawing convention

Converting DWG → DXF only changes the file format; walls are still raw lines that must be
interpreted. The parser assumes (layer names are configurable globs, case-insensitive):

| Layer (defaults) | Contents |
| --- | --- |
| `WALL`, `WALLS`, `A-WALL*` | Wall lines (`LINE`, `LWPOLYLINE`, `POLYLINE`) |
| `WINDOW`, `WINDOWS`, `A-GLAZ*`, `A-WIND*` | One entity or block per window, drawn on its wall |
| `DOOR`, `DOORS`, `A-DOOR*` | One entity or block per door (e.g. leaf + swing arc) |
| `ROOM`, `ROOMS`, `A-AREA*`, `A-ROOM*` | Closed room outlines and `TEXT`/`MTEXT` labels |

- **`wallStyle: "centerline"`** (default): each wall line is a centerline; thickness = `wallThickness`
  (default 0.15 m).
- **`wallStyle: "double_line"`**: walls drawn as two parallel face lines; thickness is inferred from
  the gap (`minWallThickness`–`maxWallThickness`), and corners/T-junctions are joined.
- Wall lines broken at a window or door are merged back into one wall.
- If no wall-layer geometry exists, **all unclassified linework is treated as walls** (with a warning).
- Units come from `$INSUNITS`; if missing they are guessed from the drawing size (with a warning).
  Override with `units` (`mm`, `cm`, `m`, `in`, `ft`) or a calibration `scale` (drawing units → meters).
- Coordinates are shifted so the plan's minimum corner is at (0, 0).

`samples/demo-apartment.dxf` (regenerate with `uv run python scripts/make_sample_dxf.py`) is the
reference drawing. The parse response's `parseStats.layers` lists every layer found, which helps
map an unfamiliar drawing's layer names.

Example parse options:

```json
{ "units": "mm", "wallStyle": "double_line", "wallLayers": ["A-WALL*"], "ceilingHeight": 2.8 }
```

## Code map

```
app/
  main.py            app factory, CORS, routers, error handlers
  config.py          settings (env vars / .env)
  db.py, models.py   async SQLAlchemy engine/session, plans + plan_edits tables
  api/               HTTP routes (plans, edits, interpret, health) and error mapping
  schemas/           building model, edit commands, parse options, API bodies
  services/
    dwg_convert.py   format detection + LibreDWG conversion
    dxf_parser.py    DXF -> building model
    edits.py         allowlisted edit operations + validation
    geometry.py      vector math and validation rules
    interpreter.py   OpenAI + rule-based natural-language interpretation
    plan_store.py    persistence: parse, edit history, undo, optimistic versioning
    sample_plan.py   the demo apartment DXF
alembic/             migrations
tests/               unit + API tests
```
