const state = { resumeToken: null };

const $ = (selector) => document.querySelector(selector);

const examples = {
  scam: {
    title: "Remote Data Entry Assistant",
    company: "",
    url: "",
    contact_email: "hr.fastjobs@gmail.com",
    location: "Remote",
    description:
      "No experience required. Earn $850 per day working from home. To start immediately, pay a one-time registration fee of $200 for your starter kit. Contact HR on WhatsApp today.",
  },
  fakeRemote: {
    title: "Remote Customer Operations Specialist",
    company: "Northstar Services",
    url: "https://example.com/jobs/customer-operations",
    contact_email: "careers@northstar.example",
    location: "Remote",
    description:
      "This role is advertised as remote, but candidates must relocate to Chicago and work in-office three days per week after onboarding. Responsibilities include customer support, reporting, and operations coordination.",
  },
  legit: {
    title: "Backend Software Engineer",
    company: "Example Cloud Systems",
    url: "https://example.com/careers/backend-engineer",
    contact_email: "careers@example.com",
    location: "Remote",
    description:
      "Build and operate Python services for a cloud platform. Requirements include 3+ years of backend engineering experience, API design, observability, and collaboration with product teams. No fees are required from applicants.",
  },
};

document.querySelectorAll(".tab").forEach((button) => {
  button.addEventListener("click", () => {
    document.querySelectorAll(".tab").forEach((b) => b.classList.remove("active"));
    document.querySelectorAll(".panel").forEach((p) => p.classList.remove("active"));
    button.classList.add("active");
    document.getElementById(button.dataset.tab).classList.add("active");
  });
});

document.querySelectorAll("[data-example]").forEach((button) => {
  button.addEventListener("click", () => {
    fillCheckForm(examples[button.dataset.example]);
    $("#checkResult").innerHTML = "";
    $("#checkStatus").textContent = "Example loaded. Run check when ready.";
  });
});

async function initHealth() {
  try {
    const res = await fetch("/api/health");
    const data = await res.json();
    $("#health").textContent = `Service ready. Max fetch: ${data.web_max_fetch}`;
    $("#apiKey").textContent = data.has_api_key ? "API key ready" : "API key missing";
    $("#apiKey").classList.toggle("warn", !data.has_api_key);
  } catch (err) {
    $("#health").textContent = "Service is not reachable.";
    $("#apiKey").textContent = "Offline";
    $("#apiKey").classList.add("warn");
  }
}

$("#checkForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  $("#checkStatus").textContent = "Running...";
  $("#checkResult").innerHTML = "";
  const payload = Object.fromEntries(new FormData(event.target).entries());
  try {
    const res = await fetch("/api/check", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const data = await parseResponse(res);
    $("#checkResult").innerHTML = renderLegitResult(data);
    $("#checkStatus").textContent = "Done";
  } catch (err) {
    $("#checkStatus").textContent = err.message;
  }
});

$("#resumeForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  $("#resumeStatus").textContent = "Saving...";
  try {
    const formData = new FormData(event.target);
    const res = await fetch("/api/resume", { method: "POST", body: formData });
    const data = await parseResponse(res);
    state.resumeToken = data.resume_token;
    $("#resumeStatus").textContent = `Saved ${data.chars} chars. Token ready for fit scoring.`;
  } catch (err) {
    $("#resumeStatus").textContent = err.message;
  }
});

$("#fetchForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  $("#fetchStatus").textContent = "Starting...";
  $("#fetchProgress").textContent = "";
  $("#fetchResult").innerHTML = "";
  const form = new FormData(event.target);
  const payload = {
    keywords: form.get("keywords"),
    exclude: form.get("exclude") || "",
    country: form.get("country") || "usa",
    location: form.get("location") || "",
    remote_only: form.get("remote_only") === "on",
    max_results: Number(form.get("max_results") || 10),
    verification_mode: form.get("verification_mode") || "fast",
    fit_scoring: form.get("fit_scoring") === "on",
    resume_token: state.resumeToken,
  };
  if (payload.fit_scoring && !state.resumeToken) {
    $("#fetchStatus").textContent = "Upload or paste a resume first.";
    return;
  }
  try {
    const res = await fetch("/api/fetch", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const data = await parseResponse(res);
    $("#fetchStatus").textContent = `Task ${data.task_id.slice(0, 8)} running`;
    pollFetch(data.task_id);
  } catch (err) {
    $("#fetchStatus").textContent = err.message;
  }
});

