import { type FormEvent, useEffect, useRef, useState } from "react";

export interface ChatMessage {
  id: number;
  role: "user" | "assistant" | "error";
  text: string;
}

interface Props {
  messages: ChatMessage[];
  busy: boolean;
  selectionLabel: string | null;
  aiMode: "openai" | "rules" | null;
  onSend(text: string): void;
}

const EXAMPLES = [
  "Make this window 50% wider",
  "Make all windows 20% wider",
  "Raise the ceiling to 3.5 meters",
  "Delete this",
];

/** Natural-language edits (POST /api/interpret-edit) plus the activity log of every change. */
export default function ChatBox({ messages, busy, selectionLabel, aiMode, onSend }: Props) {
  const [text, setText] = useState("");
  const listRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    listRef.current?.scrollTo({ top: listRef.current.scrollHeight, behavior: "smooth" });
  }, [messages.length]);

  const submit = (event: FormEvent) => {
    event.preventDefault();
    const value = text.trim();
    if (!value || busy) return;
    onSend(value);
    setText("");
  };

  return (
    <section className="panel chat">
      <header className="panel-head">
        <h3>Ask for a change</h3>
        {aiMode && <span className={`badge ${aiMode}`}>{aiMode === "openai" ? "AI" : "rules"}</span>}
      </header>
      <div className="chat-log" ref={listRef} aria-live="polite">
        {messages.length === 0 && (
          <p className="muted small">
            Try: {EXAMPLES.map((e, i) => (
              <button key={e} className="link" onClick={() => setText(e)}>
                “{e}”{i < EXAMPLES.length - 1 ? "," : ""}
              </button>
            ))}
          </p>
        )}
        {messages.map((m) => (
          <p key={m.id} className={`msg ${m.role}`}>
            {m.text}
          </p>
        ))}
        {busy && <p className="msg assistant working">Working… AI requests can take up to a minute.</p>}
      </div>
      <form onSubmit={submit} className="chat-form">
        <input
          value={text}
          onChange={(e) => setText(e.target.value)}
          placeholder={selectionLabel ? `About the selected ${selectionLabel.toLowerCase()}…` : "Describe a change…"}
          aria-label="Describe a change"
          maxLength={500}
        />
        <button className="primary" disabled={busy || !text.trim()}>
          Send
        </button>
      </form>
    </section>
  );
}
