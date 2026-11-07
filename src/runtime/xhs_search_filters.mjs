// Search can unmount its popover after a successful chip click. Reopen only the
// popover; never repeat the filter click or replace a filtered query with "all".
export async function reopenFilterPanels(panels, openPanel) {
  const current = panels();
  if (current.length) return current;
  const opened = await openPanel();
  return opened.status === 'ok' ? [opened.panel] : [];
}

export function repairFilterEvaluation(expression) {
  if (typeof expression !== 'string' || !expression.includes('const requestedFilters =')) return expression;
  const active = "const isActive = (option) => option.classList.contains('active');";
  const polling = 'const currentPanels = panels();\n            if (currentPanels.length === 1) {';
  const final = 'const finalPanels = panels();';
  for (const marker of [active, polling, final]) {
    if (expression.split(marker).length !== 2) {
      throw Object.assign(new Error('opencli_search_filter_contract_mismatch'), {code:'opencli_search_filter_contract_mismatch'});
    }
  }
  return expression
    .replace(active, `${active}\nconst reopenFilterPanels = ${reopenFilterPanels.toString()};`)
    .replace(polling, 'const currentPanels = await reopenFilterPanels(panels, openPanel);\n            if (currentPanels.length === 1) {')
    .replace(final, 'const finalPanels = await reopenFilterPanels(panels, openPanel);');
}
