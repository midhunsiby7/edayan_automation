// Cashbook AI - Frontend Application Controller

let currentMonth = "2026-05";
let activeJobId = null;
let jobPollInterval = null;
let allReviewItems = [];
let allReviewGroups = [];
let canonicalHeads = [];
let canonicalSubheads = [];
let lastPreflightSummary = null;
let execPollInterval = null;
let currentFilter = "all";
let currentDirFilter = "ALL";
let activeGroupKey = null;

function escapeHtml(str) {
  if (str == null) return "";
  return String(str)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#039;");
}

// Initialize
document.addEventListener("DOMContentLoaded", () => {
  setupNavigation();
  setupMonthSelector();
  setupDropzones();
  setupModals();
  setupExecutionControls();
  fetchStatus();
  loadCanonicalMetadata();
  loadCurrentMonthData();

  // Support direct tab linking via hash
  const initialHash = window.location.hash.replace("#", "");
  if (initialHash && ["dashboard", "upload", "processing", "review", "intake", "logs"].includes(initialHash)) {
    switchTab(initialHash);
  }
});

// =========================================================================
// Navigation & Tab Switching
// =========================================================================

function setupNavigation() {
  const tabs = document.querySelectorAll(".nav-tab");
  tabs.forEach(tab => {
    tab.addEventListener("click", () => {
      const tabName = tab.getAttribute("data-tab");
      switchTab(tabName);
    });
  });

  const btnGoToReview = document.getElementById("btnGoToReview");
  if (btnGoToReview) {
    btnGoToReview.addEventListener("click", () => {
      switchTab("review");
      loadReviewData();
    });
  }

  // Segmented Direction Filter (ALL | RECEIPTS | PAYMENTS)
  document.querySelectorAll("#dirFilterControl .segment-btn").forEach(btn => {
    btn.addEventListener("click", () => {
      document.querySelectorAll("#dirFilterControl .segment-btn").forEach(b => b.classList.remove("active"));
      btn.classList.add("active");
      currentDirFilter = btn.getAttribute("data-dir") || "ALL";
      renderReviewItems();
    });
  });

  // Clear Active Group Filter Banner button
  const btnClearGroup = document.getElementById("btnClearGroupFilter");
  if (btnClearGroup) {
    btnClearGroup.addEventListener("click", () => {
      clearGroupFilter();
    });
  }
}

function clearGroupFilter() {
  activeGroupKey = null;
  const banner = document.getElementById("activeGroupBanner");
  if (banner) banner.style.display = "none";
  document.querySelectorAll(".group-card").forEach(c => c.classList.remove("active-cluster"));
  renderReviewItems();
}

function switchTab(tabName) {
  document.querySelectorAll(".nav-tab").forEach(t => {
    t.classList.toggle("active", t.getAttribute("data-tab") === tabName);
  });
  document.querySelectorAll(".tab-view").forEach(v => {
    v.classList.toggle("active", v.id === `view-${tabName}`);
  });

  if (tabName === "upload") loadPages();
  if (tabName === "review") loadReviewData();
  if (tabName === "dashboard") refreshDashboard();
  if (tabName === "intake") loadPreflightSummary();
}

async function loadPreflightSummary() {
  try {
    const res = await fetch(`/api/preflight-summary?month=${currentMonth}`);
    if (res.ok) {
      const summary = await res.json();
      renderPreflightSummary(summary);
    }
  } catch (e) {
    console.log("No preflight summary yet:", e);
  }
}

// =========================================================================
// Month Selector & Management
// =========================================================================

async function setupMonthSelector() {
  const select = document.getElementById("monthSelect");
  select.addEventListener("change", (e) => {
    currentMonth = e.target.value;
    loadCurrentMonthData();
  });

  const btnNewMonth = document.getElementById("btnNewMonth");
  const modal = document.getElementById("newMonthModal");
  btnNewMonth.addEventListener("click", () => {
    modal.style.display = "flex";
  });
}

async function loadCurrentMonthData() {
  document.getElementById("dashActiveMonth").textContent = currentMonth;
  updateDownloadLinks();
  await loadPages();
  await loadReviewData();
  await refreshDashboard();
  applyUrlState();
}

function applyUrlState() {
  const params = new URLSearchParams(window.location.search);

  if (params.get("dir")) {
    const dir = params.get("dir").toUpperCase();
    currentDirFilter = dir;
    document.querySelectorAll("#dirFilterControl .segment-btn").forEach(btn => {
      btn.classList.toggle("active", btn.getAttribute("data-dir") === dir);
    });
    renderReviewItems();
  }

  if (params.get("cluster") !== null) {
    const clusterIdx = parseInt(params.get("cluster"), 10) || 0;
    const groupCards = document.querySelectorAll(".group-card");
    if (groupCards[clusterIdx]) {
      groupCards[clusterIdx].click();
    }
  }

  if (params.get("edit") !== null) {
    const editIdx = parseInt(params.get("edit"), 10) || 0;
    const editBtns = document.querySelectorAll(".btn-item-edit");
    if (editBtns[editIdx]) {
      editBtns[editIdx].click();
    }
    if (params.get("search")) {
      const q = params.get("search");
      const inp = document.getElementById("editHeadSearch");
      if (inp) {
        inp.value = q;
        inp.dispatchEvent(new Event("input", { bubbles: true }));
      }
    }
  }
}

function updateDownloadLinks() {
  const draftUrl = `/api/export/draft?month=${currentMonth}`;
  const btn1 = document.getElementById("btnDownloadDraftExcel");
  const btn2 = document.getElementById("btnDownloadDraftExcelFromReview");
  if (btn1) btn1.href = draftUrl;
  if (btn2) btn2.href = draftUrl;
}

async function fetchStatus() {
  try {
    const res = await fetch("/api/status");
    const data = await res.json();

    // Months dropdown
    const select = document.getElementById("monthSelect");
    select.innerHTML = "";
    data.available_months.forEach(m => {
      const opt = document.createElement("option");
      opt.value = m;
      opt.textContent = m;
      if (m === currentMonth) opt.selected = true;
      select.appendChild(opt);
    });

    // Relay indicator
    const relayDot = document.getElementById("relayDot");
    const relayText = document.getElementById("relayText");
    if (data.browser_relay_status === "ONLINE") {
      relayDot.className = "dot dot-ready";
      relayText.textContent = "Relay: Online";
    } else {
      relayDot.className = "dot dot-standby";
      relayText.textContent = "Relay: Standby (11800)";
    }

    // Model indicator
    const modelDot = document.getElementById("modelDot");
    const modelText = document.getElementById("modelText");
    if (data.local_llm_model.available) {
      modelDot.className = "dot dot-ready";
      modelText.textContent = "Qwen3.5-4B: Ready";
    } else {
      modelDot.className = "dot dot-offline";
      modelText.textContent = "Qwen3.5-4B: Offline";
    }
  } catch (e) {
    console.error("Failed to fetch status:", e);
  }
}

async function loadCanonicalMetadata() {
  try {
    const res = await fetch("/api/review/heads");
    const data = await res.json();
    canonicalHeads = data.heads || [];
    canonicalSubheads = data.subheads || [];
  } catch (e) {
    console.error("Failed to load canonical metadata:", e);
  }
}

