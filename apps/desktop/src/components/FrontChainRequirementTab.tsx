import { useCallback, useEffect, useState } from "react";
import { apiRequest } from "../lib/api";

type Status = "open" | "compilable" | "closed";
type Stage = "interpret" | "answer" | "evaluate" | "compile";

type ClarificationOption = {
  option_id: string;
  label: string;
  value: string;
  resolves: Record<string, string>;
  is_default: boolean;
  requires_free_text_suffix: boolean;
};

type ClarificationQuestion = {
  question_id: string;
  axis: string;
  layer: string;
  severity: string;
  question_text: string;
  priority: number;
  context_hint: string | null;
  suggested_options: ClarificationOption[];
};

type StateRow = {
  id: string;
  turn_number: number;
  parent_state_id: string | null;
  raw_intent: string;
  problem_formulation: any;
  input_closure: any;
  output_closure: any;
  merged_gaps: { blocking: any[]; review: any[] };
  top_questions: ClarificationQuestion[];
  solvability?: any | null;
  execution_admissibility?: any | null;
  compile_context?: any | null;
  applied_resolutions?: Array<{ axis: string; resolved_value: string }>;
};

type CompileContext = {
  compile_context_version: string;
  problem_formulation: any;
  input_closure: any;
  output_closure: any;
  solvability: any;
  execution_admissibility: any | null;
  capability_envelope: { capability_signatures: any[] };
  compile_constraints: {
    allowed_node_semantic_types: string[];
    forbidden_side_effect_classes: string[];
    require_human_gate_before: string[];
    compile_notes: string[];
  };
  candidate_work_units: Array<{
    work_unit_id: string;
    label: string | null;
    objective: string | null;
    concern_kind: string | null;
    parallelism_hint: string | null;
    human_gate_hint: string | null;
  }>;
  compile_trace: { authority_source?: string; assembler_version?: string; state_id?: string };
};

type SessionResponse = {
  id: string;
  project_id: string;
  status: Status;
  created_at: string;
  updated_at: string;
  turns: StateRow[];
  latest_compile_context: CompileContext | null;
};

type SessionSummary = {
  id: string;
  project_id: string;
  status: Status;
  created_at: string;
  updated_at: string;
  turn_count: number;
  latest_raw_intent: string | null;
};

type TurnResponse = {
  session_id: string;
  status: Status;
  stages_run: Stage[];
  latest_state: StateRow;
  compile_context: CompileContext | null;
};

const API = "/api/v1/front-chain";

