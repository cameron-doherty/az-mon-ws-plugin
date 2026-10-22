# OpenTelemetry (`hostmetrics` & `process`) PromQL Recipes for Azure Monitor Workspace

This reference provides production-ready PromQL expressions and normalization rules for querying **OpenTelemetry Collector `hostmetrics` and `process` receiver telemetry** across **Linux and Windows machines** stored in an **Azure Monitor Workspace (Managed Prometheus)**.

---

## 1. OpenTelemetry-to-Prometheus Normalization Rules

When OpenTelemetry metrics are exported to Azure Monitor Managed Prometheus (via the OTel Collector `prometheusremotewrite` / `prometheus` exporter or Azure Monitor Agent OTel pipeline), metric and attribute names undergo standard Prometheus normalization:

| OpenTelemetry Concept | OTel Name Example | Managed Prometheus Normalized Name | Notes |
|---|---|---|---|
| **Dots (`.`) → Underscores (`_`)** | `system.cpu.time` | `system_cpu_time_seconds_total` | All dots in metric and attribute names become `_`. |
| **Time Unit Suffix** | Unit `s` (seconds) | `_seconds` | Appended automatically when unit translation is enabled. |
| **Bytes Unit Suffix** | Unit `By` (bytes) | `_bytes` | e.g., `system.memory.usage` → `system_memory_usage_bytes`. |
| **Ratio / Utilization Unit** | Unit `1` (dimensionless ratio `0.0–1.0`) | `_ratio` (or no suffix) | Multiply by `100` for percentage (`0–100%`). |
| **Monotonic Counters** | Monotonic Sum | `_total` | Always wrap with `rate(...[5m])` or `increase(...[5m])`. |
| **Exporter Namespace Prefix** | Optional `namespace: <prefix>` | `<prefix>_system_cpu_time_seconds_total` | Always check `/api/v1/label/__name__/values` first in case a custom namespace prefix (like `otel_`) is configured. |
| **Case Sensitivity** | `host_name="VM-01"` vs `"vm-01"` | **Case-insensitive** | Azure Monitor Workspace treats metric names, label names, and label values as case-insensitive. |

> **Unitless / Legacy OTel Collector Note:** If the OTel Collector exporter has `add_metric_suffixes: false`, metric names omit `_seconds`, `_bytes`, `_ratio`, and `_total` (for example, `system_cpu_time`, `system_memory_usage`, `system_disk_io`). Always verify the exact names in the workspace via `/api/v1/label/__name__/values` before running range queries.

---

## 2. Machine Identification & `target_info` Resource Attribute Joins

Depending on the OTel Collector configuration (`resource_to_telemetry_conversion`), machine identity attributes (`host.name`, `host.id`, `os.type`, `cloud.resource_id`, `service.name`) are stored in one of two ways:

### Pattern A: Resource attributes promoted directly to metric labels (`resource_to_telemetry_conversion.enabled: true`)
Labels such as `host_name`, `os_type`, or `instance` appear directly on every metric:
```promql
system_memory_usage_bytes{host_name="vm-prod-01"}
```

### Pattern B: Resource attributes stored on `target_info` (Standard OTel Prometheus convention)
Metric series carry `job` and `instance`, while `host_name`, `os_type`, `host_id`, and `cloud_resource_id` live on the `target_info` metric.

**Recommended 2-Step Lookup for Azure Monitor Workspace:**
Because Azure Monitor Workspace enforces a single `__name__` matcher per query in many contexts, the safest and fastest way to resolve machine labels is:
1. Query `/api/v1/series?match[]=target_info` (or `GET /api/v1/query?query=target_info`) to map `host_name` ↔ `instance` and `job`.
2. Filter the target metric directly by `instance="<resolved_instance>"` or `host_name="<hostname>"`.

If joining in a single PromQL expression:
```promql
(
  sum by (job, instance) (rate(system_cpu_time_seconds_total{state!="idle"}[5m]))
  /
  sum by (job, instance) (rate(system_cpu_time_seconds_total[5m]))
) * 100
* on (job, instance) group_left (host_name, os_type) target_info
```

