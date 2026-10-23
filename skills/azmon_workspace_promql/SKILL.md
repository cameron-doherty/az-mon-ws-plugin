---
name: azmon_workspace_promql
description: Query Azure Monitor Workspaces (Managed Prometheus) using PromQL to investigate OpenTelemetry (OTel) hostmetrics and process performance telemetry (CPU, memory, swap/paging, disk I/O latency, filesystem capacity, network errors/connections, and per-process resource usage) across Linux and Windows machines. Use whenever asked about Azure Monitor Workspace metrics, Managed Prometheus, PromQL queries, or OTel machine/host performance telemetry.
---

# Azure Monitor Workspace (PromQL & OpenTelemetry Host/Process Telemetry) Skill

## When to Use This Skill

Use this skill whenever you need to:
- Discover or query an **Azure Monitor Workspace** (`Microsoft.Monitor/accounts` / Azure Managed Prometheus).
- Run **PromQL** instant queries (`/api/v1/query`), range queries (`/api/v1/query_range`), series metadata lookups (`/api/v1/series`), or metric discovery (`/api/v1/label/__name__/values`).
- Investigate **OpenTelemetry (`hostmetrics` and `process`)** performance telemetry from Linux or Windows machines (CPU saturation, kernel/iowait/interrupt spikes, memory pressure, swap paging, disk I/O latency, filesystem fullness, network drops/TCP states, or top resource-consuming processes).

---

## Dual-Runtime Execution Model (Azure SRE Agent vs. GitHub Copilot App / CLI)

This skill is designed to run in **two environments**:

| Runtime Environment | Available Tools | How to Execute Discovery & PromQL Queries |
|---|---|---|
| **1. Azure SRE Agent (Cloud)** | `RunAzCliReadCommands` (Azure Operation) + built-in Code Execution (`ExecutePythonCode`) | Use `RunAzCliReadCommands` (without `-o` or `--query` flags) to run `az monitor account list` and the `az_rest_command` built by `scripts/promql_helper.py`, and use built-in Python Code Execution to run `promql_helper.py` for URL construction and response summarization. |
| **2. GitHub Copilot App / CLI (Local)** | Local terminal (`powershell` / `bash`) with `python` and `az` CLI | Run `python "<skill_dir>/scripts/promql_helper.py" discover` and `python "<skill_dir>/scripts/promql_helper.py" run ...` directly in the shell. Locally, `promql_helper.py` automatically acquires the `https://prometheus.monitor.azure.com` token from your active `az login` session (`DefaultAzureCredential` / `az account get-access-token`), executes the HTTP request, and outputs the statistical summary in a single step. |

---

## Critical Naming Convention (#1 Gotcha): DOTS vs. Underscores

**Read this first to avoid wasted queries.** Azure Managed Prometheus with OpenTelemetry collectors (especially Azure Arc / hybrid compute / AMA OTel pipelines) frequently stores OTel metrics and labels using **dot-separated** names without unit suffixes, NOT the underscore-separated names found in standard Prometheus exporters.

### Metric Names (Dot-Separated vs. Underscore-Separated)

| OTel / Azure Monitor Workspace Dot Name (Most Common) | Standard Prometheus Exporter Underscore Name |
|---|---|
| `system.cpu.utilization` | `system_cpu_utilization_ratio` / `system_cpu_utilization` |
| `system.cpu.time` | `system_cpu_time_seconds_total` |
| `system.memory.utilization` | `system_memory_utilization_ratio` / `system_memory_utilization` |
| `system.memory.usage` | `system_memory_usage_bytes` |
| `system.disk.io` | `system_disk_io_bytes_total` |
| `system.disk.operations` | `system_disk_operations_total` |
| `system.disk.operation_time` | `system_disk_operation_time_seconds_total` |
| `system.filesystem.usage` | `system_filesystem_usage_bytes` |
| `system.network.io` | `system_network_io_bytes_total` |
| `process.cpu.utilization` | `process_cpu_utilization_ratio` / `process_cpu_utilization` |
| `process.cpu.time` | `process_cpu_time_seconds_total` |
| `process.memory.utilization` | `process_memory_utilization_ratio` / `process_memory_utilization` |
| `process.memory.usage` | `process_memory_usage_bytes` |

**Rule**: Always discover metric names via `/api/v1/label/__name__/values` first (Step 2A). When dot-separated names are present, follow the PromQL syntax rules below.

### Label Names in Dot-Separated OTel Workspaces

Labels also use **dots**, not underscores:

