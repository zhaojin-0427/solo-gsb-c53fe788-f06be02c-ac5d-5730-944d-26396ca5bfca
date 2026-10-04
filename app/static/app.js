const defaults = {
  duration: 6,
  maxSetpointChange: 80,
  zones: [
    {
      name: "A 区",
      initial_temperature: 20,
      coefficients: ["0.25", "0.60"],
      final_low: "105",
      final_high: "125",
      max_temperature: "150",
      exposure_threshold: "80",
      min_exposure: "20"
    },
    {
      name: "B 区",
      initial_temperature: 20,
      coefficients: ["0.15", "0.45"],
      final_low: "90",
      final_high: "120",
      max_temperature: "140",
      exposure_threshold: "60",
      min_exposure: "20"
    }
  ],
  locks: { 6: 100 }
};

const els = {};
let currentSnapshotId = null;

function onReady() {
  els.duration = document.getElementById("duration");
  els.maxSetpointChange = document.getElementById("maxSetpointChange");
  els.zones = document.getElementById("zones");
  els.lockInputs = document.getElementById("lockInputs");
  els.solveBtn = document.getElementById("solveBtn");
  els.resetBtn = document.getElementById("resetBtn");
  els.resultPanel = document.getElementById("resultPanel");
  els.statusBadge = document.getElementById("statusBadge");
  els.solveMeta = document.getElementById("solveMeta");
  els.resultMessage = document.getElementById("resultMessage");
  els.resultBody = document.getElementById("resultBody");
  els.diagnoseActions = document.getElementById("diagnoseActions");
  els.diagnoseBtn = document.getElementById("diagnoseBtn");
  els.viewDiagnosisBtn = document.getElementById("viewDiagnosisBtn");
  els.diagnosisPanel = document.getElementById("diagnosisPanel");
  els.snapshotList = document.getElementById("snapshotList");
  els.toast = document.getElementById("toast");

  els.duration.addEventListener("change", renderLockInputs);
  els.solveBtn.addEventListener("click", solve);
  els.diagnoseBtn.addEventListener("click", () => diagnoseSnapshot(currentSnapshotId));
  els.viewDiagnosisBtn.addEventListener("click", () => viewDiagnosis(currentSnapshotId));
  els.resetBtn.addEventListener("click", () => {
    fillForm(defaults);
    showToast("已恢复示例参数");
  });

  renderZones();
  fillForm(defaults);
  loadSnapshots();
}

function renderZones() {
  els.zones.innerHTML = defaults.zones.map((_, zoneIndex) => `
    <article class="zone-card" data-zone="${zoneIndex}">
      <h3>温区 ${zoneIndex + 1}</h3>
      <div class="form-grid">
        <label class="wide">名称<input data-field="name" value="温区 ${zoneIndex + 1}" /></label>
        <label>整数初温（℃）<input data-field="initial_temperature" type="number" min="0" max="300" step="1" /></label>
        <label>全程上限（℃）<input data-field="max_temperature" type="number" min="0" max="300" step="0.01" /></label>
        <label>响应系数 α₁<input data-field="coefficient-0" type="number" min="0.01" max="1" step="0.01" /></label>
        <label>响应系数 α₂<input data-field="coefficient-1" type="number" min="0.01" max="1" step="0.01" /></label>
        <label>终温下限（℃）<input data-field="final_low" type="number" min="0" max="300" step="0.01" /></label>
        <label>终温上限（℃）<input data-field="final_high" type="number" min="0" max="300" step="0.01" /></label>
        <label>热暴露阈值（℃）<input data-field="exposure_threshold" type="number" min="0" max="300" step="0.01" /></label>
        <label>最低累计热暴露<input data-field="min_exposure" type="number" min="0" step="0.01" /></label>
      </div>
    </article>
  `).join("");
}

