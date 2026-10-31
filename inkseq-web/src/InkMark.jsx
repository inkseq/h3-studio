// The Inkseq ink-drop mark family (owner decision 2026-10-20: keep every variant and animal).
// Two traits make it recognisable and never change: the golden-ratio drop outline (bottom
// circle r, tip r*phi^2 above its centre, leaning 8 degrees right) and the play-button eye. Everything
// else — colour, face, ears, tails — may vary by context, season or skin.
// Face variants take the skin's colours (--accent for the drop, --bg for features, unless a parent
// sets --ink-mark-body / --ink-mark-feat); animals keep their own colours.
import React from 'react';

export const INK_DROP = 'M50 12 C47 27 28 46 28 70 A22 22 0 0 0 72 70 C72 46 53 27 50 12Z';
const DROP = INK_DROP;
const BODY_COLOR = 'var(--ink-mark-body, var(--accent))';
const FEAT_COLOR = 'var(--ink-mark-feat, var(--bg))';
const BODY = { fill: BODY_COLOR };
const FEAT = { fill: FEAT_COLOR };
// Owner pick 2026-10-20 (option D): lean right, play-button eye on the left pointing in, so it reads as a wink.
const LEAN = 'rotate(8 50 60)';

const Drop = ({ fill, style }) => <path d={DROP} fill={fill} style={style} />;
const Eyes = ({ fill, style, dy = 0 }) => (
  <g transform={dy ? `translate(0 ${dy})` : undefined}>
    <path d="M39 58.5 L47.5 64 L39 69.5Z" fill={fill} style={style} strokeLinejoin="round" />
    <circle cx="58" cy="64" r="4.6" fill={fill} style={style} />
  </g>
);

