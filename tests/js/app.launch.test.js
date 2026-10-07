"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");

const app = require("../../src/harvest/web/app.js");

const EMAIL_PLAN = {
  kind: "email",
  normalized: "jane@example.org",
  seeds: [],
  discovery_queries: ['"jane@example.org"', "jane example.org"],
  fields: ["full_name", "email", "profile_url"],
  tools: [
    { name: "ghunt", target: "jane@example.org" },
    { name: "spiderfoot", target: "jane@example.org" },
  ],
};
const USERNAME_PLAN = {
  kind: "username",
  normalized: "janedoe",
  seeds: [],
  discovery_queries: ['"janedoe"'],
  fields: ["display_name", "username"],
  tools: [
    { name: "maigret", target: "janedoe" },
    { name: "spiderfoot", target: "janedoe" },
  ],
};

test("detected kinds read as plain language", () => {
  assert.equal(app.kindLabel("email"), "Email address");
  assert.equal(app.kindLabel("url"), "Web address");
  assert.equal(app.kindLabel("identifier"), "Other identifier");
});

test("tools are presented as source categories, not tool names", () => {
  assert.equal(app.sourceLabel("ghunt"), "Google account enrichment");
  assert.equal(app.sourceLabel("maigret"), "Profile & account discovery");
  assert.equal(app.sourceLabel("discovery_search"), "Web discovery");
});

test("field names read as plain language, unknown ones are humanised", () => {
  assert.equal(app.fieldLabel("profile_url"), "Profile pages");
  assert.equal(app.fieldLabel("job_title"), "Job title");
});

test("presets are real configuration: budgets, tools, breadth and follow-up all differ", () => {
  const offered = ["ghunt", "spiderfoot"];
  const quick = app.presetSettings("quick", offered);
  const standard = app.presetSettings("standard", offered);
  const deep = app.presetSettings("deep", offered);
  assert.deepEqual(quick.tools, ["ghunt"]);
  assert.deepEqual(standard.tools, ["ghunt", "spiderfoot"]);
  assert.equal(quick.crawl, false);
  assert.equal(standard.crawl, true);
  assert.ok(quick.requests < standard.requests && standard.requests < deep.requests);
  assert.ok(quick.seconds < standard.seconds && standard.seconds < deep.seconds);
  // Standard matches the server's own Limits defaults.
  assert.equal(standard.requests, 100);
  assert.equal(standard.seconds, 900);
});

test("presets never offer a tool the plan did not, and stay within limits.tool_runs", () => {
  for (const key of Object.keys(app.PRESETS)) {
    assert.deepEqual(app.presetSettings(key, []).tools, []);
    assert.ok(app.presetSettings(key, ["ghunt", "maigret", "spiderfoot"]).tools.length <= 3);
  }
  assert.deepEqual(app.presetSettings("deep", ["maigret"]).tools, ["maigret"]);
});

test("Deep widens breadth without unbounded budgets", () => {
  const deep = app.presetSettings("deep", ["maigret"]);
  assert.equal(deep.topSites, null); // Maigret's full site database
  assert.ok(deep.requests <= 1000 && deep.seconds <= 3600);
});

test("unknown preset falls back to Standard", () => {
  assert.deepEqual(app.presetSettings("bogus", ["ghunt"]), app.presetSettings("standard", ["ghunt"]));
});

test("proxy usage follows budgets and tool breadth", () => {
  assert.equal(app.proxyUsage(30, ["ghunt"], 100), "Low");
  assert.equal(app.proxyUsage(30, ["ghunt", "spiderfoot"], 500), "Medium");
  assert.equal(app.proxyUsage(100, [], null), "Medium");
  assert.equal(app.proxyUsage(30, ["maigret"], null), "High");
  assert.equal(app.proxyUsage(300, [], null), "High");
});

