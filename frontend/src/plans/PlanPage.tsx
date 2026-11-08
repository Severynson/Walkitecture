import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { ApiError, errorMessage } from "../api/client";
import * as endpoints from "../api/endpoints";
import type { CheckReport, EditCommand, EditResponse, PlanDetail, SceneResponse } from "../api/schema";
import AiPanel from "../edit/AiPanel";
import ChatBox, { type ChatMessage } from "../edit/ChatBox";
import EditPanel from "../edit/EditPanel";
import { describe, titleCase } from "../edit/selection";
import type { Viewer } from "../viewer/engine";
import ViewerPage from "../viewer/ViewerPage";
import Processing from "./Processing";

const POLL_MS = 2000;
/**
 * The AI clean-up buttons (review, re-classify) are no longer shown: every upload is classified,
 * reviewed and checked automatically before its 3D model is built. Kept for a future "advanced" mode.
 */
const SHOW_AI_TOOLS = false;

/** What the automatic checks did, for the activity log. */
function checkSummary(report: CheckReport | null | undefined): string | null {
  if (!report) return null;
  const changes = report.changes ?? [];
  const kept = (report.findings ?? []).filter((f) => f.decision === "keep" || f.decision === "accept");
  if (!changes.length && !(report.findings ?? []).length) return null;
  const who = report.ai === "used" ? "AI-reviewed checks" : "Automatic checks";
  const lines = [
    `${who} before building the 3D model: ${changes.length} fix${changes.length === 1 ? "" : "es"}${kept.length ? `, ${kept.length} item${kept.length === 1 ? "" : "s"} kept as drawn` : ""}.`,
    ...changes.slice(0, 10).map((c) => `• ${c.summary}`),
  ];
  if (changes.length > 10) lines.push(`• …and ${changes.length - 10} more.`);
  return lines.join("\n");
}

