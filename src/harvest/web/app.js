"use strict";

const ACTIVE_STATUSES = ["queued", "running"];
const JOB_POLL_MS = 2000;

// Populated from /meta on every route change; consulted by the Investigation tab to
// avoid offering actions (seedless discovery, model reasoning) the deployment can't run.
let capabilities = { search_configured: true, model_configured: true };

// FastAPI's automatic validation errors return `detail` as an array of
// {loc, msg, type} objects rather than a string. Left unhandled, `new Error(detail)`
// stringifies the array to "[object Object]"; this renders it as readable text instead.
function formatApiErrorDetail(detail) {
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) {
    const messages = detail
      .map((item) => {
        if (item && typeof item === "object") {
          const loc = Array.isArray(item.loc) ? item.loc.filter((p) => p !== "body" && p !== "query") : [];
          const msg = item.msg || item.message || JSON.stringify(item);
          return loc.length ? `${loc.join(".")}: ${msg}` : msg;
        }
        return String(item);
      })
      .filter(Boolean);
    return messages.length ? messages.join("; ") : null;
  }
  if (detail && typeof detail === "object" && detail.reason) {
    return detail.suggestion ? `${detail.reason} (${detail.suggestion})` : detail.reason;
  }
  if (detail && typeof detail === "object") return JSON.stringify(detail);
  return detail || null;
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    method: options.method || "GET",
    credentials: "same-origin",
    headers: options.body ? { "Content-Type": "application/json" } : undefined,
    body: options.body ? JSON.stringify(options.body) : undefined,
  });
  if (response.status === 204) return null;
  const isJson = (response.headers.get("content-type") || "").includes("application/json");
  const data = isJson ? await response.json() : await response.text();
  if (!response.ok) {
    const rawDetail = isJson && data ? data.detail : null;
    const detail = formatApiErrorDetail(rawDetail) || response.statusText;
    const error = new Error(detail || `request failed (${response.status})`);
    error.status = response.status;
    throw error;
  }
  return data;
}

function el(tag, attrs, children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs || {})) {
    if (key === "class") node.className = value;
    else if (key === "text") node.textContent = value;
    else if (key.startsWith("on") && typeof value === "function") node.addEventListener(key.slice(2), value);
    else if (value !== undefined && value !== null) node.setAttribute(key, value);
  }
  for (const child of children || []) {
    if (child === undefined || child === null) continue;
    node.appendChild(typeof child === "string" ? document.createTextNode(child) : child);
  }
  return node;
}

function clear(node) {
  while (node.firstChild) node.removeChild(node.firstChild);
}

function safeLink(href, text) {
  if (typeof href !== "string" || !/^https?:\/\//i.test(href)) {
    return el("span", { text: String(href) });
  }
  return el("a", { href, target: "_blank", rel: "noopener noreferrer", text });
}

function captureLinks(ids) {
  const span = el("span", {});
  (ids || []).forEach((id, i) => {
    if (i > 0) span.appendChild(document.createTextNode(", "));
    span.appendChild(el("a", { href: `/captures/${id}`, title: "capture metadata (authenticated)", text: `#${id}` }));
  });
  return span;
}

// The request budget is the fetcher's page allowance. Tools run outside it (see tools.py),
// so a job can still spend proxy bandwidth after this is exhausted.
function limitsFrom(inputId, secondsId) {
  const read = (id) => parseInt((document.getElementById(id) || {}).value, 10);
  const limits = {};
  if (read(inputId) > 0) limits.requests = read(inputId);
  if (secondsId && read(secondsId) > 0) limits.seconds = read(secondsId);
  return limits;
}

function statusBadge(status) {
  return el("span", { class: `status status-${status}`, text: status });
}

function showError(node, err) {
  node.textContent = err && err.message ? err.message : String(err);
  node.hidden = false;
}

function hideError(node) {
  node.hidden = true;
  node.textContent = "";
}

const views = ["login-view", "launch-view", "jobs-view", "job-detail-view", "schedules-view", "status-view"];

function showView(id) {
  for (const name of views) {
    document.getElementById(name).hidden = name !== id;
  }
}

function setNavActive(hash) {
  for (const link of document.querySelectorAll("#topnav a")) {
    link.classList.toggle("active", link.getAttribute("href") === hash);
  }
}

let toolsEnabled = [];
let capabilityDetails = {};
let lastMeta = null;

// Pure decision: does the model deployment support "Use model reasoning"? Kept separate
// from the DOM so the Investigation tab's checkbox/hint wiring is easy to test in isolation.
function modelCapabilityState(caps) {
  const configured = !!(caps && caps.model_configured);
  return { disabled: !configured, hintVisible: !configured };
}

// Pure decision: can the Investigation tab submit right now? Engine.submit 422s with
// "provide a seed URL or configure HARVEST_SEARCH_URL" whenever there are no seeds and
// search isn't configured; mirroring that check client-side lets Create stay disabled
// (with an actionable hint) instead of letting a predictable 422 reach the user.
function canCreateInvestigationJob(seeds, caps, tools) {
  const hasSeeds = Array.isArray(seeds) && seeds.length > 0;
  // A selected tool is a starting point in its own right: the engine enqueues a tool task
  // at submit, so the job has work to do with no seed and no search. Without this, an email
  // investigation -- which has no seeds by nature -- could never be submitted from the UI on
  // a deployment without search, even with ghunt selected and ready.
  const hasTools = Array.isArray(tools) && tools.length > 0;
  const searchConfigured = !!(caps && caps.search_configured);
  return hasSeeds || hasTools || searchConfigured;
}

function applyModelCapability() {
  const checkbox = document.getElementById("inv-model");
  const hint = document.getElementById("inv-model-hint");
  if (!checkbox || !hint) return;
  const state = modelCapabilityState(capabilities);
  checkbox.disabled = state.disabled;
  if (state.disabled) checkbox.checked = false;
  hint.hidden = !state.hintVisible;
}

async function checkAuthAndConfig() {
  try {
    const meta = await api("/meta");
    toolsEnabled = Array.isArray(meta.tools_enabled) ? meta.tools_enabled : [];
    capabilityDetails = meta.capabilities || {};
    document.getElementById("topnav").hidden = false;
    document.getElementById("logout").hidden = false;
    capabilities = {
      search_configured: !!meta.search_configured,
      model_configured: !!meta.model_configured,
    };
    // A tool listed in tools_enabled can still refuse every run (spiderfoot did, for weeks,
    // because its container egress was undeclared), and that reads exactly like "found
    // nothing" unless the header says so; details live on #/status.
    lastMeta = meta;
    renderSystemIndicator(meta);
    applyModelCapability();
    return true;
  } catch (err) {
    if (err.status === 401) return false;
    throw err;
  }
}

async function doLogin(token) {
  await api("/session", { method: "POST", body: { token } });
}

async function doLogout() {
  stopJobPolling();
  await api("/logout", { method: "POST" });
  document.getElementById("topnav").hidden = true;
  document.getElementById("logout").hidden = true;
  document.getElementById("system-status").hidden = true;
  location.hash = "#/launch";
  showView("login-view");
}

function initLogin() {
  const form = document.getElementById("login-form");
  const errorNode = document.getElementById("login-error");
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    hideError(errorNode);
    const token = document.getElementById("login-token").value;
    try {
      await doLogin(token);
      document.getElementById("login-token").value = "";
      await route();
    } catch (err) {
      showError(errorNode, err);
    }
  });
  document.getElementById("logout").addEventListener("click", () => {
    doLogout().catch((err) => console.error(err));
  });
}

