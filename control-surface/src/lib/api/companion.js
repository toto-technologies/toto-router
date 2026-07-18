// Typed client for the companion plane — the /chat page's network surface. Shapes mirror the
// FastAPI routes (toto_gateway/routes/companion.py + routes/sessions.py). Same-origin cookie
// auth rides client.js; live events use native EventSource (cookies + Last-Event-ID resume
// come free — no hand-rolled SSE parser, no new dep).
import { get, post, put, del } from './client.js';

/** Bootstrap: the caller's eternal conversation id — null before the first-ever message.
 *  `model` = the configured default; `chat_model` = this user's lever (null = default,
 *  'smart' = per-message routing); `persona` = the active server persona name.
 *  Throws ApiError(503, 'config_error') when the driver plane is off.
 *  @returns {Promise<{conv_id: string|null, model: string, chat_model: string|null, persona: string, voice_session_usd: number, tts_voices: string[]}>} */
export const getCompanion = () => get('/v1/companion');

/** The per-user chat-model lever: a catalog id, 'smart', or null/'' to clear to default.
 *  400 unknown model; 403 when there's no user session (operator credential).
 *  @returns {Promise<{chat_model: string|null}>} */
export const setChatModel = (model) => put('/v1/companion/model', { model });

/** What Toto remembers about this user — enumerable with provenance.
 *  @returns {Promise<{memories: Array<{memory_id: string, kind: string, content: string, source_run: string, created_at: number}>}>} */
export const getMemory = () => get('/v1/companion/memory');

/** Erase one memory — forgotten on the next wake. 404 for unknown/foreign ids. */
export const deleteMemory = (memoryId) => del(`/v1/companion/memory/${memoryId}`);

/** One session's snapshot (query/answer/status/error/tasks/cost_total) — the sub-agent card's
 *  reload + terminal shape. 404 when unknown or not the caller's. */
export const getSession = (runId) => get(`/v1/sessions/${runId}`);

/** One board's payload ({title, sections:[…]}) by object_id, or null if it's gone. Boards are
 *  data-only canvas objects; the console reads them view-only for the in-chat board card. The
 *  list endpoint returns the caller's boards (owner-scoped server-side), so we filter by id. */
export const getBoard = async (objectId) => {
  const { objects } = await get('/v1/objects', { query: { kind: 'board' } });
  return (objects ?? []).find((o) => o.object_id === objectId)?.payload ?? null;
};

/** The conversation snapshot: every turn's full session row ordered by turn (query, answer,
 *  status running|done|failed|cancelled, error, cost_total, run_id, lane — chat turns carry
 *  lane='chat'). 404 when the conv id is unknown/not the caller's.
 *  @returns {Promise<{conv_id: string, cost_total: number, turns: Array<object>}>} */
export const getTurns = (convId) => get(`/v1/sessions/${convId}/turns`);

/** Wake the companion with one message. 202 {run_id, conv_id, turn, status:'running'};
 *  409 conflict while a chat turn is already live; 503 config_error when the driver is off.
 *  `opts.model` = a one-shot model override for THIS turn (the retry-on-frontier escalation; a
 *  catalog id or 'smart', validated server-side → 400 unknown). `opts.escalatedFrom` = the run/
 *  request id this turn was escalated from; it rides the x-toto-escalated-from header, which the
 *  gateway stamps onto the trace as the routing-dissatisfaction signal (W3-C3).
 *  @returns {Promise<{run_id: string, conv_id: string, turn: number, status: string}>} */
export const sendMessage = (query, { model, escalatedFrom } = {}) =>
  post(
    '/v1/companion/messages',
    { query, ...(model ? { model } : {}) },
    escalatedFrom ? { headers: { 'x-toto-escalated-from': escalatedFrom } } : undefined,
  );

/** Cooperative stop for a live turn. `spokenChars` = characters of the answer the user has
 *  actually seen — the server truncates the persisted answer there. 409 if already finished. */
export const interrupt = (runId, spokenChars) =>
  post('/v1/companion/interrupt', { run_id: runId, spoken_chars: spokenChars });

