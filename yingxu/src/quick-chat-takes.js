export function checkedPreferences(value) {
  if (
    !Number.isSafeInteger(value?.version) ||
    value.version < 0 ||
    !['copies_max', 'takes_fold_count'].every(
      (key) => Number.isInteger(value[key]) && value[key] >= 1 && value[key] <= 2147483647,
    )
  )
    throw Error('takes.invalidPreferences');
  return value;
}
export function preferenceBody(previous, copies, fold) {
  const value = checkedPreferences({
    ...previous,
    copies_max: Number(copies),
    takes_fold_count: Number(fold),
  });
  return {
    expected_version: previous.version,
    copies_max: value.copies_max,
    takes_fold_count: value.takes_fold_count,
  };
}

export function takeError(error, t) {
  if (typeof error?.message === 'string' && error.message.startsWith('takes.'))
    return t(error.message);
  const key = {
    version_conflict: 'takes.conflict',
    copies_limit_exceeded: 'takes.capExceeded',
    invalid_take_preferences: 'takes.invalidPreferences',
    project_changed: 'takes.conflict',
    idempotency_conflict: 'takes.wait',
  }[error?.code];
  return t(key || 'takes.operationFailed');
}