| Label Name | Description | Generic Example Value |
|---|---|---|
| `microsoft.resourceid` | Full ARM resource ID of the machine (machine hostname is the last path segment) | `/subscriptions/<sub-id>/resourcegroups/<rg>/providers/microsoft.hybridcompute/machines/<machine-name>` |
| `microsoft.resourcegroupname` | Resource group of the machine | `rg-prod-compute` |
| `microsoft.resourcetype` | ARM resource type of the machine | `microsoft.hybridcompute/machines` or `microsoft.compute/virtualmachines` |
| `microsoft.subscriptionid` | Subscription GUID of the machine | `<subscription-guid>` |
| `microsoft.amwresourceid` | ARM resource ID of the Azure Monitor Workspace itself | `/subscriptions/<sub-id>/resourcegroups/<rg>/providers/microsoft.monitor/accounts/<workspace-name>` |
| `process.executable.name` | Process executable name | `sqlservr.exe`, `wdavdaemon`, `nginx` |
| `process.command` | Full process command / invocation | `"c:\program files\..."` |
| `process.owner` | Process owner | `root`, `nt service\...` |
| `process.pid` | Process ID | `5548` |
| `state` | CPU state or memory state | `idle`, `user`, `system`, `interrupt`, `nice`, `softirq`, `steal`, `wait`, `iowait`, `used`, `free` |
| `cpu` | Per-core CPU identifier (Windows machines) | `cpu0`, `cpu1` |

---

## PromQL Syntax Rules for Dot-Named Metrics and Labels

Because metric and label names contain dots (`.`), standard bare PromQL identifiers fail with parse errors. You **must** use `{__name__="..."}` selectors and double-quoted label names:

### 1. Using Dot-Separated Metric Names
```promql
# WRONG - dots are not valid in bare PromQL metric identifiers
system.cpu.utilization{state="idle"}

# CORRECT - always use the __name__ selector
{__name__="system.cpu.utilization", state="idle"}
```

### 2. Using Dot-Separated Label Names in `by()` / `without()` Clauses
Labels with dots **must be double-quoted** inside aggregation and grouping clauses:
```promql
# WRONG - unquoted dots in label names fail to parse
avg by (microsoft.resourceid) ({__name__="system.cpu.utilization"})

# CORRECT - quote the dot-separated label name
avg by ("microsoft.resourceid") ({__name__="system.cpu.utilization"})
```

### 3. Multi-Label Grouping
```promql
sum by ("microsoft.resourceid", "process.executable.name") ({__name__="process.cpu.utilization"})
```

---

## Ready-to-Use PromQL Recipes (Dot-Named OTel Schema)

Use these verified expressions directly once `/api/v1/label/__name__/values` confirms dot-separated metric names:

### CPU Utilization % per Machine (range or instant query)
```promql
(1 - avg by ("microsoft.resourceid") ({__name__="system.cpu.utilization", state="idle"})) * 100
```

### Memory Utilization % per Machine (range or instant query)
```promql
avg by ("microsoft.resourceid") ({__name__="system.memory.utilization"}) * 100
```

### Top N Processes by CPU % per Machine (instant or range query)
```promql
topk(10, sum by ("microsoft.resourceid", "process.executable.name") ({__name__="process.cpu.utilization"})) * 100
```

### Top N Processes by Memory % per Machine (instant or range query)
```promql
topk(10, sum by ("microsoft.resourceid", "process.executable.name") ({__name__="process.memory.utilization"})) * 100
```

> **Why `sum by` for process metrics?** `sum by ("microsoft.resourceid", "process.executable.name")` aggregates across multiple PIDs of the same executable name and across CPU states (`user`, `system`), giving the total resource footprint of that executable on each machine. For additional disk, filesystem, network, and underscore-schema recipes, see `references/otel_host_process_promql.md`.

---

## Critical Guardrails (API & Tool Constraints)

1. **NEVER pass `-o` / `--output` or `--query` to `RunAzCliReadCommands`**:
   - SRE Agent's `RunAzCliReadCommands` tool automatically formats output as JSON. Passing `-o json`, `-o table`, `-o tsv`, or `--query` causes the tool to fail with `"Unknown error occurred"`. Always invoke plain read commands and parse the returned JSON.
2. **Always use `GET` with `az rest` and `--resource "https://prometheus.monitor.azure.com"`**:
   - The Azure Monitor Workspace Prometheus endpoint is a **data-plane API** requiring an Entra ID token for `https://prometheus.monitor.azure.com`.
   - Always run `az rest --method get --resource "https://prometheus.monitor.azure.com" --url "https://<endpoint>/api/v1/..."` so the request executes as a **Safe** read command under `RunAzCliReadCommands` without write-approval prompts.