/**
 * Subscribe to a run's event stream. Event vocabulary (derived from the publish()/_emit call
 * sites in toto_gateway — companion/core.py, routes/companion.py, runs.py):
 *   run_created     {query, conv_id, turn, lane}          — replayed first on connect
 *   status          {phase:'thinking'}                    — instant liveness, before any I/O
 *   companion_wake  {memories, recalled, live_running}    — memory/recall loaded
 *   answer_delta    {node, text}                          — append-only answer chunks, seq order
 *   companion_agent {decision:'tool'|'answer'|'cancelled', tool?, call_id?, model?, cost?}
 *   companion_retry {outcome}
 *   tool_start      {tool, call_id, summary}              — summary is the human activity phrase
 *   tool_done       {tool, call_id, sha256, …chip}
 *   tool_refused    {tool, call_id, surface, scope_hash}
 *   llm_call / session_ref / companion_timing             — telemetry (ignored by the chat page)
 *   run_done | run_failed | run_cancelled                 — terminal; the server closes the stream
 *
 * `handlers` maps event name -> (payload) => void; payload is the parsed `data:` JSON.
 * Returns a close() function. EventSource reconnects on drop and resumes via Last-Event-ID;
 * a terminal event closes the source (the server ends the stream, which would otherwise
 * look like a drop and trigger a reconnect loop).
 */
/**
 * Replay a FINISHED run's event log and resolve the structured artifacts it contains:
 * `refs` — session_ref events (sub-agent cards); `recs` — tool_done chips with
 * kind='recommendation' (the recommend_model advice cards); and `boards` — tool_done chips
 * with kind='board' (put_object saved a session board, latest title per id wins). All three
 * live only in the chat turn's event stream (the sessions table has no parent column, and the
 * turn snapshot carries no chips), so reload reconstruction replays the log. The server replays
 * from seq 0 and closes at the terminal event, so this is one short-lived request per turn.
 * ponytail: O(turns) replays on load — fine at dozens-of-turns scale; add a REST events
 * endpoint if threads ever get huge. Resolves empty arrays on error/timeout, never rejects.
 * @returns {Promise<{refs: Array<{run_id: string, query: string}>, recs: Array<object>, boards: Array<{object_id: string, title: string}>}>}
 */
export function replayTurnRefs(runId, timeoutMs = 8000) {
  return new Promise((resolve) => {
    const es = new EventSource(`/v1/sessions/${runId}/events`);
    const refs = [];
    const recs = [];
    const boardById = new Map(); // dedupe re-PUTs of the same board to one card; last title wins
    const finish = () => {
      clearTimeout(timer);
      es.close();
      resolve({ refs, recs, boards: [...boardById.values()] });
    };
    const timer = setTimeout(finish, timeoutMs);
    es.addEventListener('session_ref', (e) => {
      try {
        const d = JSON.parse(e.data);
        if (d.run_id) refs.push({ run_id: d.run_id, query: d.query || '' });
      } catch {
        /* skip malformed frame */
      }
    });
    es.addEventListener('tool_done', (e) => {
      try {
        const d = JSON.parse(e.data);
        if (d.kind === 'recommendation') recs.push(d);
        else if (d.kind === 'board' && d.object_id) boardById.set(d.object_id, { object_id: d.object_id, title: d.title || '' });
      } catch {
        /* skip malformed frame */
      }
    });
    for (const kind of ['run_done', 'run_failed', 'run_cancelled']) es.addEventListener(kind, finish);
    es.onerror = finish; // server closed the stream (or network) — take what replayed
  });
}

export function subscribeRun(runId, handlers) {
  const es = new EventSource(`/v1/sessions/${runId}/events`);
  const terminal = new Set(['run_done', 'run_failed', 'run_cancelled']);
  for (const [kind, fn] of Object.entries(handlers)) {
    es.addEventListener(kind, (e) => {
      let data = {};
      try {
        data = JSON.parse(e.data);
      } catch {
        /* a malformed frame is dropped, not fatal */
      }
      if (terminal.has(kind)) es.close();
      fn(data);
    });
  }
  return () => es.close();
}
