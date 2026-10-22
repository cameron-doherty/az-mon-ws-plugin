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

## Critical Guardrails (Read Before Running Commands)

1. **NEVER pass `-o` / `--output` or `--query` to `RunAzCliReadCommands`**:
   - SRE Agent's `RunAzCliReadCommands` tool automatically formats output as JSON. Passing `-o json`, `-o table`, `-o tsv`, or `--query` causes the tool to fail with `"Unknown error occurred"`. Always invoke plain read commands and parse the returned JSON.
2. **Always use `GET` with `az rest` and `--resource "https://prometheus.monitor.azure.com"`**:
   - The Azure Monitor Workspace Prometheus endpoint is a **data-plane API** requiring an Entra ID token for `https://prometheus.monitor.azure.com`.
   - Always run `az rest --method get --resource "https://prometheus.monitor.azure.com" --url "https://<endpoint>/api/v1/..."` so the request executes as a **Safe** read command under `RunAzCliReadCommands` without write-approval prompts.
3. **Azure Monitor Workspace PromQL API Limitations**:
   - **Mandatory single `__name__` matcher**: Every `/query`, `/query_range`, and `/series` call **must** include an explicit metric name (e.g., `system_cpu_time_seconds_total{...}`). Bare selectors like `{host_name="vm01"}` or regex metric matchers (`{__name__=~"system_cpu.*"}`) fail with HTTP 400/422.
   - **Label values API restriction**: `/api/v1/label/<label>/values` is **ONLY supported for `__name__`** (`/api/v1/label/__name__/values`). Never call `/api/v1/label/host_name/values` or `/api/v1/label/instance/values`—use `/api/v1/series?match[]=<metric_name>` instead.
   - **No regex in `/api/v1/series`**: `/api/v1/series` only supports exact matchers (`=`, `!=`), not `=~` or `!~`, and clamps time ranges to a maximum of **12 hours**.
   - **32-day maximum `/api/v1/query_range` window**: Total range + query lookback (e.g., `[5m]`, `[1h]`) cannot exceed **32 days**.
   - **Case-insensitivity**: Metric names, label names, and label values are case-insensitive in Azure Managed Prometheus.
4. **Use the Bundled Helper Script (`scripts/promql_helper.py`)**:
   - Never hand-craft URL-encoded PromQL query strings manually. Use `scripts/promql_helper.py` via `ExecutePythonCode` to validate constraints, generate exact URL-encoded `az rest` commands (or execute directly), and summarize large Prometheus matrix responses into compact statistics (`min`, `max`, `avg`, `p95`, `latest`, `delta`).

---

## Supporting Files in This Skill

- **`scripts/promql_helper.py`**: Python helper module and CLI for PromQL constraint validation, safe URL encoding, `az rest` command construction, direct token execution, and time-series statistical summarization.
- **`references/otel_host_process_promql.md`**: Ready-to-run PromQL recipes for Linux & Windows OpenTelemetry `hostmetrics` and `process` receivers (`system_cpu_*`, `system_memory_*`, `system_paging_*`, `system_disk_*`, `system_filesystem_*`, `system_network_*`, `process_*`, and `target_info` joins).

---

## Step-by-Step Investigation Workflow

### Step 1: Discover the Azure Monitor Workspace & Prometheus Query Endpoint

If the user has not provided the Prometheus query endpoint (`https://<workspace>.<region>.prometheus.monitor.azure.com`), discover all Azure Monitor Workspaces in the subscription:

- **In GitHub Copilot App / CLI (Local)**:
  ```bash
  python "<skill_dir>/scripts/promql_helper.py" discover
  # Or filtered by subscription / resource group:
  python "<skill_dir>/scripts/promql_helper.py" discover --subscription <subscription-id> --resource-group <resource-group>
  ```
- **In Azure SRE Agent (Cloud)** via `RunAzCliReadCommands` (remember: **no `-o` or `--query` flags**):
  ```bash
  az monitor account list
  ```
  Or for a specific subscription / resource group:
  ```bash
  az monitor account list --subscription <subscription-id> --resource-group <resource-group>
  ```

From the returned JSON, extract:
- `name` and `id` (ARM Resource ID of the Azure Monitor Workspace)
- `prometheusQueryEndpoint` / `metrics.prometheusQueryEndpoint` (e.g., `https://amw-prod-abcd.eastus.prometheus.monitor.azure.com`)

---

### Step 2: Discover Available OTel Metric Names & Machine Labels

Before writing complex PromQL queries, verify the exact metric naming convention (`_seconds_total` / `_bytes` / `_ratio` suffixes vs. unitless names, or custom exporter namespaces) and how machines are labeled (`host_name`, `instance`, `job`, or `target_info`).