3. **Azure Monitor Workspace PromQL API Limitations**:
   - **Mandatory single `__name__` matcher**: Every `/query`, `/query_range`, and `/series` call **must** include an explicit metric name (e.g., `{__name__="system.cpu.utilization"}`). Bare label selectors like `{state="idle"}` fail with HTTP 400/422.
   - **No regex on `__name__`**: `{__name__=~"system.*"}` is NOT supported. Query each metric individually.
   - **Label values API restriction**: `/api/v1/label/<label>/values` is **ONLY supported for `__name__`** (`/api/v1/label/__name__/values`). To discover values of other labels, use `/api/v1/series?match[]={__name__="<metric>"}` or run an unfiltered instant query `{__name__="<metric>"}`.
   - **No regex in `/api/v1/series` & 12-hour max window**: `/api/v1/series` only supports exact matchers (`=`, `!=`), not `=~` or `!~`, and clamps time ranges to a maximum of **12 hours**.
   - **32-day maximum `/api/v1/query_range` window**: Total range + query lookback (e.g., `[5m]`, `[1h]`) cannot exceed **32 days**.
   - **Case-insensitivity**: Metric names, label names, and label values are case-insensitive in Azure Managed Prometheus.

---

## Supporting Files in This Skill

- **`scripts/promql_helper.py`**: Python helper module and CLI for PromQL constraint validation (supporting both `{__name__="system.cpu.utilization"}` dot-syntax and underscore syntax), safe URL encoding, `az rest` command construction, direct token execution, and time-series statistical summarization.
- **`references/otel_host_process_promql.md`**: Ready-to-run PromQL recipes for both **dot-separated** (`system.cpu.*`, `process.*`) and **underscore-separated** (`system_cpu_*`, `process_*`) OpenTelemetry `hostmetrics` and `process` receivers across Linux and Windows.

---

## Step-by-Step Discovery & Query Workflow (Fast Path)

When starting a new investigation, follow this exact sequence to get results on the first pass:

### Step 1: Discover the Azure Monitor Workspace & Prometheus Query Endpoint

If the user has not provided the Prometheus query endpoint (`https://<workspace>.<region>.prometheus.monitor.azure.com`), discover Azure Monitor Workspaces (`Microsoft.Monitor/accounts`):

- **In Azure SRE Agent (Cloud)** via `RunAzCliReadCommands` (remember: **no `-o` or `--query` flags**):
  ```bash
  az monitor account list
  ```
  Or for a specific subscription / across all subscriptions via Azure Resource Graph:
  ```bash
  az monitor account list --subscription <subscription-id>
  az graph query -q "Resources | where type =~ 'microsoft.monitor/accounts'"
  ```
- **In GitHub Copilot App / CLI (Local)**:
  ```bash
  python "<skill_dir>/scripts/promql_helper.py" discover
  ```

From the returned JSON, extract `name`, `id`, and `metrics.prometheusQueryEndpoint`.
> **Multi-Workspace Tip:** If multiple Azure Monitor Workspaces exist in the environment, an inactive workspace may still list metric names in `/api/v1/label/__name__/values` while having no active machine-level time series. Verify active series in Step 3 before running full range queries.

---

### Step 2: List Metric Names (`/api/v1/label/__name__/values`)

Always check the metric names first to confirm whether the workspace uses **dot-separated** names (`system.cpu.utilization`, `process.cpu.utilization`) or **underscore-separated** names (`system_cpu_time_seconds_total`):

- **In Azure SRE Agent (Cloud)** via `RunAzCliReadCommands`:
  ```bash
  az rest --method get --resource "https://prometheus.monitor.azure.com" --url "https://<prometheus-query-endpoint>/api/v1/label/__name__/values"
  ```
- **In GitHub Copilot App / CLI (Local)**:
  ```bash
  python "<skill_dir>/scripts/promql_helper.py" run \
    --endpoint "https://<prometheus-query-endpoint>" \
    --api metric_names \
    --filter "system[._]|process[._]|target_info"
  ```

---

### Step 3: Discover Machines, Labels & OS Signatures

Because `/api/v1/label/<label>/values` is only supported for `__name__`, discover all reporting machines and their exact label keys using either `/api/v1/series` (with a 2–6h window) or a raw instant query:

1. **Series metadata lookup**:
   Query `/api/v1/series?match[]={__name__="system.cpu.utilization"}` (or `system.cpu.time` / `system_cpu_time_seconds_total` based on Step 2).
