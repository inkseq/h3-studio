// The server's refusal when the current reviewed Terms version isn't accepted yet.
export const TERMS_REQUIRED = 'inkseq-terms-required';

export function termsRequired(status, detail) {
  return status === 409 && detail?.code === 'terms_acceptance_required';
}
