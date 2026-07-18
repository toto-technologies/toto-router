// Client for saved session documents — every completed work session's result, kept as
// markdown under /v1/documents (caller's own docs only; newest first).
import { get } from './client.js';

/** @returns {Promise<{documents: Array<{doc_id: string, title: string, run_id: string, bytes: number, sha256: string, created_at: number}>}>} */
export const listDocuments = (limit = 100) => get('/v1/documents', { query: { limit } });

/** Same meta as the list row plus `body` (the markdown text). */
export const getDocument = (docId) => get(`/v1/documents/${encodeURIComponent(docId)}`);

/** Direct download URL (text/markdown with content-disposition) — a plain <a href> works. */
export const documentRawUrl = (docId) => `/v1/documents/${encodeURIComponent(docId)}/raw`;