function initLaunchTabs() {
  const tabs = Array.from(document.querySelectorAll("#launch-tabs .tab"));
  const select = (tab) => {
    for (const t of tabs) {
      const on = t === tab;
      t.classList.toggle("active", on);
      t.setAttribute("aria-selected", String(on));
      t.tabIndex = on ? 0 : -1;
    }
    for (const panel of document.querySelectorAll(".tab-panel")) {
      panel.hidden = panel.dataset.panel !== tab.dataset.tab;
    }
  };
  tabs.forEach((tab, i) => {
    tab.addEventListener("click", () => select(tab));
    // Arrow keys move between tabs, as for any ARIA tablist.
    tab.addEventListener("keydown", (event) => {
      const step = event.key === "ArrowRight" ? 1 : event.key === "ArrowLeft" ? -1 : 0;
      if (!step) return;
      const next = tabs[(i + step + tabs.length) % tabs.length];
      select(next);
      next.focus();
    });
  });
}

function parseList(text) {
  return Array.from(
    new Set(
      text
        .split(/[\n,]/)
        .map((s) => s.trim())
        .filter(Boolean)
    )
  );
}

function renderPlanPreview(node, plan, onSeedsChange) {
  clear(node);
  node.hidden = false;
  if (plan.kind !== "dataset") node.appendChild(el("div", { text: `Detected type: ${kindLabel(plan.kind)}` }));
  if (plan.discovery_queries.length) {
    node.appendChild(el("div", { text: "Searches Harvest will run:" }));
    node.appendChild(
      el(
        "div",
        { class: "chip-list" },
        plan.discovery_queries.map((q) => el("span", { class: "chip", text: q }))
      )
    );
  }
  // Opt-in, and only for tools this deployment enabled: these run external binaries that
  // make their own requests outside the fetcher's budget, so nothing is checked by default.
  const offered = (plan.tools || []).filter((t) => toolsEnabled.includes(t.name));
  const toolBoxes = offered.map((t) =>
    el("input", { type: "checkbox", class: "tool-box", "data-tool": t.name, "data-target": t.target })
  );
  // Follow-up crawling and scan breadth are separate decisions from running the tool: the
  // tool's own capture is already the evidence, while crawling what it reports spends the
  // fetcher's request budget, and --all-sites is roughly ten times the outbound requests of
  // a top-sites scan. Defaults match the server's (crawl on, full site database).
  const crawlBox = el("input", { type: "checkbox", class: "tool-crawl" });
  crawlBox.checked = true;
  const breadthSelect = el("select", { class: "tool-breadth" }, [
    el("option", { value: "", text: "All sites (default)" }),
    el("option", { value: "500", text: "Top 500 sites (smaller scan)" }),
    el("option", { value: "100", text: "Top 100 sites (smallest scan)" }),
  ]);
  // A suggested tool this deployment cannot run is named with the reason, not hidden.
  for (const t of (plan.tools || []).filter((t) => !toolsEnabled.includes(t.name))) {
    const detail = (capabilityDetails[t.name] || {}).detail || "not enabled on this deployment";
    node.appendChild(el("div", { class: "hint", text: `Unavailable: ${t.name} for ${t.target} (${detail})` }));
  }
  if (offered.length) {
    node.appendChild(el("label", { text: "External tools (opt-in; each runs outside request budgets)" }));
    node.appendChild(
      el(
        "div",
        { class: "chip-list" },
        offered.map((t, i) => el("label", { class: "chip" }, [toolBoxes[i], ` ${t.name}: ${t.target}`]))
      )
    );
    node.appendChild(
      el("label", { class: "chip" }, [crawlBox, " Follow up on tool findings (fetch the profiles it reports)"])
    );
    if (offered.some((t) => t.name === "maigret")) {
      node.appendChild(el("label", { text: "Maigret scan breadth" }));
      node.appendChild(breadthSelect);
    }
  }
  const seedsLabel = el("label", { text: "Starting web pages (one per line; optional)" });
  const seedsInput = el("textarea", { class: "plan-edit-seeds", rows: "3" });
  seedsInput.value = plan.seeds.join("\n");
  if (onSeedsChange) {
    seedsInput.addEventListener("input", () => onSeedsChange(parseList(seedsInput.value)));
    // Ticking a tool can be what makes the job submittable, so it has to re-run the same
    // check. Without this the Create button stays disabled until the seeds box is touched.
    for (const box of toolBoxes) {
      box.addEventListener("change", () => onSeedsChange(parseList(seedsInput.value)));
    }
  }
  const fieldsLabel = el("label", { text: "Details to collect (comma-separated)" });
  const fieldsInput = el("textarea", { class: "plan-edit-fields", rows: "2" });
  fieldsInput.value = plan.fields.join(", ");
  node.appendChild(seedsLabel);
  node.appendChild(seedsInput);
  node.appendChild(fieldsLabel);
  node.appendChild(fieldsInput);
  return {
    seeds: () => parseList(seedsInput.value),
    fields: () => parseList(fieldsInput.value),
    tools: () =>
      toolBoxes
        .filter((box) => box.checked)
        .map((box) => {
          const run = { name: box.dataset.tool, target: box.dataset.target, crawl: crawlBox.checked };
          // Only maigret reads top_sites; sending it for another tool would be noise.
          if (box.dataset.tool === "maigret" && breadthSelect.value) {
            run.top_sites = Number(breadthSelect.value);
          }
          return run;
        }),
  };
}

// ---- Investigate tab: plain-language labels, presets and the job spec they produce. ----

const KIND_LABELS = {
  email: "Email address",
  phone: "Phone number",
  person: "Person name",
  username: "Username",
  organization: "Organization",
  domain: "Domain",
  url: "Web address",
  address: "Street address",
  identifier: "Other identifier",
};

function kindLabel(kind) {
  return KIND_LABELS[kind] || kind;
}

// What a source is to a user, keyed by capability/tool name. Tool names stay visible only
// in Advanced options and the status view.
const SOURCE_LABELS = {
  discovery_search: "Web discovery",
  fetch: "Website analysis",
  ghunt: "Google account enrichment",
  maigret: "Profile & account discovery",
  spiderfoot: "Linked accounts & breach records",
};

function sourceLabel(name) {
  return SOURCE_LABELS[name] || name;
}

const FIELD_LABELS = {
  full_name: "Full name",
  display_name: "Display name",
  name: "Name",
  email: "Email addresses",
  contact_email: "Contact email",
  phone: "Phone numbers",
  organization: "Organization",
  profile_url: "Profile pages",
  username: "Usernames",
  bio: "Profile bio",
  address: "Addresses",
  formatted_address: "Address",
  website: "Website",
  title: "Page title",
  description: "Description",
};

function fieldLabel(field) {
  if (FIELD_LABELS[field]) return FIELD_LABELS[field];
  const words = String(field).replace(/_/g, " ").trim();
  return words.charAt(0).toUpperCase() + words.slice(1);
}

// Real configuration, not labels: each preset sets the page and time budgets, which offered
// tools run, Maigret's breadth and whether tool findings are followed up. Deep raises
// budgets and breadth only; concurrency is unchanged and the SpiderFoot module set stays
// whatever the deployment allowlisted. Tool runs never exceed the default limits.tool_runs (3).
const PRESETS = {
  quick: {
    label: "Quick",
    requests: 30,
    seconds: 300,
    crawl: false,
    tools: ["ghunt", "maigret"],
    topSites: 100,
  },
  standard: {
    label: "Standard",
    requests: 100,
    seconds: 900,
    crawl: true,
    tools: ["ghunt", "maigret", "spiderfoot"],
    topSites: 500,
  },
  deep: {
    label: "Deep",
    requests: 300,
    seconds: 2700,
    crawl: true,
    tools: ["ghunt", "maigret", "spiderfoot"],
    topSites: null,
  },
};

