// Procedural storyboard diagrams, not generated media or quality examples.
export function storyboardUrl(shot, variant = 'A') {
  const reaction = shot?.data?.demoType === 'reaction',
    wide = variant === 'B';
  const room =
    '<rect width="960" height="540" fill="#15282b"/><path d="M0 400H960V540H0Z" fill="#354649"/><path d="M50 60H430V330H50Z M530 60H910V330H530Z" fill="#203d46" stroke="#789193" stroke-width="8"/><g stroke="#a4c4cb" opacity=".35">' +
    Array.from({ length: 18 }, (_, i) => `<path d="M${i * 55} 70l-45 220"/>`).join('') +
    '</g><path d="M0 415H960" stroke="#d1b36b" stroke-width="3"/>';
  const person =
    '<g><path d="M365 455l24-160q80-75 160 0l22 160" fill="#98aa94"/><path d="M400 186q55-70 116 0l-4 100-110 4Z" fill="#141f21"/><ellipse cx="457" cy="236" rx="46" ry="56" fill="#d3af91"/><path d="M410 233q3-103 97-37l20 59-40-44-76 36" fill="#1f2527"/><path d="M438 257q22 9 40 0" stroke="#8a6554" stroke-width="3" fill="none"/><path d="M430 234h10m33 0h10" stroke="#38413b" stroke-width="4"/><path d="M385 345l59 60 85-30" stroke="#d3af91" stroke-width="23" fill="none"/></g>';
  const ticket =
    '<g transform="rotate(-9 480 340)"><rect x="330" y="260" width="290" height="150" rx="8" fill="#eadbb9"/><path d="M370 270V400" stroke="#a58e67" stroke-dasharray="5 5"/><text x="399" y="305" font-size="18" fill="#526059" font-family="sans-serif">LAST TRAIN</text><text x="399" y="345" font-size="30" fill="#3b514c" font-family="sans-serif">TOMORROW</text><path d="M400 370h150" stroke="#827c64" stroke-width="3"/></g>';
  const art = reaction
    ? `${room}<g transform="${wide ? 'translate(130 -10) scale(.95)' : 'translate(-150 -110) scale(1.45)'}">${person}</g>`
    : wide
      ? `${room}${person}<g transform="translate(190 210) scale(.48)">${ticket}</g>`
      : `${room}<g transform="translate(-115 -140) scale(1.25)">${ticket}</g><path d="M150 540l130-140 97-18" stroke="#d3af91" stroke-width="70" fill="none"/>`;
  const svg = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 960 540">${art}<rect x="20" y="20" width="165" height="34" rx="5" fill="#122520"/><text x="34" y="43" fill="#e6eddf" font-family="sans-serif" font-size="17">STORYBOARD ${variant}</text></svg>`;
  return 'data:image/svg+xml;charset=utf-8,' + encodeURIComponent(svg);
}
