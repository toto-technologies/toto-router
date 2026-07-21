// Task-type display names — ONE formatter so every surface (activity chips, catalog rows,
// pickers) renders the classifier's snake_case vocabulary identically. Convention follows the
// catalog's existing "Generalist" treatment for `other`: sentence case, acronyms upper-cased.
//   code_generation → "Code generation" · open_qa → "Open QA" · other → "Generalist"
export function taskLabel(raw) {
  if (!raw) return '';
  if (raw === 'other') return 'Generalist';
  return raw
    .split(/[_\s]+/)
    .filter(Boolean)
    .map((w, i) => (w === 'qa' ? 'QA' : i === 0 ? w.charAt(0).toUpperCase() + w.slice(1) : w))
    .join(' ');
}