// =========================================================================
// Image Upload & Arrange
// =========================================================================

function setupDropzones() {
  const dropzone = document.getElementById("dropzone");
  const fileInput = document.getElementById("fileInput");

  dropzone.addEventListener("click", () => fileInput.click());
  fileInput.addEventListener("change", (e) => handleFileUpload(e.target.files));

  dropzone.addEventListener("dragover", (e) => {
    e.preventDefault();
    dropzone.classList.add("dragover");
  });
  dropzone.addEventListener("dragleave", () => dropzone.classList.remove("dragover"));
  dropzone.addEventListener("drop", (e) => {
    e.preventDefault();
    dropzone.classList.remove("dragover");
    if (e.dataTransfer.files.length) {
      handleFileUpload(e.dataTransfer.files);
    }
  });

  // Approved workbook dropzone
  const approvedDropzone = document.getElementById("approvedDropzone");
  const approvedFileInput = document.getElementById("approvedFileInput");
  approvedDropzone.addEventListener("click", () => approvedFileInput.click());
  approvedFileInput.addEventListener("change", (e) => handleApprovedUpload(e.target.files[0]));

  approvedDropzone.addEventListener("dragover", (e) => {
    e.preventDefault();
    approvedDropzone.classList.add("dragover");
  });
  approvedDropzone.addEventListener("dragleave", () => approvedDropzone.classList.remove("dragover"));
  approvedDropzone.addEventListener("drop", (e) => {
    e.preventDefault();
    approvedDropzone.classList.remove("dragover");
    if (e.dataTransfer.files.length) {
      handleApprovedUpload(e.dataTransfer.files[0]);
    }
  });

  // Start processing button
  document.getElementById("btnStartProcessing").addEventListener("click", startProcessing);
}

async function handleFileUpload(files) {
  if (!files || files.length === 0) return;

  const formData = new FormData();
  formData.append("month", currentMonth);
  for (let f of files) {
    formData.append("files", f);
  }

  try {
    const res = await fetch("/api/upload", {
      method: "POST",
      body: formData,
    });
    if (!res.ok) {
      const err = await res.json();
      alert("Upload failed: " + (err.detail || "Error uploading images"));
      return;
    }
    const data = await res.json();
    renderPages(data.pages);
  } catch (e) {
    alert("Upload error: " + e.message);
  }
}

async function loadPages() {
  try {
    const res = await fetch(`/api/pages?month=${currentMonth}`);
    const data = await res.json();
    renderPages(data.pages || []);
    document.getElementById("dashPageCount").textContent = (data.pages || []).length;
  } catch (e) {
    console.error("Failed to load pages:", e);
  }
}

function renderPages(pages) {
  const grid = document.getElementById("pagesGrid");
  const badge = document.getElementById("pageCountBadge");
  badge.textContent = pages.length;

  if (pages.length === 0) {
    grid.innerHTML = '<div class="empty-state">No images uploaded for this period yet.</div>';
    return;
  }

  grid.innerHTML = "";
  pages.forEach((p, index) => {
    const card = document.createElement("div");
    card.className = "page-card";
    const rot = p.rotation_degrees || 0;

    card.innerHTML = `
      <div class="page-card-header">
        <span class="page-num-badge">Page ${p.page_number}</span>
        ${p.is_duplicate ? '<span class="page-dup-badge">DUPLICATE HASH</span>' : ''}
      </div>
      <div class="page-preview-wrapper">
        <img class="page-preview-img" src="/api/pages/image/${currentMonth}/${encodeURIComponent(p.stored_filename)}" style="transform: rotate(${rot}deg);" alt="Page ${p.page_number}">
      </div>
      <div class="page-card-body">
        <div class="page-orig-name" title="${p.original_filename}">${p.original_filename}</div>
        <div class="page-hash">SHA-256: ${p.sha256.substring(0, 16)}...</div>
        <div class="text-muted text-sm mt-1">Size: ${(p.file_size_bytes / 1024).toFixed(1)} KB</div>
      </div>
      <div class="page-card-actions">
        <button class="btn-rotate" title="Rotate 90 degrees">↻ Rotate</button>
        <button class="btn-move-left" ${index === 0 ? "disabled" : ""} title="Move left">← Move</button>
        <button class="btn-move-right" ${index === pages.length - 1 ? "disabled" : ""} title="Move right">Move →</button>
      </div>
    `;

    // Rotate
    card.querySelector(".btn-rotate").addEventListener("click", () => {
      p.rotation_degrees = ((p.rotation_degrees || 0) + 90) % 360;
      saveReorderedPages(pages);
    });

    // Move left
    card.querySelector(".btn-move-left").addEventListener("click", () => {
      if (index > 0) {
        const temp = pages[index - 1];
        pages[index - 1] = pages[index];
        pages[index] = temp;
        saveReorderedPages(pages);
      }
    });

    // Move right
    card.querySelector(".btn-move-right").addEventListener("click", () => {
      if (index < pages.length - 1) {
        const temp = pages[index + 1];
        pages[index + 1] = pages[index];
        pages[index] = temp;
        saveReorderedPages(pages);
      }
    });

    grid.appendChild(card);
  });
}

async function saveReorderedPages(pages) {
  try {
    const res = await fetch("/api/pages/reorder", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ month: currentMonth, pages }),
    });
    const data = await res.json();
    renderPages(data.pages);
  } catch (e) {
    console.error("Failed to reorder pages:", e);
  }
}

// =========================================================================
// Processing Orchestration
// =========================================================================

async function startProcessing() {
  try {
    const res = await fetch("/api/process", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        month: currentMonth,
        use_llm: true,
        force_ocr: false,
      }),
    });

    if (!res.ok) {
      const err = await res.json();
      alert("Cannot start processing: " + (err.detail || "Unknown error"));
      return;
    }

    const data = await res.json();
    activeJobId = data.job_id;
    switchTab("processing");
    startJobPolling(activeJobId);
  } catch (e) {
    alert("Error initiating processing: " + e.message);
  }
}

function startJobPolling(jobId) {
  activeJobId = jobId;
  if (jobPollInterval) clearInterval(jobPollInterval);

  const bar = document.getElementById("jobMetaBar");
  bar.style.display = "flex";
  document.getElementById("activeJobId").textContent = jobId;
  document.getElementById("activeJobMonth").textContent = currentMonth;
  document.getElementById("btnCancelJob").style.display = "inline-block";

  pollJobStatus();
  jobPollInterval = setInterval(pollJobStatus, 1500);
}

