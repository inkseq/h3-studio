// Many menus are <details> elements whose panel floats over the page. A <details> only closes
// when its own summary is clicked again, so these menus stayed open after clicking elsewhere
// (#358 follow-up). One document-level listener closes every open floating <details> that does
// not contain the click, and Escape closes them too. Inline disclosures (panels in normal flow,
// such as "Access details") are left alone, so expanding a section still works as before.

export function isFloating(details, getStyle = (el) => getComputedStyle(el)) {
  for (const child of details.children) {
    if (child.tagName === 'SUMMARY') continue;
    const position = getStyle(child).position;
    return position === 'absolute' || position === 'fixed';
  }
  return false;
}

export function closeFloatingOutside(root, target, getStyle) {
  let closed = 0;
  for (const details of root.querySelectorAll('details[open]')) {
    if (target && details.contains(target)) continue;
    if (!isFloating(details, getStyle)) continue;
    details.removeAttribute('open');
    closed += 1;
  }
  return closed;
}

export function installPopoverDismiss(doc = document) {
  // Focus moving elsewhere (a dialog opening, Tab) also closes the menu it left.
  const onPointer = (event) => closeFloatingOutside(doc, event.target);
  const onKey = (event) => {
    if (event.key !== 'Escape') return;
    const active = doc.activeElement;
    const owner = active?.closest?.('details[open]');
    if (closeFloatingOutside(doc, null) && owner && !owner.open)
      owner.querySelector('summary')?.focus();
  };
  doc.addEventListener('pointerdown', onPointer, true);
  doc.addEventListener('focusin', onPointer);
  doc.addEventListener('keydown', onKey);
  return () => {
    doc.removeEventListener('pointerdown', onPointer, true);
    doc.removeEventListener('focusin', onPointer);
    doc.removeEventListener('keydown', onKey);
  };
}