// The preset's settings for the tools this plan actually offers.
function presetSettings(presetKey, offeredToolNames) {
  const preset = PRESETS[presetKey] || PRESETS.standard;
  return {
    requests: preset.requests,
    seconds: preset.seconds,
    crawl: preset.crawl,
    topSites: preset.topSites,
    tools: (offeredToolNames || []).filter((name) => preset.tools.includes(name)),
  };
}

// Coarse, from the cost notes in capabilities.py: a full Maigret sweep is ~50 MiB of proxy
// traffic, a SpiderFoot email sweep a few minutes of its own requests, GHunt ~100 KiB.
function proxyUsage(requests, tools, topSites) {
  let level = requests >= 300 ? 2 : requests >= 100 ? 1 : 0;
  if (tools.includes("spiderfoot")) level = Math.max(level, 1);
  if (tools.includes("maigret")) {
    level = Math.max(level, topSites == null ? 2 : topSites > 100 ? 1 : 0);
  }
  return ["Low", "Medium", "High"][level];
}

// The time budget is a hard stop for the whole investigation, tool scans included, so
// "up to" is the honest estimate.
function durationLabel(seconds) {
  const minutes = Math.max(1, Math.round(seconds / 60));
  return minutes >= 60 && minutes % 60 === 0
    ? `Up to ${minutes / 60} hour${minutes === 60 ? "" : "s"}`
    : `Up to ${minutes} min`;
}

function sourceCategories(plan, toolNames, caps) {
  const out = [];
  if (plan.discovery_queries && plan.discovery_queries.length && caps && caps.search_configured) {
    out.push(sourceLabel("discovery_search"));
  }
  if (plan.seeds && plan.seeds.length) out.push(sourceLabel("fetch"));
  for (const name of toolNames) out.push(sourceLabel(name));
  return out;
}

// Named after the target so repeat investigations of the same identifier share a dataset.
function autoDatasetName(text) {
  const slug = String(text || "")
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 80)
    .replace(/-+$/, "");
  return slug || "investigation";
}

// The exact JobSpec POST /jobs receives -- the same shape the previous form sent.
function buildInvestigationSpec(opts) {
  const tools = (opts.plan.tools || [])
    .filter((t) => opts.tools.includes(t.name))
    .map((t) => {
      const run = { name: t.name, target: t.target, crawl: opts.crawl };
      // Only maigret reads top_sites; omitted means its full site database.
      if (t.name === "maigret" && opts.topSites) run.top_sites = opts.topSites;
      return run;
    });
  return {
    objective: `Investigate ${opts.plan.normalized}`.slice(0, 4000),
    dataset: opts.dataset || autoDatasetName(opts.plan.normalized),
    mode: "targeted",
    seeds: opts.seeds,
    discovery_queries: opts.plan.discovery_queries,
    fields: opts.fields,
    tools,
    use_model: !!opts.useModel,
    limits: { requests: opts.requests, seconds: opts.seconds },
  };
}

function setText(id, text, muted) {
  const node = document.getElementById(id);
  node.textContent = text;
  node.classList.toggle("muted", !!muted);
}