/** `/plans/:id`: walk a plan and change it (sliders, chat, AI clean-up), with undo. */
export default function PlanPage() {
  const { id = "" } = useParams();
  const [plan, setPlan] = useState<PlanDetail | null>(null);
  const [scene, setScene] = useState<SceneResponse | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [aiMode, setAiMode] = useState<"openai" | "rules" | null>(null);
  const [panelsOpen, setPanelsOpen] = useState(true);
  const viewerRef = useRef<Viewer | null>(null);
  const nextMessageId = useRef(1);

  const say = useCallback((role: ChatMessage["role"], text: string) => {
    setMessages((list) => [...list.slice(-49), { id: nextMessageId.current++, role, text }]);
  }, []);

  const [retrying, setRetrying] = useState(false);

  useEffect(() => {
    setPlan(null);
    setScene(null);
    setLoadError(null);
    setSelectedId(null);
    endpoints.getPlan(id).then(setPlan, (e) => setLoadError(errorMessage(e)));
    endpoints.health().then((h) => setAiMode(h.interpreter), () => setAiMode(null));
  }, [id]);

  // While the drawing is processed in the background, poll until it is parsed (or failed).
  useEffect(() => {
    if (plan?.status !== "processing") return;
    const timer = window.setTimeout(() => {
      endpoints.getPlan(id).then(setPlan, (e) => setLoadError(errorMessage(e)));
    }, POLL_MS);
    return () => window.clearTimeout(timer);
  }, [id, plan]);

  const ready = plan?.status === "parsed" && plan.hasModel;
  useEffect(() => {
    if (!ready) return;
    endpoints.getScene(id).then(setScene, (e) => setLoadError(errorMessage(e)));
    const summary = checkSummary(plan?.report);
    if (summary) say("assistant", summary);
    // eslint-disable-next-line react-hooks/exhaustive-deps -- once per plan, when it becomes ready
  }, [id, ready]);

  const retry = async () => {
    setRetrying(true);
    try {
      setPlan(await endpoints.processPlan(id));
    } catch (e) {
      setLoadError(errorMessage(e));
    } finally {
      setRetrying(false);
    }
  };

  const selection = useMemo(() => (scene ? describe(scene.model, selectedId) : null), [scene, selectedId]);

  // Keep the highlight in sync when the selected object disappears (e.g. after undo or AI fixes).
  useEffect(() => {
    if (selectedId && scene && !selection) {
      setSelectedId(null);
      viewerRef.current?.select(null);
    }
  }, [selectedId, scene, selection]);

  const select = useCallback((objectId: string | null) => {
    setSelectedId(objectId);
    viewerRef.current?.select(objectId);
  }, []);

  /** An edit/undo result: update only the affected meshes, keep the camera. */
  const applyResult = useCallback(
    (result: EditResponse) => {
      viewerRef.current?.applyUpdate(result.babylon, result.affectedIds);
      setScene({ planId: result.planId, version: result.version, model: result.model, babylon: result.babylon });
    },
    [],
  );

  /** Reload the stored scene (after AI fixes or a version conflict), keeping the camera. */
  const reloadScene = useCallback(async () => {
    const fresh = await endpoints.getScene(id);
    viewerRef.current?.load(fresh.babylon, { keepCamera: true });
    setScene(fresh);
    return fresh;
  }, [id]);

  const handleFailure = useCallback(
    async (error: unknown) => {
      if (error instanceof ApiError && error.code === "version_conflict") {
        await reloadScene().catch(() => {});
        say("error", "The plan was changed elsewhere, so it was reloaded. Please try again.");
      } else if (error instanceof ApiError && error.code === "nothing_to_undo") {
        say("assistant", "Nothing to undo.");
      } else {
        say("error", errorMessage(error));
      }
    },
    [reloadScene, say],
  );

  const run = useCallback(
    async (task: () => Promise<void>) => {
      setBusy(true);
      try {
        await task();
      } catch (e) {
        await handleFailure(e);
      } finally {
        setBusy(false);
      }
    },
    [handleFailure],
  );

  const edit = (command: EditCommand) =>
    run(async () => {
      const result = await endpoints.applyEdit(id, command, scene!.version);
      applyResult(result);
      say("assistant", result.summary);
    });

  const undo = () =>
    run(async () => {
      const result = await endpoints.undo(id);
      applyResult(result);
      say("assistant", result.summary);
    });

  const chat = (text: string) =>
    run(async () => {
      say("user", text);
      const viewer = viewerRef.current;
      const result = await endpoints.interpret({
        text,
        planId: id,
        selectedId,
        cameraForward: viewer ? viewer.cameraForward() : null,
        baseVersion: scene?.version,
      });
      if (result.applied) {
        applyResult(result.applied);
        say("assistant", result.applied.summary || result.explanation);
      } else if (result.clarification) {
        say("assistant", result.clarification);
      } else if (result.problems?.length) {
        say("error", result.problems.map((p) => p.message).join(" "));
      } else {
        say("assistant", result.explanation || "I couldn't turn that into a supported change.");
      }
    });

  const reviewPlan = () =>
    run(async () => {
      say("assistant", "Reviewing the plan with AI… (can take 1–2 minutes)");
      const result = await endpoints.review(id);
      const applied = result.patches.filter((p) => p.status === "applied");
      const rejected = result.patches.filter((p) => p.status === "rejected");
      if (applied.length) await reloadScene();
      const lines = [
        `Review: ${result.suspects.length} suspicious item${result.suspects.length === 1 ? "" : "s"}, ${applied.length} fix${applied.length === 1 ? "" : "es"} applied${rejected.length ? `, ${rejected.length} rejected` : ""}.`,
        ...applied.map((p) => `• ${p.summary ?? p.reason}`),
        result.notes,
      ].filter(Boolean);
      say("assistant", lines.join("\n"));
      if (applied.length) say("assistant", "Each fix can be reverted with Undo.");
    });

  const classifyPlan = () => {
    if (!window.confirm("Re-read the drawing with AI-assigned layer roles? This rebuilds the plan and clears its edit history.")) return;
    void run(async () => {
      say("assistant", "Asking AI to classify the drawing's layers and blocks… (can take 1–2 minutes)");
      const result = await endpoints.classify(id);
      if (result.applied && result.result) {
        viewerRef.current?.load(result.result.scene.babylon, { keepCamera: true });
        setScene(result.result.scene);
        setPlan(result.result.plan);
        select(null);
      }
      const changed = result.proposal.layers.length + result.proposal.blocks.length;
      say(
        "assistant",
        [`Classification: ${changed} layer/block role${changed === 1 ? "" : "s"} proposed${result.applied ? " and applied" : ""}.`, result.proposal.notes]
          .filter(Boolean)
          .join("\n"),
      );
    });
  };

  if (loadError) {
    return (
      <main className="page">
        <p className="error">{loadError}</p>
        <Link to="/plans">Back to your plans</Link>
      </main>
    );
  }
  if (plan && (plan.status === "processing" || (plan.status === "failed" && !scene))) {
    return <Processing plan={plan} onRetry={retry} retrying={retrying} />;
  }

  return (
    <ViewerPage
      scene={scene}
      loadKey={id}
      title={plan?.filename ?? "Loading…"}
      onReady={(viewer) => (viewerRef.current = viewer)}
      onSelect={(meta) => select(meta?.objectId ?? null)}
      hudExtra={
        <div className="hud-links">
          <Link to="/plans">← Your plans</Link>
          <button className="link" onClick={() => setPanelsOpen((open) => !open)}>
            {panelsOpen ? "Hide panels" : "Show panels"}
          </button>
        </div>
      }
    >
      {scene && panelsOpen && (
        <>
          <EditPanel
            model={scene.model}
            selection={selection}
            busy={busy}
            onEdit={edit}
            onUndo={undo}
            onClearSelection={() => select(null)}
          />
          <ChatBox
            messages={messages}
            busy={busy}
            selectionLabel={selection ? titleCase(selection.kind) : null}
            aiMode={aiMode}
            onSend={chat}
          />
          <div hidden={!SHOW_AI_TOOLS}>
            <AiPanel
              busy={busy}
              aiAvailable={aiMode === "openai"}
              warnings={plan?.parseWarnings ?? []}
              onClassify={classifyPlan}
              onReview={reviewPlan}
            />
          </div>
        </>
      )}
    </ViewerPage>
  );
}