test("duration is the hard time limit, stated as an upper bound", () => {
  assert.equal(app.durationLabel(300), "Up to 5 min");
  assert.equal(app.durationLabel(2700), "Up to 45 min");
  assert.equal(app.durationLabel(3600), "Up to 1 hour");
});

test("source categories include web discovery only when search is configured", () => {
  assert.deepEqual(app.sourceCategories(EMAIL_PLAN, ["ghunt"], { search_configured: true }), [
    "Web discovery",
    "Google account enrichment",
  ]);
  assert.deepEqual(app.sourceCategories(EMAIL_PLAN, ["ghunt"], { search_configured: false }), [
    "Google account enrichment",
  ]);
  const domain = { kind: "domain", seeds: ["https://example.org/"], discovery_queries: [] };
  assert.deepEqual(app.sourceCategories(domain, [], { search_configured: true }), ["Website analysis"]);
});

test("auto-generated names satisfy the dataset pattern", () => {
  const pattern = /^[a-zA-Z0-9_-]{1,80}$/;
  for (const input of ["jane@example.org", "Jane Doe", "https://x.org/a?b=c", "", "!!!", "a".repeat(200)]) {
    assert.match(app.autoDatasetName(input), pattern, input);
  }
  assert.equal(app.autoDatasetName("jane@example.org"), "jane-example-org");
  assert.equal(app.autoDatasetName("!!!"), "investigation");
});

test("launch payload keeps the JobSpec shape the previous form sent", () => {
  const spec = app.buildInvestigationSpec({
    plan: EMAIL_PLAN,
    dataset: "",
    seeds: [],
    fields: EMAIL_PLAN.fields,
    tools: ["ghunt"],
    crawl: true,
    topSites: 500,
    useModel: false,
    requests: 100,
    seconds: 900,
  });
  assert.deepEqual(spec, {
    objective: "Investigate jane@example.org",
    dataset: "jane-example-org",
    mode: "targeted",
    seeds: [],
    discovery_queries: EMAIL_PLAN.discovery_queries,
    fields: EMAIL_PLAN.fields,
    tools: [{ name: "ghunt", target: "jane@example.org", crawl: true }],
    use_model: false,
    limits: { requests: 100, seconds: 900 },
  });
});

test("launch payload: unselected sources are dropped, top_sites goes to maigret only", () => {
  const spec = app.buildInvestigationSpec({
    plan: USERNAME_PLAN,
    dataset: "custom_name",
    seeds: ["https://example.org/"],
    fields: ["username"],
    tools: ["maigret", "spiderfoot"],
    crawl: false,
    topSites: 100,
    useModel: true,
    requests: 30,
    seconds: 300,
  });
  assert.equal(spec.dataset, "custom_name");
  assert.deepEqual(spec.tools, [
    { name: "maigret", target: "janedoe", crawl: false, top_sites: 100 },
    { name: "spiderfoot", target: "janedoe", crawl: false },
  ]);
  assert.equal(spec.use_model, true);
  const allSites = app.buildInvestigationSpec({ ...spec, plan: USERNAME_PLAN, tools: ["maigret"], topSites: null, crawl: true });
  assert.deepEqual(allSites.tools, [{ name: "maigret", target: "janedoe", crawl: true }]);
});

test("Deep opts into pivots and budgets tool runs for them; other presets never pivot", () => {
  assert.equal(app.presetSettings("deep", []).pivots, 3);
  assert.equal(app.presetSettings("standard", []).pivots, 0);
  assert.equal(app.presetSettings("quick", []).pivots, 0);
  const base = { plan: EMAIL_PLAN, dataset: "", seeds: [], fields: [], crawl: true, topSites: null, requests: 300, seconds: 2700 };
  const deep = app.buildInvestigationSpec({ ...base, tools: ["ghunt", "spiderfoot"], pivots: 3 });
  // 2 root runs + up to 2 tools for each of 3 pivots.
  assert.deepEqual(deep.limits, { requests: 300, seconds: 2700, pivots: 3, tool_runs: 8 });
  const standard = app.buildInvestigationSpec({ ...base, tools: ["ghunt"], pivots: 0 });
  assert.deepEqual(standard.limits, { requests: 300, seconds: 2700 });
});