#### 2A. List Metric Names (`/api/v1/label/__name__/values`)
- **In GitHub Copilot App / CLI (Local)** (executes directly & summarizes by OTel domain in one command):
  ```bash
  python "<skill_dir>/scripts/promql_helper.py" run \
    --endpoint "https://<prometheus-query-endpoint>" \
    --api metric_names \
    --filter "system_|process_|target_info"
  ```
- **In Azure SRE Agent (Cloud)**:
  Run via `RunAzCliReadCommands`:
  ```bash
  az rest --method get --resource "https://prometheus.monitor.azure.com" --url "https://<prometheus-query-endpoint>/api/v1/label/__name__/values"
  ```
  Then pass the JSON response into `ExecutePythonCode` with `promql_helper.summarize_prometheus_response(payload, filter_pattern="system_|process_|target_info")` to categorize all available OTel metrics by domain (`cpu`, `memory`, `paging`, `disk`, `filesystem`, `network`, `process`, `metadata_target_info`).

#### 2B. Resolve Machine Hostnames, OS Types, and Instances (`/api/v1/series`)
Because `/api/v1/label/host_name/values` is **unsupported** by Azure Monitor Workspace, inspect series metadata using `target_info` or a core host metric (`system_cpu_time_seconds_total` or `system_memory_usage_bytes`).

- **In GitHub Copilot App / CLI (Local)**:
  ```bash
  python "<skill_dir>/scripts/promql_helper.py" run \
    --endpoint "https://<prometheus-query-endpoint>" \
    --api series \
    --match "target_info" \
    --match "system_cpu_time_seconds_total" \
    --start "-6h" --end "now"
  ```
- **In Azure SRE Agent (Cloud)**:
  Use `ExecutePythonCode` with `scripts/promql_helper.py` to build (or execute) the `/api/v1/series` request:

```python
import sys
sys.path.append("skills/azmon_workspace_promql/scripts")
import promql_helper

req = promql_helper.build_az_rest_request(
    endpoint="https://<prometheus-query-endpoint>",
    api="series",
    match=["target_info", "system_cpu_time_seconds_total"],
    start="-6h",
    end="now",
)
print(req["az_rest_command"])
```

Execute the generated `az_rest_command` with `RunAzCliReadCommands` and summarize the discovered `host_name`, `instance`, `os_type`, and `job` labels using `promql_helper.summarize_prometheus_response(response_json)`.

---

### Step 3: Build, Execute & Summarize PromQL Queries

Consult `references/otel_host_process_promql.md` for the appropriate Linux/Windows OTel PromQL expression, then run via `promql_helper.py`:

- **In GitHub Copilot App / CLI (Local)** (CLI one-liner):
  ```bash
  python "<skill_dir>/scripts/promql_helper.py" run \
    --endpoint "https://<prometheus-query-endpoint>" \
    --api query_range \
    --query '(1 - (sum by (host_name, instance) (rate(system_cpu_time_seconds_total{state="idle"}[5m])) / sum by (host_name, instance) (rate(system_cpu_time_seconds_total[5m])))) * 100' \
    --start "-6h" --end "now" --step "5m" --top 20
  ```
- **In Azure SRE Agent (Cloud)** (via `ExecutePythonCode` + `RunAzCliReadCommands` fallback):

```python
import sys
sys.path.append("skills/azmon_workspace_promql/scripts")
import promql_helper

# Example: Overall CPU utilization % per machine over the last 6 hours
promql = """
(
  1 - (
    sum by (host_name, instance) (rate(system_cpu_time_seconds_total{state="idle"}[5m]))
    /
    sum by (host_name, instance) (rate(system_cpu_time_seconds_total[5m]))
  )
) * 100
"""

result = promql_helper.execute_or_build(
    endpoint="https://<prometheus-query-endpoint>",
    api="query_range",
    query=promql,
    start="-6h",
    end="now",
    step="5m",
    top_n=20,
)
print(result)
```

- **If `result["executed"] == True`**: `result["summary"]` already contains the per-series `min`, `max`, `avg`, `p95`, `latest`, `delta`, and `peak_timestamp`.
- **If `result["executed"] == False`**: Run `result["az_rest_command"]` using `RunAzCliReadCommands`, then summarize the returned JSON payload in `ExecutePythonCode`:

```python
summary = promql_helper.summarize_prometheus_response(raw_az_rest_json, top_n=20)
print(summary)
```

---

## OpenTelemetry Machine Triage Playbook (Golden Signals for Hosts)

When asked to diagnose machine performance or investigate a degraded host, evaluate the 5 host telemetry pillars in parallel:

