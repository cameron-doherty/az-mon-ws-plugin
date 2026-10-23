# OpenTelemetry (`hostmetrics` & `process`) PromQL Recipes for Azure Monitor Workspace

This reference provides production-ready PromQL expressions and naming rules for querying **OpenTelemetry Collector `hostmetrics` and `process` receiver telemetry** across **Linux and Windows machines** stored in an **Azure Monitor Workspace (Managed Prometheus)**.

---

## 1. Two Naming Schemas in Azure Monitor Workspaces (Dots vs. Underscores)

Depending on how OpenTelemetry metrics are ingested into Azure Monitor Workspace, metric and label names follow one of two schemas. **Always check `/api/v1/label/__name__/values` first.**

### Schema A (Most Common with Azure Managed Prometheus + OTel Collectors / Arc): Dot-Separated Names

Azure Managed Prometheus with OTel collectors preserves **dot-separated** OTel metric and label names without appending unit suffixes:

| OTel Metric (Dot-Separated) | Standard Prometheus Exporter Equivalent | Description |
|---|---|---|
| `system.cpu.utilization` | `system_cpu_utilization_ratio` | CPU utilization fraction (`0.0–1.0`) per state/core |
| `system.cpu.time` | `system_cpu_time_seconds_total` | Cumulative CPU seconds per state/core |
| `system.memory.utilization` | `system_memory_utilization_ratio` | Memory utilization fraction (`0.0–1.0`) |
| `system.memory.usage` | `system_memory_usage_bytes` | Memory usage in bytes by `state` |
| `system.linux.memory.available` | `system_linux_memory_available_bytes` | Available memory on Linux machines |
| `system.linux.memory.dirty` | `system_linux_memory_dirty_bytes` | Dirty page cache memory on Linux |
| `system.disk.io` | `system_disk_io_bytes_total` | Cumulative disk bytes read/written |
| `system.disk.operations` | `system_disk_operations_total` | Cumulative disk I/O operations |
| `system.disk.operation_time` | `system_disk_operation_time_seconds_total` | Cumulative time spent in disk operations |
| `system.disk.io_time` | `system_disk_io_time_seconds_total` | Cumulative active disk busy time |
| `system.disk.pending_operations` | `system_disk_pending_operations` | Current disk queue depth |
| `system.filesystem.usage` | `system_filesystem_usage_bytes` | Filesystem capacity usage in bytes |
| `system.filesystem.utilization` | `system_filesystem_utilization_ratio` | Filesystem capacity utilization fraction (`0.0–1.0`) |
| `system.network.io` | `system_network_io_bytes_total` | Cumulative network bytes received/transmitted |
| `system.network.packets` | `system_network_packets_total` | Cumulative network packets |
| `system.network.errors` | `system_network_errors_total` | Cumulative network interface errors |
| `system.network.dropped` | `system_network_dropped_total` | Cumulative dropped network packets |
| `system.network.connections` | `system_network_connections` | Active network sockets by protocol & state |
| `process.cpu.utilization` | `process_cpu_utilization_ratio` | Per-process CPU utilization fraction (`0.0–1.0`) |
| `process.cpu.time` | `process_cpu_time_seconds_total` | Per-process cumulative CPU time |
| `process.memory.utilization` | `process_memory_utilization_ratio` | Per-process memory utilization fraction (`0.0–1.0`) |
| `process.memory.usage` | `process_memory_usage_bytes` | Per-process physical (RSS) memory in bytes |
| `process.disk.io` | `process_disk_io_bytes_total` | Per-process disk I/O bytes |
| `process.threads` | `process_threads` | Per-process thread count |
| `process.handles` | `process_handles` | Windows-only per-process handle count |
| `process.open_file_descriptors` | `process_open_file_descriptors` | Linux-only per-process open file descriptors |
| `process.signals_pending` | `process_signals_pending` | Linux-only pending signals |

