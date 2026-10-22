#!/usr/bin/env python3
"""
Azure Monitor Workspace (Managed Prometheus) PromQL Helper for Azure SRE Agent.

Provides:
1. PromQL & API constraint validation specific to Azure Monitor Workspace:
   - Mandatory single `__name__` metric matcher on `/query`, `/query_range`, and `/series`
   - Blocks unsupported `/api/v1/label/<other>/values` calls (only `__name__` is supported)
   - Blocks regex matchers (`=~`, `!~`) on `/api/v1/series`
   - Enforces max 32-day window (including range-vector lookback) on `/api/v1/query_range`
   - Clamps `/api/v1/series` window to max 12 hours
2. Safe URL encoding and `az rest --method get` command generation (no `-o` or `--query` flags,
   compatible with SRE Agent's `RunAzCliReadCommands` tool).
3. Optional direct HTTP execution using `DefaultAzureCredential` or `az account get-access-token`
   for resource `https://prometheus.monitor.azure.com`.
4. Compact statistical summarization (`min`, `max`, `avg`, `p95`, `latest`, `delta`, `peak_time`)
   for Prometheus `matrix`, `vector`, `series`, and `label/__name__/values` responses.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import os
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

PROMETHEUS_RESOURCE = "https://prometheus.monitor.azure.com"
PROMETHEUS_SCOPE = "https://prometheus.monitor.azure.com/.default"
MAX_QUERY_RANGE_SECONDS = 32 * 86400  # 32 days
MAX_SERIES_RANGE_SECONDS = 12 * 3600  # 12 hours

DURATION_UNIT_SECONDS = {
    "ms": 0.001,
    "s": 1,
    "m": 60,
    "h": 3600,
    "d": 86400,
    "w": 7 * 86400,
    "y": 365 * 86400,
}

PROMQL_KEYWORDS = {
    "and", "or", "unless", "by", "without", "on", "ignoring", "group_left",
    "group_right", "bool", "offset",
    "abs", "absent", "absent_over_time", "acos", "acosh", "asin", "asinh",
    "atan", "atanh", "avg", "avg_over_time", "bottomk", "ceil", "changes",
    "clamp", "clamp_max", "clamp_min", "cos", "cosh", "count",
    "count_over_time", "count_values", "day_of_month", "day_of_week",
    "day_of_year", "days_in_month", "deg", "delta", "deriv", "exp", "floor",
    "group", "histogram_avg", "histogram_count", "histogram_fraction",
    "histogram_quantile", "histogram_stddev", "histogram_stdvar",
    "histogram_sum", "holt_winters", "hour", "idelta", "increase", "irate",
    "label_join", "label_replace", "last_over_time", "ln", "log10", "log2",
    "max", "max_over_time", "min", "min_over_time", "minute", "month",
    "pi", "predict_linear", "present_over_time", "quantile",
    "quantile_over_time", "rad", "rate", "resets", "round", "scalar",
    "sgn", "sin", "sinh", "sort", "sort_desc", "sort_by_label",
    "sort_by_label_desc", "sqrt", "stddev", "stddev_over_time", "stdvar",
    "stdvar_over_time", "sum", "sum_over_time", "tan", "tanh", "time",
    "timestamp", "topk", "vector", "year",
}


def parse_duration_seconds(duration_str: str) -> float:
    """Parse a Prometheus duration string (e.g. '5m', '1h30m', '2d') into seconds."""
    text = duration_str.strip()
    if text.startswith("-"):
        text = text[1:]
    if not text:
        raise ValueError(f"Empty duration string: {duration_str!r}")
    if text.isdigit():
        return float(text)
    matches = list(re.finditer(r"(\d+(?:\.\d+)?)(ms|s|m|h|d|w|y)", text))
    if not matches or "".join(m.group(0) for m in matches) != text:
        raise ValueError(f"Invalid Prometheus duration: {duration_str!r}")
    total = 0.0
    for match in matches:
        val = float(match.group(1))
        unit = match.group(2)
        total += val * DURATION_UNIT_SECONDS[unit]
    return total


def parse_timestamp(value: Optional[str], now: Optional[dt.datetime] = None) -> dt.datetime:
    """
    Parse a timestamp specification into a timezone-aware UTC datetime.
    Supports:
      - None or 'now'
      - Relative durations like '-15m', '-1h', '-6h', '-7d', '1h' (interpreted as ago)
      - Unix epoch seconds (e.g. '1729510000')
      - ISO-8601 strings (e.g. '2026-10-21T10:00:00Z' or '2026-10-21T10:00:00+00:00')
    """
    ref_now = now or dt.datetime.now(dt.timezone.utc)
    if value is None or value.strip().lower() == "now":
        return ref_now

    raw = value.strip()
    # Relative duration (e.g., '-1h' or '30m')
    if raw.startswith("-") or re.fullmatch(r"(\d+(?:\.\d+)?(ms|s|m|h|d|w|y))+", raw):
        seconds = parse_duration_seconds(raw.lstrip("-"))
        return ref_now - dt.timedelta(seconds=seconds)

    # Numeric epoch seconds
    if re.fullmatch(r"\d+(?:\.\d+)?", raw):
        return dt.datetime.fromtimestamp(float(raw), tz=dt.timezone.utc)

    # ISO-8601
    iso_text = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
    parsed = dt.datetime.fromisoformat(iso_text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def format_rfc3339(ts: dt.datetime) -> str:
    """Format a UTC datetime as RFC3339 / ISO-8601 with 'Z'."""
    utc_ts = ts.astimezone(dt.timezone.utc)
    return utc_ts.strftime("%Y-%m-%dT%H:%M:%SZ")


def recommend_step(range_seconds: float, target_points: int = 120) -> str:
    """Choose a clean Prometheus step size so range queries return ~60-240 points per series."""
    if range_seconds <= 0:
        return "60s"
    raw_step = max(15.0, range_seconds / max(10, target_points))
    candidate_steps = [
        (15, "15s"),
        (30, "30s"),
        (60, "1m"),
        (120, "2m"),
        (300, "5m"),
        (600, "10m"),
        (900, "15m"),
        (1800, "30m"),
        (3600, "1h"),
        (7200, "2h"),
        (14400, "4h"),
        (21600, "6h"),
        (43200, "12h"),
        (86400, "1d"),
    ]
    for seconds, label in candidate_steps:
        if seconds >= raw_step:
            return label
    return "1d"


def extract_max_lookback_seconds(promql: str) -> float:
    """Extract the maximum range-vector lookback window `[...]` in a PromQL query."""
    # Remove quoted strings first so brackets inside label values aren't misread
    stripped = re.sub(r'"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'', '""', promql)
    lookbacks = re.findall(r"\[(\d+(?:\.\d+)?(?:ms|s|m|h|d|w|y))(?::[^\]]*)?\]", stripped)
    if not lookbacks:
        return 0.0
    return max(parse_duration_seconds(lb) for lb in lookbacks)


def validate_promql_query(promql: str) -> Dict[str, Any]:
    """
    Validate a PromQL expression against Azure Monitor Workspace API rules:
    1. Must not be empty.
    2. Must include a metric name (`__name__` matcher), not a bare `{label="val"}` selector.
    3. Cannot use regex on `__name__` (`__name__=~` or `__name__!~`).
    """
    errors: List[str] = []
    warnings: List[str] = []

    if not promql or not promql.strip():
        return {"valid": False, "errors": ["PromQL query cannot be empty."], "warnings": []}

    q = promql.strip()
    if re.search(r"__name__\s*(?:=~|!~|!=)", q):
        errors.append(
            "Azure Monitor Workspace requires an exact metric name (`__name__=\"<metric>\"` or `<metric>{...}`). "
            "Regex or negative matchers on `__name__` (`=~`, `!~`, `!=`) are not supported."
        )

    # Strip quoted strings before inspecting tokens
    no_strings = re.sub(r'"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'', '""', q)

    # Check if there is an explicit exact __name__="metric"
    has_explicit_name = bool(re.search(r"__name__\s*=\s*\"\"", no_strings))

    # Detect bare selector `{foo="bar"}` not preceded by a metric identifier
    bare_selectors = re.findall(r"(?:^|[\(\s,\+\-\*/%\^=<>!])\{([^}]*)\}", no_strings)
    for selector_body in bare_selectors:
        if not re.search(r"\b__name__\s*=\s*\"\"", selector_body):
            errors.append(
                "Azure Monitor Workspace requires every time-series selector to include a `__name__` metric matcher "
                "(e.g. `system_cpu_time_seconds_total{...}`). Bare label selectors like `{host_name=\"...\"}` fail with HTTP 400/422."
            )
            break

    # Remove label matcher blocks `{...}` and `by (...)` / `on (...)` / `ignoring (...)` / `group_left (...)` label lists
    outside_braces = re.sub(r"\{[^}]*\}", "", no_strings)
    outside_braces = re.sub(
        r"\b(?:by|without|on|ignoring|group_left|group_right)\s*\([^)]*\)",
        "",
        outside_braces,
        flags=re.IGNORECASE,
    )
    # Remove range lookbacks like `[5m]`
    outside_braces = re.sub(r"\[[^\]]*\]", "", outside_braces)

    # Extract candidate metric identifiers (tokens not immediately followed by '(' and not keywords)
    tokens = re.findall(r"\b([a-zA-Z_:][a-zA-Z0-9_:]*)\b(?!\s*\()", outside_braces)
    metric_tokens = [t for t in tokens if t.lower() not in PROMQL_KEYWORDS]

    if not has_explicit_name and not metric_tokens:
        errors.append(
            "Query does not contain a recognizable metric name (`__name__`). "
            "Every `/query` or `/query_range` request in Azure Monitor Workspace must be scoped to a metric."
        )

    distinct_metrics = []
    for m in metric_tokens:
        if m not in distinct_metrics:
            distinct_metrics.append(m)

    if len(distinct_metrics) > 1 and "target_info" in [m.lower() for m in distinct_metrics]:
        warnings.append(
            f"Query joins multiple metrics ({', '.join(distinct_metrics)}). "
            "Note: Azure Monitor Workspace enforces one `__name__` matcher per selector; if the API rejects a binary "
            "join with `target_info`, query `target_info` via `/api/v1/series?match[]=target_info` first to resolve `instance`/`job` labels."
        )
    elif len(distinct_metrics) > 1:
        warnings.append(
            f"Query references multiple metric names ({', '.join(distinct_metrics)}). "
            "Azure Monitor Workspace states there can be only one `__name__` label matcher in a query; "
            "if rejected, query each metric separately and correlate in Python."
        )

    return {
        "valid": len(errors) == 0,
        "errors": errors,
        "warnings": warnings,
        "detected_metrics": distinct_metrics,
        "max_lookback_seconds": extract_max_lookback_seconds(q),
    }


def validate_series_matcher(matchers: List[str]) -> Dict[str, Any]:
    """
    Validate `/api/v1/series` `match[]` selectors against Azure Monitor Workspace rules:
    1. At least one `match[]` parameter is required.
    2. Regex filters (`=~`, `!~`) are NOT supported in `/api/v1/series`.
    3. Each matcher must include a metric name (`__name__`).
    """
    errors: List[str] = []
    if not matchers:
        return {"valid": False, "errors": ["At least one `match[]` selector is required for `/api/v1/series`."]}

    for matcher in matchers:
        m = matcher.strip()
        if "=~" in m or "!~" in m:
            errors.append(
                f"Invalid `/series` matcher `{m}`: Azure Monitor Workspace `/api/v1/series` does NOT support regular expression filters (`=~` or `!~`)."
            )
        no_strings = re.sub(r'"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'', '""', m)
        has_Leading_metric = bool(re.match(r"^[a-zA-Z_:][a-zA-Z0-9_:]*", no_strings))
        has_explicit_name = "__name__" in no_strings
        if not has_Leading_metric and not has_explicit_name:
            errors.append(
                f"Invalid `/series` matcher `{m}`: Must include a metric name (e.g. `system_cpu_time_seconds_total` or `target_info{{host_name=\"vm01\"}}`)."
            )

    return {"valid": len(errors) == 0, "errors": errors}


def normalize_endpoint(endpoint: str) -> str:
    """Normalize an Azure Monitor Workspace Prometheus query endpoint URL."""
    ep = endpoint.strip().rstrip("/")
    if not ep.startswith("http://") and not ep.startswith("https://"):
        ep = "https://" + ep
    return ep


def build_az_rest_request(
    endpoint: str,
    api: str,
    query: Optional[str] = None,
    start: Optional[str] = None,
    end: Optional[str] = None,
    step: Optional[str] = None,
    time_param: Optional[str] = None,
    match: Optional[List[str]] = None,
    label_name: str = "__name__",
    now: Optional[dt.datetime] = None,
) -> Dict[str, Any]:
    """
    Validate inputs and construct the URL and safe `az rest --method get` command
    for Azure Monitor Workspace Prometheus REST APIs.
    """
    base_url = normalize_endpoint(endpoint)
    ref_now = now or dt.datetime.now(dt.timezone.utc)
    warnings: List[str] = []

    api_norm = api.strip().lower().replace("-", "_")
    if api_norm == "label_values":
        if label_name != "__name__":
            return {
                "valid": False,
                "errors": [
                    f"Unsupported label values request for `{label_name}`: Azure Monitor Workspace ONLY supports "
                    "`/api/v1/label/__name__/values`. To inspect values for `{label_name}`, use `api='series'` "
                    "with `match=['<metric_name>']` or run an instant query `group by ({label_name}) (<metric_name>)`."
                ],
            }
        api_norm = "metric_names"

    if api_norm == "metric_names":
        path = "/api/v1/label/__name__/values"
        query_string = ""
    elif api_norm == "labels":
        path = "/api/v1/labels"
        query_string = ""
    elif api_norm == "series":
        match_list = match or ([query] if query else [])
        validation = validate_series_matcher(match_list)
        if not validation["valid"]:
            return validation
        end_dt = parse_timestamp(end, ref_now) if end else ref_now
        start_dt = parse_timestamp(start, ref_now) if start else (end_dt - dt.timedelta(hours=6))
        if (end_dt - start_dt).total_seconds() > MAX_SERIES_RANGE_SECONDS:
            warnings.append(
                "Requested `/series` window exceeds Azure Monitor Workspace's 12-hour limit; clamped `start` to `end - 12h`."
            )
            start_dt = end_dt - dt.timedelta(seconds=MAX_SERIES_RANGE_SECONDS)
        params: List[Tuple[str, str]] = [("match[]", m.strip()) for m in match_list]
        params.append(("start", format_rfc3339(start_dt)))
        params.append(("end", format_rfc3339(end_dt)))
        path = "/api/v1/series"
        query_string = urllib.parse.urlencode(params, doseq=True, quote_via=urllib.parse.quote)
    elif api_norm == "query":
        validation = validate_promql_query(query or "")
        if not validation["valid"]:
            return validation
        warnings.extend(validation.get("warnings", []))
        params = [("query", (query or "").strip())]
        if time_param:
            params.append(("time", format_rfc3339(parse_timestamp(time_param, ref_now))))
        path = "/api/v1/query"
        query_string = urllib.parse.urlencode(params, quote_via=urllib.parse.quote)
    elif api_norm == "query_range":
        validation = validate_promql_query(query or "")
        if not validation["valid"]:
            return validation
        warnings.extend(validation.get("warnings", []))
        end_dt = parse_timestamp(end, ref_now) if end else ref_now
        start_dt = parse_timestamp(start, ref_now) if start else (end_dt - dt.timedelta(hours=1))
        range_seconds = (end_dt - start_dt).total_seconds()
        if range_seconds <= 0:
            return {
                "valid": False,
                "errors": [f"`end` ({format_rfc3339(end_dt)}) must be after `start` ({format_rfc3339(start_dt)})."],
            }
        lookback_seconds = validation.get("max_lookback_seconds", 0.0)
        total_window = range_seconds + lookback_seconds
        if total_window > MAX_QUERY_RANGE_SECONDS:
            return {
                "valid": False,
                "errors": [
                    f"Total queried time window ({total_window / 86400:.2f} days, including {lookback_seconds / 3600:.1f}h "
                    "query range lookback) exceeds Azure Monitor Workspace's 32-day maximum limit."
                ],
            }
        resolved_step = step.strip() if step else recommend_step(range_seconds)
        params = [
            ("query", (query or "").strip()),
            ("start", format_rfc3339(start_dt)),
            ("end", format_rfc3339(end_dt)),
            ("step", resolved_step),
        ]
        path = "/api/v1/query_range"
        query_string = urllib.parse.urlencode(params, quote_via=urllib.parse.quote)
    else:
        return {
            "valid": False,
            "errors": [f"Unknown API type `{api}`. Supported values: query, query_range, series, labels, metric_names."],
        }

    full_url = f"{base_url}{path}" + (f"?{query_string}" if query_string else "")
    # Note: Never include -o/--output or --query in RunAzCliReadCommands
    az_rest_cmd = f'az rest --method get --resource "{PROMETHEUS_RESOURCE}" --url "{full_url}"'

    return {
        "valid": True,
        "api": api_norm,
        "url": full_url,
        "az_rest_command": az_rest_cmd,
        "warnings": warnings,
    }


def discover_workspaces(
    subscription: Optional[str] = None,
    resource_group: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Discover Azure Monitor Workspaces (`Microsoft.Monitor/accounts`) and extract their
    `metrics.prometheusQueryEndpoint` values.
    Works in local GitHub Copilot App / CLI environments (via `az monitor account list`)
    or returns the exact plain `az monitor account list` command for Azure SRE Agent's
    `RunAzCliReadCommands` tool (without `-o` or `--query` flags).
    """
    plain_cmd_parts = ["az", "monitor", "account", "list"]
    if subscription:
        plain_cmd_parts.extend(["--subscription", subscription])
    if resource_group:
        plain_cmd_parts.extend(["--resource-group", resource_group])
    plain_sre_cmd = " ".join(plain_cmd_parts)

    try:
        proc = subprocess.run(
            plain_cmd_parts + ["-o", "json"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
            shell=(os.name == "nt"),
        )
        if proc.returncode == 0 and proc.stdout.strip():
            raw_list = json.loads(proc.stdout)
            workspaces = []
            for item in raw_list:
                metrics_obj = item.get("metrics") or (item.get("properties") or {}).get("metrics") or {}
                workspaces.append(
                    {
                        "name": item.get("name"),
                        "resourceGroup": item.get("resourceGroup"),
                        "location": item.get("location"),
                        "id": item.get("id"),
                        "prometheusQueryEndpoint": metrics_obj.get("prometheusQueryEndpoint"),
                        "defaultIngestionEndpoint": (
                            (item.get("defaultIngestionSettings") or {}).get("dataCollectionEndpointResourceId")
                        ),
                    }
                )
            return {
                "executed": True,
                "count": len(workspaces),
                "workspaces": workspaces,
            }
        err_msg = proc.stderr.strip() or "az monitor account list returned non-zero status"
    except Exception as exc:
        err_msg = str(exc)

    return {
        "executed": False,
        "az_cli_command": plain_sre_cmd,
        "note": (
            f"Local Azure CLI execution unavailable ({err_msg}). "
            "In Azure SRE Agent, run `az_cli_command` via `RunAzCliReadCommands` (without `-o` or `--query`)."
        ),
    }


def acquire_prometheus_token(explicit_token: Optional[str] = None) -> Optional[str]:
    """
    Attempt to acquire a Microsoft Entra ID Bearer token for `https://prometheus.monitor.azure.com`.
    Tries:
      1. `explicit_token` or `AZMON_PROMETHEUS_TOKEN` env var
      2. `azure.identity` (`ManagedIdentityCredential` if `AZURE_CLIENT_ID` is set, or `DefaultAzureCredential`)
      3. `az account get-access-token --resource https://prometheus.monitor.azure.com`
    Returns None if running in a sandbox without direct token access for the Prometheus audience.
    """
    token = explicit_token or os.environ.get("AZMON_PROMETHEUS_TOKEN")
    if token:
        return token.strip()

    try:
        from azure.identity import DefaultAzureCredential, ManagedIdentityCredential  # type: ignore

        client_id = os.environ.get("AZURE_CLIENT_ID")
        cred = ManagedIdentityCredential(client_id=client_id) if client_id else DefaultAzureCredential()
        tok = cred.get_token(PROMETHEUS_SCOPE)
        if tok and tok.token:
            return tok.token
    except Exception:
        pass

    try:
        proc = subprocess.run(
            [
                "az",
                "account",
                "get-access-token",
                "--resource",
                PROMETHEUS_RESOURCE,
                "--query",
                "accessToken",
                "-o",
                "tsv",
            ],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
            shell=(os.name == "nt"),
        )
        if proc.returncode == 0 and proc.stdout.strip():
            return proc.stdout.strip()
    except Exception:
        pass

    return None


def classify_otel_metric(metric_name: str) -> str:
    """Categorize a Prometheus metric name into an OpenTelemetry host/process domain."""
    m = metric_name.lower()
    if "target_info" in m or "otel_scope_info" in m:
        return "metadata_target_info"
    if "cpu" in m or "load_average" in m:
        return "cpu"
    if "paging" in m or "swap" in m or "pagefile" in m:
        return "paging"
    if "memory" in m or "mem_" in m:
        return "memory"
    if "filesystem" in m or "fs_" in m:
        return "filesystem"
    if "disk" in m:
        return "disk"
    if "network" in m or "net_" in m or "tcp" in m:
        return "network"
    if m.startswith("process_") or "_process_" in m:
        return "process"
    return "other"


def _compute_percentile(sorted_vals: List[float], pct: float) -> float:
    if not sorted_vals:
        return 0.0
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    rank = (pct / 100.0) * (len(sorted_vals) - 1)
    low = int(math.floor(rank))
    high = int(math.ceil(rank))
    if low == high:
        return sorted_vals[low]
    weight = rank - low
    return sorted_vals[low] * (1.0 - weight) + sorted_vals[high] * weight


def summarize_prometheus_response(
    payload: Dict[str, Any],
    top_n: int = 25,
    filter_pattern: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Summarize a Prometheus HTTP API JSON response into a compact, LLM-friendly structure.
    Handles:
      - `matrix` (from `/api/v1/query_range`)
      - `vector` (from `/api/v1/query`)
      - `scalar` (from `/api/v1/query`)
      - list of metric/label strings (from `/api/v1/label/__name__/values` or `/api/v1/labels`)
      - list of label dicts (from `/api/v1/series`)
    """
    status = payload.get("status")
    if status != "success":
        return {
            "status": status or "error",
            "errorType": payload.get("errorType"),
            "error": payload.get("error", "Unknown Prometheus API error"),
        }

    data = payload.get("data")

    # 1. `/api/v1/label/__name__/values` or `/api/v1/labels` -> list of strings
    if isinstance(data, list) and all(isinstance(x, str) for x in data):
        items = data
        if filter_pattern:
            regex = re.compile(filter_pattern, re.IGNORECASE)
            items = [x for x in items if regex.search(x)]
        grouped: Dict[str, List[str]] = {}
        for item in items:
            cat = classify_otel_metric(item)
            grouped.setdefault(cat, []).append(item)
        return {
            "status": "success",
            "resultType": "string_list",
            "total_count": len(data),
            "filtered_count": len(items),
            "grouped_by_otel_domain": {k: sorted(v)[:top_n] for k, v in sorted(grouped.items())},
            "sample_values": sorted(items)[: min(len(items), top_n * 4)],
        }

    # 2. `/api/v1/series` -> list of label dicts
    if isinstance(data, list) and all(isinstance(x, dict) for x in data):
        series_list: List[Dict[str, str]] = data
        if filter_pattern:
            regex = re.compile(filter_pattern, re.IGNORECASE)
            series_list = [
                s for s in series_list if any(regex.search(f"{k}={v}") for k, v in s.items())
            ]
        label_keys: Dict[str, set] = {}
        for s in series_list:
            for k, v in s.items():
                label_keys.setdefault(k, set()).add(str(v))
        distinct_summary = {
            k: {
                "distinct_count": len(vals),
                "sample_values": sorted(vals)[:top_n],
            }
            for k, vals in sorted(label_keys.items())
        }
        return {
            "status": "success",
            "resultType": "series_list",
            "total_series": len(data),
            "filtered_series": len(series_list),
            "label_value_summary": distinct_summary,
            "sample_series": series_list[:top_n],
        }

    # 3. `/api/v1/query` or `/api/v1/query_range` -> dict with `resultType` and `result`
    if isinstance(data, dict):
        result_type = data.get("resultType", "")
        result_items = data.get("result", [])

        if result_type == "scalar" and isinstance(result_items, list) and len(result_items) == 2:
            ts_epoch, val_str = result_items
            return {
                "status": "success",
                "resultType": "scalar",
                "timestamp": format_rfc3339(dt.datetime.fromtimestamp(float(ts_epoch), tz=dt.timezone.utc)),
                "value": float(val_str),
            }

        summaries: List[Dict[str, Any]] = []
        for entry in result_items:
            metric_labels = entry.get("metric", {})
            if filter_pattern:
                regex = re.compile(filter_pattern, re.IGNORECASE)
                if not any(regex.search(f"{k}={v}") for k, v in metric_labels.items()):
                    continue

            raw_points: List[List[Any]] = []
            if result_type == "matrix":
                raw_points = entry.get("values", [])
            elif result_type == "vector":
                single = entry.get("value")
                if single:
                    raw_points = [single]

            parsed_points: List[Tuple[float, float]] = []
            for pt in raw_points:
                if len(pt) >= 2:
                    try:
                        ts_val = float(pt[0])
                        num_val = float(pt[1])
                        if not math.isnan(num_val) and not math.isinf(num_val):
                            parsed_points.append((ts_val, num_val))
                    except (ValueError, TypeError):
                        continue

            if not parsed_points:
                continue

            values_only = [p[1] for p in parsed_points]
            sorted_vals = sorted(values_only)
            min_pt = min(parsed_points, key=lambda p: p[1])
            max_pt = max(parsed_points, key=lambda p: p[1])
            first_pt = parsed_points[0]
            latest_pt = parsed_points[-1]
            avg_val = sum(values_only) / len(values_only)
            p95_val = _compute_percentile(sorted_vals, 95.0)

            summaries.append(
                {
                    "metric": metric_labels,
                    "samples": len(parsed_points),
                    "min": round(min_pt[1], 6),
                    "max": round(max_pt[1], 6),
                    "avg": round(avg_val, 6),
                    "p95": round(p95_val, 6),
                    "first": round(first_pt[1], 6),
                    "latest": round(latest_pt[1], 6),
                    "delta": round(latest_pt[1] - first_pt[1], 6),
                    "peak_timestamp": format_rfc3339(
                        dt.datetime.fromtimestamp(max_pt[0], tz=dt.timezone.utc)
                    ),
                    "latest_timestamp": format_rfc3339(
                        dt.datetime.fromtimestamp(latest_pt[0], tz=dt.timezone.utc)
                    ),
                }
            )

        # Sort descending by `max` then `latest`
        summaries.sort(key=lambda s: (s["max"], s["latest"]), reverse=True)

        return {
            "status": "success",
            "resultType": result_type,
            "total_series": len(result_items),
            "returned_series": min(len(summaries), top_n),
            "series_summaries": summaries[:top_n],
        }

    return {"status": "success", "data": data}


def execute_or_build(
    endpoint: str,
    api: str,
    query: Optional[str] = None,
    start: Optional[str] = None,
    end: Optional[str] = None,
    step: Optional[str] = None,
    time_param: Optional[str] = None,
    match: Optional[List[str]] = None,
    label_name: str = "__name__",
    top_n: int = 25,
    filter_pattern: Optional[str] = None,
    token: Optional[str] = None,
    build_only: bool = False,
) -> Dict[str, Any]:
    """
    Validate request, build the safe `az rest` GET command, and—unless `build_only=True`—
    attempt direct HTTP execution with Entra ID token acquisition and automatic summarization.
    """
    built = build_az_rest_request(
        endpoint=endpoint,
        api=api,
        query=query,
        start=start,
        end=end,
        step=step,
        time_param=time_param,
        match=match,
        label_name=label_name,
    )
    if not built.get("valid"):
        return built

    if build_only:
        return built

    bearer = acquire_prometheus_token(token)
    if not bearer:
        built["executed"] = False
        built["next_step"] = (
            "Direct token acquisition for `https://prometheus.monitor.azure.com` was not available in this Python sandbox. "
            "Run the `az_rest_command` above using `RunAzCliReadCommands`, then pass the returned JSON to "
            "`promql_helper.py summarize` (or `summarize_prometheus_response()`)."
        )
        return built

    req = urllib.request.Request(
        built["url"],
        headers={
            "Authorization": f"Bearer {bearer}",
            "Accept": "application/json",
        },
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw_body = resp.read().decode("utf-8")
            payload = json.loads(raw_body)
            summary = summarize_prometheus_response(
                payload, top_n=top_n, filter_pattern=filter_pattern
            )
            return {
                "valid": True,
                "executed": True,
                "api": built["api"],
                "url": built["url"],
                "warnings": built.get("warnings", []),
                "summary": summary,
            }
    except urllib.error.HTTPError as exc:
        err_body = exc.read().decode("utf-8", errors="replace")
        return {
            "valid": True,
            "executed": False,
            "http_status": exc.code,
            "error": err_body,
            "url": built["url"],
            "az_rest_command": built["az_rest_command"],
            "troubleshooting_hint": (
                "If HTTP 401/403: verify the SRE Agent's User-Assigned Managed Identity (UAMI) has the "
                "'Monitoring Data Reader' role (b0d8363b-8ddd-447d-831f-62ca05bff136) assigned on the Azure Monitor Workspace."
                if exc.code in (401, 403)
                else "If HTTP 400/422: check that the query has a single `__name__` metric matcher and valid PromQL syntax."
            ),
        }
    except Exception as exc:
        return {
            "valid": True,
            "executed": False,
            "error": str(exc),
            "url": built["url"],
            "az_rest_command": built["az_rest_command"],
        }


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Azure Monitor Workspace (Managed Prometheus) PromQL validator, URL builder, and response summarizer."
    )
    sub = parser.add_subparsers(dest="subcommand", required=True)

    # 1. build / run
    for cmd_name in ("build", "run"):
        p = sub.add_parser(
            cmd_name,
            help=(
                "Validate and build a safe `az rest --method get` command"
                if cmd_name == "build"
                else "Validate, execute (if token available), and summarize a PromQL API request"
            ),
        )
        p.add_argument(
            "--endpoint",
            required=True,
            help="Azure Monitor Workspace Prometheus query endpoint (e.g. https://<ws>.<region>.prometheus.monitor.azure.com)",
        )
        p.add_argument(
            "--api",
            required=True,
            choices=["query", "query_range", "series", "labels", "metric_names", "label_values"],
            help="Target Prometheus HTTP API endpoint",
        )
        p.add_argument("--query", help="PromQL expression (for `query` or `query_range`)")
        p.add_argument(
            "--match",
            action="append",
            help="Series selector for `series` API (repeatable, e.g. --match 'target_info' --match 'system_cpu_time_seconds_total')",
        )
        p.add_argument("--start", help="Range start time (ISO-8601, epoch, or relative like '-1h', '-6h', '-7d')")
        p.add_argument("--end", help="Range end time (ISO-8601, epoch, or 'now'; default 'now')")
        p.add_argument("--step", help="Query range resolution step (e.g. '1m', '5m', '1h'; auto-calculated if omitted)")
        p.add_argument("--time", dest="time_param", help="Evaluation timestamp for instant `query`")
        p.add_argument("--label", default="__name__", help="Label name for `label_values` (only `__name__` is supported)")
        p.add_argument("--top", type=int, default=25, help="Maximum number of series/items in summary (default: 25)")
        p.add_argument("--filter", dest="filter_pattern", help="Optional regex filter for metric names or labels")
        p.add_argument("--token", help="Optional explicit Bearer token for https://prometheus.monitor.azure.com")

    # 2. summarize
    s = sub.add_parser(
        "summarize",
        help="Summarize raw Prometheus JSON output (from a file or stdin) into compact per-series statistics",
    )
    s.add_argument("--input", "-i", help="Path to JSON file containing Prometheus API response (reads stdin if omitted)")
    s.add_argument("--top", type=int, default=25, help="Maximum number of series to return (default: 25)")
    s.add_argument("--filter", dest="filter_pattern", help="Optional regex filter on metric names or label key=value pairs")

    # 3. validate
    v = sub.add_parser("validate", help="Validate a PromQL expression against Azure Monitor Workspace API limitations")
    v.add_argument("--query", required=True, help="PromQL expression to validate")

    # 4. discover
    d = sub.add_parser(
        "discover",
        help="Discover Azure Monitor Workspaces and their prometheusQueryEndpoint URLs (or output the plain SRE Agent CLI command)",
    )
    d.add_argument("--subscription", help="Optional Azure subscription ID or name")
    d.add_argument("--resource-group", "-g", dest="resource_group", help="Optional Azure resource group name")

    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = _build_arg_parser()
    args = parser.parse_args(argv)

    if args.subcommand == "discover":
        res = discover_workspaces(
            subscription=args.subscription,
            resource_group=args.resource_group,
        )
        print(json.dumps(res, indent=2))
        return 0

    if args.subcommand == "validate":
        res = validate_promql_query(args.query)
        print(json.dumps(res, indent=2))
        return 0 if res.get("valid") else 1

    if args.subcommand == "summarize":
        raw_text = (
            open(args.input, "r", encoding="utf-8").read()
            if args.input
            else sys.stdin.read()
        )
        payload = json.loads(raw_text)
        res = summarize_prometheus_response(
            payload, top_n=args.top, filter_pattern=args.filter_pattern
        )
        print(json.dumps(res, indent=2))
        return 0 if res.get("status") == "success" else 1

    if args.subcommand in ("build", "run"):
        res = execute_or_build(
            endpoint=args.endpoint,
            api=args.api,
            query=args.query,
            start=args.start,
            end=args.end,
            step=args.step,
            time_param=args.time_param,
            match=args.match,
            label_name=args.label,
            top_n=args.top,
            filter_pattern=args.filter_pattern,
            token=args.token,
            build_only=(args.subcommand == "build"),
        )
        print(json.dumps(res, indent=2))
        return 0 if res.get("valid") else 1

    return 1


if __name__ == "__main__":
    sys.exit(main())
