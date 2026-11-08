# Changelog

What changed recently, in plain language. Newest first. Every commit adds an entry here (see `CLAUDE.md`).

## 2026-11-08 — Pitch deck notes kept off GitHub

- `PRESENTATION.md` (the pitch deck content) is no longer in the repository. It stays only on its author's
  computer and is git-ignored, so it won't be committed again by accident.

## 2026-11-08 — Pitch deck content and team notes

- **Pitch deck content** (`PRESENTATION.md`): the text, footer and picture for each of the 12 slides, in the
  same order as the Capital Hacks template. Each slide says which page of our `Walkitecture.pdf` draft to reuse
  or rebuild. It also has a cleanup list for the draft, a scoring map, a time budget per slide, a backup slide
  for questions and the submission rules (Google Slides, editor access for the judges, due 9 am). It leads with
  our main advantage: affordable ($0.25–1.00 per house), fast (minutes) and no extra input needed.
- **This changelog** and **`CLAUDE.md`**, the house rules for AI coding assistants: log every commit here,
  combine conflicting pushes instead of overwriting them, and run the tests before calling a feature done.
- Merged Chris's "Accuracy Fixes" and "Last minute fixes" from GitHub (summarised below). Everything passes
  after the merge: 353 backend tests, the drawing tests on the real example DWGs and the browser smoke test.

## 2026-11-08 — Accuracy fixes (Chris)

- **Doors stay usable.** Furniture is pushed out of the floor in front of every door, on both sides, or
  removed when there's no room for it. Every room keeps a 0.9 m walking path between its doors, also after
  manual and chat edits.
- **Door leaves never block anything.** An open door never stands across a hallway or in front of another
  doorway. Doors at a corner pick swings that never hit each other. If nothing works, the narrower door is
  left as an open doorway.
- **Hallways that are too narrow get widened.** If the only way between two doors is narrower than 0.9 m,
  one wall moves out by the shortfall, up to 60 cm, and the walls that meet it stretch to follow.
- **Furniture belongs in its labelled room.** On upload, things that clearly don't fit the room's label are
  removed, such as a bed in a room labelled kitchen or a toilet in a bedroom. Labels in English and Spanish
  are understood. Open-plan, unlabelled and storage rooms are left alone.
- **Never stuck in the 3D walk.** If a door swings shut onto you, or an edit or undo puts a wall or furniture
  where you stand, it stops blocking you until you step out of it. The floor and the map's edge always stay
  solid.
- **Better start point.** The walk starts in the roomiest room you can actually leave through a door, never
  inside a wall or furniture.
- **Fewer sealed doorways.** Overlapping pieces of one wall are now merged into a single wall. Before, a door
  could be cut into only one of the pieces while the other still filled the doorway (this sealed the call
  center's starting room).
