// OSS single-tenant console auth. The operator token (TOTO_GW_AUTH_TOKEN) gates the console —
// there are no accounts. The token is written to the `toto_operator` cookie the gateway already
// reads (routes/deps.py), so it rides same-origin on every /v1 call. The token-gate screen sets it
// from a manual paste; a seamless launch sets the SAME cookie from a `#token=…` URL fragment in an
// inline app.html script (pre-hydration).

export const OPERATOR_COOKIE = 'toto_operator';

/** Write the operator token cookie. Raw value (no encoding) to byte-match the gateway's compare.
 *  ponytail: assumes an opaque alnum token (cookie-safe); an exotic token with `;`/space/`,` would
 *  need encoding on BOTH sides — cross that bridge if a real token ever needs it. */
export function setOperatorToken(token) {
  document.cookie = `${OPERATOR_COOKIE}=${token}; path=/; SameSite=Lax`;
}

/** Clear the operator token cookie (a rejected paste, or sign-out). */
export function clearOperatorToken() {
  document.cookie = `${OPERATOR_COOKIE}=; path=/; Max-Age=0; SameSite=Lax`;
}