async function pollJobStatus() {
  if (!activeJobId) return;

  try {
    const res = await fetch(`/api/jobs/${activeJobId}`);
    if (!res.ok) return;
    const job = await res.json();

    const statusBadge = document.getElementById("activeJobStatus");
    statusBadge.textContent = job.status;
    statusBadge.className = `badge badge-${job.status.toLowerCase()}`;

    // Update stepper
    job.stages.forEach(stage => {
      const stepEl = document.querySelector(`.stage-step[data-stage="${stage.id}"]`);
      if (stepEl) {
        stepEl.className = `stage-step ${stage.status.toLowerCase()}`;
        const badgeEl = stepEl.querySelector(".stage-status-badge");
        badgeEl.textContent = stage.status;

        const detailEl = stepEl.querySelector(".stage-detail");
        if (stage.details) {
          detailEl.textContent = stage.details;
        }
      }
    });

    // Check completion
    if (job.status === "COMPLETED") {
      clearInterval(jobPollInterval);
      document.getElementById("btnCancelJob").style.display = "none";
      document.getElementById("jobActionLinks").style.display = "flex";
      loadReviewData();
      refreshDashboard();
    } else if (job.status === "FAILED" || job.status === "CANCELLED") {
      clearInterval(jobPollInterval);
      document.getElementById("btnCancelJob").style.display = "none";
      if (job.error_message) {
        alert(`Job ${job.status}: ${job.error_message}`);
      }
    }
  } catch (e) {
    console.error("Error polling job status:", e);
  }
}

document.getElementById("btnCancelJob").addEventListener("click", async () => {
  if (!activeJobId) return;
  if (!confirm("Are you sure you want to cancel the active processing job?")) return;

  try {
    await fetch(`/api/jobs/${activeJobId}/cancel`, { method: "POST" });
    pollJobStatus();
  } catch (e) {
    alert("Error requesting cancellation: " + e.message);
  }
});

// =========================================================================
// Review Intelligence
// =========================================================================

async function loadReviewData() {
  try {
    const res = await fetch(`/api/review?month=${currentMonth}`);
    const data = await res.json();

    allReviewItems = data.items || [];
    allReviewGroups = data.groups || [];

    // Summary counts
    const stats = data.stats || { total: 0, verified: 0, suggested: 0, provisional: 0, unresolved: 0 };
    document.getElementById("cntAll").textContent = stats.total;
    const needsRev = (stats.suggested || 0) + (stats.provisional || 0) + (stats.unresolved || 0);
    document.getElementById("cntNeedsReview").textContent = needsRev;
    document.getElementById("cntGroups").textContent = allReviewGroups.length;
    document.getElementById("cntVerified").textContent = stats.verified;

    // Dashboard update
    document.getElementById("dashResolvedCount").textContent = stats.verified;
    document.getElementById("dashPendingReviewCount").textContent = needsRev;

    renderReviewItems();
    renderReviewGroups();
  } catch (e) {
    console.error("Failed to load review data:", e);
  }
}

function renderReviewGroups() {
  const container = document.getElementById("reviewGroupsSection");
  container.innerHTML = "";

  if (allReviewGroups.length === 0) {
    return;
  }

  allReviewGroups.forEach(g => {
    if (g.transaction_count <= 1) return; // Only show multi-item clusters

    const card = document.createElement("div");
    card.className = "group-card" + (activeGroupKey === g.group_key ? " active-cluster" : "");
    const sampleText = g.sample_narrations.join(" / ");

    card.innerHTML = `
      <div class="group-header">
        <div>
          <div class="group-title">${escapeHtml(g.suggested_head_name || "Unassigned Head")} (${g.transaction_count} transactions)</div>
          <div class="group-meta">
            Narrations: <em>"${escapeHtml(sampleText)}"</em> | Total: <strong>₹${g.total_amount.toLocaleString("en-IN", {minimumFractionDigits: 2})}</strong>
          </div>
        </div>
        <div class="group-actions">
          <button class="btn btn-sm btn-success btn-group-confirm">Confirm All (${g.transaction_count})</button>
          <button class="btn btn-sm btn-outline btn-group-override">Override All</button>
        </div>
      </div>
    `;

    // Clicking group card body (not action buttons) toggles group filter
    card.addEventListener("click", (e) => {
      if (e.target.closest(".group-actions") || e.target.closest("button")) {
        return;
      }
      if (activeGroupKey === g.group_key) {
        clearGroupFilter();
      } else {
        activeGroupKey = g.group_key;
        document.querySelectorAll(".group-card").forEach(c => c.classList.remove("active-cluster"));
        card.classList.add("active-cluster");
        const banner = document.getElementById("activeGroupBanner");
        if (banner) {
          banner.style.display = "flex";
          document.getElementById("activeGroupTitle").textContent = g.suggested_head_name || "Unassigned Head";
          document.getElementById("activeGroupCount").textContent = g.transaction_count;
        }
        renderReviewItems();
      }
    });

    // Group Confirm
    card.querySelector(".btn-group-confirm").addEventListener("click", (e) => {
      e.stopPropagation();
      applyGroupDecision(g.group_key, "CONFIRM", g.suggested_head_id);
    });

    // Group Override
    card.querySelector(".btn-group-override").addEventListener("click", (e) => {
      e.stopPropagation();
      openOverrideModal(null, g.group_key, sampleText, g.total_amount, "ANY");
    });

    container.appendChild(card);
  });
}