---

## 3. CPU Telemetry (`system.cpu.*`)

### Linux & Windows State Labels
- **Linux `state` values**: `user`, `system`, `idle`, `iowait`, `irq`, `softirq`, `steal`, `nice`
- **Windows `state` values**: `user`, `system` (privileged), `idle`, `interrupt`, `dpc`

### Overall CPU Utilization % per Machine (Linux & Windows)
Works on both Linux and Windows because both OSes report `state="idle"`:
```promql
(
  1 - (
    sum by (host_name, instance) (rate(system_cpu_time_seconds_total{state="idle"}[5m]))
    /
    sum by (host_name, instance) (rate(system_cpu_time_seconds_total[5m]))
  )
) * 100
```

### Alternative: Direct CPU Utilization Gauge (When `cpu.utilization` Scraper Metric is Enabled)
```promql
avg by (host_name, instance) (system_cpu_utilization_ratio{state!="idle"}) * 100
```

### CPU Breakdown by State % (Identify Kernel/System, I/O Wait, Steal, or Interrupt Bottlenecks)
```promql
(
  sum by (host_name, instance, state) (rate(system_cpu_time_seconds_total{state!="idle"}[5m]))
  / ignoring (state) group_left
  sum by (host_name, instance) (rate(system_cpu_time_seconds_total[5m]))
) * 100
```
- **Linux**: High `iowait` (>15%) indicates storage/disk bottleneck; high `steal` (>5%) indicates hypervisor/VM noisy neighbor or burstable SKU credit exhaustion.
- **Windows**: High `interrupt` or `dpc` (>10%) indicates network/storage driver or hardware interrupt saturation.

### System Load Average (Linux & Windows)
```promql
# 1-minute, 5-minute, and 15-minute load averages
system_cpu_load_average_1m
system_cpu_load_average_5m
system_cpu_load_average_15m
```

---

## 4. Memory & Paging Telemetry (`system.memory.*` & `system.paging.*`)

### Linux & Windows Memory `state` Labels
- **Linux `state` values**: `used`, `free`, `buffered`, `cached`, `slab_reclaimable`, `slab_unreclaimable`, `shared`
- **Windows `state` values**: `used`, `free`

### Memory Utilization % per Machine (Linux & Windows)
```promql
(
  sum by (host_name, instance) (system_memory_usage_bytes{state="used"})
  /
  sum by (host_name, instance) (system_memory_usage_bytes)
) * 100
```

### Available Memory in GiB (Free + Reclaimable Cache on Linux; Free on Windows)
```promql
sum by (host_name, instance) (
  system_memory_usage_bytes{state=~"free|cached|buffered|slab_reclaimable"}
) / 1073741824
```

### Memory Breakdown by State (Bytes)
```promql
sum by (host_name, instance, state) (system_memory_usage_bytes)
```

### Swap / Pagefile Utilization % (`system.paging.usage`)
```promql
(
  sum by (host_name, instance) (system_paging_usage_bytes{state="used"})
  /
  sum by (host_name, instance) (system_paging_usage_bytes)
) * 100
```

### Major Page Faults & Swap Thrashing Rate (`system.paging.faults` / `system.paging.operations`)
```promql
# Major (disk-backed) page faults per second
sum by (host_name, instance) (rate(system_paging_faults_total{type="major"}[5m]))

# Paging I/O operations (page-in / page-out) per second
sum by (host_name, instance, direction) (rate(system_paging_operations_total{type="major"}[5m]))
```

---

## 5. Disk I/O & Filesystem Telemetry (`system.disk.*` & `system.filesystem.*`)

### Disk Read / Write Throughput (MB/s per Device)
```promql
sum by (host_name, instance, device, direction) (
  rate(system_disk_io_bytes_total[5m])
) / 1048576
```

### Disk IOPS (Reads & Writes per Second)
```promql
sum by (host_name, instance, device, direction) (
  rate(system_disk_operations_total[5m])
)
```