export function FrontChainRequirementTab({ projectId }: { projectId: string }) {
  const [sessions, setSessions] = useState<SessionSummary[]>([]);
  const [activeSession, setActiveSession] = useState<SessionResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [rawIntent, setRawIntent] = useState("");
  const [answerDrafts, setAnswerDrafts] = useState<Record<string, string>>({});

  const loadSessions = useCallback(async () => {
    try {
      const items = await apiRequest<SessionSummary[]>(`${API}/projects/${projectId}/sessions`);
      setSessions(items);
    } catch (err) {
      setError(String(err));
    }
  }, [projectId]);

  const loadSession = useCallback(async (sessionId: string) => {
    setLoading(true);
    setError(null);
    try {
      const s = await apiRequest<SessionResponse>(`${API}/sessions/${sessionId}`);
      setActiveSession(s);
      setAnswerDrafts({});
    } catch (err) {
      setError(String(err));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void loadSessions();
  }, [loadSessions]);

  const createSession = async () => {
    setLoading(true);
    setError(null);
    try {
      const s = await apiRequest<SessionResponse>(`${API}/sessions`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ project_id: projectId }),
      });
      setActiveSession(s);
      setAnswerDrafts({});
      setRawIntent("");
      await loadSessions();
    } catch (err) {
      setError(String(err));
    } finally {
      setLoading(false);
    }
  };

  const submitRawIntent = async () => {
    if (!activeSession || !rawIntent.trim()) return;
    setLoading(true);
    setError(null);
    try {
      await apiRequest<TurnResponse>(`${API}/sessions/${activeSession.id}/turn`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ kind: "raw_intent", raw_intent: rawIntent.trim() }),
      });
      setRawIntent("");
      await loadSession(activeSession.id);
      await loadSessions();
    } catch (err) {
      setError(String(err));
    } finally {
      setLoading(false);
    }
  };

  const submitAnswers = async () => {
    if (!activeSession) return;
    const latestTurn = activeSession.turns[activeSession.turns.length - 1];
    if (!latestTurn || latestTurn.top_questions.length === 0) return;
    const answers = latestTurn.top_questions
      .map((q) => {
        const draft = (answerDrafts[q.question_id] ?? "").trim();
        if (!draft) return null;
        return { question_id: q.question_id, answer_text: draft };
      })
      .filter((x): x is { question_id: string; answer_text: string } => x !== null);
    if (answers.length === 0) return;
    setLoading(true);
    setError(null);
    try {
      await apiRequest<TurnResponse>(`${API}/sessions/${activeSession.id}/turn`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ kind: "answers", answers }),
      });
      setAnswerDrafts({});
      await loadSession(activeSession.id);
      await loadSessions();
    } catch (err) {
      setError(String(err));
    } finally {
      setLoading(false);
    }
  };

  const closeSession = async () => {
    if (!activeSession) return;
    setLoading(true);
    try {
      await apiRequest<SessionResponse>(`${API}/sessions/${activeSession.id}/close`, {
        method: "POST",
      });
      await loadSession(activeSession.id);
      await loadSessions();
    } catch (err) {
      setError(String(err));
    } finally {
      setLoading(false);
    }
  };

  const latestTurn = activeSession?.turns[activeSession.turns.length - 1] ?? null;
  const hasTurns = (activeSession?.turns.length ?? 0) > 0;

  return (
    <div className="mt-5 grid gap-4 lg:grid-cols-[240px,1fr,320px]">
      {/* Session list */}
      <div className="space-y-3">
        <button
          className="w-full rounded-2xl bg-ink px-4 py-3 text-sm font-semibold text-white disabled:opacity-40"
          onClick={() => void createSession()}
          disabled={loading}
          type="button"
        >
          New Session
        </button>
        {sessions.length === 0 ? (
          <p className="rounded-2xl bg-slate-50 p-4 text-sm text-slate-500">
            No sessions yet.
          </p>
        ) : (
          sessions.map((s) => (
            <button
              key={s.id}
              className={`w-full rounded-3xl border p-3 text-left text-sm ${
                activeSession?.id === s.id
                  ? "border-accent bg-accent/5"
                  : "border-slate-200 bg-slate-50"
              }`}
              onClick={() => void loadSession(s.id)}
              type="button"
            >
              <p className="font-medium text-ink line-clamp-2">
                {s.latest_raw_intent ?? "(empty session)"}
              </p>
              <p className="mt-1 text-[11px] uppercase tracking-[0.16em] text-slate-500">
                {s.status} · {s.turn_count} turn{s.turn_count === 1 ? "" : "s"}
              </p>
            </button>
          ))
        )}
      </div>

      {/* Conversation */}
      <div className="rounded-3xl border border-slate-200 bg-white p-4">
        {!activeSession ? (
          <p className="text-sm text-slate-500">
            Pick or create a session to start.
          </p>
        ) : (
          <>
            <div className="flex items-center justify-between border-b border-slate-100 pb-2">
              <div>
                <p className="text-xs font-semibold uppercase tracking-[0.18em] text-slate-500">
                  Session · {activeSession.status}
                </p>
                <p className="text-xs text-slate-400">{activeSession.id.slice(0, 8)}</p>
              </div>
              {activeSession.status !== "closed" ? (
                <button
                  className="rounded-2xl border border-slate-300 px-3 py-1 text-xs text-slate-600"
                  onClick={() => void closeSession()}
                  type="button"
                >
                  Close
                </button>
              ) : null}
            </div>

            {error ? (
              <p className="mt-3 rounded-2xl bg-red-50 px-3 py-2 text-xs text-red-700">
                {error}
              </p>
            ) : null}

            {/* Conversation turns */}
            <div className="mt-3 space-y-3">
              {activeSession.turns.map((turn) => (
                <div key={turn.id} className="rounded-2xl bg-slate-50 p-3 text-sm">
                  <p className="text-[11px] uppercase tracking-[0.16em] text-slate-500">
                    Turn {turn.turn_number}
                  </p>
                  <p className="mt-1 text-slate-800">
                    {turn.raw_intent}
                    {turn.applied_resolutions && turn.applied_resolutions.length > 0 ? (
                      <span className="ml-2 text-xs text-slate-500">
                        (answered: {turn.applied_resolutions.map((r) => `${r.axis}=${r.resolved_value}`).join(", ")})
                      </span>
                    ) : null}
                  </p>
                  {turn.top_questions.length > 0 ? (
                    <p className="mt-2 text-xs text-amber-700">
                      {turn.top_questions.length} question{turn.top_questions.length === 1 ? "" : "s"} pending
                    </p>
                  ) : null}
                </div>
              ))}
            </div>

            {/* Input area */}
            {!hasTurns ? (
              <div className="mt-4 space-y-2 border-t border-slate-100 pt-3">
                <label className="block text-xs font-semibold uppercase tracking-[0.16em] text-slate-600">
                  Describe your requirement
                </label>
                <textarea
                  className="min-h-24 w-full rounded-2xl border border-slate-200 bg-white px-3 py-2 text-sm"
                  value={rawIntent}
                  onChange={(e) => setRawIntent(e.target.value)}
                  placeholder="e.g. Analyze the repository at https://github.com/example/foo and produce a markdown report"
                />
                <button
                  className="rounded-2xl bg-accent px-4 py-2 text-sm font-semibold text-white disabled:opacity-40"
                  disabled={loading || !rawIntent.trim()}
                  onClick={() => void submitRawIntent()}
                  type="button"
                >
                  Send
                </button>
              </div>
            ) : latestTurn && latestTurn.top_questions.length > 0 ? (
              <div className="mt-4 space-y-3 border-t border-slate-100 pt-3">
                <p className="text-xs font-semibold uppercase tracking-[0.16em] text-slate-600">
                  Please answer
                </p>
                {latestTurn.top_questions.map((q) => (
                  <div key={q.question_id} className="space-y-1">
                    <p className="text-sm font-medium text-slate-700">{q.question_text}</p>
                    {q.context_hint ? (
                      <p className="text-xs text-slate-500">{q.context_hint}</p>
                    ) : null}
                    <input
                      className="w-full rounded-2xl border border-slate-200 bg-white px-3 py-2 text-sm"
                      value={answerDrafts[q.question_id] ?? ""}
                      onChange={(e) =>
                        setAnswerDrafts((prev) => ({ ...prev, [q.question_id]: e.target.value }))
                      }
                      placeholder="Your answer"
                    />
                  </div>
                ))}
                <button
                  className="rounded-2xl bg-accent px-4 py-2 text-sm font-semibold text-white disabled:opacity-40"
                  disabled={loading}
                  onClick={() => void submitAnswers()}
                  type="button"
                >
                  Submit answers
                </button>
              </div>
            ) : activeSession.status === "compilable" ? (
              <p className="mt-4 border-t border-slate-100 pt-3 text-xs text-emerald-700">
                Requirement is compilable. See the card on the right.
              </p>
            ) : null}
          </>
        )}
      </div>

      {/* Compilable card */}
      <div className="rounded-3xl border border-slate-200 bg-slate-50 p-4">
        <p className="text-xs font-semibold uppercase tracking-[0.18em] text-slate-600">
          Compilable Card
        </p>
        {activeSession?.latest_compile_context ? (
          <CompileCard ctx={activeSession.latest_compile_context} />
        ) : (
          <p className="mt-3 text-xs text-slate-500">
            Becomes available once closures close and solvability is derived.
          </p>
        )}
      </div>
    </div>
  );
}