function renderReviewItems() {
  const container = document.getElementById("reviewItemsSection");
  container.innerHTML = "";

  let filtered = [...allReviewItems];

  // 1. Group cluster filter
  if (activeGroupKey) {
    const group = allReviewGroups.find(g => g.group_key === activeGroupKey);
    if (group && group.transaction_ids) {
      filtered = filtered.filter(it => group.transaction_ids.includes(it.transaction_id));
    }
  }

  // 2. Status filter
  if (currentFilter === "needs_review") {
    filtered = filtered.filter(it => it.mapping_status !== "VERIFIED");
  } else if (currentFilter === "verified") {
    filtered = filtered.filter(it => it.mapping_status === "VERIFIED");
  } else if (currentFilter === "groups") {
    const groupedTxIds = new Set();
    allReviewGroups.forEach(g => {
      if (g.transaction_count > 1 && g.transaction_ids) {
        g.transaction_ids.forEach(id => groupedTxIds.add(id));
      }
    });
    filtered = filtered.filter(it => groupedTxIds.has(it.transaction_id));
  }

  // 3. Direction filter (Segmented Control: ALL | RECEIPTS | PAYMENTS)
  if (currentDirFilter === "RECEIPT") {
    filtered = filtered.filter(it => it.direction === "RECEIPT");
  } else if (currentDirFilter === "PAYMENT") {
    filtered = filtered.filter(it => it.direction === "PAYMENT");
  }

  if (filtered.length === 0) {
    container.innerHTML = "<div class='empty-state'>No transactions matching this filter.</div>";
    return;
  }

  filtered.forEach(it => {
    const card = document.createElement("div");
    card.className = `review-card status-${it.mapping_status.toLowerCase()}`;

    const isRec = it.direction === "RECEIPT";
    const dirClass = isRec ? "tx-receipt" : "tx-payment";
    const amtStr = it.amount !== null ? `₹${it.amount.toLocaleString("en-IN", {minimumFractionDigits: 2})}` : "₹0.00";

    card.innerHTML = `
      <div class="review-cell-meta">
        <div><strong>Pg ${escapeHtml(it.source_page)}</strong></div>
        <div>V.# ${escapeHtml(it.raw_voucher_number || "-")}</div>
        <div>Folio ${escapeHtml(it.raw_head_number || "-")}</div>
      </div>

      <div class="review-cell-narration">
        <div class="tx-malayalam">${escapeHtml(it.raw_narration || "(Empty Narration)")}</div>
        <div class="tx-english">${escapeHtml(it.english_narration || "")}</div>
        <div class="tx-amt ${dirClass}">${escapeHtml(it.direction)}: ${amtStr}</div>
      </div>

      <div class="review-cell-mapping">
        <div class="mapped-head-name">${escapeHtml(it.canonical_head_name) || '<span class="text-danger">UNRESOLVED</span>'}</div>
        ${it.canonical_subhead_name ? `<div class="mapped-subhead-name">Subhead: ${escapeHtml(it.canonical_subhead_name)}</div>` : ''}
        <div class="mapped-meta">
          <span class="badge badge-${it.mapping_status.toLowerCase()}">${it.mapping_status}</span>
          <span class="text-muted text-sm">${(it.confidence_score * 100).toFixed(0)}% conf</span>
          <span class="text-muted text-sm">(${escapeHtml(it.mapping_method)})</span>
        </div>
      </div>

      <div class="review-cell-actions">
        <div class="action-btn-row">
          <button class="btn btn-sm btn-success btn-item-confirm" title="Confirm classification">✓ Confirm</button>
          <button class="btn btn-sm btn-outline btn-item-override" title="Override head/subhead">Override</button>
        </div>
        <div class="action-btn-row">
          <button class="btn btn-sm btn-danger btn-item-reject" title="Reject suggestion">✗ Reject</button>
          <button class="btn btn-sm btn-secondary btn-item-defer" title="Defer for later">Defer</button>
        </div>
        <div class="action-btn-row mt-1">
          <button class="btn btn-sm btn-primary-outline btn-item-edit" title="Edit transaction fields" style="width: 100%;">✎ Edit</button>
        </div>
      </div>
    `;

    // Confirm
    card.querySelector(".btn-item-confirm").addEventListener("click", () => {
      applyItemDecision(it.transaction_id, "CONFIRM");
    });

    // Override
    card.querySelector(".btn-item-override").addEventListener("click", () => {
      openOverrideModal(it.transaction_id, null, it.raw_narration, it.amount, it.direction);
    });

    // Reject
    card.querySelector(".btn-item-reject").addEventListener("click", () => {
      applyItemDecision(it.transaction_id, "REJECT");
    });

    // Defer
    card.querySelector(".btn-item-defer").addEventListener("click", () => {
      applyItemDecision(it.transaction_id, "DEFER");
    });

    // Edit
    card.querySelector(".btn-item-edit").addEventListener("click", () => {
      openEditModal(it);
    });

    container.appendChild(card);
  });
}

// Review filter tabs
document.querySelectorAll(".filter-pill").forEach(pill => {
  pill.addEventListener("click", () => {
    document.querySelectorAll(".filter-pill").forEach(p => p.classList.remove("active"));
    pill.classList.add("active");
    currentFilter = pill.getAttribute("data-filter") || "all";
    renderReviewItems();
  });
});

async function applyItemDecision(txId, action, headId = null, subheadId = null, notes = "", promoteAlias = false) {
  try {
    const res = await fetch(`/api/review/${txId}/decision`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        month: currentMonth,
        action: action,
        confirmed_head_id: headId,
        confirmed_subhead_id: subheadId,
        notes: notes,
        promote_to_alias: promoteAlias,
        reviewer: "Accountant",
      }),
    });
    if (!res.ok) {
      const err = await res.json();
      alert("Decision failed: " + err.detail);
      return;
    }
    await loadReviewData();
  } catch (e) {
    alert("Error applying decision: " + e.message);
  }
}

async function applyGroupDecision(groupKey, action, headId = null) {
  try {
    const res = await fetch("/api/review/group/decision", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        month: currentMonth,
        group_key: groupKey,
        action: action,
        confirmed_head_id: headId,
        reviewer: "Accountant",
      }),
    });
    const data = await res.json();
    alert(data.message);
    await loadReviewData();
  } catch (e) {
    alert("Error applying group decision: " + e.message);
  }
}

// =========================================================================
// Override Modal
// =========================================================================

function openOverrideModal(txId, groupKey, narration, amount, direction) {
  const modal = document.getElementById("overrideModal");
  document.getElementById("overrideTxId").value = txId || "";
  document.getElementById("overrideNarration").textContent = narration || "--";
  document.getElementById("overrideAmount").textContent = amount !== null ? `₹${amount}` : "--";
  document.getElementById("overrideDirection").textContent = direction || "--";

  // Populate heads dropdown
  const headSelect = document.getElementById("overrideHeadSelect");
  headSelect.innerHTML = '<option value="">-- Choose Canonical Head --</option>';
  canonicalHeads.forEach(h => {
    const opt = document.createElement("option");
    opt.value = h.id;
    opt.textContent = `${h.name} (${h.account_set})`;
    headSelect.appendChild(opt);
  });

  // Populate subheads
  updateSubheadDropdown(null);
  headSelect.addEventListener("change", (e) => {
    updateSubheadDropdown(e.target.value);
  });

  modal.style.display = "flex";
}

function updateSubheadDropdown(headId) {
  const subSelect = document.getElementById("overrideSubheadSelect");
  subSelect.innerHTML = '<option value="">-- No Subhead (Standard) --</option>';

  const head = canonicalHeads.find(h => h.id === headId);
  if (!head || !head.allowed_subheads || head.allowed_subheads.length === 0) {
    return;
  }

  head.allowed_subheads.forEach(subId => {
    const s = canonicalSubheads.find(x => x.id === subId);
    if (s) {
      const opt = document.createElement("option");
      opt.value = s.id;
      opt.textContent = s.name;
      subSelect.appendChild(opt);
    }
  });
}

