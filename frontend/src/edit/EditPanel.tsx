import { useEffect, useState } from "react";
import type { Building, EditCommand } from "../api/schema";
import { metersAndFeet, type OpeningSelection, type Selection, titleCase, wallLength } from "./selection";

interface Props {
  model: Building;
  selection: Selection | null;
  busy: boolean;
  onEdit(command: EditCommand): void;
  onUndo(): void;
  onClearSelection(): void;
}

const CEILING_MIN = 2.1;
const CEILING_MAX = 6.0;
const WALL_STEP = 0.25; // m per "move wall" click

/** Sliders and buttons that call the same allowlisted edit operations the AI uses. */
export default function EditPanel({ model, selection, busy, onEdit, onUndo, onClearSelection }: Props) {
  return (
    <section className="panel">
      <header className="panel-head">
        <h3>Edit</h3>
        <button onClick={onUndo} disabled={busy} title="Revert the latest change">
          Undo
        </button>
      </header>
      <SelectionEditor selection={selection} busy={busy} onEdit={onEdit} onClear={onClearSelection} />
      <CeilingEditor model={model} busy={busy} onEdit={onEdit} />
      <div className="field">
        <span className="label">All windows</span>
        <div className="row">
          <button disabled={busy} onClick={() => onEdit({ operation: "resize_windows", targetId: null, parameters: { factor: 0.8 } })}>
            20% narrower
          </button>
          <button disabled={busy} onClick={() => onEdit({ operation: "resize_windows", targetId: null, parameters: { factor: 1.2 } })}>
            20% wider
          </button>
        </div>
      </div>
    </section>
  );
}

function SelectionEditor({
  selection,
  busy,
  onEdit,
  onClear,
}: {
  selection: Selection | null;
  busy: boolean;
  onEdit(command: EditCommand): void;
  onClear(): void;
}) {
  if (!selection) {
    return <p className="muted small">Click a window, door, wall, or piece of furniture to select it.</p>;
  }
  return (
    <div className="selection">
      <div className="row">
        <strong>{selectionTitle(selection)}</strong>
        <span className="spacer" />
        <button className="link" onClick={onClear}>
          Clear
        </button>
      </div>
      {selection.kind === "window" && <WindowEditor selection={selection} busy={busy} onEdit={onEdit} />}
      {selection.kind === "door" && (
        <p className="muted small">
          {metersAndFeet(selection.opening.width)} wide · {metersAndFeet(selection.opening.height)} high. Door sizes are
          fixed in this version.
        </p>
      )}
      {selection.kind === "wall" && (
        <>
          <p className="muted small">
            {metersAndFeet(wallLength(selection.wall))} long · {(selection.wall.thickness * 100).toFixed(0)} cm thick ·{" "}
            {selection.wall.openings?.length ?? 0} openings
          </p>
          <div className="row">
            <button
              disabled={busy}
              onClick={() => onEdit({ operation: "move_selected_wall", targetId: selection.id, parameters: { offset: -WALL_STEP } })}
            >
              Shift −25 cm
            </button>
            <button
              disabled={busy}
              onClick={() => onEdit({ operation: "move_selected_wall", targetId: selection.id, parameters: { offset: WALL_STEP } })}
            >
              Shift +25 cm
            </button>
          </div>
          <p className="hint">Only free-standing walls can move; connected walls are rejected to keep rooms intact.</p>
        </>
      )}
      {selection.kind === "furniture" && (
        <p className="muted small">
          {titleCase(selection.furniture.category)} · {selection.furniture.width.toFixed(2)} × {selection.furniture.depth.toFixed(2)} m
          {selection.furniture.sourceBlock ? ` · from block “${selection.furniture.sourceBlock}”` : ""}
        </p>
      )}
    </div>
  );
}

function WindowEditor({
  selection,
  busy,
  onEdit,
}: {
  selection: OpeningSelection;
  busy: boolean;
  onEdit(command: EditCommand): void;
}) {
  const current = selection.opening.width;
  const max = Math.max(current, Math.min(wallLength(selection.wall) - 0.2, 8));
  const [width, setWidth] = useState(current);
  useEffect(() => setWidth(current), [current, selection.id]);

  const commit = () => {
    if (Math.abs(width - current) > 0.005) {
      onEdit({ operation: "resize_selected_window", targetId: selection.id, parameters: { width: round(width) } });
    }
  };
  const factor = (f: number) =>
    onEdit({ operation: "resize_selected_window", targetId: selection.id, parameters: { factor: f } });

  return (
    <>
      <label className="field">
        <span className="label">
          Width <span className="muted">{metersAndFeet(width)}</span>
        </span>
        <input
          type="range"
          min={0.3}
          max={round(max)}
          step={0.05}
          value={width}
          disabled={busy}
          onChange={(e) => setWidth(Number(e.target.value))}
          onPointerUp={commit}
          onKeyUp={commit}
        />
      </label>
      <div className="row">
        <button disabled={busy} onClick={() => factor(0.75)}>
          −25%
        </button>
        <button disabled={busy} onClick={() => factor(1.25)}>
          +25%
        </button>
        <button disabled={busy} onClick={() => factor(1.5)}>
          +50%
        </button>
      </div>
      <p className="muted small">
        Sill {metersAndFeet(selection.opening.sillHeight ?? 0)} · height {metersAndFeet(selection.opening.height)}
      </p>
    </>
  );
}

function CeilingEditor({ model, busy, onEdit }: { model: Building; busy: boolean; onEdit(command: EditCommand): void }) {
  const current = model.ceilingHeight;
  const [height, setHeight] = useState(current);
  useEffect(() => setHeight(current), [current]);
  const commit = () => {
    if (Math.abs(height - current) > 0.005) {
      onEdit({ operation: "set_ceiling_height", targetId: null, parameters: { height: round(height) } });
    }
  };
  return (
    <label className="field">
      <span className="label">
        Ceiling height <span className="muted">{metersAndFeet(height)}</span>
      </span>
      <input
        type="range"
        min={CEILING_MIN}
        max={CEILING_MAX}
        step={0.05}
        value={height}
        disabled={busy}
        onChange={(e) => setHeight(Number(e.target.value))}
        onPointerUp={commit}
        onKeyUp={commit}
      />
    </label>
  );
}

function selectionTitle(selection: Selection): string {
  switch (selection.kind) {
    case "window":
      return `Window · ${selection.opening.width.toFixed(2)} m`;
    case "door":
      return "Door";
    case "wall":
      return "Wall";
    case "furniture":
      return titleCase(selection.furniture.category);
  }
}

function round(value: number): number {
  return Math.round(value * 100) / 100;
}