2. **Raw sample instant query**:
   Run an unfiltered instant query `{__name__="system.cpu.utilization"}` against `/api/v1/query` to see the exact label key names (e.g., `microsoft.resourceid`, `cpu`, `state`) and live sample values before building aggregated queries.

#### How to Identify Machine Hostname & OS from Telemetry
- **Machine Name**: When labeled with `microsoft.resourceid` (e.g., `/subscriptions/.../providers/microsoft.hybridcompute/machines/<name>` or `.../microsoft.compute/virtualmachines/<name>`), the machine name is the **last segment** of `microsoft.resourceid`.
- **Windows Machines**:
  - Report `system.cpu.*` with a per-core `cpu` label (e.g., `cpu0`, `cpu1`) and `state` values `idle`, `interrupt`, `system`, `user` (and `dpc`).
  - Report the Windows-only process metric `process.handles`.
- **Linux Machines**:
  - Omit the `cpu` label on `system.cpu.*` (reporting a single aggregated series per state) and report Linux-specific `state` values: `nice`, `softirq`, `steal`, `wait`, `iowait`.
  - Report Linux-only metrics: `system.linux.memory.available`, `system.linux.memory.dirty`, `process.open_file_descriptors`, and `process.signals_pending`.

---

### Step 4: Build, Execute & Summarize PromQL Queries

#### Option A: Using `promql_helper.py` in `ExecutePythonCode` (Recommended)
In Azure SRE Agent, plugins are mounted dynamically under `runtime-content/skills/plugins/...`. Use `glob` to locate `promql_helper.py` automatically regardless of working directory:

```python
import glob, os, sys
helper_matches = glob.glob("**/promql_helper.py", recursive=True)
if helper_matches:
    sys.path.insert(0, os.path.dirname(os.path.abspath(helper_matches[0])))
import promql_helper

result = promql_helper.build_az_rest_request(
    endpoint="https://<prometheus-query-endpoint>",
    api="query_range",  # or "query", "series", "metric_names"
    query='(1 - avg by ("microsoft.resourceid") ({__name__="system.cpu.utilization", state="idle"})) * 100',
    start="-2h",
    end="now",
    step="5m",
)
print(result["az_rest_command"])
```

Then execute `result["az_rest_command"]` via `RunAzCliReadCommands` (with **no** `-o`, `--output`, or `--query` flags):
```bash
az rest --method get --resource "https://prometheus.monitor.azure.com" --url "<URL from Step 4>"
```

Pass the returned JSON payload back to `promql_helper.summarize_prometheus_response(raw_json, top_n=20)` in `ExecutePythonCode` to get per-machine `machine_name`, `min`, `max`, `avg`, `p95`, `latest`, `delta`, and `peak_timestamp`.

#### Option B: Direct URL Construction in Python (Lightweight Alternative)
If you need to construct a URL-encoded `az rest` command directly in Python without loading the helper:

```python
import urllib.parse

ep = "https://<prometheus-query-endpoint>"
query = '(1 - avg by ("microsoft.resourceid") ({__name__="system.cpu.utilization", state="idle"})) * 100'
params = [
    ("query", query.strip()),
    ("start", "2026-10-22T12:00:00Z"),
    ("end", "2026-10-22T14:00:00Z"),
    ("step", "5m"),
]
qs = urllib.parse.urlencode(params, quote_via=urllib.parse.quote)
url = f"{ep}/api/v1/query_range?{qs}"
cmd = f'az rest --method get --resource "https://prometheus.monitor.azure.com" --url "{url}"'
print(cmd)
```

---

## OpenTelemetry Machine Triage Playbook (Golden Signals for Hosts)

When asked to diagnose machine performance or investigate a degraded host, evaluate the 6 host telemetry pillars:

