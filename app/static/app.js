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
  els.snapshotList = document.getElementById("snapshotList");
  els.toast = document.getElementById("toast");
  els.diagnosisPanel = document.getElementById("diagnosisPanel");
  els.diagnoseBtn = document.getElementById("diagnoseBtn");
  els.diagnosisHint = document.getElementById("diagnosisHint");
  els.diagnosisBody = document.getElementById("diagnosisBody");

  els.duration.addEventListener("change", renderLockInputs);
  els.solveBtn.addEventListener("click", solve);
  els.diagnoseBtn.addEventListener("click", runDiagnosis);
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

  currentSnapshotId = result.snapshot_id ?? null;
  updateDiagnosisPanel(result);

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

function updateDiagnosisPanel(result) {
  if (result.status === "no_solution" && result.snapshot_id) {
    els.diagnosisPanel.hidden = false;
    els.diagnosisBody.innerHTML = "";
    els.diagnosisHint.hidden = false;
    els.diagnosisHint.textContent =
      "该快照已证明无解。可运行诊断：按原精确模型从快照输入还原约束，在共享 10 秒预算内找出极小冲突约束组。";
    els.diagnoseBtn.textContent = "运行诊断";
    loadSavedDiagnosis(result.snapshot_id);
  } else {
    els.diagnosisPanel.hidden = true;
    els.diagnosisBody.innerHTML = "";
  }
}

async function loadSavedDiagnosis(snapshotId) {
  try {
    const response = await fetch(`/api/snapshots/${snapshotId}/diagnosis`);
    if (!response.ok) return;
    const data = await response.json();
    if (currentSnapshotId !== snapshotId) return;
    renderDiagnosis(data, true);
  } catch (error) {
    // 没有已保存的诊断时保持提示状态。
  }
}

async function runDiagnosis() {
  if (!currentSnapshotId) return;
  const snapshotId = currentSnapshotId;
  els.diagnoseBtn.disabled = true;
  els.diagnoseBtn.textContent = "诊断中（最多 10 秒）";
  try {
    const response = await fetch(`/api/snapshots/${snapshotId}/diagnose`, { method: "POST" });
    const data = await response.json();
    if (!response.ok) {
      showToast(typeof data.detail === "string" ? data.detail : "诊断失败", true);
      return;
    }
    if (currentSnapshotId !== snapshotId) return;
    renderDiagnosis(data, false);
    showToast("诊断完成并已保存");
  } catch (error) {
    showToast(`诊断请求失败：${error.message}`, true);
  } finally {
    els.diagnoseBtn.disabled = false;
    els.diagnoseBtn.textContent = "重新诊断";
  }
}

function describeGroup(group) {
  const d = group.detail || {};
  switch (group.kind) {
    case "setpoint_range":
      return `位置：第 ${d.minutes[0]}～${d.minutes[d.minutes.length - 1]} 分钟全部设定值`;
    case "ramp_limit":
      return `位置：相邻分钟对 ${d.pairs.map((p) => `${p[0]}–${p[1]}`).join("、")}，跃变上限 ${d.max_change}℃`;
    case "lock":
      return `位置：第 ${d.minute} 分钟，锁定值 ${d.value}℃`;
    case "zone_upper":
      return `关联轨迹：${d.zone_name}（α=${d.coefficients.join("、")}）；位置：第 ${d.minutes[0]}～${d.minutes[d.minutes.length - 1]} 分钟全程（含初温）`;
    case "zone_final":
      return `关联轨迹：${d.zone_name}（α=${d.coefficients.join("、")}）；位置：第 ${d.minute} 分钟末，区间 [${d.low}, ${d.high}]℃`;
    case "zone_exposure":
      return `关联轨迹：${d.zone_name}（α=${d.coefficients.join("、")}）；位置：第 ${d.minutes[0]}～${d.minutes[d.minutes.length - 1]} 分钟累计，阈值 ${d.threshold}℃`;
    default:
      return "";
  }
}

function renderDiagnosis(data, cached) {
  els.diagnosisHint.hidden = true;
  const coreSet = new Set(data.core);
  const coreGroups = data.groups.filter((group) => coreSet.has(group.id));
  const savedAt = data.created_at ? ` · 保存于 ${data.created_at.replace("T", " ").replace("+00:00", " UTC")}` : "";
  els.diagnosisBody.innerHTML = `
    <p class="diagnosis-meta">诊断 #${data.diagnosis_id} · ${cached ? "已保存结果" : "本次运行"} · 耗时 ${data.elapsed_ms} ms / 预算 ${data.timeout_ms} ms · 共 ${data.checks} 次求解${savedAt}</p>
    <p class="result-message">${escapeHtml(data.message)}</p>
    <h4>极小冲突组（${coreGroups.length} 组，删去任一组即有解）</h4>
    <ol class="core-list">
      ${coreGroups.map((group) => `
        <li>
          <strong>${escapeHtml(group.name)}</strong>
          <span class="muted">${escapeHtml(describeGroup(group))}</span>
        </li>`).join("")}
    </ol>
    <h4>逐组删除的可行性见证（从最终冲突子集删除）</h4>
    ${data.witnesses.map((witness) => `
      <div class="witness-card">
        <div class="witness-title">删除「${escapeHtml(witness.group_name)}」→ 有可行曲线</div>
        <div class="witness-curve">${witness.setpoints.map((value, index) => `<span class="chip">U${index + 1}=${value}</span>`).join("")}</div>
        <div class="muted">设定值总和 ${witness.objective_total}（仅满足删除该组后的约束，供定位参考）</div>
      </div>`).join("")}
    <h4>全部可开关约束组（${data.groups.length} 组）</h4>
    <div class="table-wrap">
      <table>
        <thead><tr><th>约束组</th><th>关联轨迹 / 位置</th><th>约束数</th><th>在冲突组中</th></tr></thead>
        <tbody>
          ${data.groups.map((group) => `
            <tr class="${coreSet.has(group.id) ? "core-row" : ""}">
              <td>${escapeHtml(group.name)}</td>
              <td>${escapeHtml(describeGroup(group))}</td>
              <td>${group.constraint_count}</td>
              <td>${coreSet.has(group.id) ? "是" : "否"}</td>
            </tr>`).join("")}
        </tbody>
      </table>
    </div>
  `;
  els.diagnoseBtn.textContent = "重新诊断";
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
  return `
    <div class="snapshot-item">
      <div class="top"><span>#${item.id}</span><span>${item.status_text}</span></div>
      <div class="meta">${item.created_at.replace("T", " ").replace("+00:00", " UTC")}<br>${item.duration ?? "-"} 分钟 · ${item.solver_ms} ms${item.objective_total === null ? "" : ` · 总和 ${item.objective_total}`}</div>
      <button class="link" type="button" onclick="loadSnapshot(${item.id})">载入结果</button>
    </div>
  `;
}

async function loadSnapshot(id) {
  try {
    const response = await fetch(`/api/snapshots/${id}`);
    if (!response.ok) throw new Error("快照不存在");
    const result = await response.json();
    renderResult(result);
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