async function pollFetch(taskId) {
  const res = await fetch(`/api/fetch/${taskId}`);
  const data = await parseResponse(res);
  if (data.status === "running") {
    const p = data.progress || {};
    $("#fetchProgress").textContent = `${p.phase || "running"} ${p.done || 0}/${p.total || 0}`;
    setTimeout(() => pollFetch(taskId), 2000);
    return;
  }
  if (data.status === "error") {
    $("#fetchStatus").textContent = data.error || "Task failed";
    return;
  }
  $("#fetchStatus").textContent = "Done";
  $("#fetchProgress").textContent = "";
  $("#fetchResult").innerHTML = renderFetchResults(data);
}

async function parseResponse(res) {
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const detail = typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail || data);
    throw new Error(detail || `Request failed: ${res.status}`);
  }
  return data;
}

function renderLegitResult(data) {
  const flags = data.flags || [];
  return `
    <div class="verdict ${data.verdict}">
      <strong>${escapeHtml(data.verdict || "unknown").toUpperCase()}</strong>
      <span>${Number(data.score || 0).toFixed(1)}/10</span>
    </div>
    <p class="result-note">${verdictCopy(data.verdict)}</p>
    ${renderFlags(flags)}
    <details>
      <summary>Layer details</summary>
      <pre>${escapeHtml(JSON.stringify(data.layers || {}, null, 2))}</pre>
    </details>
  `;
}

function renderFetchResults(data) {
  const stats = data.stats || {};
  const rows = data.results || [];
  if (!rows.length) {
    return `<p class="empty">No jobs returned. Fetched ${stats.fetched || 0}.</p>`;
  }
  return `
    <div class="summary">Fetched ${stats.fetched || 0}; after include ${stats.after_include || 0}; after exclude ${stats.after_exclude || 0}</div>
    <div class="table-wrap">
      <table>
        <thead>
          <tr><th>Job</th><th>Legitimacy</th><th>Fit</th><th>Evidence</th></tr>
        </thead>
        <tbody>
          ${rows.map(renderJobRow).join("")}
        </tbody>
      </table>
    </div>
  `;
}

function renderJobRow(row) {
  const legit = row.legit || {};
  const fit = row.fit;
  return `
    <tr>
      <td>
        <a href="${escapeAttr(row.url || "#")}" target="_blank" rel="noreferrer">${escapeHtml(row.title || "Untitled")}</a>
        <div class="muted">${escapeHtml(row.company || "")}</div>
      </td>
      <td><span class="badge ${escapeAttr(legit.verdict || "")}">${escapeHtml(legit.verdict || "")}</span> ${Number(legit.score || 0).toFixed(1)}</td>
      <td>${fit ? `${escapeHtml(String(fit.score ?? ""))} ${fit.matched ? "matched" : "not matched"}<div class="muted">${escapeHtml(fit.reason || "")}</div>` : "<span class='muted'>off</span>"}</td>
      <td>${renderFlags(legit.flags || [])}</td>
    </tr>
  `;
}

function renderFlags(flags) {
  if (!flags.length) return `<p class="muted">No flags returned.</p>`;
  return `<div class="flags">${flags.map((flag) => `
    <div class="flag ${escapeAttr(flag.severity || "yellow")}">
      <strong>${escapeHtml(flag.code || flag.severity || "flag")}</strong>
      <span>${escapeHtml(flag.message || "")}</span>
      ${flag.source ? `<small>${escapeHtml(flag.source)}</small>` : ""}
    </div>
  `).join("")}</div>`;
}

function escapeHtml(value) {
  return String(value).replace(/[&<>"']/g, (ch) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#39;",
  }[ch]));
}

function escapeAttr(value) {
  return escapeHtml(value).replace(/`/g, "&#96;");
}

function fillCheckForm(example) {
  const form = $("#checkForm");
  Object.entries(example).forEach(([key, value]) => {
    const field = form.elements[key];
    if (field) field.value = value;
  });
}

function verdictCopy(verdict) {
  if (verdict === "reject") return "Likely unsafe or not aligned with the configured requirements.";
  if (verdict === "review") return "Ambiguous. The posting needs human review before applying.";
  if (verdict === "pass") return "No blocking legitimacy issue was found by the configured checks.";
  return "The system returned a verdict with supporting flags below.";
}

initHealth();