function setupModals() {
  // New Month Modal
  const m1 = document.getElementById("newMonthModal");
  document.getElementById("btnCloseNewMonth").addEventListener("click", () => m1.style.display = "none");
  document.getElementById("btnCancelNewMonth").addEventListener("click", () => m1.style.display = "none");
  document.getElementById("btnSubmitNewMonth").addEventListener("click", async () => {
    const val = document.getElementById("newMonthInput").value.trim();
    if (!val) return;
    const res = await fetch("/api/months", {
      method: "POST",
      headers: { "Content-Type": "application/x-www-form-urlencoded" },
      body: `month=${encodeURIComponent(val)}`,
    });
    if (res.ok) {
      m1.style.display = "none";
      currentMonth = val;
      await fetchStatus();
      await loadCurrentMonthData();
    } else {
      const err = await res.json();
      alert(err.detail || "Failed to create month");
    }
  });

  // Override Modal
  const m2 = document.getElementById("overrideModal");
  document.getElementById("btnCloseOverride").addEventListener("click", () => m2.style.display = "none");
  document.getElementById("btnCancelOverride").addEventListener("click", () => m2.style.display = "none");
  document.getElementById("btnSubmitOverride").addEventListener("click", async () => {
    const txId = document.getElementById("overrideTxId").value;
    const headId = document.getElementById("overrideHeadSelect").value;
    const subheadId = document.getElementById("overrideSubheadSelect").value || null;
    const notes = document.getElementById("overrideNotes").value.trim();
    const promote = document.getElementById("overridePromoteAlias").checked;

    if (!headId) {
      alert("Please select a canonical head.");
      return;
    }

    if (promote) {
      const narr = document.getElementById("overrideNarration").textContent;
      const headObj = canonicalHeads.find(h => h.id === headId);
      const conf = confirm(`CONFIRM PERMANENT ALIAS PROMOTION:\n\nPhrase: "${narr}"\nTarget Canonical Head: "${headObj.name}"\n\nThis will permanently update canonical_registry.json for all future months. Proceed?`);
      if (!conf) return;
    }

    m2.style.display = "none";
    if (txId) {
      await applyItemDecision(txId, "OVERRIDE", headId, subheadId, notes, promote);
    }
  });

  // 3. Edit Transaction Modal Setup
  const editModal = document.getElementById("editModal");
  const btnCloseEdit = document.getElementById("btnCloseEdit");
  const btnCancelEdit = document.getElementById("btnCancelEdit");
  const btnSaveEdit = document.getElementById("btnSaveEdit");
  const headSearchInput = document.getElementById("editHeadSearch");
  const headDropdown = document.getElementById("editHeadDropdown");
  const headIdHidden = document.getElementById("editHeadId");
  const dirSelect = document.getElementById("editDirection");
  const editSubSelect = document.getElementById("editSubheadSelect");

  function closeEditModal() {
    if (editModal) editModal.style.display = "none";
    if (headDropdown) headDropdown.style.display = "none";
  }

  if (btnCloseEdit) btnCloseEdit.addEventListener("click", closeEditModal);
  if (btnCancelEdit) btnCancelEdit.addEventListener("click", closeEditModal);

  // Dynamic combobox search filter
  function filterHeadDropdown(query = "") {
    if (!headDropdown) return;
    const currentDir = dirSelect ? dirSelect.value : "RECEIPT";
    const q = query.trim().toLowerCase();

    const matches = canonicalHeads.filter(h => {
      if (h.is_active === false) return false;
      if (h.direction !== "BOTH" && h.direction !== currentDir) return false;
      if (!q) return true;
      return (
        (h.name && h.name.toLowerCase().includes(q)) ||
        (h.account_set && h.account_set.toLowerCase().includes(q)) ||
        (h.id && h.id.toLowerCase().includes(q))
      );
    });

    headDropdown.innerHTML = "";
    if (matches.length === 0) {
      headDropdown.innerHTML = '<div class="combobox-item text-muted" style="cursor: default;">No matching canonical heads for this direction</div>';
    } else {
      matches.forEach(h => {
        const item = document.createElement("div");
        item.className = "combobox-item" + (headIdHidden.value === h.id ? " selected" : "");
        item.innerHTML = `
          <div>
            <strong>${escapeHtml(h.name)}</strong>
            <span class="badge badge-sm badge-secondary" style="margin-left: 6px;">${escapeHtml(h.account_set)}</span>
          </div>
          <div class="combobox-item-policy">${escapeHtml(h.policy)}</div>
        `;
        item.addEventListener("mousedown", (e) => {
          e.preventDefault();
          headIdHidden.value = h.id;
          headSearchInput.value = h.name;
          headDropdown.style.display = "none";
          updateEditHeadPolicyHint(h.id);
          updateEditSubheadDropdown(h.id, null);
        });
        headDropdown.appendChild(item);
      });
    }
    headDropdown.style.display = "block";
  }

  if (headSearchInput) {
    headSearchInput.addEventListener("input", (e) => {
      const val = e.target.value.trim().toLowerCase();
      const currentDir = dirSelect ? dirSelect.value : "RECEIPT";
      const exact = canonicalHeads.find(h => {
        const dirOk = (h.direction === "BOTH" || h.direction === currentDir);
        return h.is_active !== false && dirOk && h.name.toLowerCase() === val;
      });
      if (exact) {
        headIdHidden.value = exact.id;
        updateEditSubheadDropdown(exact.id, null);
        updateEditHeadPolicyHint(exact.id);
      } else {
        headIdHidden.value = "";
        updateEditHeadPolicyHint("");
      }
      filterHeadDropdown(e.target.value);
    });

    headSearchInput.addEventListener("focus", () => {
      filterHeadDropdown(headSearchInput.value);
    });

    headSearchInput.addEventListener("blur", () => {
      setTimeout(() => {
        if (headDropdown) headDropdown.style.display = "none";
        // If user typed something that isn't a valid head, reset to blank
        if (!headIdHidden.value) {
          headSearchInput.value = "";
          updateEditHeadPolicyHint("");
          updateEditSubheadDropdown(null, null);
        }
      }, 200);
    });
  }

  // Direction dropdown change resets head if incompatible
  if (dirSelect) {
    dirSelect.addEventListener("change", () => {
      const curHeadId = headIdHidden.value;
      const curHead = canonicalHeads.find(h => h.id === curHeadId);
      if (curHead && curHead.direction !== "BOTH" && curHead.direction !== dirSelect.value) {
        headIdHidden.value = "";
        if (headSearchInput) headSearchInput.value = "";
        updateEditSubheadDropdown(null, null);
        updateEditHeadPolicyHint("");
      }
      if (headDropdown && headDropdown.style.display === "block") {
        filterHeadDropdown(headSearchInput ? headSearchInput.value : "");
      }
    });
  }

  // Subhead dropdown change validation
  if (editSubSelect) {
    editSubSelect.addEventListener("change", () => {
      const headId = headIdHidden.value;
      const head = canonicalHeads.find(h => h.id === headId);
      const warning = document.getElementById("editSubheadPolicyWarning");
      if (head && head.policy === "MANDATORY" && warning) {
        warning.style.display = editSubSelect.value ? "none" : "block";
      }
    });
  }

  // Save changes button
  if (btnSaveEdit) {
    btnSaveEdit.addEventListener("click", async () => {
      const txId = document.getElementById("editTxId").value;
      const dateVal = document.getElementById("editDate").value.trim();
      const dirVal = document.getElementById("editDirection").value;
      const vouchVal = document.getElementById("editVoucher").value.trim();
      const amtStr = document.getElementById("editAmount").value.trim();
      const headId = headIdHidden.value;
      const subheadId = editSubSelect.value || null;
      const narrVal = document.getElementById("editNarration").value.trim();

      // 1. Validate Date
      if (!dateVal || !(/^\d{4}-\d{2}-\d{2}$/.test(dateVal) || /^\d{1,2}[\.\/\-]\d{1,2}([\.\/\-]\d{2,4})?$/.test(dateVal))) {
        alert("Please enter a valid Date in YYYY-MM-DD or DD.MM.YYYY format.");
        document.getElementById("editDate").focus();
        return;
      }

      // 2. Validate Amount
      const amt = parseFloat(amtStr);
      if (isNaN(amt) || amt <= 0) {
        alert("Please enter a valid positive Amount greater than 0.");
        document.getElementById("editAmount").focus();
        return;
      }

      // 3. Validate Canonical Head (prevent arbitrary free text)
      if (!headId) {
        alert("Please select a valid canonical head from the searchable dropdown. Free-text entries are not allowed.");
        document.getElementById("editHeadSearch").focus();
        return;
      }
      const headObj = canonicalHeads.find(h => h.id === headId);
      if (!headObj) {
        alert("Selected canonical head is not valid.");
        return;
      }

      // 4. Validate Subhead Policy
      if (headObj.policy === "MANDATORY" && !subheadId) {
        alert(`Canonical head '${headObj.name}' requires a mandatory subhead. Please select a valid subhead.`);
        editSubSelect.focus();
        return;
      }

      btnSaveEdit.disabled = true;
      btnSaveEdit.textContent = "Saving...";

      try {
        const res = await fetch(`/api/review/${encodeURIComponent(txId)}/edit`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            month: currentMonth,
            date: dateVal,
            direction: dirVal,
            voucher_number: vouchVal,
            amount: amt,
            canonical_head_id: headId,
            canonical_subhead_id: subheadId,
            narration: narrVal,
            editor: "Accountant"
          })
        });

        if (!res.ok) {
          const err = await res.json();
          alert("Failed to save transaction edit: " + (err.detail || JSON.stringify(err)));
          btnSaveEdit.disabled = false;
          btnSaveEdit.textContent = "Save Changes";
          return;
        }

        closeEditModal();

        // Invalidate preflight summary in memory if present
        if (lastPreflightSummary) {
          lastPreflightSummary.is_valid = false;
          lastPreflightSummary.status = "INVALIDATED_BY_DRAFT_EDIT";
          const badge = document.getElementById("preflightStatusBadge");
          if (badge) {
            badge.textContent = "INVALIDATED_BY_DRAFT_EDIT";
            badge.className = "badge badge-unresolved";
          }
          const succSection = document.getElementById("pfSuccessSection");
          if (succSection) succSection.style.display = "none";
        }

        await loadReviewData();
      } catch (e) {
        alert("Error saving transaction edit: " + e.message);
      } finally {
        btnSaveEdit.disabled = false;
        btnSaveEdit.textContent = "Save Changes";
      }
    });
  }
}

