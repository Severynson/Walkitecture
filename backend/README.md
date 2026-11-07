# Walkitecture backend

FastAPI service implementing the pipeline in [`ARCHITECTURE.md`](../ARCHITECTURE.md):

```
upload DWG ─► DWG→DXF (LibreDWG) ─► descriptive drawing JSON ─► building model ─► Babylon.js scene spec
                                     (every entity classified)    (walls, openings,    (boxes, headers/sills,
                                                                   rooms, furniture)    glass, GLB furniture)
```

Every stage is stored in PostgreSQL per plan, per user. On top of that: validated, allowlisted
edits with undo, and natural-language edit interpretation (OpenAI, with a rule-based fallback).

| Concern | Implementation |
| --- | --- |
| API | FastAPI, camelCase JSON, OpenAPI docs at `/docs` (use **Authorize** to log in) |
| Auth | Username/password accounts (argon2id), JWT bearer tokens; plans are private to their owner |
| Storage | PostgreSQL 17: uploads as `bytea`; drawing JSON, model, and scene as `JSONB`; edit history |
| DWG → DXF | LibreDWG 0.14 `dwg2dxf`, compiled into the Docker image (handles DWG 2018+, e.g. `/examples`) |
| CAD parsing | ezdxf |
| LLM (optional) | OpenAI structured outputs: edit interpretation and identifying unknown furniture blocks |
| Migrations | Alembic (run automatically on container start) |

## Run it

```bash
# from the repository root (Docker or Podman: `podman compose up --build`)
docker compose up --build          # API: http://localhost:8000/docs   Postgres: localhost:5433
```

Put secrets in a top-level `.env` (git-ignored): `JWT_SECRET=$(openssl rand -hex 32)` (otherwise
tokens reset whenever the backend restarts) and optionally `OPENAI_API_KEY=sk-...`.

Local development without the backend container: `docker compose up -d db`, then in `backend/`:
`cp .env.example .env && uv sync && uv run alembic upgrade head && uv run uvicorn app.main:app --reload`.
DWG uploads need `dwg2dxf` on your `PATH`; DXF uploads work anywhere.

### Inspect a drawing (ARCHITECTURE.md test script)

```bash
uv run python scripts/dwg_to_json.py ../examples/call-center-offices.dwg   # writes call-center-offices.json
# without a local dwg2dxf, run it in the image:
docker run --rm -v "$PWD/../examples:/data" walkitecture-backend python scripts/dwg_to_json.py /data/call-center-offices.dwg
```

It prints the classification summary, layers and blocks, and writes every entity's geometry
(lines, polylines, circles, arcs, inserts, text...) with its category and the reason for it.

### Tests

```bash
uv run pytest            # API tests need the compose database (skipped if it is down);
                         # /examples tests need dwg2dxf (skipped locally, run them in the image)
uv run ruff check . && uv run ruff format --check .
```

## Typical flow

```bash
TOKEN=$(curl -s localhost:8000/api/auth/register -H 'content-type: application/json' \
  -d '{"username":"alice","password":"walk-it-2026"}' | jq -r .accessToken)
AUTH="Authorization: Bearer $TOKEN"

curl -H "$AUTH" -F file=@../examples/call-center-offices.dwg localhost:8000/api/plans   # runs the pipeline
curl -H "$AUTH" localhost:8000/api/plans/$PLAN/scene      # {model, babylon}
curl -H "$AUTH" "localhost:8000/api/plans/$PLAN/drawing?entities=false"
curl -H "$AUTH" localhost:8000/api/interpret-edit -H 'content-type: application/json' \
  -d '{"text":"Make the selected window 50% wider","selectedId":"window_1","planId":"'$PLAN'","apply":true}'
curl -H "$AUTH" -X POST localhost:8000/api/plans/$PLAN/undo
```

## Endpoints

All `/api/plans*` and `/api/interpret-edit` routes require `Authorization: Bearer <token>`.

| Method & path | Purpose |
| --- | --- |
| `POST /api/auth/register` · `POST /api/auth/login` | Create account / log in (JSON) → `{accessToken, user}` |
| `POST /api/auth/token` · `GET /api/auth/me` | OAuth2 form login (Swagger "Authorize") · current user |
| `GET /api/health` | DB status, `dwg2dxf` availability, active interpreter (public) |
| `POST /api/plans` | Upload `.dwg`/`.dxf` and run the whole pipeline (`?parse=false` to only store it) |
| `POST /api/plans/demo` | Create the built-in sample apartment |
| `GET /api/plans` · `GET/DELETE /api/plans/{id}` | Your plans |
| `POST /api/plans/{id}/parse` | Re-run the pipeline with options (units, wall style, layer names...); clears history |
| `GET /api/plans/{id}/drawing[?entities=false]` | Descriptive drawing JSON |
| `GET /api/plans/{id}/scene` | `{version, model, babylon}`: canonical model + Babylon scene spec |
| `PUT /api/plans/{id}/scene` | Replace the model with a hand-authored one (validated, undoable) |
| `GET /api/plans/{id}/file?kind=original\|dxf` | Download the upload or the converted DXF |
| `POST /api/plans/{id}/edits[?dryRun=true]` | Apply (or preview) one allowlisted edit |
| `GET /api/plans/{id}/edits` · `POST /api/plans/{id}/undo` | History · revert latest edit |
| `POST /api/interpret-edit` | Text + `selectedId` + `planId` → typed command, validation, optional apply |

Edit responses include `affectedIds`, the new `version`, the new `model`, and the regenerated
`babylon` scene; send `baseVersion` to get `409` instead of overwriting a newer model.