function fillForm(data) {
  els.duration.value = data.duration;
  els.maxSetpointChange.value = data.maxSetpointChange;

  if (els.zones.children.length === 0) renderZones();
  document.querySelectorAll(".zone-card").forEach((card, zoneIndex) => {
    const zone = data.zones[zoneIndex];
    card.querySelector('[data-field="name"]').value = zone.name;
    card.querySelector('[data-field="initial_temperature"]').value = zone.initial_temperature;
    card.querySelector('[data-field="max_temperature"]').value = zone.max_temperature;
    card.querySelector('[data-field="coefficient-0"]').value = zone.coefficients[0];
    card.querySelector('[data-field="coefficient-1"]').value = zone.coefficients[1];
    card.querySelector('[data-field="final_low"]').value = zone.final_low;
    card.querySelector('[data-field="final_high"]').value = zone.final_high;
    card.querySelector('[data-field="exposure_threshold"]').value = zone.exposure_threshold;
    card.querySelector('[data-field="min_exposure"]').value = zone.min_exposure;
  });

  renderLockInputs(data.locks || {});
}

function currentLocksFromInputs() {
  const locks = {};
  document.querySelectorAll("#lockInputs input").forEach((input) => {
    if (input.value !== "") locks[input.dataset.minute] = Number(input.value);
  });
  return locks;
}

function renderLockInputs(savedLocks) {
  const locks = savedLocks || currentLocksFromInputs();
  const duration = clampInt(els.duration.value, 1, 24, defaults.duration);
  els.duration.value = duration;
  els.lockInputs.innerHTML = Array.from({ length: duration }, (_, index) => {
    const minute = index + 1;
    const value = locks[minute] ?? locks[String(minute)] ?? "";
    return `<label>第 ${minute} 分钟<input type="number" min="0" max="300" step="1" data-minute="${minute}" value="${value}" /></label>`;
  }).join("");
}

function readForm() {
  const zones = Array.from(document.querySelectorAll(".zone-card")).map((card) => {
    const field = (name) => card.querySelector(`[data-field="${name}"]`).value.trim();
    return {
      name: field("name"),
      initial_temperature: clampInt(field("initial_temperature"), 0, 300),
      coefficients: [field("coefficient-0"), field("coefficient-1")],
      final_low: field("final_low"),
      final_high: field("final_high"),
      max_temperature: field("max_temperature"),
      exposure_threshold: field("exposure_threshold"),
      min_exposure: field("min_exposure")
    };
  });

  const locked = {};
  document.querySelectorAll("#lockInputs input").forEach((input) => {
    if (input.value.trim() !== "") locked[Number(input.dataset.minute)] = clampInt(input.value, 0, 300);
  });

  return {
    duration: clampInt(els.duration.value, 1, 24),
    max_setpoint_change: clampInt(els.maxSetpointChange.value, 0, 300),
    locked_minutes: locked,
    zones
  };
}

async function solve() {
  const payload = readForm();
  els.solveBtn.disabled = true;
  els.solveBtn.textContent = "求解中（最多 10 秒）";
  try {
    const response = await fetch("/api/solve", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload)
    });
    const data = await response.json();
    if (!response.ok) {
      showToast(formatErrors(data.detail), true);
      return;
    }
    renderResult(data);
    await loadSnapshots();
  } catch (error) {
    showToast(`请求失败：${error.message}`, true);
  } finally {
    els.solveBtn.disabled = false;
    els.solveBtn.textContent = "开始试算";
  }
}