// =========================================================================
// Edit Modal Helper Functions
// =========================================================================

function openEditModal(it) {
  const modal = document.getElementById("editModal");
  if (!modal) return;

  document.getElementById("editTxId").value = it.transaction_id;

  // Preserve original extracted evidence in preview
  const orig = it.original_extracted_data || {};
  document.getElementById("editSourceImage").textContent = it.source_image || "--";
  document.getElementById("editSourcePage").textContent = it.source_page || "1";
  document.getElementById("editSourceRow").textContent = it.source_row || "1";
  document.getElementById("editOrigVoucher").textContent = orig.raw_voucher_number || it.raw_voucher_number || "-";
  document.getElementById("editOrigHead").textContent = orig.raw_head_number || it.raw_head_number || "-";

  // Six Editable Accounting Fields
  document.getElementById("editDate").value = it.date || it.raw_date || "";
  document.getElementById("editDirection").value = it.direction || "RECEIPT";
  document.getElementById("editVoucher").value = it.voucher_number || it.raw_voucher_number || "";
  document.getElementById("editAmount").value = it.amount != null ? it.amount : "";
  document.getElementById("editNarration").value = it.raw_narration || "";

  // Head combobox
  const headId = it.canonical_head_id || "";
  const headObj = canonicalHeads.find(h => h.id === headId);
  document.getElementById("editHeadId").value = headId;
  document.getElementById("editHeadSearch").value = headObj ? headObj.name : (it.canonical_head_name || "");
  const dropdown = document.getElementById("editHeadDropdown");
  if (dropdown) dropdown.style.display = "none";

  updateEditHeadPolicyHint(headId);
  updateEditSubheadDropdown(headId, it.canonical_subhead_id);

  modal.style.display = "flex";
}

function updateEditHeadPolicyHint(headId) {
  const hint = document.getElementById("editHeadPolicyHint");
  if (!hint) return;
  const head = canonicalHeads.find(h => h.id === headId);
  if (!head) {
    hint.textContent = "Type to search canonical accounting heads. Free-text entries are not permitted.";
    hint.className = "text-muted";
    return;
  }
  hint.textContent = `Canonical Head: [${head.id}] ${head.name} | Set: ${head.account_set} | Subheads: ${head.policy} | Direction: ${head.direction}`;
  hint.className = "text-muted text-xs";
}

function updateEditSubheadDropdown(headId, selectedSubheadId = null) {
  const subSelect = document.getElementById("editSubheadSelect");
  const subLabel = document.getElementById("editSubheadLabel");
  const warning = document.getElementById("editSubheadPolicyWarning");
  if (!subSelect) return;

  subSelect.innerHTML = "";
  const head = canonicalHeads.find(h => h.id === headId);

  if (!head) {
    subSelect.disabled = true;
    subSelect.innerHTML = '<option value="">-- Select Canonical Head First --</option>';
    if (subLabel) subLabel.innerHTML = 'Subhead:';
    if (warning) warning.style.display = "none";
    return;
  }

  const policy = head.policy || "PROHIBITED";
  const allowed = head.allowed_subheads || [];

  if (policy === "PROHIBITED" || allowed.length === 0) {
    subSelect.disabled = true;
    subSelect.innerHTML = '<option value="">-- No Subhead Permitted --</option>';
    if (subLabel) subLabel.innerHTML = 'Subhead: <span class="badge badge-sm badge-outline">PROHIBITED</span>';
    if (warning) warning.style.display = "none";
  } else if (policy === "MANDATORY") {
    subSelect.disabled = false;
    if (subLabel) subLabel.innerHTML = 'Subhead: <span class="badge badge-sm badge-warning">MANDATORY</span>';
    
    const defaultOpt = document.createElement("option");
    defaultOpt.value = "";
    defaultOpt.textContent = "-- Select Mandatory Subhead --";
    subSelect.appendChild(defaultOpt);

    allowed.forEach(sId => {
      const sObj = canonicalSubheads.find(s => s.id === sId);
      const opt = document.createElement("option");
      opt.value = sId;
      opt.textContent = sObj ? sObj.name : sId;
      if (sId === selectedSubheadId) opt.selected = true;
      subSelect.appendChild(opt);
    });

    if (warning) {
      warning.style.display = subSelect.value ? "none" : "block";
    }
  } else { // OPTIONAL
    subSelect.disabled = false;
    if (subLabel) subLabel.innerHTML = 'Subhead: <span class="badge badge-sm badge-secondary">OPTIONAL</span>';
    
    const defaultOpt = document.createElement("option");
    defaultOpt.value = "";
    defaultOpt.textContent = "-- None (Optional) --";
    subSelect.appendChild(defaultOpt);

    allowed.forEach(sId => {
      const sObj = canonicalSubheads.find(s => s.id === sId);
      const opt = document.createElement("option");
      opt.value = sId;
      opt.textContent = sObj ? sObj.name : sId;
      if (sId === selectedSubheadId) opt.selected = true;
      subSelect.appendChild(opt);
    });

    if (warning) warning.style.display = "none";
  }
}