| Pillar | Primary OTel Metrics (Dot / Underscore) | Key Diagnostic Signals & Thresholds |
|---|---|---|
| **1. CPU Saturation** | `system.cpu.utilization`, `system.cpu.time` (`system_cpu_time_seconds_total`), `system.cpu.load_average.1m`/`5m`/`15m` | Total CPU **> 85%** sustained; Linux `state="wait"` / `"iowait"` **> 15%** (disk wait) or `state="steal"` **> 5%** (hypervisor throttling); Windows `state="interrupt"` / `"dpc"` **> 10%**. |
| **2. Memory & Paging** | `system.memory.utilization`, `system.memory.usage` (`system_memory_usage_bytes`), `system.linux.memory.available`, `system.paging.usage`, `system.paging.faults`, `system.paging.operations` | Used memory **> 90%** (or available memory `< 5%`); non-zero rate of `system.paging.faults{type="major"}` or `system.paging.operations{type="major"}` indicating active swap thrashing. |
| **3. Disk I/O & Latency** | `system.disk.io` (`system_disk_io_bytes_total`), `system.disk.operations`, `system.disk.operation_time`, `system.disk.pending_operations` | Average disk I/O latency (`rate(operation_time) / rate(operations) * 1000`) **> 15 ms** (warning) or **> 30 ms** (critical storage throttling); `system.disk.pending_operations` **> 4** per disk. |
| **4. Filesystem Capacity** | `system.filesystem.utilization`, `system.filesystem.usage` (`system_filesystem_usage_bytes`), `system.filesystem.inodes.usage` | Capacity utilization **> 85%** (warning) or **> 95%** (imminent disk-full outage); Linux inode usage **> 90%**. |
| **5. Network & TCP Health** | `system.network.io` (`system_network_io_bytes_total`), `system.network.errors`, `system.network.dropped`, `system.network.connections` | Any non-zero `rate(system.network.errors[5m])` or `rate(system.network.dropped[5m])`; elevated TCP `CLOSE_WAIT` (socket leak) or `TIME_WAIT` / `SYN_SENT` (SNAT/port exhaustion). |
| **6. Top Processes** | `process.cpu.utilization`, `process.cpu.time`, `process.memory.utilization`, `process.memory.usage`, `process.disk.io`, `process.threads`, `process.open_file_descriptors`, `process.handles` | Correlate the exact `process.executable.name` and `process.pid` spiking at the `peak_timestamp` identified in Pillars 1–3. |

---

## Troubleshooting Common Errors

| Symptom / HTTP Status | Root Cause | Resolution |
|---|---|---|
| **HTTP 401 / 403 Forbidden** on `*.prometheus.monitor.azure.com` | The SRE Agent's User-Assigned Managed Identity (UAMI) lacks the **`Monitoring Data Reader`** role on the Azure Monitor Workspace. (`Monitoring Reader` and `Monitoring Contributor` do **not** include `Microsoft.Monitor/accounts/Metrics/read`). | Instruct the administrator to grant **`Monitoring Data Reader`** (`b0d8363b-8ddd-447d-831f-62ca05bff136`) to the agent's UAMI on the Azure Monitor Workspace resource. |
| **`RunAzCliReadCommands` returns `"Unknown error occurred"`** | Command included `-o`, `--output`, or `--query`, or experienced a transient first-call failure. | Remove all `-o` / `--output` / `--query` flags and retry the plain command once. |
| **HTTP 400 parse error on dot-named metrics or labels** | Used bare dot-metric name (`system.cpu.utilization`) or unquoted dot-label name (`by (microsoft.resourceid)`). | Wrap dot-named metrics in `{__name__="system.cpu.utilization"}` and double-quote dot-named labels in grouping clauses: `by ("microsoft.resourceid")`. |
| **HTTP 400 / 422 on `/query` or `/query_range`** | Query omitted a metric name (`__name__`), used regex on `__name__`, or exceeded the 32-day total time window. | Validate the query with `promql_helper.validate_promql_query()` and ensure an exact `__name__` matcher is present. |
| **HTTP 400 / 404 on `/api/v1/label/<label>/values`** | Azure Monitor Workspace only supports `/api/v1/label/__name__/values`. | Use `/api/v1/series?match[]={__name__="<metric>"}` or an unfiltered instant query `{__name__="<metric>"}` to inspect label values. |
| **Empty results (`"result": []`) for OTel metrics** | Queried underscore metric names against a dot-named workspace (or vice versa), or queried an inactive workspace when multiple workspaces exist. | Run Step 2 (`/api/v1/label/__name__/values`) and Step 3 (`/api/v1/series?match[]={__name__="system.cpu.utilization"}`) to verify exact metric/label names and active series. |

---

## Expected Output Format

Present findings in a structured diagnostic report:
1. **Workspace & Scope**: Azure Monitor Workspace name, region, Prometheus endpoint, and evaluated time window (UTC).
2. **Machine Health Summary Table**: Machine Name (from `microsoft.resourceid` or `host_name`), OS (Windows/Linux), CPU % (avg / p95 / peak), Memory Used %, Swap Activity, Max Disk Latency (ms), Max Filesystem %, Network Drops/Errors, and Status (`🟢 Healthy`, `🟡 Warning`, `🔴 Critical`).
3. **Top Culprit Processes & Timeline Correlation**: Peak timestamps, offending `process.executable.name` / `process.pid`, and resource deltas.
4. **Root Cause & Actionable Remediation**: Specific recommendations backed by the PromQL evidence.