function initInvestigationForm() {
  const form = document.getElementById("investigation-form");
  const errorNode = document.getElementById("investigation-error");
  const submitButton = document.getElementById("inv-submit");
  const createHintNode = document.getElementById("inv-create-hint");
  const valueInput = document.getElementById("inv-value");
  const detectNode = document.getElementById("inv-detect");
  const kindPicker = document.getElementById("inv-kind-picker");
  const sourcesNode = document.getElementById("inv-sources");
  const fieldsNode = document.getElementById("inv-fields");
  const followup = document.getElementById("inv-followup");
  const followupHint = document.getElementById("inv-followup-hint");
  const breadthRow = document.getElementById("inv-breadth-row");
  const breadth = document.getElementById("inv-breadth");
  const requestsInput = document.getElementById("inv-requests");
  const minutesInput = document.getElementById("inv-minutes");
  const seedsInput = document.getElementById("inv-seeds");
  const fieldsExtra = document.getElementById("inv-fields-extra");
  const datasetInput = document.getElementById("inv-dataset");

  let plan = null;
  let planFor = null;
  let chosenKind = null;
  let customized = false;
  let planSeq = 0;
  let debounce = null;

  const SEEDLESS_HINT =
    "Search is not configured on this deployment, so this investigation needs a starting web page or a source under Advanced options.";

  const preset = () => (form.querySelector('input[name="inv-preset"]:checked') || {}).value || "standard";
  const offeredTools = () => (plan ? (plan.tools || []).filter((t) => toolsEnabled.includes(t.name)) : []);
  const selectedTools = () =>
    Array.from(sourcesNode.querySelectorAll("input.tool-box"))
      .filter((box) => box.checked)
      .map((box) => box.dataset.tool);
  const selectedFields = () =>
    Array.from(new Set([
      ...Array.from(fieldsNode.querySelectorAll("input:checked")).map((box) => box.value),
      ...parseList(fieldsExtra.value),
    ]));
  const minutes = () => parseInt(minutesInput.value, 10);
  const topSites = () => (breadth.value ? Number(breadth.value) : null);

  for (const key of Object.keys(PRESETS)) {
    const meta = form.querySelector(`[data-preset-meta="${key}"]`);
    if (meta) meta.textContent = `${durationLabel(PRESETS[key].seconds)} · up to ${PRESETS[key].requests} pages`;
  }

  function applyPreset() {
    const settings = presetSettings(preset(), offeredTools().map((t) => t.name));
    requestsInput.value = settings.requests;
    minutesInput.value = Math.round(settings.seconds / 60);
    for (const box of sourcesNode.querySelectorAll("input.tool-box")) {
      box.checked = settings.tools.includes(box.dataset.tool);
    }
    breadth.value = settings.topSites == null ? "" : String(settings.topSites);
    // Nothing to follow up without an account check; unchecked so it doesn't read as active.
    followup.checked = settings.crawl && settings.tools.length > 0;
    customized = false;
  }

  function renderSources() {
    clear(sourcesNode);
    sourcesNode.appendChild(el("legend", { text: "Sources" }));
    if (!plan) {
      sourcesNode.appendChild(el("p", { class: "hint", text: "Enter an identifier to see the sources available for it." }));
      breadthRow.hidden = true;
      return;
    }
    const builtin = sourceCategories(plan, [], capabilities);
    if (builtin.length) {
      sourcesNode.appendChild(el("p", { class: "hint", text: `Always included: ${builtin.join(", ")}.` }));
    }
    // Only tools this deployment enabled are offered; the rest are named with the reason.
    for (const t of offeredTools()) {
      const box = el("input", { type: "checkbox", class: "tool-box", "data-tool": t.name, "data-target": t.target });
      box.addEventListener("change", () => {
        customized = true;
        refresh();
      });
      sourcesNode.appendChild(
        el("label", { class: "checkbox" }, [box, ` ${sourceLabel(t.name)} `, el("span", { class: "hint", text: `(${t.name})` })])
      );
    }
    const unavailable = [
      ...(plan.unavailable_tools || []),
      ...(plan.tools || [])
        .filter((t) => !toolsEnabled.includes(t.name))
        .map((t) => ({ name: t.name, detail: (capabilityDetails[t.name] || {}).detail || "not enabled on this deployment" })),
    ];
    for (const t of unavailable) {
      sourcesNode.appendChild(el("p", { class: "hint", text: `Unavailable: ${sourceLabel(t.name)} (${t.name}) — ${t.detail}` }));
    }
    if (!offeredTools().length && !builtin.length) {
      sourcesNode.appendChild(el("p", { class: "hint", text: "No automatic sources for this input; add a starting web page below." }));
    }
    breadthRow.hidden = !offeredTools().some((t) => t.name === "maigret");
  }

  function renderFields() {
    clear(fieldsNode);
    fieldsNode.appendChild(el("legend", { text: "Details to collect" }));
    for (const field of plan ? plan.fields : []) {
      const box = el("input", { type: "checkbox", value: field });
      box.checked = true;
      box.addEventListener("change", refresh);
      fieldsNode.appendChild(el("label", { class: "checkbox" }, [box, ` ${fieldLabel(field)}`]));
    }
  }

  function renderSummary() {
    const tools = selectedTools();
    setText("sum-target", plan ? plan.normalized : valueInput.value.trim() || "Not entered yet", !plan);
    setText("sum-kind", plan ? kindLabel(plan.kind) : "—", !plan);
    setText("sum-depth", PRESETS[preset()].label + (customized ? " (customized)" : ""));
    const fields = selectedFields();
    setText("sum-lookfor", plan && fields.length ? fields.map(fieldLabel).join(", ") : "—", !plan);
    const seeds = parseList(seedsInput.value);
    const sources = plan ? sourceCategories({ ...plan, seeds }, tools, capabilities) : [];
    setText("sum-sources", sources.length ? sources.join(", ") : "—", !sources.length);
    const seconds = (minutes() || 0) * 60;
    setText("sum-duration", seconds ? durationLabel(seconds) : "—");
    setText("sum-proxy", proxyUsage(parseInt(requestsInput.value, 10) || 0, tools, topSites()));
  }

  function refresh() {
    const tools = selectedTools();
    const hasTools = offeredTools().length > 0;
    followup.disabled = !hasTools || !tools.length;
    followupHint.textContent = hasTools
      ? "Visits the profile pages and identifiers that account checks report."
      : "No account checks run for this kind of input, so there is nothing to follow up.";
    renderSummary();
    if (!plan) {
      submitButton.disabled = true;
      createHintNode.hidden = true;
      return;
    }
    if (canCreateInvestigationJob(parseList(seedsInput.value), capabilities, tools)) {
      submitButton.disabled = false;
      createHintNode.hidden = true;
    } else {
      submitButton.disabled = true;
      createHintNode.textContent = SEEDLESS_HINT;
      createHintNode.hidden = false;
    }
  }

  function setPlan(next) {
    plan = next;
    if (!next) planFor = null;
    seedsInput.value = plan ? plan.seeds.join("\n") : "";
    datasetInput.placeholder = plan ? autoDatasetName(plan.normalized) : "";
    renderSources();
    renderFields();
    applyPreset();
    refresh();
  }

  function showDetect(text, tone, withChange) {
    clear(detectNode);
    detectNode.className = `detect${tone ? ` detect-${tone}` : ""}`;
    detectNode.appendChild(document.createTextNode(text));
    if (withChange) {
      detectNode.appendChild(document.createTextNode(" "));
      detectNode.appendChild(
        el("button", {
          type: "button",
          class: "link",
          text: chosenKind ? "Detect automatically" : "Change",
          onclick: () => {
            if (chosenKind) {
              chosenKind = null;
              for (const r of kindPicker.querySelectorAll("input")) r.checked = false;
              kindPicker.hidden = true;
              detect();
            } else {
              kindPicker.hidden = false;
              const current = kindPicker.querySelector(`input[value="${plan ? plan.kind : ""}"]`);
              if (current) current.checked = true;
              (current || kindPicker.querySelector("input")).focus();
            }
          },
        })
      );
    }
  }

  async function detect() {
    const value = valueInput.value.trim();
    const seq = ++planSeq;
    hideError(errorNode);
    if (!value) {
      chosenKind = null;
      kindPicker.hidden = true;
      showDetect("", null, false);
      setPlan(null);
      return;
    }
    try {
      const next = await api("/plan/investigation", { method: "POST", body: { value, kind: chosenKind } });
      if (seq !== planSeq) return; // a newer keystroke owns the form now
      showDetect(chosenKind ? `Treating this as: ${kindLabel(next.kind)}.` : `${kindLabel(next.kind)} detected.`, "ok", true);
      planFor = value;
      setPlan(next);
    } catch (err) {
      if (seq !== planSeq) return;
      setPlan(null);
      if (err.status === 422 && !chosenKind) {
        // The planner only auto-classifies structurally unambiguous input; free text could
        // be a name, organization or address, so the user says which.
        showDetect("Choose what kind of identifier this is.", "ask", false);
        kindPicker.hidden = false;
      } else if (err.status === 422) {
        showDetect(err.message, "error", true);
      } else {
        showError(errorNode, err);
      }
    }
  }

  valueInput.addEventListener("input", () => {
    clearTimeout(debounce);
    debounce = setTimeout(detect, 350);
  });
  kindPicker.addEventListener("change", (event) => {
    chosenKind = event.target.value;
    detect();
  });
  for (const radio of form.querySelectorAll('input[name="inv-preset"]')) {
    radio.addEventListener("change", () => {
      applyPreset();
      refresh();
    });
  }
  for (const input of [requestsInput, minutesInput, breadth, followup]) {
    input.addEventListener("change", () => {
      customized = true;
      refresh();
    });
  }
  for (const input of [seedsInput, fieldsExtra]) input.addEventListener("input", refresh);
  refresh();

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    hideError(errorNode);
    clearTimeout(debounce);
    // Enter can arrive before the debounced detection: plan for exactly what is typed.
    if (!plan || planFor !== valueInput.value.trim()) await detect();
    if (!plan) {
      valueInput.focus();
      return;
    }
    const seeds = parseList(seedsInput.value);
    const tools = selectedTools();
    if (!canCreateInvestigationJob(seeds, capabilities, tools)) {
      refresh();
      showError(errorNode, new Error(SEEDLESS_HINT));
      return;
    }
    const dataset = datasetInput.value.trim();
    if (dataset && !datasetInput.checkValidity()) {
      document.getElementById("inv-advanced").open = true;
      showError(errorNode, new Error("Investigation name may only use letters, digits, - and _."));
      datasetInput.focus();
      return;
    }
    const requests = parseInt(requestsInput.value, 10);
    if (!(requests >= 1) || !(minutes() >= 1)) {
      document.getElementById("inv-advanced").open = true;
      showError(errorNode, new Error("Pages to visit and time limit must be at least 1."));
      return;
    }
    const spec = buildInvestigationSpec({
      plan,
      dataset,
      seeds,
      fields: selectedFields(),
      tools,
      crawl: followup.checked,
      topSites: topSites(),
      useModel: document.getElementById("inv-model").checked,
      requests,
      seconds: minutes() * 60,
    });
    submitButton.disabled = true;
    try {
      const job = await api("/jobs", { method: "POST", body: spec });
      location.hash = `#/jobs/${job.id}`;
    } catch (err) {
      showError(errorNode, err);
      refresh();
    }
  });

}