// =========================================================================
// Approved Workbook Intake & Autonomous Execution (Milestone 7)
// =========================================================================

async function handleApprovedUpload(file) {
  if (!file) return;

  const formData = new FormData();
  formData.append("month", currentMonth);
  formData.append("file", file);

  try {
    const res = await fetch("/api/upload-approved", {
      method: "POST",
      body: formData,
    });
    if (!res.ok) {
      const err = await res.json();
      alert("Validation failed: " + (err.detail || JSON.stringify(err)));
      return;
    }
    const summary = await res.json();
    renderPreflightSummary(summary);
  } catch (e) {
    alert("Error uploading approved workbook: " + e.message);
  }
}

function renderPreflightSummary(summary) {
  lastPreflightSummary = summary;
  const card = document.getElementById("preflightResultCard");
  card.style.display = "block";

  const badge = document.getElementById("preflightStatusBadge");
  badge.textContent = summary.status;
  badge.className = `badge ${summary.is_valid ? "badge-verified" : "badge-unresolved"}`;

  document.getElementById("pfMonth").textContent = summary.month;
  document.getElementById("pfFileName").textContent = summary.file_name;
  document.getElementById("pfHash").textContent = summary.file_hash_sha256;

  document.getElementById("pfReceipts").textContent = `${summary.receipts_count} items (₹${summary.receipts_total.toLocaleString("en-IN", {minimumFractionDigits: 2})})`;
  document.getElementById("pfPayments").textContent = `${summary.payments_count} items (₹${summary.payments_total.toLocaleString("en-IN", {minimumFractionDigits: 2})})`;
  document.getElementById("pfNet").textContent = `₹${summary.net_movement.toLocaleString("en-IN", {minimumFractionDigits: 2})}`;

  const openBal = summary.opening_balance != null ? summary.opening_balance : 18506.60;
  const closeBal = summary.expected_closing_balance != null ? summary.expected_closing_balance : (openBal + summary.net_movement);
  document.getElementById("pfOpeningBal").textContent = `₹${openBal.toLocaleString("en-IN", {minimumFractionDigits: 2})}`;
  document.getElementById("pfClosingBal").textContent = `₹${closeBal.toLocaleString("en-IN", {minimumFractionDigits: 2})}`;
  document.getElementById("pfCoverage").textContent = `${(summary.coverage_percent || 100).toFixed(1)}%`;
  
  const unmapped = summary.unmapped_heads_count || 0;
  const webMapElem = document.getElementById("pfWebMappings");
  webMapElem.textContent = (unmapped === 0) ? "COMPLETE (0 unmapped)" : `${unmapped} UNMAPPED`;
  webMapElem.className = (unmapped === 0) ? "text-success" : "text-danger";

  const errSection = document.getElementById("pfErrorsSection");
  const errList = document.getElementById("pfErrorsList");
  const successSection = document.getElementById("pfSuccessSection");

  if (!summary.is_valid && summary.errors && summary.errors.length > 0) {
    errSection.style.display = "block";
    successSection.style.display = "none";
    document.getElementById("pfErrorCount").textContent = summary.errors.length;
    errList.innerHTML = summary.errors.map(e => `<li>[Sheet: ${e.sheet}, Row ${e.row_number}] ${e.column}: ${escapeHtml(e.error_message)}</li>`).join("");
  } else {
    errSection.style.display = "none";
    successSection.style.display = "block";
  }
}

function setupExecutionControls() {
  const btnAuth = document.getElementById("btnAuthorizeBatch");
  if (btnAuth) {
    btnAuth.addEventListener("click", async () => {
      if (!lastPreflightSummary || !lastPreflightSummary.file_hash_sha256) {
        alert("No verified approved workbook snapshot available to authorize.");
        return;
      }
      const openBal = lastPreflightSummary.opening_balance != null ? lastPreflightSummary.opening_balance : 18506.60;
      const closeBal = lastPreflightSummary.expected_closing_balance != null ? lastPreflightSummary.expected_closing_balance : 0;
      
      const confirmAuth = confirm(
        `ACCOUNTING BATCH AUTHORIZATION\n` +
        `-----------------------------------------\n` +
        `Accounting Month: ${currentMonth}\n` +
        `Source File: ${lastPreflightSummary.file_name}\n` +
        `SHA-256: ${lastPreflightSummary.file_hash_sha256}\n` +
        `Receipts: ${lastPreflightSummary.receipts_count} items (₹${lastPreflightSummary.receipts_total.toFixed(2)})\n` +
        `Payments: ${lastPreflightSummary.payments_count} items (₹${lastPreflightSummary.payments_total.toFixed(2)})\n` +
        `Opening Cash Target: ₹${openBal.toFixed(2)}\n` +
        `Expected Closing Cash: ₹${closeBal.toFixed(2)}\n\n` +
        `This will create signed submission manifests and prepare single-owner bounded execution.\n` +
        `Do you authorize this batch?`
      );
      if (!confirmAuth) return;

      try {
        const res = await fetch("/api/execution/authorize", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            month: currentMonth,
            file_hash_sha256: lastPreflightSummary.file_hash_sha256,
            authorizer_role: "Accountant",
            authorizer_notes: "Authorized via Cashbook AI UI"
          })
        });
        if (!res.ok) {
          const err = await res.json();
          alert("Authorization failed: " + (err.detail || JSON.stringify(err)));
          return;
        }
        const data = await res.json();
        alert(`Batch Successfully Authorized!\n\nBatch ID: ${data.batch_id}\nManifests: Receipts (${data.receipts_count}), Payments (${data.payments_count})`);

        const startNow = confirm(
          `Start autonomous batch entry now?\n\n` +
          `Execution will proceed under single-owner process lock.\n` +
          `(Dry-run verification mode is active by default).`
        );
        if (startNow) {
          await startBatchExecution(false);
        }
      } catch (e) {
        alert("Error authorizing batch: " + e.message);
      }
    });
  }

  const btnPause = document.getElementById("btnPauseExec");
  if (btnPause) {
    btnPause.addEventListener("click", async () => {
      try {
        const res = await fetch(`/api/execution/pause?month=${encodeURIComponent(currentMonth)}`, { method: "POST" });
        const d = await res.json();
        alert(d.message || "Pause requested");
      } catch (e) {
        alert("Error pausing: " + e.message);
      }
    });
  }

  const btnResume = document.getElementById("btnResumeExec");
  if (btnResume) {
    btnResume.addEventListener("click", async () => {
      try {
        const res = await fetch(`/api/execution/resume?month=${encodeURIComponent(currentMonth)}`, { method: "POST" });
        const d = await res.json();
        alert(d.message || "Resume requested");
      } catch (e) {
        alert("Error resuming: " + e.message);
      }
    });
  }

  const btnCancel = document.getElementById("btnCancelExec");
  if (btnCancel) {
    btnCancel.addEventListener("click", async () => {
      if (confirm("Are you sure you want to cancel execution? In-flight entry will complete gracefully.")) {
        try {
          const res = await fetch(`/api/execution/cancel?month=${encodeURIComponent(currentMonth)}`, { method: "POST" });
          const d = await res.json();
          alert(d.message || "Cancellation requested");
        } catch (e) {
          alert("Error cancelling: " + e.message);
        }
      }
    });
  }
}

