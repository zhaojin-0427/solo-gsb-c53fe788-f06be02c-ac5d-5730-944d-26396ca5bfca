# 双区烘箱曲线试算 Web 应用

使用 Python、FastAPI、SQLite、Z3 与原生 JavaScript 实现。应用会为两个温区、各自两种响应系数共四条轨迹寻找同一条共享设定温度曲线，并自动保存每次试算快照。

## 一键启动

要求已安装 Docker 与 Docker Compose v2：

```bash
docker compose up --build -d
```

启动后访问：

- Web 页面：<http://localhost:8000>
- 健康检查：<http://localhost:8000/api/health>
- 快照列表 API：<http://localhost:8000/api/snapshots>
- 在页面选择一条“约束无解”快照即可一键“诊断无解原因”，诊断独立保存、可随时重看。

停止服务：

```bash
docker compose down
```

SQLite 数据库保存在宿主机 `./data/oven.db`，容器内路径为 `/data/oven.db`。

## 输入参数

- 试算时长：1～24 分钟。
- 相邻分钟设定温度最大跃变：0～300℃ 的非负整数。
- 两个温区的整数初温：0～300℃。
- 每个温区两种响应系数 α：0.01～1.00，最多两位小数。
- 每个温区录入：
  - 终温闭区间；
  - 全程温度上限（含初温与每分钟末温度）；
  - 热暴露阈值；
  - 非负最低累计热暴露。
- 共享设定温度：0～300℃ 整数。
- 相邻分钟设定温度跃变：不超过给定非负整数。
- 可锁定任意分钟的设定温度；留空表示不锁定。

## 数学模型

对每个温区、每种系数，从初温开始按分钟精确递推：

\[
T(t+1)=T(t)+\alpha\bigl(U(t)-T(t)\bigr)
\]

其中：

- \(U(t)\) 是第 \(t\) 分钟共享设定温度，为 0～300 的整数；
- \(T(t)\) 为分钟末实际温度；
- 第 \(t\) 分钟末热暴露增量为 `max(0, T(t) - 阈值)`；
- 全程热暴露量是各分钟末增量之和。

所有四条轨迹必须同时满足：

1. 初温及每个分钟末温度不超过温区全程上限；
2. 最终温度位于终温闭区间；
3. 累计热暴露达到最低要求；
4. 设定温度范围、相邻跃变和锁定值要求。

优化目标按严格优先级排列：

1. 最小化设定温度总和；
2. 在总和相同的曲线中，按分钟序最小化 \(U(1), U(2),\ldots,U(N)\)。

Z3 模型使用整数设定温度与有理数温度/热暴露变量。后端在取得模型后，还用 Python `Fraction` 重新独立递推和校验，避免浮点误差。

## 求解状态与 10 秒限时

每次求解的 Z3 总限时为 10 秒，页面明确区分：

- **已证明最优**：找到可行曲线，并完成两级字典序最优性证明；
- **找到可行曲线，尚未证明最优**：已找到并校验曲线，但限时内未完成最优性证明；
- **约束无解**：Z3 证明不存在满足全部约束的曲线；
- **尚未找到曲线**：限时内未取得可行候选，不代表约束一定无解。

## 页面结果

页面会为每种温区和响应系数展示逐分钟表格：

- 分钟；
- 设定温度；
- 实际温度；
- 距全程上限的约束余量；
- 本分钟末热暴露增量；
- 累计热暴露。

表格顶部还展示终温距区间两端的余量、热暴露达标余量和最小全程温度余量。鼠标悬停数值可查看精确分数及精确有限小数值。

每次点击“开始试算”都会保存一条快照，包含原始输入、状态、耗时、曲线和全部轨迹余量。可从右侧快照列表重新载入。

## 无解原因诊断

当某次试算状态为 **约束无解**（Z3 已证明不存在可行曲线）时，可以在该快照上触发无解原因诊断。诊断从快照的 `request_json` 还原输入、使用与原求解**完全相同的精确 Z3 模型**，绝不读取页面当前表单。

诊断把模型约束划分为：

- **始终有效**（不参与开关）：设定值整数性、温度逐分钟精确递推、各分钟末温度非负、热暴露正部 `max(0, T-阈值)` 定义；
- **可开关组**（按固定顺序排列）：
  1. 全部设定值的 0～300 范围（一组）；
  2. 全部相邻分钟跃变上限（一组）；
  3. 每个被锁定分钟的锁定值各一组（按分钟递增）；
  4. 再按输入温区顺序，每个温区依次给出「该温区全部两条轨迹的全程温度上限」「终温闭区间」「最低累计热暴露」各一组。

### 极小冲突组（group-MUS）算法

从全集开始按上述顺序逐组试删：**只有删除后剩余约束仍被精确模型证明无解，才真正删除该组**；删除后即可行（或预算内无法判定）的组保留。最终留下的是一个仍无解、且删掉其中任意一组都会可行的极小子集。

为在 10 秒预算内完成，删除扫描分两步：先用带临时极宽界（±10⁹）的模型快速试探（其 `sat` 是精确的、且见证还会用 Python `Fraction` 独立复核；其 `unsat` 只作为候选），再对每个候选删除用**无界原模型**严格证明无解后才删除。因此最终记录的极小组每一条删除都有无界精确模型的无解证明。