function initDatasetForm() {
  const form = document.getElementById("dataset-form");
  const errorNode = document.getElementById("dataset-error");
  const previewNode = document.getElementById("ds-preview-result");
  const submitButton = document.getElementById("ds-submit");
  let currentPlan = null;
  let currentEditor = null;

  async function preview() {
    hideError(errorNode);
    const description = document.getElementById("ds-description").value.trim();
    if (!description) return;
    try {
      currentPlan = await api("/plan/dataset", { method: "POST", body: { description } });
      currentEditor = renderPlanPreview(previewNode, currentPlan);
      submitButton.disabled = false;
    } catch (err) {
      currentPlan = null;
      currentEditor = null;
      submitButton.disabled = true;
      showError(errorNode, err);
    }
  }

  document.getElementById("ds-preview").addEventListener("click", preview);
  document.getElementById("ds-description").addEventListener("change", () => {
    submitButton.disabled = true;
    currentPlan = null;
    currentEditor = null;
  });

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    hideError(errorNode);
    if (!currentPlan) {
      await preview();
      if (!currentPlan) return;
    }
    const dataset =
      document.getElementById("ds-dataset").value.trim() || autoDatasetName(currentPlan.normalized);
    const spec = {
      objective: `Build dataset: ${currentPlan.normalized}`.slice(0, 4000),
      dataset,
      mode: "enumerative",
      seeds: currentEditor.seeds(),
      discovery_queries: currentPlan.discovery_queries,
      fields: currentEditor.fields(),
      tools: currentEditor.tools(),
      limits: limitsFrom("ds-requests"),
    };
    try {
      const job = await api("/jobs", { method: "POST", body: spec });
      location.hash = `#/jobs/${job.id}`;
    } catch (err) {
      showError(errorNode, err);
    }
  });
}

function initRerunForm() {
  const form = document.getElementById("rerun-form");
  const errorNode = document.getElementById("rerun-error");
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    hideError(errorNode);
    const jobId = document.getElementById("rerun-job-id").value.trim();
    try {
      const job = await api(`/jobs/${encodeURIComponent(jobId)}/rerun`, { method: "POST" });
      location.hash = `#/jobs/${job.id}`;
    } catch (err) {
      showError(errorNode, err);
    }
  });
}

function initContinuousForm() {
  const form = document.getElementById("continuous-form");
  const errorNode = document.getElementById("continuous-error");
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    hideError(errorNode);
    const value = document.getElementById("cont-value").value.trim();
    const kind = document.getElementById("cont-kind").value || null;
    const interval = parseInt(document.getElementById("cont-interval-hours").value, 10) * 3600;
    try {
      const plan = await api("/plan/investigation", { method: "POST", body: { value, kind } });
      const dataset = document.getElementById("cont-dataset").value.trim() || autoDatasetName(plan.normalized);
      const spec = {
        objective: `Continuously monitor ${plan.normalized}`.slice(0, 4000),
        dataset,
        mode: "continuous",
        refresh_seconds: interval,
        seeds: plan.seeds,
        discovery_queries: plan.discovery_queries,
        fields: plan.fields,
        limits: limitsFrom("cont-requests"),
      };
      const job = await api("/jobs", { method: "POST", body: spec });
      location.hash = `#/jobs/${job.id}`;
    } catch (err) {
      showError(errorNode, err);
    }
  });
}

async function renderJobsList() {
  const tbody = document.querySelector("#jobs-table tbody");
  clear(tbody);
  const jobs = await api("/jobs");
  for (const job of jobs) {
    tbody.appendChild(
      el("tr", {}, [
        el("td", {}, [el("a", { href: `#/jobs/${job.id}`, text: job.id })]),
        el("td", {}, [statusBadge(job.status)]),
        el("td", { text: new Date(job.created * 1000).toLocaleString() }),
        el("td", { text: job.reason || "" }),
      ])
    );
  }
}

function renderCandidate(candidate) {
  const metaLine = el("div", { class: "hint" }, [
    `method ${candidate.method}, confidence ${candidate.confidence}, `,
    "capture ",
  ]);
  metaLine.appendChild(captureLinks(candidate.capture_ids));
  metaLine.appendChild(
    document.createTextNode(
      `, extraction ${JSON.stringify(candidate.extraction_ids)}, observation ${candidate.observation_id}`
    )
  );
  return el("li", {}, [
    el("span", { class: "mono", text: JSON.stringify(candidate.value) }),
    el("span", { text: " via " }),
    safeLink(candidate.source_url, candidate.source_url),
    metaLine,
    el("div", { class: "hint mono", text: `evidence: "${candidate.evidence}" at ${candidate.locator}` }),
  ]);
}

function renderRecords(node, records, missingFields) {
  clear(node);
  if (missingFields && missingFields.length) {
    node.appendChild(
      el("p", { class: "warning", text: `Missing requested fields (job-wide): ${missingFields.join(", ")}` })
    );
  }
  if (!records.length) {
    node.appendChild(el("p", { class: "hint", text: "No records yet." }));
    return;
  }
  for (const record of records) {
    const card = el("div", { class: "entity-card" }, [
      el("h3", { class: "mono", text: record.entity_key }),
    ]);
    const fieldNames = Object.keys(record.fields).sort();
    for (const name of fieldNames) {
      const field = record.fields[name];
      let valueNode;
      if (field.missing) {
        valueNode = el("span", { class: "field-missing", text: "(missing)" });
      } else if (field.conflict) {
        valueNode = el("span", { class: "field-conflict", text: "CONFLICT (no value chosen)" });
      } else {
        valueNode = el("span", { class: "field-value mono", text: JSON.stringify(field.value) });
      }
      card.appendChild(
        el("div", { class: "field-row" }, [el("span", { class: "field-name", text: name }), valueNode])
      );
      if (!field.missing) {
        const n = field.candidates.length;
        card.appendChild(
          el("details", {}, [
            el("summary", {
              class: "hint",
              text: `evidence: ${n} source${n === 1 ? "" : "s"}${field.via ? ` (from ${field.via})` : ""}`,
            }),
            el("ul", { class: "candidates" }, field.candidates.map(renderCandidate)),
          ])
        );
      }
    }
    node.appendChild(card);
  }
}

function renderOverview(node, overview, jobId, active) {
  clear(node);
  const head = `${overview.accounts.length} reported account(s), ${overview.verified_pages} page(s) naming the identifier, requests ${overview.requests}`;
  node.appendChild(el("p", { text: head }));
  if (overview.accounts.length) {
    const rows = overview.accounts.map((a) => {
      const action = el("td");
      // Only pages never fetched: a robots.txt or HTTP block would just be blocked again.
      if (!active && /^unchecked: (not_fetched|cancelled|pending)/.test(a.page_check) && /^https?:/.test(a.url)) {
        action.appendChild(
          el("button", {
            class: "secondary",
            text: "Follow up",
            onclick: async () => {
              try {
                const child = await api(`/jobs/${encodeURIComponent(jobId)}/followup`, {
                  method: "POST",
                  body: { url: a.url },
                });
                location.hash = `#/jobs/${child.id}`;
              } catch (err) {
                action.textContent = err.message;
              }
            },
          })
        );
      }
      return el("tr", {}, [
        el("td", { text: a.site || "" }),
        el("td", {}, [safeLink(a.url, a.url)]),
        el("td", { text: a.existence || "" }),
        el("td", { class: a.page_check === "profile_evidence" ? "" : "hint", text: a.page_check }),
        el("td", { text: a.ownership || "" }),
        el("td", { class: "mono", text: a.display_name ? JSON.stringify(a.display_name) : "" }),
        action,
      ]);
    });
    node.appendChild(
      el("table", {}, [
        el("thead", {}, [
          el("tr", {}, ["Site", "URL", "Exists (tool)", "Page check", "Ownership", "Display name", ""].map((h) => el("th", { text: h }))),
        ]),
        el("tbody", {}, rows),
      ])
    );
  }
  if (overview.unknowns.length) {
    node.appendChild(el("ul", {}, overview.unknowns.map((u) => el("li", { class: "hint", text: `Unknown: ${u}` }))));
  }
}