async function startBatchExecution(arm = false) {
  try {
    const res = await fetch("/api/execution/start", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ month: currentMonth, arm: arm })
    });
    if (!res.ok) {
      const err = await res.json();
      alert("Failed to start execution: " + (err.detail || JSON.stringify(err)));
      return;
    }
    const data = await res.json();
    showExecutionMonitor(data.batch_id);
    startExecutionPolling();
  } catch (e) {
    alert("Error starting execution: " + e.message);
  }
}

function showExecutionMonitor(batchId) {
  const card = document.getElementById("execMonitorCard");
  card.style.display = "block";
  document.getElementById("execBatchId").textContent = batchId || "--";
  document.getElementById("execStatusBadge").textContent = "RUNNING";
  document.getElementById("execStatusBadge").className = "badge badge-provisional";
  document.getElementById("execLockBadge").textContent = "LOCKED";
  document.getElementById("execLockBadge").className = "badge badge-verified";
}

function startExecutionPolling() {
  if (execPollInterval) clearInterval(execPollInterval);
  execPollInterval = setInterval(async () => {
    await pollExecutionStatus();
  }, 1000);
}

async function pollExecutionStatus() {
  try {
    const res = await fetch(`/api/execution/status?month=${encodeURIComponent(currentMonth)}`);
    if (!res.ok) return;
    const status = await res.json();

    document.getElementById("execBatchId").textContent = status.batch_id || "--";
    const badge = document.getElementById("execStatusBadge");
    badge.textContent = status.status;
    if (status.status === "RUNNING") badge.className = "badge badge-provisional";
    else if (status.status === "COMPLETED") badge.className = "badge badge-verified";
    else if (status.status === "FAILED") badge.className = "badge badge-unresolved";
    else if (status.status === "PAUSED") badge.className = "badge badge-warning";
    else badge.className = "badge";

    const total = status.total_transactions || 0;
    const completed = status.completed_transactions || 0;
    const pct = total > 0 ? Math.round((completed / total) * 100) : 0;

    document.getElementById("execProgressText").textContent = `${completed} / ${total} transactions (${pct}%)`;
    document.getElementById("execProgressBar").style.width = `${pct}%`;

    const auditLog = document.getElementById("execAuditLog");
    if (status.recent_logs && status.recent_logs.length > 0) {
      auditLog.innerHTML = status.recent_logs.map(l => `<div><code>${escapeHtml(l)}</code></div>`).join("");
      auditLog.scrollTop = auditLog.scrollHeight;
    }

    const btnPause = document.getElementById("btnPauseExec");
    const btnResume = document.getElementById("btnResumeExec");
    if (status.status === "PAUSED") {
      btnPause.style.display = "none";
      btnResume.style.display = "inline-block";
    } else if (status.status === "RUNNING") {
      btnPause.style.display = "inline-block";
      btnResume.style.display = "none";
    }

    if (["COMPLETED", "FAILED", "CANCELLED"].includes(status.status)) {
      clearInterval(execPollInterval);
      execPollInterval = null;
      document.getElementById("execLockBadge").textContent = "RELEASED";
      document.getElementById("execLockBadge").className = "badge";

      await loadReconciliationReport();
    }
  } catch (e) {
    console.error("Error polling execution status:", e);
  }
}

async function loadReconciliationReport() {
  try {
    const res = await fetch(`/api/execution/reconciliation?month=${encodeURIComponent(currentMonth)}`);
    if (!res.ok) return;
    const rep = await res.json();
    if (!rep || !rep.report) return;

    const r = rep.report;
    const reconCard = document.getElementById("reconciliationCard");
    reconCard.style.display = "block";

    document.getElementById("reconStatusBadge").textContent = r.overall_reconciled ? "RECONCILED_COMPLETE" : "DISCREPANCY_FLAGGED";
    document.getElementById("reconStatusBadge").className = `badge ${r.overall_reconciled ? "badge-verified" : "badge-unresolved"}`;

    document.getElementById("reconRecStatus").textContent = `${r.confirmed_receipts_count}/${r.expected_receipts_count} (₹${(r.confirmed_receipts_total || 0).toFixed(2)})`;
    document.getElementById("reconPayStatus").textContent = `${r.confirmed_payments_count}/${r.expected_payments_count} (₹${(r.confirmed_payments_total || 0).toFixed(2)})`;
    document.getElementById("reconOpenBal").textContent = `₹${(r.opening_balance || 18506.60).toLocaleString("en-IN", {minimumFractionDigits: 2})}`;
    document.getElementById("reconCloseBal").textContent = `₹${(r.expected_closing_balance || 0).toLocaleString("en-IN", {minimumFractionDigits: 2})}`;
    document.getElementById("reconVariance").textContent = `₹${(r.closing_balance_variance || 0).toFixed(2)}`;
    document.getElementById("reconVariance").className = (r.closing_balance_variance === 0) ? "text-success" : "text-danger";
    document.getElementById("reconUncertain").textContent = `${r.uncertain_count || 0} (Flagged)`;
    document.getElementById("reconUncertain").className = (r.uncertain_count === 0) ? "text-success" : "text-danger";
  } catch (e) {
    console.error("Error loading reconciliation report:", e);
  }
}


// =========================================================================
// Dashboard & Jobs Table
// =========================================================================

async function refreshDashboard() {
  try {
    const res = await fetch("/api/jobs");
    const data = await res.json();
    const tbody = document.getElementById("jobsTableBody");

    if (!data.jobs || data.jobs.length === 0) {
      tbody.innerHTML = '<tr><td colspan="6" class="text-muted">No processing jobs run yet.</td></tr>';
      return;
    }

    tbody.innerHTML = "";
    data.jobs.slice(0, 8).forEach(j => {
      const tr = document.createElement("tr");
      tr.innerHTML = `
        <td><code>${j.job_id.substring(0, 18)}...</code></td>
        <td>${j.month}</td>
        <td><span class="badge badge-${j.status.toLowerCase()}">${j.status}</span></td>
        <td>${j.current_stage}</td>
        <td>${j.created_at.substring(11, 19)}</td>
        <td>
          <button class="btn btn-sm btn-outline btn-view-job">View</button>
        </td>
      `;
      tr.querySelector(".btn-view-job").addEventListener("click", () => {
        switchTab("processing");
        startJobPolling(j.job_id);
      });
      tbody.appendChild(tr);
    });
  } catch (e) {
    console.error("Failed to refresh dashboard:", e);
  }
}

document.getElementById("btnRefreshJobs").addEventListener("click", refreshDashboard);

// Export draft excel refresh button
document.getElementById("btnExportDraftExcel").addEventListener("click", async () => {
  try {
    const res = await fetch("/api/export/draft", {
      method: "POST",
      headers: { "Content-Type": "application/x-www-form-urlencoded" },
      body: `month=${encodeURIComponent(currentMonth)}`,
    });
    const data = await res.json();
    alert(data.message);
  } catch (e) {
    alert("Error refreshing draft excel: " + e.message);
  }
});
