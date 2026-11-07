interface Props {
  busy: boolean;
  aiAvailable: boolean;
  warnings: string[];
  onClassify(): void;
  onReview(): void;
}

/** LLM tools over the whole plan (ARCHITECTURE.md §4.2, §4.3) and the parser's warnings. */
export default function AiPanel({ busy, aiAvailable, warnings, onClassify, onReview }: Props) {
  return (
    <section className="panel">
      <header className="panel-head">
        <h3>AI clean-up</h3>
      </header>
      <div className="row">
        <button disabled={busy || !aiAvailable} onClick={onReview} title="Find and fix stray walls, wrong furniture, unnamed rooms">
          Review plan
        </button>
        <button
          disabled={busy || !aiAvailable}
          onClick={onClassify}
          title="Re-read the drawing with AI-assigned layer and block roles (resets edits)"
        >
          Re-classify layers
        </button>
      </div>
      {!aiAvailable && <p className="hint">AI tools need an OpenAI key on the server; sliders and simple chat commands still work.</p>}
      {warnings.length > 0 && (
        <details>
          <summary className="small">{warnings.length} drawing warning{warnings.length === 1 ? "" : "s"}</summary>
          <ul className="warnings">
            {warnings.map((w, i) => (
              <li key={i} className="small">
                {w}
              </li>
            ))}
          </ul>
        </details>
      )}
    </section>
  );
}