// Face variants: the brand drop with different expressions and poses.
const FACES = {
  wink: (
    <g transform={LEAN}>
      <Drop style={BODY} />
      <Eyes style={FEAT} />
    </g>
  ),
  play: (
    <g transform={LEAN}>
      <Drop style={BODY} />
      <path
        d="M45 56 L58.5 64.5 L45 73Z"
        style={{ ...FEAT, stroke: FEAT_COLOR }}
        strokeWidth="2.2"
        strokeLinejoin="round"
      />
    </g>
  ),
  fall: (
    <>
      <path
        d="M14 40 H32 M8 52 H28 M16 64 H30"
        style={{ stroke: BODY_COLOR }}
        strokeWidth="3"
        strokeLinecap="round"
        opacity="0.45"
      />
      <g transform="rotate(20 52 60)">
        <Drop style={BODY} />
        <Eyes style={FEAT} />
      </g>
    </>
  ),
  ripple: (
    <>
      <g transform="translate(15 -4) scale(0.7)">
        <Drop style={BODY} />
        <path d="M45 58 L57 65 L45 72Z" style={FEAT} />
      </g>
      <ellipse
        cx="50"
        cy="80"
        rx="22"
        ry="5"
        fill="none"
        style={{ stroke: BODY_COLOR }}
        strokeWidth="2.6"
      />
      <ellipse
        cx="50"
        cy="82"
        rx="35.6"
        ry="8"
        fill="none"
        style={{ stroke: BODY_COLOR }}
        strokeWidth="2"
        opacity="0.5"
      />
    </>
  ),
  brush: (
    <g transform={LEAN}>
      <path
        d="M58 6 C66 10 60 18 52 16 C47 30 28 47 28 70 A22 22 0 0 0 72 70 C72 47 56 32 52 16"
        style={BODY}
      />
      <Eyes style={FEAT} />
    </g>
  ),
  screen: (
    <g transform={LEAN}>
      <Drop style={BODY} />
      <rect x="37.6" y="56.3" width="24.8" height="15.3" rx="3" style={FEAT} />
      <path d="M47 59.5 L54.5 64 L47 68.5Z" style={BODY} />
    </g>
  ),
  tentacles: (
    <g transform="rotate(8 50 56)">
      <path
        d="M36 84 q-3 8 -8 10 M50 88 q0 6 -3 9 M64 84 q3 8 8 10"
        fill="none"
        style={{ stroke: BODY_COLOR }}
        strokeWidth="5"
        strokeLinecap="round"
      />
      <Drop style={BODY} />
      <Eyes style={FEAT} dy={-2} />
    </g>
  ),
  gloss: (
    <g transform={LEAN}>
      <Drop style={BODY} />
      <path
        d="M36 66 C35 56 39 47 45 40"
        fill="none"
        stroke="#ffffff"
        strokeWidth="3.4"
        strokeLinecap="round"
        opacity="0.7"
      />
      <path d="M41 61 L48.5 66 L41 71Z" style={FEAT} />
      <circle cx="58" cy="66" r="4.2" style={FEAT} />
    </g>
  ),
  smile: (
    <g transform={LEAN}>
      <Drop style={BODY} />
      <path
        d="M38 60 q4 -5 8 0 M54 60 q4 -5 8 0"
        fill="none"
        style={{ stroke: FEAT_COLOR }}
        strokeWidth="2.8"
        strokeLinecap="round"
      />
      <path d="M46.5 68 L55.5 73 L46.5 78Z" style={FEAT} />
    </g>
  ),
  cutout: (
    <g transform={LEAN}>
      <Drop style={BODY} />
      <path d="M43 55 L61 66 L43 77Z" style={FEAT} />
    </g>
  ),
  pair: (
    <>
      <g transform={'translate(-6 0) ' + LEAN}>
        <Drop style={BODY} />
        <Eyes style={FEAT} />
      </g>
      <g transform="translate(60 44) scale(0.382) rotate(12 50 60)">
        <Drop style={BODY} />
      </g>
    </>
  ),
  spiral: (
    <g transform={LEAN}>
      <Drop style={BODY} />
      <path
        d="M50 70 A3 3 0 0 1 47 67 A5 5 0 0 1 52 62 A8 8 0 0 1 60 70 A13 13 0 0 1 47 83 A21 21 0 0 1 32 62"
        fill="none"
        style={{ stroke: FEAT_COLOR }}
        strokeWidth="2.6"
        strokeLinecap="round"
      />
    </g>
  ),
};