#### Mandatory PromQL Syntax for Dot-Separated Metrics & Labels
1. **Metric names with dots CANNOT be used as bare identifiers**. Always use `{__name__="<metric>"}`:
   ```promql
   # WRONG:
   system.cpu.utilization{state="idle"}

   # CORRECT:
   {__name__="system.cpu.utilization", state="idle"}
   ```
2. **Label names with dots MUST be double-quoted** in `by()` / `without()` / `on()` / `ignoring()` / `group_left()` clauses:
   ```promql
   # WRONG:
   avg by (microsoft.resourceid) ({__name__="system.cpu.utilization"})

   # CORRECT:
   avg by ("microsoft.resourceid") ({__name__="system.cpu.utilization"})
   ```

---

### Schema B: Prometheus-Normalized Underscore Names

When an OTel Collector exports via a Prometheus exporter configured to normalize names (`dots → underscores` and unit suffixes enabled), `system.cpu.time` becomes `system_cpu_time_seconds_total` and `host.name` becomes `host_name`. Both schemas are provided for every recipe below.

---

## 2. Machine Identification, Labels & OS Detection

### Dot-Separated Azure & Process Labels (Schema A)

| Label Name | Description | Generic Example Value |
|---|---|---|
| `microsoft.resourceid` | Full ARM resource ID of the machine (machine name is the last segment) | `/subscriptions/.../providers/microsoft.hybridcompute/machines/<machine-name>` |
| `microsoft.resourcegroupname` | Resource group of the machine | `rg-prod-machines` |
| `microsoft.resourcetype` | ARM resource type | `microsoft.hybridcompute/machines` or `microsoft.compute/virtualmachines` |
| `microsoft.subscriptionid` | Subscription GUID | `<subscription-guid>` |
| `microsoft.amwresourceid` | ARM resource ID of the Azure Monitor Workspace | `/subscriptions/.../providers/microsoft.monitor/accounts/<workspace-name>` |
| `process.executable.name` | Process executable name | `sqlservr.exe`, `wdavdaemon`, `postgres` |
| `process.command` | Full process command line | `"c:\program files\..."` |
| `process.owner` | Process owner | `root`, `nt service\...` |
| `process.pid` | Process ID | `5548` |
| `state` | CPU / memory / filesystem state | `idle`, `user`, `system`, `interrupt`, `nice`, `softirq`, `steal`, `wait`, `iowait` |
| `cpu` | Per-core CPU identifier (Windows machines) | `cpu0`, `cpu1` |

### How to Identify Machine OS Directly from Metrics & Labels
Even without querying `target_info`, you can immediately determine whether a machine is running **Windows** or **Linux**:
- **Windows machines**:
  - Report CPU metrics with a `cpu` label (`cpu0`, `cpu1`, ...) and `state` values `idle`, `interrupt`, `system`, `user` (plus `dpc`).
  - Emit the `process.handles` metric (Windows-only).
- **Linux machines**:
  - Omit the `cpu` label on `system.cpu.*` (single aggregated value per state) and report additional `state` values: `nice`, `softirq`, `steal`, `wait`, `iowait`.
  - Emit `system.linux.memory.available`, `system.linux.memory.dirty`, `process.open_file_descriptors`, and `process.signals_pending` (Linux-only).

### Alternative: `host_name` / `target_info` Joins (Schema B)
If machines are labeled with `host_name`, `instance`, or `target_info` instead of `microsoft.resourceid`:
1. Query `/api/v1/series?match[]=target_info` to map `host_name` ↔ `instance` and `job`.
2. Group or filter by `(host_name, instance)`.

---

## 3. CPU Telemetry (`system.cpu.*`)

### Overall CPU Utilization % per Machine (Linux & Windows)

**Schema A (Dot-named `system.cpu.utilization` — Preferred Fast Query):**
```promql
(1 - avg by ("microsoft.resourceid") ({__name__="system.cpu.utilization", state="idle"})) * 100
```

