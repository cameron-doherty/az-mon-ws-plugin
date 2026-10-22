# Azure Monitor Workspace (PromQL & OpenTelemetry) Plugin for Azure SRE Agent

A reusable, zero-infrastructure **Azure SRE Agent Plugin** that enables your SRE Agent to discover and query **Azure Monitor Workspaces (Managed Prometheus)** using PromQL to diagnose **OpenTelemetry (`hostmetrics` and `process`)** performance telemetry from Linux and Windows machines.

---

## Why This Plugin Exists

Azure SRE Agent has built-in tools for Azure Monitor Metrics (ARM), Log Analytics, Application Insights, and Azure Resource Graph, but does not have a built-in connector for the **Azure Monitor Workspace (Managed Prometheus) data-plane API** (`https://<workspace>.<region>.prometheus.monitor.azure.com`).

This plugin fills that gap without requiring you to deploy or maintain an external MCP server:
- **Zero-Infrastructure Skill (`azmon_workspace_promql`)**: Uses the SRE Agent's built-in `RunAzCliReadCommands` and `ExecutePythonCode` tools with the agent's existing **User-Assigned Managed Identity (UAMI)**.
- **PromQL Constraint Validator & URL Builder (`scripts/promql_helper.py`)**: Prevents common LLM URL-encoding mistakes, enforces Azure Monitor Workspace PromQL API limits before calling the endpoint, supports direct `DefaultAzureCredential` execution when available, and summarizes large Prometheus `matrix`/`vector` JSON responses into compact statistical tables (`min`, `max`, `avg`, `p95`, `latest`, `delta`).
- **OpenTelemetry Host & Process Recipe Library (`references/otel_host_process_promql.md`)**: Ready-to-run PromQL queries for Linux and Windows OTel Collector `hostmetrics` and `process` receivers (`system.cpu`, `system.memory`, `system.paging`, `system.disk`, `system.filesystem`, `system.network`, `process.*`, and `target_info` resource-attribute joins).

---

## Package Structure

```text
az-mon-ws-plugin/
├── plugin.json                                           # Standalone plugin manifest (Azure SRE Agent & Copilot)
├── .github/
│   └── plugin/
│       ├── plugin.json                                   # Copilot App / agent-plugins.org manifest
│       └── marketplace.json                              # Marketplace catalog manifest (Connect marketplace)
├── README.md
└── skills/
    └── azmon_workspace_promql/
        ├── SKILL.md                                      # Dual-runtime playbook (Azure SRE Agent + Copilot App)
        ├── scripts/
        │   └── promql_helper.py                          # Discover, validate, URL/az-rest builder & matrix summarizer
        └── references/
            └── otel_host_process_promql.md               # OTel Linux & Windows hostmetrics + process PromQL recipes
```

---

## Prerequisites & Required RBAC (Critical Gotcha)

> **Important:** By default, Azure SRE Agent assigns `Reader`, `Log Analytics Reader`, `Monitoring Reader`, and `Monitoring Contributor` to its User-Assigned Managed Identity (UAMI). **None of these roles include the `Microsoft.Monitor/accounts/Metrics/read` data action required to query an Azure Monitor Workspace's Prometheus endpoint.**

You **must** assign the **`Monitoring Data Reader`** role (`b0d8363b-8ddd-447d-831f-62ca05bff136`) to your SRE Agent's **User-Assigned Managed Identity (UAMI)** on the Azure Monitor Workspace (or its parent Resource Group / Subscription).

### 1. Find your SRE Agent's UAMI Principal ID

In the SRE Agent portal, go to **Settings > Azure settings** to view the Managed Identity, or run:

```bash
az resource show \
  --resource-group <sre-agent-rg> \
  --name <sre-agent-name> \
  --resource-type Microsoft.App/agents \
  --query identity.userAssignedIdentities
```

### 2. Grant `Monitoring Data Reader` on the Azure Monitor Workspace

```bash
# Scope to a specific Azure Monitor Workspace:
az role assignment create \
  --assignee-object-id <uami-principal-id> \
  --assignee-principal-type ServicePrincipal \
  --role "Monitoring Data Reader" \
  --scope "/subscriptions/<sub-id>/resourceGroups/<amw-rg>/providers/Microsoft.Monitor/accounts/<amw-name>"

# Or scope to the Resource Group / Subscription containing your Azure Monitor Workspaces:
az role assignment create \
  --assignee-object-id <uami-principal-id> \
  --assignee-principal-type ServicePrincipal \
  --role "Monitoring Data Reader" \
  --scope "/subscriptions/<sub-id>/resourceGroups/<amw-rg>"
```

### 3. Built-in Tool & Skill Availability in the SRE Agent Portal