function renderResult(result) {
  els.resultPanel.hidden = false;
  els.statusBadge.textContent = result.status_text;
  els.statusBadge.className = `badge ${result.status}`;
  els.resultMessage.textContent = result.message;
  els.solveMeta.innerHTML = `求解耗时 ${result.elapsed_ms} ms / 限时 ${result.timeout_ms} ms<br>快照编号 #${result.snapshot_id}`;

  currentSnapshotId = result.snapshot_id;
  renderDiagnoseActions(result.status === "no_solution", !!result.has_diagnosis);
  els.diagnosisPanel.hidden = true;
  els.diagnosisPanel.innerHTML = "";

  if (!result.setpoints || result.setpoints.length === 0) {
    els.resultBody.innerHTML = "";
    els.resultPanel.scrollIntoView({ behavior: "smooth", block: "nearest" });
    return;
  }

  const input = result.input;
  els.resultBody.innerHTML = `
    <div class="curve-summary">
      <div class="metric"><div class="label">设定温度总和（首要目标）</div><div class="value">${result.objective_total} ℃·min</div></div>
      <div class="metric"><div class="label">时长</div><div class="value">${input.duration} 分钟</div></div>
      <div class="metric"><div class="label">相邻最大允许跃变</div><div class="value">${input.max_setpoint_change} ℃</div></div>
    </div>
    ${(result.trajectories || []).map(renderTrajectory).join("")}
  `;
  els.resultPanel.scrollIntoView({ behavior: "smooth", block: "nearest" });
}

function renderTrajectory(trajectory) {
  const summary = trajectory.summary;
  const positive = (value) => value.decimal.startsWith("-") ? "bad" : "good";
  return `
    <div class="trajectory-title">
      <h3>${escapeHtml(trajectory.zone_name)} · α = ${trajectory.coefficient}</h3>
      <span class="pill good">终温 ${summary.final_temperature.display}</span>
      <span class="pill ${positive(summary.final_low_margin)}">距下限 ${summary.final_low_margin.display}</span>
      <span class="pill ${positive(summary.final_high_margin)}">距上限 ${summary.final_high_margin.display}</span>
      <span class="pill ${positive(summary.exposure_margin)}">热暴露余量 ${summary.exposure_margin.display}</span>
      <span class="pill ${positive(summary.minimum_upper_margin)}">最小全程余量 ${summary.minimum_upper_margin.display}</span>
    </div>
    <div class="table-wrap">
      <table>
        <thead>
          <tr>
            <th>分钟 t</th><th>设定 U(t)</th><th>锁定</th><th>距上分钟跃变余量</th><th>距下分钟跃变余量</th><th>温度 T(t)</th>
            <th>距全程上限</th><th>本分钟末热暴露</th><th>累计热暴露</th>
          </tr>
        </thead>
        <tbody>
          ${trajectory.rows.map((row) => `
            <tr class="${row.minute === 0 ? "initial" : ""}">
              <td>${row.minute}</td>
              <td>${row.setpoint === null ? "—" : row.setpoint}</td>
              <td>${row.locked ? "是" : ""}</td>
              <td>${row.ramp_prev_margin === null ? "—" : row.ramp_prev_margin}</td>
              <td>${row.ramp_next_margin === null ? "—" : row.ramp_next_margin}</td>
              <td title="精确值：${row.temperature.fraction} = ${row.temperature.decimal}">${row.temperature.display}</td>
              <td title="精确值：${row.upper_margin.fraction} = ${row.upper_margin.decimal}"
                  class="${row.upper_margin.decimal.startsWith("-") ? "pill bad" : ""}">${row.upper_margin.display}</td>
              <td title="精确值：${row.exposure_delta.fraction} = ${row.exposure_delta.decimal}">${row.exposure_delta.display}</td>
              <td title="精确值：${row.exposure_sum.fraction} = ${row.exposure_sum.decimal}">${row.exposure_sum.display}</td>
            </tr>
          `).join("")}
        </tbody>
      </table>
    </div>
  `;
}

function renderDiagnoseActions(isUnsat, hasDiagnosis) {
  els.diagnoseActions.hidden = !isUnsat;
  els.diagnoseBtn.disabled = false;
  els.diagnoseBtn.textContent = hasDiagnosis
    ? "重新诊断无解原因（约 10 秒内）"
    : "诊断无解原因（约 10 秒内）";
  els.viewDiagnosisBtn.hidden = !hasDiagnosis;
}