function renderProgress(node, job) {
  clear(node);
  const groups = [
    ["Tasks", job.progress],
    ["Extractions", job.extraction_progress],
  ];
  for (const [label, counts] of groups) {
    const entries = Object.entries(counts || {});
    const row = el("div", { class: "stat-row" }, [el("span", { class: "hint", text: `${label}:` })]);
    if (!entries.length) {
      row.appendChild(el("span", { class: "hint", text: "none yet" }));
    }
    for (const [key, value] of entries) {
      row.appendChild(el("span", { class: "stat-chip", text: `${key}: ${value}` }));
    }
    node.appendChild(row);
  }
  const limits = (job.spec && job.spec.limits) || {};
  const recordsLine =
    `records processed: ${job.records_processed}` + (limits.records ? ` / ${limits.records}` : "");
  const claimsLine =
    `claims processed: ${job.claims_processed}` + (limits.claims ? ` / ${limits.claims}` : "");
  node.appendChild(el("div", { class: "hint", text: recordsLine }));
  node.appendChild(el("div", { class: "hint", text: claimsLine }));
}

async function renderSources(node, jobId) {
  clear(node);
  const [captures, extractions] = await Promise.all([
    api(`/jobs/${encodeURIComponent(jobId)}/captures?limit=1000`),
    api(`/jobs/${encodeURIComponent(jobId)}/extractions?limit=1000`),
  ]);
  if (!captures.length) {
    node.appendChild(el("p", { class: "hint", text: "No sources acquired yet." }));
    return;
  }
  const latestExtractionByCapture = new Map();
  for (const x of extractions) {
    const existing = latestExtractionByCapture.get(x.capture_id);
    if (!existing || x.id > existing.id) latestExtractionByCapture.set(x.capture_id, x);
  }
  const table = el("table", {}, [
    el("thead", {}, [
      el(
        "tr",
        {},
        ["Capture", "URL", "HTTP", "Changed", "Extraction", "Records", "Warnings"].map((h) =>
          el("th", { text: h })
        )
      ),
    ]),
  ]);
  const tbody = el("tbody");
  for (const capture of captures) {
    const extraction = latestExtractionByCapture.get(capture.id);
    const recordsCell =
      extraction && extraction.records_total != null
        ? `${extraction.records_processed || 0} / ${extraction.records_total}`
        : extraction && extraction.records_processed != null
          ? String(extraction.records_processed)
          : "";
    const captureCell = el("span", {}, [
      el("a", { href: `/captures/${capture.id}`, title: "capture metadata (authenticated)", text: `#${capture.id}` }),
      " ",
      el("a", { href: `/captures/${capture.id}/body`, title: "raw captured body (authenticated)", text: "body" }),
    ]);
    tbody.appendChild(
      el("tr", {}, [
        el("td", {}, [captureCell]),
        el("td", {}, [safeLink(capture.url, capture.url)]),
        el("td", { text: String(capture.status) }),
        el("td", { text: capture.changed ? "yes" : "no" }),
        el("td", {}, [statusBadge(extraction ? extraction.status : "not_scheduled")]),
        el("td", { text: recordsCell }),
        el("td", { text: extraction && extraction.had_warnings ? "yes" : "" }),
      ])
    );
  }
  table.appendChild(tbody);
  node.appendChild(table);
}

function eventSeverity(type) {
  if (["failed", "blocked"].includes(type)) return "severity-failed";
  if (["deferred", "extraction_limit", "evidence_omitted", "frontier_limit", "lease_expired", "task_done"].includes(type)) return "severity-warn";
  if (["budget_exhausted", "plateau", "cancelled"].includes(type)) return "severity-stopped";
  return null;
}

function describeEvent(event) {
  const d = event.details || {};
  if (event.type === "task_done" && d.partial) return `Task ${d.task} kept partial results: ${d.partial}`;
  switch (event.type) {
    case "failed":
      return `Task ${d.task} failed: ${d.reason || "unknown reason"}`;
    case "blocked":
      return `Task ${d.task} blocked: ${d.reason || "policy denied"}`;
    case "deferred":
      return `Task ${d.task} deferred, retrying in ${typeof d.delay === "number" ? d.delay.toFixed(1) : "?"}s: ${d.reason || ""}`;
    case "lease_expired":
      return `Task ${d.task} lease expired; now ${d.status}`;
    case "evidence_omitted":
      return `Value omitted on task ${d.task} (no field-level evidence): ${d.reason || ""}`;
    case "extraction_limit":
      return `Extraction limit on task ${d.task}: ${d.reason || ""}`;
    case "frontier_limit":
      return `Task frontier limit reached (${d.limit})`;
    case "tool_started":
      return `Running ${d.tool} (task ${d.task}) for up to ${d.max_seconds}s; the job waits for it`;
    case "budget_exhausted":
      return `Job stopped: budget exhausted${d.reason ? ` (${d.reason})` : ""}`;
    case "plateau":
      return `Job stopped: no novel observations${d.reason ? ` (${d.reason})` : ""}`;
    case "cancelled":
      return `Job cancelled${d.reason ? `: ${d.reason}` : ""}`;
    default:
      return null;
  }
}

function renderWarnings(node, events) {
  clear(node);
  const rows = events.filter((e) => describeEvent(e) !== null);
  if (!rows.length) {
    node.appendChild(el("li", { class: "hint", text: "No warnings or failures." }));
    return;
  }
  for (const event of rows.slice().reverse()) {
    const severity = eventSeverity(event.type);
    node.appendChild(
      el("li", { class: severity }, [
        el("span", { class: "mono", text: `${new Date(event.at * 1000).toLocaleTimeString()} ` }),
        el("strong", { text: `${event.type}: ` }),
        el("span", { text: describeEvent(event) }),
      ])
    );
  }
}

let jobPollTimer = null;

function stopJobPolling() {
  if (jobPollTimer) {
    clearTimeout(jobPollTimer);
    jobPollTimer = null;
  }
}