// Animals: the same drop and play-button eye, in their own colours.
const ANIMALS = {
  cat: (
    <g transform={LEAN}>
      <path d="M33 50 L29 30 L44 40Z M67 50 L71 30 L56 40Z" fill="#c7f36b" />
      <Drop fill="#c7f36b" />
      <Eyes fill="#0b0c0c" />
      <path d="M48 72 L52 72 L50 74.5Z" fill="#e2627a" />
      <path
        d="M30 70 H40 M30 75 L40 73 M60 73 L70 75 M60 70 H70"
        stroke="#0b0c0c"
        strokeWidth="1.4"
        strokeLinecap="round"
      />
    </g>
  ),
  rabbit: (
    <g transform={LEAN}>
      <ellipse cx="40" cy="26" rx="6" ry="17" transform="rotate(-14 40 26)" fill="#f4f1ea" />
      <ellipse cx="60" cy="26" rx="6" ry="17" transform="rotate(14 60 26)" fill="#f4f1ea" />
      <ellipse cx="40" cy="26" rx="2.8" ry="12" transform="rotate(-14 40 26)" fill="#f2a7b8" />
      <ellipse cx="60" cy="26" rx="2.8" ry="12" transform="rotate(14 60 26)" fill="#f2a7b8" />
      <Drop fill="#f4f1ea" />
      <Eyes fill="#1d2421" />
      <circle cx="37" cy="72" r="3" fill="#f2a7b8" opacity="0.8" />
      <circle cx="63" cy="72" r="3" fill="#f2a7b8" opacity="0.8" />
    </g>
  ),
  fox: (
    <g transform={LEAN}>
      <path d="M32 52 L27 26 L45 40Z M68 52 L73 26 L55 40Z" fill="#f08a3c" />
      <path d="M33 47 L30 32 L41 41Z M67 47 L70 32 L59 41Z" fill="#2a1a10" />
      <Drop fill="#f08a3c" />
      <path
        d="M30 72 C38 70 46 78 50 90 C54 78 62 70 70 72 C70 84 62 92 50 92 C38 92 30 84 30 72Z"
        fill="#fff6ea"
      />
      <Eyes fill="#2a1a10" />
      <circle cx="50" cy="78" r="2.4" fill="#2a1a10" />
    </g>
  ),
  panda: (
    <g transform={LEAN}>
      <circle cx="35" cy="44" r="7" fill="#1d2421" />
      <circle cx="65" cy="44" r="7" fill="#1d2421" />
      <Drop fill="#f8f8f4" />
      <ellipse cx="41.5" cy="64" rx="7" ry="8.5" transform="rotate(20 41.5 64)" fill="#1d2421" />
      <ellipse cx="57.5" cy="64" rx="7.5" ry="8.5" transform="rotate(-20 57.5 64)" fill="#1d2421" />
      <Eyes fill="#f8f8f4" />
      <ellipse cx="50" cy="75" rx="3" ry="2" fill="#1d2421" />
    </g>
  ),
  frog: (
    <g transform={LEAN}>
      <Drop fill="#6cc24a" />
      <circle cx="40" cy="52" r="8.5" fill="#6cc24a" />
      <circle cx="60" cy="52" r="8.5" fill="#6cc24a" />
      <circle cx="40" cy="52" r="6" fill="#ffffff" />
      <circle cx="60" cy="52" r="6" fill="#ffffff" />
      <path d="M37.5 48.5 L43.5 52 L37.5 55.5Z" fill="#13301b" />
      <circle cx="60" cy="52" r="3" fill="#13301b" />
      <path
        d="M38 74 q12 8 24 0"
        fill="none"
        stroke="#13301b"
        strokeWidth="2.6"
        strokeLinecap="round"
      />
    </g>
  ),
  chick: (
    <g transform={LEAN}>
      <Drop fill="#ffd23f" />
      <path
        d="M50 12 C46 6 42 6 41 9 M50 12 C51 5 55 3 57 6"
        fill="none"
        stroke="#ffd23f"
        strokeWidth="3"
        strokeLinecap="round"
      />
      <Eyes fill="#2b2110" />
      <path d="M45.5 71 L54.5 71 L50 77Z" fill="#f08a3c" />
      <path d="M28 70 q-6 4 -4 10 q5 -2 8 -6" fill="#f2bb1b" />
    </g>
  ),
  penguin: (
    <g transform={LEAN}>
      <Drop fill="#3b4b5e" />
      <ellipse cx="50" cy="76" rx="15" ry="15" fill="#f8f8f4" />
      <path d="M39 53 L47 58 L39 63Z" fill="#f8f8f4" />
      <circle cx="58" cy="58" r="4.2" fill="#f8f8f4" />
      <path d="M46.5 64 L53.5 64 L50 68.5Z" fill="#f2a33a" />
      <path d="M40 92 h7 M53 92 h7" stroke="#f2a33a" strokeWidth="3.4" strokeLinecap="round" />
    </g>
  ),
  squid: (
    <g transform="rotate(8 50 56)">
      <path
        d="M36 84 q-3 8 -8 10 M50 88 q0 6 -3 9 M64 84 q3 8 8 10"
        fill="none"
        stroke="#c7f36b"
        strokeWidth="5"
        strokeLinecap="round"
      />
      <Drop fill="#c7f36b" />
      <Eyes fill="#0b0c0c" dy={-2} />
    </g>
  ),
  fish: (
    <>
      <g transform="rotate(-82 50 56)">
        <path d="M50 12 L38 -2 L50 4 L62 -2Z" fill="#3b82f6" />
        <Drop fill="#5aa2ff" />
        <path d="M36 70 q14 -6 28 0" fill="none" stroke="#3b82f6" strokeWidth="2.4" opacity="0.7" />
      </g>
      <path d="M30 46 L40 52 L30 58Z" fill="#0f2a55" />
      <path
        d="M24 64 q4 3 8 1"
        fill="none"
        stroke="#0f2a55"
        strokeWidth="2"
        strokeLinecap="round"
      />
    </>
  ),
  bear: (
    <g transform={LEAN}>
      <circle cx="35" cy="46" r="7.5" fill="#8b5a2b" />
      <circle cx="65" cy="46" r="7.5" fill="#8b5a2b" />
      <circle cx="35" cy="46" r="3.5" fill="#d9a46b" />
      <circle cx="65" cy="46" r="3.5" fill="#d9a46b" />
      <Drop fill="#8b5a2b" />
      <ellipse cx="50" cy="75" rx="10" ry="7" fill="#d9a46b" />
      <Eyes fill="#21140a" />
      <ellipse cx="50" cy="73" rx="2.8" ry="2" fill="#21140a" />
    </g>
  ),
  owl: (
    <g transform={LEAN}>
      <path d="M33 48 L31 34 L41 42Z M67 48 L69 34 L59 42Z" fill="#7c5cff" />
      <Drop fill="#7c5cff" />
      <circle cx="41" cy="62" r="8" fill="#ffffff" />
      <circle cx="59" cy="62" r="8" fill="#ffffff" />
      <path d="M38 57.5 L45.5 62 L38 66.5Z" fill="#1b1340" />
      <circle cx="59" cy="62" r="3.6" fill="#1b1340" />
      <path d="M47 70 L53 70 L50 75Z" fill="#f2a33a" />
      <path
        d="M40 80 q3 2 6 0 M47 82 q3 2 6 0 M54 80 q3 2 6 0"
        fill="none"
        stroke="#b9a7ff"
        strokeWidth="1.6"
        strokeLinecap="round"
      />
    </g>
  ),
  mouse: (
    <g transform={LEAN}>
      <path
        d="M70 86 q16 4 16 -10 q0 -8 -8 -8"
        fill="none"
        stroke="#9aa3ad"
        strokeWidth="3"
        strokeLinecap="round"
      />
      <circle cx="33" cy="46" r="10" fill="#9aa3ad" />
      <circle cx="67" cy="46" r="10" fill="#9aa3ad" />
      <circle cx="33" cy="46" r="6" fill="#f2a7b8" />
      <circle cx="67" cy="46" r="6" fill="#f2a7b8" />
      <Drop fill="#9aa3ad" />
      <Eyes fill="#1d2421" />
      <circle cx="50" cy="74" r="2.4" fill="#f2a7b8" />
    </g>
  ),
};

const ART = { ...FACES, ...ANIMALS };
export const INK_MARK_FACES = Object.keys(FACES);
export const INK_MARK_ANIMALS = Object.keys(ANIMALS);

// lean: 'left' (default, owner preference) or 'right'. The artwork is drawn leaning right; the left
// version turns it 16 degrees back, which keeps the play-button eye on the inner side. The ripple
// stays level because the water surface must not tilt.
export default function InkMark({
  variant = 'wink',
  lean = 'left',
  size = 24,
  title,
  className = '',
}) {
  const art = ART[variant] || FACES.wink;
  return (
    <svg
      className={'ink-mark ' + className}
      width={size}
      height={size}
      viewBox="0 0 100 100"
      role={title ? 'img' : undefined}
      aria-label={title || undefined}
      aria-hidden={title ? undefined : 'true'}
      focusable="false"
    >
      {lean === 'right' || variant === 'ripple' ? art : <g transform="rotate(-16 50 60)">{art}</g>}
    </svg>
  );
}
