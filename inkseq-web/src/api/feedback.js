// Feedback routes of the server API. First area moved behind the API layer (h3-studio#992,
// step 2): pages call these functions and never write a '/v1/feedback…' path themselves.
//
// The pattern for every area that follows (inventory: studio-app/API-INVENTORY.md):
// - one module per server area, one exported function per server operation;
// - the first argument is the account's transport (createCloudClient, use-client.js), which
//   keeps sign-in, refusals, retries and read admission in one place;
// - the caller owns the idempotency key, because only it knows when a resend is the same
//   request; shaping, checking and caching of the answer stay with the caller for now;
// - a function names the server's fields once, so generated types can replace its body later
//   without touching a page.
const REPORTS = '/v1/feedback';
const SETTINGS = '/v1/feedback/settings';

// POST /v1/feedback: a problem report from the web form. Returns the receipt ({ report_id }).
export const sendFeedback = (client, body, key) =>
  client.request(REPORTS, { method: 'POST', key, body });

// GET /v1/feedback/settings: { revision, enabled }, whether assistants may send feedback.
export const readFeedbackSettings = (client) => client.request(SETTINGS);

// PATCH /v1/feedback/settings: refused with 409 when `revision` is no longer the saved one.
export const saveFeedbackSettings = (client, { enabled, revision }, key) =>
  client.request(SETTINGS, {
    method: 'PATCH',
    body: { expected_revision: revision, enabled },
    key,
  });
