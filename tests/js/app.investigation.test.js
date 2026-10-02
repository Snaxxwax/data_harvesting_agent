"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");

const {
  api,
  formatApiErrorDetail,
  canCreateInvestigationJob,
  modelCapabilityState,
} = require("../../src/harvest/web/app.js");

function jsonResponse(status, body) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

test("formatApiErrorDetail passes through a plain string detail unchanged", () => {
  assert.equal(
    formatApiErrorDetail("provide a seed URL or configure HARVEST_SEARCH_URL"),
    "provide a seed URL or configure HARVEST_SEARCH_URL"
  );
});

test("formatApiErrorDetail renders a FastAPI validation-error array as readable text", () => {
  const detail = [
    { loc: ["body", "seeds", 0], msg: "invalid or unsafe URL", type: "value_error" },
  ];
  const message = formatApiErrorDetail(detail);
  assert.ok(message, "expected a non-empty message");
  assert.ok(!message.includes("[object Object]"), `message was: ${message}`);
  assert.match(message, /seeds\.0/);
  assert.match(message, /invalid or unsafe URL/);
});

test("api() surfaces a FastAPI array-shaped 422 detail as readable text, never [object Object]", async () => {
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async () =>
    jsonResponse(422, {
      detail: [{ loc: ["body", "seeds", 0], msg: "invalid or unsafe URL", type: "value_error" }],
    });
  try {
    await assert.rejects(api("/jobs", { method: "POST", body: {} }), (err) => {
      assert.ok(!err.message.includes("[object Object]"), `message was: ${err.message}`);
      assert.match(err.message, /invalid or unsafe URL/);
      return true;
    });
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("api() still surfaces a plain string 422 detail unchanged", async () => {
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async () =>
    jsonResponse(422, { detail: "provide a seed URL or configure HARVEST_SEARCH_URL" });
  try {
    await assert.rejects(api("/jobs", { method: "POST", body: {} }), (err) => {
      assert.equal(err.message, "provide a seed URL or configure HARVEST_SEARCH_URL");
      return true;
    });
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("canCreateInvestigationJob disables creation for a seedless plan when search is not configured", () => {
  assert.equal(canCreateInvestigationJob([], { search_configured: false }), false);
});

test("canCreateInvestigationJob allows creation once a valid seed is present, even without search", () => {
  assert.equal(
    canCreateInvestigationJob(["https://example.org/"], { search_configured: false }),
    true
  );
});

test("canCreateInvestigationJob allows seedless creation when search is configured", () => {
  assert.equal(canCreateInvestigationJob([], { search_configured: true }), true);
});

test("canCreateInvestigationJob allows direct seeds even when search is configured", () => {
  assert.equal(
    canCreateInvestigationJob(["https://example.org/"], { search_configured: true }),
    true
  );
});

test("modelCapabilityState disables the checkbox and shows a hint when model is not configured", () => {
  const state = modelCapabilityState({ model_configured: false });
  assert.equal(state.disabled, true);
  assert.equal(state.hintVisible, true);
});

test("modelCapabilityState enables the checkbox and hides the hint when model is configured", () => {
  const state = modelCapabilityState({ model_configured: true });
  assert.equal(state.disabled, false);
  assert.equal(state.hintVisible, false);
});