**Schema A (From cumulative `system.cpu.time` counter):**
```promql
(
  1 - (
    sum by ("microsoft.resourceid") (rate({__name__="system.cpu.time", state="idle"}[5m]))
    /
    sum by ("microsoft.resourceid") (rate({__name__="system.cpu.time"}[5m]))
  )
) * 100
```

**Schema B (Underscore-named `system_cpu_time_seconds_total`):**
```promql
(
  1 - (
    sum by (host_name, instance) (rate(system_cpu_time_seconds_total{state="idle"}[5m]))
    /
    sum by (host_name, instance) (rate(system_cpu_time_seconds_total[5m]))
  )
) * 100
```

### CPU Breakdown by State % (Identify Kernel/System, I/O Wait, Steal, or Interrupt Bottlenecks)

**Schema A (Dot-named):**
```promql
avg by ("microsoft.resourceid", state) ({__name__="system.cpu.utilization", state!="idle"}) * 100
```

**Schema B (Underscore-named):**
```promql
(
  sum by (host_name, instance, state) (rate(system_cpu_time_seconds_total{state!="idle"}[5m]))
  / ignoring (state) group_left
  sum by (host_name, instance) (rate(system_cpu_time_seconds_total[5m]))
) * 100
```
- **Linux**: High `wait` / `iowait` (>15%) indicates storage/disk bottleneck; high `steal` (>5%) indicates hypervisor/VM noisy neighbor or burstable SKU credit exhaustion.
- **Windows**: High `interrupt` or `dpc` (>10%) indicates network/storage driver or hardware interrupt saturation.

### System Load Average
```promql
# Schema A (Dot-named)
{__name__="system.cpu.load_average.1m"}
{__name__="system.cpu.load_average.5m"}
{__name__="system.cpu.load_average.15m"}

# Schema B (Underscore-named)
system_cpu_load_average_1m
system_cpu_load_average_5m
system_cpu_load_average_15m
```

---

## 4. Memory & Paging Telemetry (`system.memory.*` & `system.paging.*`)

### Memory Utilization % per Machine (Linux & Windows)

**Schema A (Dot-named `system.memory.utilization` — Preferred Fast Query):**
```promql
avg by ("microsoft.resourceid") ({__name__="system.memory.utilization"}) * 100
```
*(Note: If `system.memory.utilization` includes a `state` label in your collector configuration, filter with `state="used"`.)*

**Schema A (From `system.memory.usage` bytes):**
```promql
(
  sum by ("microsoft.resourceid") ({__name__="system.memory.usage", state="used"})
  /
  sum by ("microsoft.resourceid") ({__name__="system.memory.usage"})
) * 100
```

**Schema B (Underscore-named `system_memory_usage_bytes`):**
```promql
(
  sum by (host_name, instance) (system_memory_usage_bytes{state="used"})
  /
  sum by (host_name, instance) (system_memory_usage_bytes)
) * 100
```

### Available Memory in GiB

**Schema A (Dot-named):**
```promql
# Linux direct available memory metric (when present):
avg by ("microsoft.resourceid") ({__name__="system.linux.memory.available"}) / 1073741824

# Or from free + reclaimable states:
sum by ("microsoft.resourceid") (
  {__name__="system.memory.usage", state=~"free|cached|buffered|slab_reclaimable"}
) / 1073741824
```

**Schema B (Underscore-named):**
```promql
sum by (host_name, instance) (
  system_memory_usage_bytes{state=~"free|cached|buffered|slab_reclaimable"}
) / 1073741824
```

### Swap / Pagefile Utilization % & Major Page Faults (`system.paging.*`)

**Schema A (Dot-named):**
```promql
# Swap / pagefile utilization %
avg by ("microsoft.resourceid") ({__name__="system.paging.utilization", state="used"}) * 100

# Major (disk-backed) page faults per second
sum by ("microsoft.resourceid") (rate({__name__="system.paging.faults", type="major"}[5m]))

# Paging I/O operations per second
sum by ("microsoft.resourceid", direction) (rate({__name__="system.paging.operations", type="major"}[5m]))
```