No special tool configuration is required on a default SRE Agent:
- **Azure CLI Read Commands (`RunAzCliReadCommands`)**: Enabled by default under **Capabilities > Tools > Built-in tools** (in the **Azure Operation** category).
- **Python / Code Execution**: Provided by the built-in **Code execution** core capability under **Capabilities > Skills > Built-in skills > Core skills**, which is **always enabled and cannot be disabled**. (Note: `ExecutePythonCode` and `GetAzCliHelp` are internal runtime identifiers used by the agent under the hood and do not appear as separate checkboxes in the portal UI.)

---

## Installation

### Option A: Install Directly from GitHub URL (Recommended)

1. Push this directory to a GitHub repository (public or private).
2. In the [Azure SRE Agent Portal](https://sre.azure.com), navigate to **Builder > Plugins**.
3. Click **Install from URL**.
4. Enter your repository in `owner/repo` format (or full URL). Leave **Path in repo** blank if at the repository root.
5. Click **Install**. Confirm that `azmon_workspace_promql` appears under **Builder > Skills**.

### Option B: Install via REST API

```bash
curl -X POST "https://<agent-endpoint>/api/v2/plugins/install-direct" \
  -H "Authorization: Bearer <token>" \
  -H "Content-Type: application/json" \
  -d '{
    "sourceUrl": "<owner>/<repo>",
    "pathInRepo": ""
  }'
```

### Option C: Register as a Plugin Marketplace

Because this repository also includes `.github/plugin/marketplace.json`, you can register it under **Builder > Plugins > Connect marketplace** to distribute version-pinned skills across multiple SRE Agents in your tenant.

### Option D: Local Testing in GitHub Copilot App / CLI

This plugin is **dual-runtime compatible** with both **Azure SRE Agent** and the **GitHub Copilot App / CLI**:
1. Authenticate locally with Azure CLI (`az login`) using an identity that has **`Monitoring Data Reader`** on your Azure Monitor Workspace.
2. Copy (or symlink) `skills\azmon_workspace_promql` into `~/.copilot/skills/azmon_workspace_promql` (on Windows: `C:\Users\<user>\.copilot\skills\azmon_workspace_promql`), or work directly inside this workspace.
3. Locally, `promql_helper.py` supports single-step workspace discovery and query execution:
   ```powershell
   # Discover Azure Monitor Workspaces and their Prometheus endpoints
   python .\skills\azmon_workspace_promql\scripts\promql_helper.py discover

   # Run a range query directly and summarize min/max/avg/p95/latest
   python .\skills\azmon_workspace_promql\scripts\promql_helper.py run `
     --endpoint "https://<workspace>.<region>.prometheus.monitor.azure.com" `
     --api query_range `
     --query 'sum by (host_name, state) (rate(system_cpu_time_seconds_total[5m]))' `
     --start "-1h" --end "now" --step "1m"
   ```

---

## Key Nuances & Gotchas Addressed by This Plugin

1. **Mandatory Single `__name__` Matcher**: Azure Monitor Workspace requires every `/query`, `/query_range`, and `/series` call to include a single metric name (`__name__`). Bare label selectors like `{host_name="vm01"}` or regex metric selectors like `{__name__=~"system_cpu.*"}` are rejected by the service.
2. **Label Values Endpoint Limitation**: `/api/v1/label/<label>/values` is **only** supported for `__name__` (`/api/v1/label/__name__/values`). To discover values for `host_name`, `instance`, `job`, or `os_type`, the plugin uses `/api/v1/series?match[]=<metric_name>` or `group by (<label>) (<metric_name>)`.
3. **No Regex in `/api/v1/series` & 12-Hour Clamp**: `/api/v1/series` rejects `=~` and `!~` regex matchers and clamps queries to a maximum 12-hour window.
4. **32-Day Maximum Range Window**: `/api/v1/query_range` allows up to 32 days total, **including** any range-vector lookback inside the PromQL expression (e.g., `rate(m[1h])` over 24 hours queries 25 hours).
5. **Case-Insensitivity**: Azure Managed Prometheus treats metric names, label names, and label values as case-insensitive.
6. **`RunAzCliReadCommands` Safe Execution**:
   - Always uses `az rest --method get --resource "https://prometheus.monitor.azure.com"` so queries execute under `RunAzCliReadCommands` without write-approval prompts.
   - Never passes `-o` / `--output` or `--query` flags to `RunAzCliReadCommands` (which causes SRE Agent's read wrapper to fail).

---

## Example Prompts in SRE Agent Chat

- *"Discover our Azure Monitor Workspaces and list the OpenTelemetry machine metrics being collected."*
- *"Check CPU, memory, disk I/O latency, and network errors over the last 6 hours for host `vm-prod-api-01` in our Azure Monitor Workspace."*
- *"Which machines in our Azure Monitor Workspace have experienced high memory pressure, swap paging, or filesystem exhaustion today?"*
- *"Identify the top processes consuming CPU and RSS memory on `win-sql-02` during the spike between 14:00Z and 16:00Z."*

---

## License

MIT
