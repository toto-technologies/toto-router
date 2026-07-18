// Focused client for the benchmark evidence surface. The compatibility routes stay under
// /v1/admin/benchmarks while the platform evolves; callers import this module so that transition
// does not keep growing the general admin client.
import { get, post } from './client.js';

/** @returns {Promise<import('./types').BenchmarkModelsResponse>} */
export const getBenchmarkModels = (query = {}) => get('/v1/admin/benchmarks/models', { query });

/** @param {string} id @returns {Promise<import('./types').BenchmarkModelDetail>} */
export const getBenchmarkModel = (id) =>
  get('/v1/admin/benchmarks/model', { query: { id } });

/** @returns {Promise<import('./types').BenchmarkCoverage>} */
export const getBenchmarkCoverage = () => get('/v1/admin/benchmarks/coverage');

/** @returns {Promise<Record<string, import('./types').BenchmarkRefreshResult>>} */
export const refreshBenchmarks = () => post('/v1/admin/benchmarks/refresh');

/** @returns {Promise<{aliases: Array<Record<string, unknown>>, count: number}>} */
export const getBenchmarkAliases = (maxConfidence = 0.8) =>
  get('/v1/admin/benchmarks/aliases', { query: { max_confidence: maxConfidence } });