**Schema B (Underscore-named):**
```promql
(
  sum by (host_name, instance) (system_paging_usage_bytes{state="used"})
  /
  sum by (host_name, instance) (system_paging_usage_bytes)
) * 100

sum by (host_name, instance) (rate(system_paging_faults_total{type="major"}[5m]))
```

---

## 5. Disk I/O & Filesystem Telemetry (`system.disk.*` & `system.filesystem.*`)

### Disk Read / Write Throughput (MB/s per Device)

**Schema A (Dot-named):**
```promql
sum by ("microsoft.resourceid", device, direction) (
  rate({__name__="system.disk.io"}[5m])
) / 1048576
```

**Schema B (Underscore-named):**
```promql
sum by (host_name, instance, device, direction) (
  rate(system_disk_io_bytes_total[5m])
) / 1048576
```

### Disk IOPS (Reads & Writes per Second)

**Schema A (Dot-named):**
```promql
sum by ("microsoft.resourceid", device, direction) (
  rate({__name__="system.disk.operations"}[5m])
)
```

**Schema B (Underscore-named):**
```promql
sum by (host_name, instance, device, direction) (
  rate(system_disk_operations_total[5m])
)
```

### Average Disk I/O Latency in Milliseconds (Critical for Storage Bottlenecks)
Computes `(delta operation_time_seconds / delta operations) * 1000` (`< 10 ms` healthy, `15–30 ms` warning, `> 30 ms` critical):

**Schema A (Dot-named):**
```promql
(
  sum by ("microsoft.resourceid", device, direction) (rate({__name__="system.disk.operation_time"}[5m]))
  /
  clamp_min(sum by ("microsoft.resourceid", device, direction) (rate({__name__="system.disk.operations"}[5m])), 0.001)
) * 1000
```

**Schema B (Underscore-named):**
```promql
(
  sum by (host_name, instance, device, direction) (rate(system_disk_operation_time_seconds_total[5m]))
  /
  clamp_min(sum by (host_name, instance, device, direction) (rate(system_disk_operations_total[5m])), 0.001)
) * 1000
```

### Disk Queue Depth (Pending Operations)
```promql
# Schema A (Dot-named)
sum by ("microsoft.resourceid", device) ({__name__="system.disk.pending_operations"})

# Schema B (Underscore-named)
sum by (host_name, instance, device) (system_disk_pending_operations)
```

### Filesystem Capacity Utilization % (`system.filesystem.*`)

**Schema A (Dot-named):**
```promql
# Direct utilization metric (when present):
avg by ("microsoft.resourceid", mountpoint, device) ({__name__="system.filesystem.utilization"}) * 100

# Or computed from system.filesystem.usage bytes:
(
  sum by ("microsoft.resourceid", mountpoint, device) ({__name__="system.filesystem.usage", state="used"})
  /
  sum by ("microsoft.resourceid", mountpoint, device) ({__name__="system.filesystem.usage"})
) * 100
```

**Schema B (Underscore-named):**
```promql
(
  sum by (host_name, instance, mountpoint, device) (system_filesystem_usage_bytes{state="used"})
  /
  sum by (host_name, instance, mountpoint, device) (system_filesystem_usage_bytes)
) * 100
```

---

## 6. Network Telemetry (`system.network.*`)

### Network Throughput (Receive & Transmit MB/s per Interface)

**Schema A (Dot-named):**
```promql
sum by ("microsoft.resourceid", device, direction) (
  rate({__name__="system.network.io", device!="lo"}[5m])
) / 1048576
```

**Schema B (Underscore-named):**
```promql
sum by (host_name, instance, device, direction) (
  rate(system_network_io_bytes_total{device!="lo"}[5m])
) / 1048576
```

### Network Errors & Dropped Packets per Second