async function diagnoseSnapshot(id) {
  if (!id) return;
  els.diagnoseBtn.disabled = true;
  els.diagnoseBtn.textContent = "诊断中：所有重跑共享 10 秒预算…";
  els.diagnosisPanel.hidden = false;
  els.diagnosisPanel.innerHTML = `
    <div class="diagnosis-loading">正在从该快照保存的输入重建原 Z3 模型，逐组试删并生成可行性见证，请稍候…</div>`;
  try {
    const response = await fetch(`/api/snapshots/${id}/diagnose`, { method: "POST" });
    const data = await response.json();
    if (!response.ok) {
      renderDiagnosisError(response.status, data.detail);
      return;
    }
    renderDiagnosis(data);
    renderDiagnoseActions(true, true);
    showToast(`快照 #${id} 的无解原因诊断已完成并保存`);
    await loadSnapshots();
  } catch (error) {
    els.diagnosisPanel.innerHTML = `<div class="diagnosis-error">诊断请求失败：${escapeHtml(error.message)}</div>`;
  } finally {
    els.diagnoseBtn.disabled = false;
    els.diagnoseBtn.textContent = "重新诊断无解原因（约 10 秒内）";
  }
}

async function viewDiagnosis(id) {
  if (!id) return;
  try {
    const response = await fetch(`/api/snapshots/${id}/diagnose`);
    const data = await response.json();
    if (!response.ok) {
      showToast(formatErrors(data.detail), true);
      return;
    }
    els.diagnosisPanel.hidden = false;
    renderDiagnosis(data);
    els.diagnosisPanel.scrollIntoView({ behavior: "smooth", block: "nearest" });
  } catch (error) {
    showToast(`诊断加载失败：${error.message}`, true);
  }
}
window.diagnoseSnapshot = diagnoseSnapshot;
window.viewDiagnosis = viewDiagnosis;

function renderDiagnosisError(status, detail) {
  const info = typeof detail === "object" && detail !== null ? detail : { message: formatErrors(detail) };
  const codeText = info.code ? `<div class="diagnosis-code">错误类型：${escapeHtml(info.code)}</div>` : "";
  els.diagnosisPanel.innerHTML = `
    <div class="diagnosis-error">
      <strong>${status === 409 ? "诊断未完成（未保存任何部分结果）" : "无法诊断"}</strong>
      <div>${escapeHtml(info.message || "请求失败")}</div>
      ${codeText}
    </div>`;
}

function renderDiagnosis(diag) {
  const groupsByKey = Object.fromEntries((diag.groups || []).map((g) => [g.key, g]));
  const minimal = diag.minimal_groups || [];
  const minimalSet = new Set(minimal);

  const groupsHtml = minimal.map((key, index) => {
    const g = groupsByKey[key];
    if (!g) return "";
    return `
      <li class="conflict-group">
        <span class="conflict-order">${index + 1}</span>
        <div class="conflict-body">
          <div class="conflict-name">${escapeHtml(g.name)}</div>
          <div class="conflict-related">${renderGroupRelated(g)}</div>
        </div>
      </li>`;
  }).join("");

  const witnessesHtml = (diag.witnesses || []).map((w) => renderWitness(w, groupsByKey)).join("");

  els.diagnosisPanel.innerHTML = `
    <div class="diagnosis-header">
      <h3>无解原因诊断</h3>
      <div class="diagnosis-meta">
        极小组共 <strong>${minimal.length}</strong> / ${diag.total_groups} 组 ·
        重跑 ${diag.checks} 次 · 共用预算内耗时 ${diag.elapsed_ms} ms / 上限 ${diag.timeout_ms} ms
      </div>
    </div>
    <p class="diagnosis-explain">
      下列约束组仍联立无解；逐组删除其中任意一组后即可行（见下方可行性见证）。
      设定值整数性、温度递推与非负性、热暴露正部定义为始终有效的基础约束，不参与开关。
    </p>
    <ul class="conflict-list">${groupsHtml}</ul>
    <h4 class="witness-heading">逐组删除的可行性见证（取自最终极小组，非全集删组结果）</h4>
    <div class="witness-list">${witnessesHtml}</div>`;
  els.diagnosisPanel.scrollIntoView({ behavior: "smooth", block: "nearest" });
}

