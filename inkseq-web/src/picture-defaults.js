// A story's default picture (h3-studio#891): the aspect, quality, kept sound and takes per
// generation that new shots start with. Saved in the story as journey.pictureDefaults.
// Existing shots keep their own settings until "apply to all shots" is pressed; a shot can
// still differ, and the card marks it. Duration stays per shot (it follows the shot's words).

export const ASPECTS = Object.freeze(['16:9', '9:16', '1:1']);
export const QUALITIES = Object.freeze(['draft', 'final']);
export const MAX_COPIES = 4;
const FALLBACK = Object.freeze({ aspect: '16:9', quality: 'draft', keepAudio: true, copies: 1 });

/** The story's defaults, each field checked; `set` says whether the story chose any. */
export function pictureDefaults(project) {
  const saved = project?.journey?.pictureDefaults;
  const value = saved && typeof saved === 'object' ? saved : {};
  return {
    aspect: ASPECTS.includes(value.aspect) ? value.aspect : FALLBACK.aspect,
    quality: QUALITIES.includes(value.quality) ? value.quality : FALLBACK.quality,
    keepAudio: typeof value.keepAudio === 'boolean' ? value.keepAudio : FALLBACK.keepAudio,
    copies:
      Number.isInteger(value.copies) && value.copies >= 1 && value.copies <= MAX_COPIES
        ? value.copies
        : FALLBACK.copies,
    set: !!saved,
  };
}

export function setPictureDefaults(project, value) {
  const read = pictureDefaults({ journey: { pictureDefaults: value } }),
    clean = {
      aspect: read.aspect,
      quality: read.quality,
      keepAudio: read.keepAudio,
      copies: read.copies,
    };
  project.journey = { ...project.journey, pictureDefaults: clean };
  return clean;
}

/** Give one shot entity the defaults (a new shot, or "apply to all shots"). */
export function applyPictureDefaults(shot, defaults) {
  const data = shot.data;
  if (data.storyboard) data.storyboard = { ...data.storyboard, quality: defaults.quality };
  else data.qualityIntent = defaults.quality;
  const controls = { ...data.h3?.controls, aspect_ratio: defaults.aspect };
  if (defaults.keepAudio) delete controls.generate_audio;
  else controls.generate_audio = false;
  data.h3 = { ...data.h3, controls };
}

/** The takes-per-generation box's first value: the story default, within the account limit. */
export const defaultCopies = (state) =>
  String(Math.max(1, Math.min(state.project?.pictureDefaults?.copies || 1, state.copiesMax || 1)));