### Edit operations (the allowlist)

| `operation` | `targetId` | `parameters` |
| --- | --- | --- |
| `resize_selected_window` | window ID (a wall with exactly one window also works) | `factor` *or* `width`, optional `height`, `sillHeight` |
| `resize_windows` | — | `factor` |
| `set_ceiling_height` | — | `height` (2.1–6.0 m): the wall height |
| `move_selected_wall` | free-standing wall ID | `offset`: meters along the wall's left-hand normal |

## Babylon.js scene spec (`babylon`)

Babylon world space, meters, **y up; plan x → x, plan y → z**. Heights follow ARCHITECTURE.md:

| Element | Geometry |
| --- | --- |
| Walls | All one height: **10 ft** (3.048 m). Split into `wall` boxes around openings; connected ends extended by half a thickness so corners close |
| Doors | Open from 0 to **7 ft**; a `header` box (same thickness) from 7 ft to the wall top |
| Windows | Open from **2.5 ft to 6.5 ft**; a `sill` box below, a `header` above, and a thin `glass` pane |
| Floor / ceiling | `floor` and `ceiling` slabs over the plan bounds |
| Furniture | GLB placements from `frontend/furniture` |

```js
for (const m of scene.meshes) {
  const box = BABYLON.MeshBuilder.CreateBox(m.id, { width: m.size[0], height: m.size[1], depth: m.size[2] }, s);
  box.position.set(...m.position);
  box.rotation.y = m.rotationY;        // material: scene.materials[m.kind]
}
for (const f of scene.furniture) {     // URL is relative to the frontend's served /furniture/ folder
  const root = new BABYLON.TransformNode(f.id, s);
  const { meshes } = await BABYLON.SceneLoader.ImportMeshAsync("", "", f.url, s);
  meshes[0].parent = root;             // the glTF __root__ node
  root.position.set(...f.position); root.rotation.y = f.rotationY; root.scaling.set(...f.scaling);
}
```

Each mesh carries `wallId`/`openingId`, so after an edit the client can rebuild only the meshes
of walls in `affectedIds`. `camera` gives a first-person spawn point at 1.6 m eye height, `rooms`
the label positions.

## Drawing interpretation

Converting DWG → DXF only changes the file format; the parser still has to decide what is a wall.
Classification (also reported per entity in the drawing JSON) uses, strongest first:

1. **Block names** in English and Spanish: `door/puerta`, `window/ventana`, `bed/cama`,
   `chair/silla`, `desk/escritorio`, `table/mesa`, `sofa`, `closet/armario`, `toilet/WC/inodoro`...
   Cryptic names (`pp`, `PC40`, `pndo`) count as doors only if the block contains a swing arc.
2. **Layer names** (configurable globs, see `.env.example`): walls `WALL*`, `A-WALL*`, `MURO*`;
   windows `WINDOW*`, `VENTANA*`; doors `DOOR*`, `PUERTA*`; mixed openings `CARPINTERIA*` (blocks
   with a swing arc are doors, others windows); rooms `ROOM*`, `SUPERFICIE*`; furniture `FURN*`,
   `MUEBLE*`, `MOB*`, `SANITARIO*`.
3. **Geometry**:
   - `wallStyle: "auto"` (default) detects double-line walls; pairing takes the widest gap first and
     absorbs inner lines, so cavity/plastered walls drawn with 3–4 lines become one wall.
   - When wall layers are sparse, top-level layer `0` linework is used too.
   - Wall pieces split by an opening or a T-junction are re-joined; corners are closed.
   - Declared units are checked against the building size (both `/examples` files declare wrong
     units); override with `units` or a calibration `scale`.
   - Anything outside the building outline (other sheets/details in the file) is ignored.

**Furniture** (ARCHITECTURE.md, algorithmic first): blocks are matched to the GLB library by name
(then by footprint for unnamed blocks on generic furniture layers), sized from the drawing, and
turned so their **front faces away from the wall they stand against**; chairs near a table face
it. Blocks the rules cannot identify are sent to the LLM for a category when `OPENAI_API_KEY` is
set (placement stays rule-based); the rest are listed in `parseStats.unresolvedFurnitureBlocks`.

Results on `/examples`: the call center gives 43 walls, 11 windows, 17 doors, 13 labelled rooms,
and 20 furniture models; the apartment gives 26 walls, 6 windows, 7 doors, 8 named rooms, and 17
furniture models.

## Code map

```
app/
  main.py, config.py, db.py, models.py   app factory, settings, async engine, tables
  api/            auth, plans (upload/drawing/scene), edits, interpret, health; deps + error mapping
  schemas/        building model, Babylon scene spec, edit commands, parse options, API bodies
  services/
    dwg_convert.py        format detection + LibreDWG conversion
    drawing_reader.py     DXF -> descriptive drawing JSON (module 1)
    classify.py, layers.py  entity classification (block names, layers, geometry)
    dxf_parser.py         DXF -> building model (walls, openings, rooms, furniture)
    furniture_*.py        GLB catalog, rule-based placement, LLM fallback; glb_bounds.py measures GLBs
    babylon_scene.py      building model -> Babylon.js scene spec (module 2)
    edits.py, geometry.py allowlisted edits + validation
    interpreter.py        OpenAI + rule-based natural-language interpretation
    plan_store.py         persistence: pipeline, edit history, undo, ownership, versions
    auth.py               password hashing + JWT
scripts/dwg_to_json.py    DWG/DXF -> descriptive JSON (ARCHITECTURE.md test script)
alembic/                  migrations
```