| Pillar | Primary OTel Metrics | Key Diagnostic Signals & Thresholds |
|---|---|---|
| **1. CPU Saturation** | `system_cpu_time_seconds_total`, `system_cpu_load_average_1m`/`5m`/`15m` | Total CPU **> 85%** sustained; Linux `state="iowait"` **> 15%** (disk wait) or `state="steal"` **> 5%** (hypervisor throttling); Windows `state="interrupt"` / `"dpc"` **> 10%**. |
| **2. Memory & Paging** | `system_memory_usage_bytes`, `system_paging_usage_bytes`, `system_paging_faults_total`, `system_paging_operations_total` | Used memory **> 90%** (or available memory `< 5%`); non-zero rate of `system_paging_faults_total{type="major"}` or `system_paging_operations_total{type="major"}` indicating active swap thrashing. |
| **3. Disk I/O & Latency** | `system_disk_io_bytes_total`, `system_disk_operations_total`, `system_disk_operation_time_seconds_total`, `system_disk_pending_operations` | Average disk I/O latency (`rate(operation_time) / rate(operations) * 1000`) **> 15 ms** (warning) or **> 30 ms** (critical storage throttling); `system_disk_pending_operations` **> 4** per disk. |
| **4. Filesystem Capacity** | `system_filesystem_usage_bytes`, `system_filesystem_inodes_usage` | Capacity utilization **> 85%** (warning) or **> 95%** (imminent disk-full outage); Linux inode usage **> 90%**. |
| **5. Network & TCP Health** | `system_network_io_bytes_total`, `system_network_errors_total`, `system_network_dropped_total`, `system_network_connections` | Any non-zero `rate(system_network_errors_total[5m])` or `rate(system_network_dropped_total[5m])`; elevated TCP `CLOSE_WAIT` (socket leak) or `TIME_WAIT` / `SYN_SENT` (SNAT/port exhaustion). |
| **6. Top Processes** | `process_cpu_time_seconds_total`, `process_memory_usage_bytes`, `process_disk_io_bytes_total`, `process_threads`, `process_open_file_descriptors`, `process_handles` | Correlate the exact `process_executable_name` and `pid` spiking at the `peak_timestamp` identified in Pillars 1–3. |

---

## Troubleshooting Common Errors

| Symptom / HTTP Status | Root Cause | Resolution |
|---|---|---|
| **HTTP 401 / 403 Forbidden** on `*.prometheus.monitor.azure.com` | The SRE Agent's User-Assigned Managed Identity (UAMI) lacks the **`Monitoring Data Reader`** role on the Azure Monitor Workspace. (`Monitoring Reader` and `Monitoring Contributor` do **not** include `Microsoft.Monitor/accounts/Metrics/read`). | Instruct the administrator to grant **`Monitoring Data Reader`** (`b0d8363b-8ddd-447d-831f-62ca05bff136`) to the agent's UAMI on the Azure Monitor Workspace resource. |
| **`RunAzCliReadCommands` returns `"Unknown error occurred"`** | Command included `-o`, `--output`, or `--query`, or experienced a transient first-call failure. | Remove all `-o` / `--output` / `--query` flags and retry the plain command once. |
| **HTTP 400 / 422 on `/query` or `/query_range`** | Query omitted a metric name (`__name__`), used regex on `__name__`, or exceeded the 32-day total time window. | Validate the query with `promql_helper.validate_promql_query()` and ensure a single explicit metric name is present. |
| **HTTP 400 / 404 on `/api/v1/label/<label>/values`** | Azure Monitor Workspace only supports `/api/v1/label/__name__/values`. | Use `/api/v1/series?match[]=<metric_name>` or `group by (<label>) (<metric_name>)` to inspect label values. |
| **Empty results (`"result": []`) for OTel metrics** | Metric names may use unitless naming (`system_cpu_time` instead of `system_cpu_time_seconds_total`) or an exporter namespace prefix, or machine identity lives on `target_info` rather than `host_name` on the metric. | Run Step 2A (`/api/v1/label/__name__/values`) and Step 2B (`/api/v1/series?match[]=target_info`) to inspect the exact metric and label names in the workspace. |

---

## Expected Output Format

Present findings in a structured diagnostic report:
1. **Workspace & Scope**: Azure Monitor Workspace name, region, Prometheus endpoint, and evaluated time window (UTC).
2. **Machine Health Summary Table**: Hostname / Instance, OS, CPU (avg / p95 / peak), Memory Used %, Swap Activity, Max Disk Latency (ms), Max Filesystem %, Network Drops/Errors, and Status (`🟢 Healthy`, `🟡 Warning`, `🔴 Critical`).
3. **Top Culprit Processes & Timeline Correlation**: Peak timestamps, offending `process_executable_name` / `pid`, and resource deltas.
4. **Root Cause & Actionable Remediation**: Specific recommendations backed by the PromQL evidence.