async function renderJobDetail(jobId) {
  stopJobPolling();
  const title = document.getElementById("job-detail-title");
  const summary = document.getElementById("job-summary");
  const actions = document.getElementById("job-actions");
  const progressNode = document.getElementById("job-progress");
  const sourcesNode = document.getElementById("job-sources");
  const warningsNode = document.getElementById("job-warnings");
  const recordsNode = document.getElementById("job-records");
  const eventsNode = document.getElementById("job-events");
  title.textContent = `Job ${jobId}`;
  clear(summary);
  clear(actions);
  clear(eventsNode);

  const job = await api(`/jobs/${encodeURIComponent(jobId)}`);
  const active = ACTIVE_STATUSES.includes(job.status);
  title.textContent = job.spec.objective;
  summary.appendChild(
    el("div", {}, [
      statusBadge(job.status),
      el("span", { class: "hint", text: ` · ${job.spec.dataset} · job ` }),
      el("span", { class: "hint mono", text: jobId }),
      active ? el("span", { class: "hint", text: " · auto-refreshing" }) : null,
    ])
  );
  const requestLimit = job.spec && job.spec.limits ? job.spec.limits.requests : null;
  summary.appendChild(
    el("div", { class: "hint" }, [
      `requests ${job.requests}${requestLimit ? `/${requestLimit}` : ""}, ` +
        `captures ${job.captures}, cost $${job.cost_reserved_usd.toFixed(4)}`,
    ])
  );
  // "budget_exhausted" is a stop reason, not a failure, and the distinction is invisible
  // unless the exhausted budget is named: the crawl was truncated, not completed.
  if (job.status === "budget_exhausted") {
    summary.appendChild(
      el("div", { class: "warning" }, [
        `Stopped early: budget exhausted${job.reason ? ` (${job.reason})` : ""}. Results are partial` +
          (job.stop_advice ? `; ${job.stop_advice}.` : "."),
      ])
    );
  }
  // Nothing is "missing" before the job has had a chance to find it.
  if (!active && job.missing_fields && job.missing_fields.length) {
    summary.appendChild(
      el("div", { class: "warning", text: `Missing requested fields: ${job.missing_fields.join(", ")}` })
    );
  }

  if (active) {
    actions.appendChild(
      el("button", {
        class: "danger",
        text: "Cancel",
        onclick: async () => {
          await api(`/jobs/${encodeURIComponent(jobId)}/cancel`, { method: "POST" });
          renderJobDetail(jobId);
        },
      })
    );
  }
  if (job.spec.mode !== "continuous") {
    actions.appendChild(
      el("button", {
        class: "secondary",
        text: "Rerun",
        onclick: async () => {
          const rerun = await api(`/jobs/${encodeURIComponent(jobId)}/rerun`, { method: "POST" });
          location.hash = `#/jobs/${rerun.id}`;
        },
      })
    );
  }
  actions.appendChild(el("a", { href: `/jobs/${encodeURIComponent(jobId)}/export`, text: "Download JSONL" }));
  actions.appendChild(el("a", { href: `/jobs/${encodeURIComponent(jobId)}/export.csv`, text: "Download CSV" }));

  renderProgress(progressNode, job);

  // ponytail: one page of 1000 tasks; limits.tasks defaults to 300, page with `after` if raised.
  const [records, events, overview, tasks] = await Promise.all([
    api(`/jobs/${encodeURIComponent(jobId)}/records`),
    api(`/jobs/${encodeURIComponent(jobId)}/events?limit=200`),
    api(`/jobs/${encodeURIComponent(jobId)}/summary`),
    api(`/jobs/${encodeURIComponent(jobId)}/tasks?limit=1000`),
  ]);
  renderStages(document.getElementById("job-stages"), deriveStages(job, tasks));
  renderCounters(document.getElementById("job-counters"), job, overview, records);
  renderOverview(document.getElementById("job-overview"), overview, jobId, active);
  renderRecords(recordsNode, records, job.missing_fields);
  renderWarnings(warningsNode, events);
  await renderSources(sourcesNode, jobId);

  for (const event of events.slice(-50).reverse()) {
    eventsNode.appendChild(
      el("li", {}, [
        el("span", { class: "mono", text: `${new Date(event.at * 1000).toLocaleTimeString()} ` }),
        el("strong", { text: event.type }),
        el("span", { class: "hint mono", text: ` ${JSON.stringify(event.details)}` }),
      ])
    );
  }

  if (active) {
    jobPollTimer = setTimeout(() => {
      renderJobDetail(jobId).catch((err) => console.error(err));
    }, JOB_POLL_MS);
  }
}

async function renderSchedules() {
  const tbody = document.querySelector("#schedules-table tbody");
  clear(tbody);
  const schedules = await api("/schedules");
  for (const schedule of schedules) {
    const row = el("tr", {}, [
      el("td", {}, [el("a", { href: `#/jobs/${schedule.id}`, text: schedule.id })]),
      el("td", { text: String(schedule.interval) }),
      el("td", { text: new Date(schedule.next_run * 1000).toLocaleString() }),
      el("td", {}, [el("a", { href: `#/jobs/${schedule.last_job}`, text: schedule.last_job })]),
      el("td", { text: schedule.enabled ? "enabled" : "disabled" }),
    ]);
    const actionCell = el("td", {});
    if (schedule.enabled) {
      actionCell.appendChild(
        el("button", {
          class: "secondary",
          text: "Disable",
          onclick: async () => {
            await api(`/schedules/${encodeURIComponent(schedule.id)}/disable`, { method: "POST" });
            renderSchedules();
          },
        })
      );
    }
    row.appendChild(actionCell);
    tbody.appendChild(row);
  }
}

// ---- Job progress: plain-language stages derived from the job's real tasks. ----

const STAGES = [
  ["prepare", "Preparing investigation"],
  ["public", "Checking public sources"],
  ["discover", "Discovering profiles"],
  ["enrich", "Enriching identifiers"],
  ["correlate", "Correlating findings"],
  ["report", "Building report"],
];
const ENRICH_TOOLS = ["ghunt", "spiderfoot"];

// Which stage a task belongs to. Fetches descended from a tool run are the profile pages
// that tool reported; every other fetch or search is a public-source check.
function taskStage(task, byId) {
  if (task.kind === "tool") {
    return ENRICH_TOOLS.includes(String(task.key).split(":")[0]) ? "enrich" : "discover";
  }
  if (task.kind === "extract") return "correlate";
  if (task.kind === "reason") return "report";
  for (let parent = byId.get(task.parent); parent; parent = byId.get(parent.parent)) {
    if (parent.kind === "tool") return "discover";
  }
  return "public";
}

// Each stage is done / active / waiting / skipped / failed, from task statuses only. A stage
// with no tasks is shown only if this job's spec says it will have some.
function deriveStages(job, tasks) {
  const spec = job.spec || {};
  const active = ACTIVE_STATUSES.includes(job.status);
  const toolNames = (spec.tools || []).map((t) => t.name);
  const expected = {
    prepare: true,
    public: !!((spec.seeds || []).length || (spec.discovery_queries || []).length),
    discover:
      toolNames.some((n) => !ENRICH_TOOLS.includes(n)) || (spec.tools || []).some((t) => t.crawl),
    enrich: toolNames.some((n) => ENRICH_TOOLS.includes(n)),
    correlate: true,
    report: true,
  };
  const byId = new Map(tasks.map((t) => [t.id, t]));
  const groups = {};
  for (const task of tasks) (groups[taskStage(task, byId)] ||= []).push(task);

  const stages = [];
  for (const [key, label] of STAGES) {
    const group = groups[key] || [];
    const open = group.filter((t) => t.status === "pending" || t.status === "running").length;
    const done = group.filter((t) => t.status === "done").length;
    let state;
    if (key === "prepare") {
      state = job.status === "queued" ? "active" : "done";
    } else if (key === "report" && !open) {
      // The report is the job reaching a terminal status, whatever the last task was.
      state = !active ? (job.status === "failed" || job.status === "cancelled" ? "failed" : "done")
        : stages.every((s) => s.state === "done" || s.state === "skipped") ? "active" : "waiting";
    } else if (!group.length) {
      if (!expected[key]) continue;
      state = active ? "waiting" : "skipped";
    } else if (job.status === "queued") {
      state = "waiting"; // tasks exist but no worker has picked the job up yet
    } else if (open) {
      state = "active";
    } else {
      state = done ? "done" : "failed";
    }
    const failed = group.length - open - done;
    stages.push({ key, label, state, total: group.length, done, failed });
  }
  return stages;
}