function renderGroupRelated(g) {
  const r = g.related || {};
  if (g.kind === "setpoint_range") {
    return `全部 ${r.minutes ? r.minutes.length : 0} 个分钟设定值 · 位置：第 1～${r.minutes ? r.minutes.length : 0} 分钟`;
  }
  if (g.kind === "ramp_limits") {
    return `全部 ${(r.pairs || []).length} 对相邻跃变 · 上限 ${r.max_change}℃`;
  }
  if (g.kind === "lock") {
    return `位置：第 ${r.minute} 分钟 · 锁定值 ${r.value}℃`;
  }
  const tracks = (r.trajectories || [])
    .map((t) => `α=${escapeHtml(t.coefficient)}`)
    .join("、");
  const zone = `${escapeHtml(r.zone_name || "")}（温区 ${r.zone_index}）`;
  if (g.kind === "upper_bound") return `${zone} · 关联轨迹：${tracks} · 上限 ${escapeHtml(r.limit)}℃`;
  if (g.kind === "final_range") return `${zone} · 关联轨迹：${tracks} · 区间 [${escapeHtml(r.low)}, ${escapeHtml(r.high)}]℃`;
  if (g.kind === "min_exposure") {
    return `${zone} · 关联轨迹：${tracks} · 阈值 ${escapeHtml(r.threshold)}℃，最低累计 ${escapeHtml(r.required)}`;
  }
  return tracks ? `${zone} · ${tracks}` : "";
}

function chipClass(ok) {
  return ok ? "good" : "bad";
}

function renderWitness(w, groupsByKey) {
  const removed = groupsByKey[w.removed_group] || { name: w.removed_group };
  const tracks = (w.trajectories || []).map((t) => `
    <tr>
      <td>${escapeHtml(t.zone_name)} · α=${escapeHtml(t.coefficient)}</td>
      <td><span class="mini-pill ${chipClass(t.final_ok)}">终温 ${t.final_temperature.display}</span></td>
      <td><span class="mini-pill ${chipClass(t.upper_ok)}">全程最高 ${t.max_temperature.display}</span></td>
      <td><span class="mini-pill ${chipClass(t.exposure_ok)}">累计热暴露 ${t.exposure_total.display}</span></td>
    </tr>`).join("");

  const violationsHtml = (w.violations || []).length
    ? `
      <div class="violations">
        <div class="violations-title">放宽该组后被违反的约束（说明该组确属冲突根源）：</div>
        <ul>${(w.violations || []).map((v) => `<li>${renderViolation(v)}</li>`).join("")}</ul>
      </div>`
    : `<div class="violations muted-note">该见证未违反任何已关闭约束组。</div>`;

  return `
    <div class="witness-card">
      <div class="witness-title">删除「${escapeHtml(removed.name)}」后 → 可行</div>
      <div class="witness-setpoints">
        见证设定曲线 U(1..N)：
        ${(w.setpoints || []).map((v, i) =>
          `<span class="sp ${v < 0 || v > 300 ? "out" : ""}" title="第 ${i + 1} 分钟">${v}</span>`
        ).join("")}
        <span class="sp-sum">总和 ${w.setpoint_sum}℃·min</span>
      </div>
      <div class="table-wrap witness-table">
        <table>
          <thead><tr><th>轨迹</th><th>终温</th><th>全程最高温</th><th>累计热暴露</th></tr></thead>
          <tbody>${tracks}</tbody>
        </table>
      </div>
      ${violationsHtml}
    </div>`;
}