### Average Disk I/O Latency in Milliseconds (Critical for Storage Bottlenecks)
Computes `(delta operation_time_seconds / delta operations) * 1000`:
```promql
(
  sum by (host_name, instance, device, direction) (rate(system_disk_operation_time_seconds_total[5m]))
  /
  clamp_min(sum by (host_name, instance, device, direction) (rate(system_disk_operations_total[5m])), 0.001)
) * 1000
```
- **Healthy SSD/Premium Disk**: `< 10 ms`
- **Warning**: `15–30 ms`
- **Critical saturation / throttling**: `> 30 ms`

### Disk Busy / Active I/O Utilization %
```promql
sum by (host_name, instance, device) (
  rate(system_disk_io_time_seconds_total[5m])
) * 100
```

### Disk Queue Depth (Pending Operations)
```promql
sum by (host_name, instance, device) (system_disk_pending_operations)
```

### Filesystem Capacity Utilization % (`system.filesystem.usage`)
- **Linux `state` values**: `used`, `free`, `reserved`
- **Windows `state` values**: `used`, `free`
```promql
(
  sum by (host_name, instance, mountpoint, device) (system_filesystem_usage_bytes{state="used"})
  /
  sum by (host_name, instance, mountpoint, device) (system_filesystem_usage_bytes)
) * 100
```

### Linux Filesystem Inode Utilization %
```promql
(
  sum by (host_name, instance, mountpoint) (system_filesystem_inodes_usage{state="used"})
  /
  sum by (host_name, instance, mountpoint) (system_filesystem_inodes_usage)
) * 100
```

---

## 6. Network Telemetry (`system.network.*`)

### Network Throughput (Receive & Transmit MB/s per Interface)
`direction` label values: `receive`, `transmit`.
```promql
sum by (host_name, instance, device, direction) (
  rate(system_network_io_bytes_total{device!="lo"}[5m])
) / 1048576
```

### Network Packet Rate (Packets/s)
```promql
sum by (host_name, instance, device, direction) (
  rate(system_network_packets_total{device!="lo"}[5m])
)
```

### Network Errors & Dropped Packets per Second (NIC Saturation / Driver / NSG Drops)
```promql
# Network interface errors/sec
sum by (host_name, instance, device, direction) (
  rate(system_network_errors_total[5m])
) > 0

# Dropped packets/sec
sum by (host_name, instance, device, direction) (
  rate(system_network_dropped_total[5m])
) > 0
```

### TCP Connection States (Port Exhaustion, `TIME_WAIT`, `CLOSE_WAIT`, `ESTABLISHED`)
```promql
sum by (host_name, instance, state) (
  system_network_connections{protocol="tcp"}
)
```
- High `CLOSE_WAIT`: Application is not closing sockets after remote peer disconnects (connection leak).
- High `TIME_WAIT` or `SYN_SENT`: Ephemeral port exhaustion or outbound SNAT/dependency timeout.

---

## 7. Per-Process Telemetry (`process.*`)

When the OTel Collector `process` receiver (or `hostmetrics` `processes` scraper) is enabled, use these queries to pinpoint the exact binary/PID responsible for host resource exhaustion:

### Top 10 Processes by CPU Consumption (Cores Used)
```promql
topk(10,
  sum by (host_name, instance, process_executable_name, pid, command) (
    rate(process_cpu_time_seconds_total[5m])
  )
)
```

### Top 10 Processes by Physical (RSS) Memory (MiB)
```promql
topk(10,
  sum by (host_name, instance, process_executable_name, pid) (
    process_memory_usage_bytes
  ) / 1048576
)
```

### Top 10 Processes by Disk Read/Write Throughput (MB/s)
```promql
topk(10,
  sum by (host_name, instance, process_executable_name, pid, direction) (
    rate(process_disk_io_bytes_total[5m])
  ) / 1048576
)
```

### Process Thread Count & Open File Descriptors / Handles
```promql
# Thread count per process
topk(10, sum by (host_name, instance, process_executable_name, pid) (process_threads))

# Linux open file descriptors
topk(10, sum by (host_name, instance, process_executable_name, pid) (process_open_file_descriptors))

# Windows handle count
topk(10, sum by (host_name, instance, process_executable_name, pid) (process_handles))
```