### 可行性见证

对最终极小组中的每一组，单独删除该组（从**最终子集**出发重新求解，绝不用全集删组的结果代替）取得一条整数设定曲线，并用 `Fraction` 独立重建四条轨迹、校验所有始终有效约束与剩余组约束全部精确成立；同时列出该曲线违反了哪些已关闭组（例如哪些设定值越界、哪条轨迹终温越界），直观说明该组为何属于冲突根源。

### 预算与保存语义

诊断的所有 Z3 重跑（复证无解、全部试删、全部见证）**共享同一个 10 秒预算**（单次检查另有墙钟上限，避免一次困难重跑耗尽全部预算）。

- 成功：诊断**独立保存**在单独的数据库表中，可随时通过页面或 `GET` 接口重新查看；
- 预算内未决/超时，或见证无法取得/未通过精确校验：**不保存任何部分诊断**，接口返回 409 且 `detail.code` 为 `inconclusive`；
- 只有已证明无解（`no_solution`）的快照可诊断；`尚未找到曲线`/超时等未决快照返回 409、`not_proved_unsat`；
- 快照不存在返回 404；快照缺少或无法解析保存的输入返回 404、`missing_input`。

页面上：在“约束无解”的试算结果面板，或右侧快照列表对应条目，点击“诊断无解原因”即可触发；诊断完成后可“查看诊断”。诊断面板展示极小组的组名、关联的轨迹或位置，以及逐组删除的可行性见证。

## 本地非 Docker 运行（可选）

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

## 主要接口

- `POST /api/solve`：提交参数并求解；
- `GET /api/snapshots`：查看最近 50 条快照（含 `has_diagnosis` 标记）；
- `GET /api/snapshots/{id}`：读取单条快照完整结果；
- `POST /api/snapshots/{id}/diagnose`：对“约束无解”快照运行极小冲突组诊断（独立保存，可重复调用覆盖）；
- `GET /api/snapshots/{id}/diagnose`：读取已保存的诊断。

### 诊断接口示例

先制造一条无解快照并诊断（终温区间整体高于全程上限）：

```bash
curl -s -X POST http://localhost:8000/api/solve \
  -H 'Content-Type: application/json' \
  -d '{
    "duration": 6, "max_setpoint_change": 80, "locked_minutes": {"6": 100},
    "zones": [
      {"name":"A 区","initial_temperature":20,"coefficients":["0.25","0.60"],
       "final_low":"200","final_high":"250","max_temperature":"150",
       "exposure_threshold":"80","min_exposure":"20"},
      {"name":"B 区","initial_temperature":20,"coefficients":["0.15","0.45"],
       "final_low":"90","final_high":"120","max_temperature":"140",
       "exposure_threshold":"60","min_exposure":"20"}
    ]
  }'
# 返回中的 snapshot_id 记为 $ID

curl -s -X POST http://localhost:8000/api/snapshots/$ID/diagnose
```

成功响应（节选）：

```json
{
  "snapshot_id": 1,
  "elapsed_ms": 529,
  "timeout_ms": 10000,
  "total_groups": 9,
  "minimal_groups": ["zone_0_final", "zone_1_final"],
  "groups": [
    {"key":"zone_0_final","name":"A 区：终温闭区间 [200, 250]℃","kind":"final_range",
     "related":{"zone_index":1,"zone_name":"A 区","low":"200","high":"250",
                "trajectories":[{"coefficient_index":1,"coefficient":"0.25"},
                                {"coefficient_index":2,"coefficient":"0.60"}]}}
  ],
  "witnesses": [
    {
      "removed_group": "zone_0_final",
      "setpoints": [288, 59, 105, 63, 375, -2],
      "setpoint_sum": 894,
      "trajectories": [
        {"zone_name":"A 区","coefficient":"0.25",
         "final_temperature":{"fraction":"29455/256","decimal":"115.05859375","display":"115.0586"},
         "max_temperature":{"display":"150.0000"},
         "exposure_total":{"display":"40.0000"},
         "final_ok": false, "upper_ok": true, "exposure_ok": true}
      ],
      "violations": [
        {"group_key":"setpoint_range","kind":"setpoint_range","minute":5,"value":375,"low":0,"high":300}
      ]
    }
  ],
  "traces": [{"stage":"verify_full","outcome":"unsat","elapsed_ms":20,"attempts":[...] }]
}
```

- `minimal_groups`：仍无解、删任一组即可行的极小子集；
- `groups[].related`：每组关联的轨迹（温区、α）或位置（分钟、区间等）；
- `witnesses`：从最终极小组逐组删除重跑并经精确分数校验的可行性见证，`violations` 列出被违反的已关闭组；
- `traces`：每次重跑的阶段、结论、引擎尝试与耗时，便于核对 10 秒预算的使用。

失败时 HTTP 状态码非 2xx，`detail.code` 区分原因（如 `inconclusive`、`not_proved_unsat`、`missing_input`、`no_diagnosis`）。