function renderViolation(v) {
  if (v.kind === "setpoint_range") return `设定值范围：第 ${v.minute} 分钟 U=${v.value}，超出 [${v.low}, ${v.high}]`;
  if (v.kind === "ramp_limits") return `跃变上限：第 ${v.from_minute}→${v.to_minute} 分钟跃变 ${v.change}，超过上限 ${v.limit}`;
  if (v.kind === "lock") return `锁定：第 ${v.minute} 分钟要求 ${v.expected}℃，实际 ${v.actual}℃`;
  if (v.kind === "upper_bound") {
    return `全程上限：${escapeHtml(v.zone_name)} α=${escapeHtml(String(v.coefficient))} 轨迹第 ${v.minute} 分钟温度 ${v.value.display} &gt; ${v.limit}℃`;
  }
  if (v.kind === "final_range") {
    return `终温区间：${escapeHtml(v.zone_name)} 轨迹终温 ${v.actual.display}，不在 [${v.low}, ${v.high}]℃`;
  }
  if (v.kind === "min_exposure") {
    return `最低热暴露：${escapeHtml(v.zone_name)} 轨迹累计 ${v.actual.display}，低于要求 ${v.required}`;
  }
  return escapeHtml(v.group_name || v.group_key);
}

async function loadSnapshots() {
  try {
    const response = await fetch("/api/snapshots");
    const data = await response.json();
    els.snapshotList.innerHTML = data.items.length
      ? data.items.map(renderSnapshotItem).join("")
      : '<p class="muted">暂无快照。</p>';
  } catch (error) {
    els.snapshotList.innerHTML = '<p class="muted">快照加载失败。</p>';
  }
}

function renderSnapshotItem(item) {
  const diagnoseButtons = item.status === "no_solution"
    ? `<div class="snapshot-actions">
         <button class="link" type="button" onclick="diagnoseSnapshot(${item.id})">${item.has_diagnosis ? "重新诊断" : "诊断无解原因"}</button>
         ${item.has_diagnosis ? `<button class="link" type="button" onclick="viewDiagnosis(${item.id})">查看诊断</button>` : ""}
       </div>`
    : "";
  return `
    <div class="snapshot-item">
      <div class="top"><span>#${item.id}</span><span>${item.status_text}</span></div>
      <div class="meta">${item.created_at.replace("T", " ").replace("+00:00", " UTC")}<br>${item.duration ?? "-"} 分钟 · ${item.solver_ms} ms${item.objective_total === null ? "" : ` · 总和 ${item.objective_total}`}${item.has_diagnosis ? " · 已诊断" : ""}</div>
      <div class="snapshot-actions">
        <button class="link" type="button" onclick="loadSnapshot(${item.id})">载入结果</button>
      </div>
      ${diagnoseButtons}
    </div>
  `;
}

async function loadSnapshot(id) {
  try {
    const response = await fetch(`/api/snapshots/${id}`);
    if (!response.ok) throw new Error("快照不存在");
    const result = await response.json();
    renderResult(result);
    if (result.status === "no_solution" && result.has_diagnosis) {
      await viewDiagnosis(id);
    }
    showToast(`已载入快照 #${id}`);
  } catch (error) {
    showToast(error.message, true);
  }
}
window.loadSnapshot = loadSnapshot;

function clampInt(value, min, max, fallback = min) {
  const number = Number.parseInt(value, 10);
  if (!Number.isInteger(number)) return fallback;
  return Math.min(max, Math.max(min, number));
}

function formatErrors(detail) {
  if (typeof detail === "string") return detail;
  if (!Array.isArray(detail)) return "输入参数无效";
  return detail.map((error) => {
    const location = (error.loc || []).slice(1).join(" / ");
    return `${location ? `${location}: ` : ""}${error.msg}`;
  }).join("\n");
}

function escapeHtml(value) {
  return String(value).replace(/[&<>"']/g, (char) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
  }[char]));
}

let toastTimer;
function showToast(message, isError = false) {
  els.toast.textContent = message;
  els.toast.className = `toast${isError ? " error" : ""}`;
  els.toast.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { els.toast.hidden = true; }, isError ? 6000 : 2500);
}

document.addEventListener("DOMContentLoaded", onReady);
