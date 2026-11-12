// Our voice list in the picker (h3-studio#1226): which filters to offer and which voices a
// filter shows. The server tags every voice with a gender and an age band; the name carries
// the manner. A filter with one value only is not offered, and the voice in use always shows.
export const VOICE_GENDERS = ['female', 'male'];
export const VOICE_AGES = ['child', 'teen', 'young', 'adult', 'senior'];

const present = (voices, key, order) =>
  order.filter((value) => voices.some((voice) => voice[key] === value));

export function voiceFilters(voices) {
  const genders = present(voices, 'gender', VOICE_GENDERS),
    ages = present(voices, 'age', VOICE_AGES);
  return { genders: genders.length > 1 ? genders : [], ages: ages.length > 1 ? ages : [] };
}

export function shownVoices(voices, { gender = '', age = '' } = {}, chosenId = '') {
  return voices.filter(
    (voice) =>
      voice.id === chosenId ||
      ((!gender || voice.gender === gender) && (!age || voice.age === age)),
  );
}

// A designed voice is a sample only; an older server that does not say can speak them all.
export const voiceSpeaks = (voices, id) =>
  !!id && voices.find((voice) => voice.id === id)?.speaks !== false;