const STAGE_STATE_TEXT = {
  done: "Done",
  active: "In progress",
  waiting: "Waiting",
  skipped: "Not reached",
  failed: "Did not complete",
};

function renderStages(node, stages) {
  clear(node);
  for (const stage of stages) {
    const detail = stage.total
      ? ` · ${stage.done} of ${stage.total} step${stage.total === 1 ? "" : "s"}` +
        (stage.failed ? ` (${stage.failed} failed)` : "")
      : "";
    node.appendChild(
      el("li", { class: `stage stage-${stage.state}`, "aria-current": stage.state === "active" ? "step" : null }, [
        el("span", { class: "stage-dot", "aria-hidden": "true" }),
        el("span", { class: "stage-label", text: stage.label }),
        el("span", { class: "stage-state", text: STAGE_STATE_TEXT[stage.state] + detail }),
      ])
    );
  }
}

function renderCounters(node, job, overview, records) {
  clear(node);
  const limits = (job.spec && job.spec.limits) || {};
  const end = job.finished || Date.now() / 1000;
  const elapsed = job.started ? Math.max(0, Math.round(end - job.started)) : 0;
  const counters = [
    ["Pages visited", `${job.requests}${limits.requests ? ` / ${limits.requests}` : ""}`],
    ["Sources captured", String(job.captures)],
    ["Accounts reported", String(overview.accounts.length)],
    ["Pages naming the target", String(overview.verified_pages)],
    ["Records", String(records.length)],
    ["Elapsed", `${Math.floor(elapsed / 60)}m ${String(elapsed % 60).padStart(2, "0")}s`],
  ];
  for (const [label, value] of counters) {
    node.appendChild(el("div", { class: "counter" }, [el("span", { class: "counter-value", text: value }), el("span", { class: "counter-label", text: label })]));
  }
}

// ---- System status: the header indicator and the #/status details view. ----

function systemIssues(meta) {
  const caps = meta.capabilities || {};
  const issues = [];
  if (!meta.search_configured) {
    issues.push(
      "Web discovery is not configured: only web addresses and domains can be investigated without a starting page." +
        (caps.discovery_search && caps.discovery_search.detail ? ` (${caps.discovery_search.detail})` : "")
    );
  }
  const sf = caps.spiderfoot;
  if (sf && !sf.ready && sf.detail && sf.detail !== "not in HARVEST_TOOLS") {
    issues.push(`${sourceLabel("spiderfoot")} cannot run: ${sf.detail}.`);
  }
  return issues;
}

function renderSystemIndicator(meta) {
  const link = document.getElementById("system-status");
  const issues = systemIssues(meta);
  link.hidden = false;
  link.classList.toggle("sys-warn", issues.length > 0);
  document.getElementById("system-status-text").textContent = issues.length ? "Needs attention" : "System ready";
  link.title = issues.length ? issues.join(" ") : "All configured sources are ready";
}

function renderStatusView(meta) {
  const list = document.getElementById("status-list");
  clear(list);
  const caps = meta.capabilities || {};
  const row = (ok, name, detail) =>
    list.appendChild(
      el("li", { class: ok ? "ok" : "bad" }, [
        el("span", { class: "dot", "aria-hidden": "true" }),
        el("strong", { text: name }),
        el("span", { class: "hint", text: ` ${ok ? "Ready" : "Unavailable"}${detail ? ` — ${detail}` : ""}` }),
      ])
    );
  row(true, "Harvest API", `version ${meta.version}`);
  row(!!meta.search_configured, `${sourceLabel("discovery_search")} (search)`, caps.discovery_search && caps.discovery_search.detail);
  row(!!meta.model_configured, "AI reasoning (model)", meta.model_configured ? "" : "not configured");
  for (const name of ["ghunt", "maigret", "spiderfoot"]) {
    const cap = caps[name];
    if (!cap) continue;
    let detail = cap.detail;
    if (name === "spiderfoot" && cap.ready && Array.isArray(cap.modules)) {
      detail = `${cap.modules.length} modules enabled, egress ${cap.egress}`;
    }
    row(!!cap.ready, `${sourceLabel(name)} (${name})`, detail);
  }
  for (const issue of systemIssues(meta)) list.appendChild(el("li", { class: "bad hint", text: issue }));

  const body = document.getElementById("status-modules-body");
  clear(body);
  const sf = caps.spiderfoot;
  document.getElementById("status-modules").hidden = !(sf && Array.isArray(sf.modules) && sf.modules.length);
  if (sf && sf.plans) {
    for (const [type, plan] of Object.entries(sf.plans)) {
      body.appendChild(el("p", { class: "hint", text: `${type}: ${plan.modules.length} module(s) run — ${plan.modules.join(", ") || "none"}` }));
    }
  }
  if (sf && Array.isArray(sf.modules)) {
    body.appendChild(el("div", { class: "chip-list" }, sf.modules.map((m) => el("span", { class: "chip mono", text: m }))));
  }
}

async function route() {
  stopJobPolling();
  const authenticated = await checkAuthAndConfig();
  if (!authenticated) {
    showView("login-view");
    return;
  }
  const hash = location.hash || "#/launch";
  const jobMatch = hash.match(/^#\/jobs\/(.+)$/);
  if (hash === "#/jobs") {
    setNavActive("#/jobs");
    showView("jobs-view");
    await renderJobsList();
  } else if (jobMatch) {
    setNavActive("#/jobs");
    showView("job-detail-view");
    await renderJobDetail(decodeURIComponent(jobMatch[1]));
  } else if (hash === "#/status") {
    setNavActive("#/status");
    showView("status-view");
    renderStatusView(lastMeta);
  } else if (hash === "#/schedules") {
    setNavActive("#/schedules");
    showView("schedules-view");
    await renderSchedules();
  } else {
    setNavActive("#/launch");
    showView("launch-view");
  }
}

function init() {
  initLogin();
  initLaunchTabs();
  initInvestigationForm();
  initDatasetForm();
  initRerunForm();
  initContinuousForm();
  window.addEventListener("hashchange", () => route().catch((err) => console.error(err)));
  ticketLogin()
    .then(route)
    .catch((err) => console.error(err));
}

// `harvest login-link` opens /#login=<ticket>. Drop it from the address bar and history
// before anything else, then trade it for a session cookie; a stale ticket just falls
// through to the normal sign-in form.
async function ticketLogin() {
  const match = location.hash.match(/^#login=(.+)$/);
  if (!match) return;
  history.replaceState(null, "", location.pathname + "#/launch");
  try {
    await doLogin(match[1]);
  } catch {
    showError(document.getElementById("login-error"), new Error("Sign-in link expired or invalid; run harvest-ui again."));
  }
}

if (typeof document !== "undefined") {
  document.addEventListener("DOMContentLoaded", init);
}

if (typeof module !== "undefined" && module.exports) {
  module.exports = {
    api,
    formatApiErrorDetail,
    canCreateInvestigationJob,
    modelCapabilityState,
    kindLabel,
    sourceLabel,
    fieldLabel,
    PRESETS,
    presetSettings,
    proxyUsage,
    durationLabel,
    sourceCategories,
    autoDatasetName,
    buildInvestigationSpec,
    deriveStages,
    systemIssues,
  };
}