function job(status, spec) {
  return { status, spec: { seeds: [], discovery_queries: ["q"], tools: [], ...spec } };
}

test("stages come from real tasks: a running tool marks enrichment in progress", () => {
  const stages = app.deriveStages(job("running", { tools: [{ name: "ghunt", crawl: true }] }), [
    { id: 1, kind: "tool", key: "ghunt:jane@example.org", status: "running", parent: null },
    { id: 2, kind: "search", key: "q", status: "done", parent: null },
  ]);
  const byKey = Object.fromEntries(stages.map((s) => [s.key, s.state]));
  assert.equal(byKey.prepare, "done");
  assert.equal(byKey.public, "done");
  assert.equal(byKey.enrich, "active");
  assert.equal(byKey.discover, "waiting"); // crawl:true means tool findings will be visited
  assert.equal(byKey.report, "waiting");
});

test("stages: fetches descended from a tool are profile discovery, others public checks", () => {
  const stages = app.deriveStages(job("running", { tools: [{ name: "maigret", crawl: true }] }), [
    { id: 1, kind: "tool", key: "maigret:janedoe", status: "done", parent: null },
    { id: 2, kind: "extract", key: "capture", status: "done", parent: 1 },
    { id: 3, kind: "fetch", key: "https://github.com/janedoe", status: "pending", parent: 2 },
    { id: 4, kind: "fetch", key: "https://example.org/", status: "done", parent: null },
  ]);
  const discover = stages.find((s) => s.key === "discover");
  assert.equal(discover.state, "active");
  assert.equal(discover.total, 2);
  assert.equal(stages.find((s) => s.key === "public").total, 1);
});

test("stages never invent work: unexpected empty stages are omitted, terminal job closes out", () => {
  const stages = app.deriveStages(job("completed"), [
    { id: 1, kind: "search", key: "q", status: "done", parent: null },
  ]);
  const keys = stages.map((s) => s.key);
  assert.ok(!keys.includes("discover") && !keys.includes("enrich"));
  assert.equal(stages.find((s) => s.key === "correlate").state, "skipped");
  assert.equal(stages.find((s) => s.key === "report").state, "done");
});

test("stages: a queued job is still preparing; a failed job's report did not complete", () => {
  const queued = app.deriveStages(job("queued"), [{ id: 1, kind: "search", key: "q", status: "pending", parent: null }]);
  assert.equal(queued[0].state, "active");
  assert.equal(queued.find((s) => s.key === "public").state, "waiting");
  const failed = app.deriveStages(job("failed"), [{ id: 1, kind: "search", key: "q", status: "failed", parent: null }]);
  assert.equal(failed.find((s) => s.key === "public").state, "failed");
  assert.equal(failed.find((s) => s.key === "public").failed, 1);
  assert.equal(failed.find((s) => s.key === "report").state, "failed");
});

test("system issues: ready deployment has none; missing search and blocked SpiderFoot are named", () => {
  assert.deepEqual(app.systemIssues({ search_configured: true, capabilities: { spiderfoot: { ready: true } } }), []);
  const issues = app.systemIssues({
    search_configured: false,
    capabilities: { spiderfoot: { ready: false, detail: "HARVEST_SPIDERFOOT_URL is not set" } },
  });
  assert.equal(issues.length, 2);
  assert.match(issues[1], /HARVEST_SPIDERFOOT_URL/);
  // A tool simply not enabled is a choice, not a fault.
  assert.deepEqual(
    app.systemIssues({ search_configured: true, capabilities: { spiderfoot: { ready: false, detail: "not in HARVEST_TOOLS" } } }),
    []
  );
});