function CompileCard({ ctx }: { ctx: CompileContext }) {
  const pf = ctx.problem_formulation ?? {};
  const ic = ctx.input_closure ?? {};
  const oc = ctx.output_closure ?? {};
  const solv = ctx.solvability ?? {};
  const adm = ctx.execution_admissibility ?? {};
  const sigs = ctx.capability_envelope?.capability_signatures ?? [];
  const units = ctx.candidate_work_units ?? [];
  const constraints = ctx.compile_constraints ?? {
    allowed_node_semantic_types: [],
    forbidden_side_effect_classes: [],
    require_human_gate_before: [],
    compile_notes: [],
  };

  return (
    <div className="mt-3 space-y-3 text-xs">
      <Section title="Problem">
        <KV k="kind" v={pf.interpreted_problem_kind} />
        <KV k="target" v={pf.target_object} />
        <KV k="status" v={pf.problem_status} />
      </Section>
      <Section title="Input">
        <KV k="source" v={ic.resolved_source_locator} />
        <KV k="path" v={ic.acquisition_path_kind} />
        <KV k="closure" v={ic.closure_status} />
      </Section>
      <Section title="Output">
        <KV k="form" v={oc.deliverable_form} />
        <KV k="target" v={oc.delivery_target} />
        <KV k="modality" v={oc.output_modality} />
        <KV k="closure" v={oc.closure_status} />
      </Section>
      <Section title="Solvability">
        <KV k="status" v={solv.solvability_status} />
        <KV k="subgoals" v={String(solv.required_subgoals?.length ?? 0)} />
        <KV k="graph" v={solv.graph_feasibility?.candidate_shape} />
      </Section>
      <Section title="Admissibility">
        <KV k="status" v={adm.admissibility_status} />
        <KV k="regime" v={adm.primary_execution_regime} />
        <KV k="automation" v={adm.max_automation_level} />
        <KV k="review" v={adm.requires_human_review ? "required" : "no"} />
      </Section>
      <Section title="Capabilities">
        <p className="text-slate-700">{sigs.length} signature{sigs.length === 1 ? "" : "s"}</p>
      </Section>
      <Section title="Work units (hints)">
        {units.length === 0 ? (
          <p className="text-slate-500">none</p>
        ) : (
          units.map((u) => (
            <p key={u.work_unit_id} className="text-slate-700">
              {u.work_unit_id}: {u.label ?? "—"}{" "}
              {u.concern_kind ? <span className="text-slate-500">({u.concern_kind})</span> : null}
            </p>
          ))
        )}
      </Section>
      {constraints.compile_notes.length > 0 ? (
        <Section title="Constraints">
          {constraints.compile_notes.map((n, i) => (
            <p key={i} className="text-slate-700">• {n}</p>
          ))}
        </Section>
      ) : null}
      <p className="mt-2 text-[10px] uppercase tracking-[0.16em] text-slate-400">
        source: {ctx.compile_trace?.authority_source ?? "unknown"}
      </p>
    </div>
  );
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div className="rounded-2xl bg-white p-3">
      <p className="text-[10px] font-semibold uppercase tracking-[0.16em] text-slate-500">
        {title}
      </p>
      <div className="mt-1 space-y-1">{children}</div>
    </div>
  );
}

function KV({ k, v }: { k: string; v: string | null | undefined }) {
  return (
    <div className="flex justify-between gap-3">
      <span className="text-slate-500">{k}</span>
      <span className="text-right text-slate-800 truncate">{v ?? "—"}</span>
    </div>
  );
}
