// Closed lists shared with the server's storyboard check (h3-studio#1043). Empty means Auto.
export const LOOKS = Object.freeze(['film', 'anime', 'watercolor', 'noir', 'soft3d', 'storybook']);
export const CAMERAS = Object.freeze([
  'static',
  'push',
  'pull',
  'pan',
  'track',
  'handheld',
  'orbit',
]);

const ARROWS = {
  static: '·',
  push: '↗',
  pull: '↙',
  pan: '→',
  track: '⇉',
  handheld: '~',
  orbit: '↻',
};

export const validLook = (value) => value === '' || LOOKS.includes(value);
export const validCamera = (value) => value === '' || CAMERAS.includes(value);
export const cameraArrow = (value) => ARROWS[value] || '';
export const lookPicture = (value) => '/v1/story-chats/starter-pictures/look-' + value;
// The words a look puts in the story's style, in the person's language, so they can read and edit it.
export const styleOf = (t, value) => t('shotLook.style.' + value);