**Schema A (Dot-named):**
```promql
# Network interface errors/sec
sum by ("microsoft.resourceid", device, direction) (
  rate({__name__="system.network.errors"}[5m])
) > 0

# Dropped packets/sec
sum by ("microsoft.resourceid", device, direction) (
  rate({__name__="system.network.dropped"}[5m])
) > 0
```

**Schema B (Underscore-named):**
```promql
sum by (host_name, instance, device, direction) (rate(system_network_errors_total[5m])) > 0
sum by (host_name, instance, device, direction) (rate(system_network_dropped_total[5m])) > 0
```

### TCP Connection States (`CLOSE_WAIT`, `TIME_WAIT`, `ESTABLISHED`)
```promql
# Schema A (Dot-named)
sum by ("microsoft.resourceid", state) ({__name__="system.network.connections", protocol="tcp"})

# Schema B (Underscore-named)
sum by (host_name, instance, state) (system_network_connections{protocol="tcp"})
```

---

## 7. Per-Process Telemetry (`process.*`)

When the OTel Collector `process` receiver (or `hostmetrics` `processes` scraper) is enabled, use these queries to pinpoint the exact binary/executable responsible for host resource exhaustion.

> **Aggregation Note:** Grouping with `sum by ("microsoft.resourceid", "process.executable.name")` aggregates across multiple PIDs of the same executable name and across CPU states (`user`, `system`) to show the total footprint of that application on the machine. Include `"process.pid"` in `by (...)` when you need per-PID granularity.

### Top 10 Processes by CPU Utilization % per Machine

**Schema A (Dot-named `process.cpu.utilization` — Preferred Fast Query):**
```promql
topk(10, sum by ("microsoft.resourceid", "process.executable.name") ({__name__="process.cpu.utilization"})) * 100
```

**Schema A (From cumulative `process.cpu.time`):**
```promql
topk(10,
  sum by ("microsoft.resourceid", "process.executable.name") (
    rate({__name__="process.cpu.time"}[5m])
  )
) * 100
```

**Schema B (Underscore-named `process_cpu_time_seconds_total`):**
```promql
topk(10,
  sum by (host_name, instance, process_executable_name, pid, command) (
    rate(process_cpu_time_seconds_total[5m])
  )
)
```

### Top 10 Processes by Memory Utilization % or Physical (RSS) Memory

**Schema A (Dot-named `process.memory.utilization` % — Preferred Fast Query):**
```promql
topk(10, sum by ("microsoft.resourceid", "process.executable.name") ({__name__="process.memory.utilization"})) * 100
```

**Schema A (Dot-named `process.memory.usage` in MiB):**
```promql
topk(10,
  sum by ("microsoft.resourceid", "process.executable.name") (
    {__name__="process.memory.usage"}
  ) / 1048576
)
```

**Schema B (Underscore-named `process_memory_usage_bytes` in MiB):**
```promql
topk(10,
  sum by (host_name, instance, process_executable_name, pid) (
    process_memory_usage_bytes
  ) / 1048576
)
```

### Top 10 Processes by Disk Read/Write Throughput (MB/s)

**Schema A (Dot-named):**
```promql
topk(10,
  sum by ("microsoft.resourceid", "process.executable.name", direction) (
    rate({__name__="process.disk.io"}[5m])
  ) / 1048576
)
```

**Schema B (Underscore-named):**
```promql
topk(10,
  sum by (host_name, instance, process_executable_name, pid, direction) (
    rate(process_disk_io_bytes_total[5m])
  ) / 1048576
)
```

### Process Thread Count, Open File Descriptors (Linux) & Handles (Windows)

```promql
# Thread count per process (Schema A)
topk(10, sum by ("microsoft.resourceid", "process.executable.name") ({__name__="process.threads"}))

# Linux open file descriptors (Schema A)
topk(10, sum by ("microsoft.resourceid", "process.executable.name") ({__name__="process.open_file_descriptors"}))

# Windows handle count (Schema A)
topk(10, sum by ("microsoft.resourceid", "process.executable.name") ({__name__="process.handles"}))
```

