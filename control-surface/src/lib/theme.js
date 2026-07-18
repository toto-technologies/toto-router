// Theme toggle — light default, forest-dark on demand. Persists to localStorage.
// The pre-paint stamp lives in app.html; this just flips it at runtime.
export function currentTheme() {
  if (typeof document === 'undefined') return 'light';
  return document.documentElement.getAttribute('data-theme') === 'dark' ? 'dark' : 'light';
}

export function toggleTheme() {
  const next = currentTheme() === 'dark' ? 'light' : 'dark';
  document.documentElement.setAttribute('data-theme', next);
  try { localStorage.setItem('toto-theme', next); } catch (e) {}
  return next;
}
