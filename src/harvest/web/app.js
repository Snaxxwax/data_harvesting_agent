"use strict";

const ACTIVE_STATUSES = ["queued", "running"];
const JOB_POLL_MS = 2000;

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
    const detail = isJson && data && data.detail ? data.detail : response.statusText;
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

const views = ["login-view", "launch-view", "jobs-view", "job-detail-view", "schedules-view"];

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

async function checkAuthAndConfig() {
  try {
    const meta = await api("/meta");
    document.getElementById("topnav").hidden = false;
    document.getElementById("logout").hidden = false;
    const warning = document.getElementById("config-warning");
    if (!meta.search_configured) {
      warning.textContent = "Search is not configured: only URL/domain seeds will work.";
      warning.hidden = false;
    } else {
      warning.hidden = true;
    }
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
  const tabs = document.querySelectorAll("#launch-tabs .tab");
  for (const tab of tabs) {
    tab.addEventListener("click", () => {
      for (const t of tabs) t.classList.toggle("active", t === tab);
      const name = tab.dataset.tab;
      for (const panel of document.querySelectorAll(".tab-panel")) {
        panel.hidden = panel.dataset.panel !== name;
      }
    });
  }
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

function renderPlanPreview(node, plan) {
  clear(node);
  node.hidden = false;
  node.appendChild(el("div", { text: `Detected type: ${plan.kind}` }));
  if (plan.discovery_queries.length) {
    node.appendChild(el("div", { text: "Discovery queries:" }));
    node.appendChild(
      el(
        "div",
        { class: "chip-list" },
        plan.discovery_queries.map((q) => el("span", { class: "chip", text: q }))
      )
    );
  }
  const seedsLabel = el("label", { text: "Seed URLs (editable, one per line; no search required)" });
  const seedsInput = el("textarea", { class: "plan-edit-seeds", rows: "3" });
  seedsInput.value = plan.seeds.join("\n");
  const fieldsLabel = el("label", { text: "Fields (editable, comma-separated)" });
  const fieldsInput = el("textarea", { class: "plan-edit-fields", rows: "2" });
  fieldsInput.value = plan.fields.join(", ");
  node.appendChild(seedsLabel);
  node.appendChild(seedsInput);
  node.appendChild(fieldsLabel);
  node.appendChild(fieldsInput);
  return {
    seeds: () => parseList(seedsInput.value),
    fields: () => parseList(fieldsInput.value),
  };
}

function initInvestigationForm() {
  const form = document.getElementById("investigation-form");
  const errorNode = document.getElementById("investigation-error");
  const previewNode = document.getElementById("inv-preview-result");
  const submitButton = document.getElementById("inv-submit");
  let currentPlan = null;
  let currentEditor = null;

  async function preview() {
    hideError(errorNode);
    const value = document.getElementById("inv-value").value.trim();
    const kind = document.getElementById("inv-kind").value || null;
    if (!value) return;
    try {
      currentPlan = await api("/plan/investigation", { method: "POST", body: { value, kind } });
      currentEditor = renderPlanPreview(previewNode, currentPlan);
      submitButton.disabled = false;
    } catch (err) {
      currentPlan = null;
      currentEditor = null;
      submitButton.disabled = true;
      showError(errorNode, err);
    }
  }

  document.getElementById("inv-preview").addEventListener("click", preview);
  for (const field of ["inv-value", "inv-kind"]) {
    document.getElementById(field).addEventListener("change", () => {
      submitButton.disabled = true;
      currentPlan = null;
      currentEditor = null;
    });
  }

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    hideError(errorNode);
    if (!currentPlan) {
      await preview();
      if (!currentPlan) return;
    }
    const dataset = document.getElementById("inv-dataset").value.trim();
    const useModel = document.getElementById("inv-model").checked;
    const spec = {
      objective: `Investigate ${currentPlan.normalized}`.slice(0, 4000),
      dataset,
      mode: "targeted",
      seeds: currentEditor.seeds(),
      discovery_queries: currentPlan.discovery_queries,
      fields: currentEditor.fields(),
      use_model: useModel,
    };
    try {
      const job = await api("/jobs", { method: "POST", body: spec });
      location.hash = `#/jobs/${job.id}`;
    } catch (err) {
      showError(errorNode, err);
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
    const dataset = document.getElementById("ds-dataset").value.trim();
    const spec = {
      objective: `Build dataset: ${currentPlan.normalized}`.slice(0, 4000),
      dataset,
      mode: "enumerative",
      seeds: currentEditor.seeds(),
      discovery_queries: currentPlan.discovery_queries,
      fields: currentEditor.fields(),
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
    const dataset = document.getElementById("cont-dataset").value.trim();
    const interval = parseInt(document.getElementById("cont-interval").value, 10);
    try {
      const plan = await api("/plan/investigation", { method: "POST", body: { value, kind } });
      const spec = {
        objective: `Continuously monitor ${plan.normalized}`.slice(0, 4000),
        dataset,
        mode: "continuous",
        refresh_seconds: interval,
        seeds: plan.seeds,
        discovery_queries: plan.discovery_queries,
        fields: plan.fields,
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
        card.appendChild(el("ul", { class: "candidates" }, field.candidates.map(renderCandidate)));
      }
    }
    node.appendChild(card);
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
  if (["deferred", "extraction_limit", "frontier_limit", "lease_expired"].includes(type)) return "severity-warn";
  if (["budget_exhausted", "plateau", "cancelled"].includes(type)) return "severity-stopped";
  return null;
}

function describeEvent(event) {
  const d = event.details || {};
  switch (event.type) {
    case "failed":
      return `Task ${d.task} failed: ${d.reason || "unknown reason"}`;
    case "blocked":
      return `Task ${d.task} blocked: ${d.reason || "policy denied"}`;
    case "deferred":
      return `Task ${d.task} deferred, retrying in ${typeof d.delay === "number" ? d.delay.toFixed(1) : "?"}s: ${d.reason || ""}`;
    case "lease_expired":
      return `Task ${d.task} lease expired; now ${d.status}`;
    case "extraction_limit":
      return `Extraction limit on task ${d.task}: ${d.reason || ""}`;
    case "frontier_limit":
      return `Task frontier limit reached (${d.limit})`;
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
  summary.appendChild(
    el("div", {}, [
      statusBadge(job.status),
      el("span", { text: ` · dataset ${job.spec.dataset} · mode ${job.spec.mode}` }),
      active ? el("span", { class: "hint", text: " · auto-refreshing" }) : null,
    ])
  );
  summary.appendChild(
    el("div", { class: "hint" }, [
      `requests ${job.requests}, captures ${job.captures}, cost $${job.cost_reserved_usd.toFixed(4)}`,
    ])
  );
  if (job.missing_fields && job.missing_fields.length) {
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

  const [records, events] = await Promise.all([
    api(`/jobs/${encodeURIComponent(jobId)}/records`),
    api(`/jobs/${encodeURIComponent(jobId)}/events?limit=200`),
  ]);
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
  route().catch((err) => console.error(err));
}

document.addEventListener("DOMContentLoaded", init);
