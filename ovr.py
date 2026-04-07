#!/usr/bin/env python3
"""
ovr — RVTools-equivalent for OpenShift Virtualization
Collects VM inventory + real consumption data from OCP-V clusters
and produces an HTML report + CSV export.

Author: Mohammed Salih Puthenpurayil (Mo)
License: Apache 2.0
GitHub: https://github.com/linusali/ocpv-reporter
"""

import argparse
import base64
import csv
import json
import os
import subprocess
import sys
import urllib.parse
import urllib.request
import ssl
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Union

# ── Optional: kubernetes Python client ──────────────────────────────────────
try:
    from kubernetes import client, config as k8s_config
    HAS_K8S_CLIENT = True
except ImportError:
    HAS_K8S_CLIENT = False

TOOL_VERSION = "1.0.0"

# ── Prometheus metric queries ────────────────────────────────────────────────
PROM_QUERIES = {
    "cpu_usage":         'sum(rate(kubevirt_vmi_cpu_usage_seconds_total[30m])) by (name, namespace)',
    "mem_used":          'kubevirt_vmi_memory_used_bytes',
    "mem_available":     'kubevirt_vmi_memory_available_bytes',
    "disk_read_bps":     'sum(rate(kubevirt_vmi_storage_read_traffic_bytes_total[30m])) by (name, namespace)',
    "disk_write_bps":    'sum(rate(kubevirt_vmi_storage_write_traffic_bytes_total[30m])) by (name, namespace)',
    "net_rx_bps":        'sum(rate(kubevirt_vmi_network_receive_bytes_total[30m])) by (name, namespace)',
    "net_tx_bps":        'sum(rate(kubevirt_vmi_network_transmit_bytes_total[30m])) by (name, namespace)',
    "disk_read_iops":    'sum(rate(kubevirt_vmi_storage_iops_read_total[30m])) by (name, namespace)',
    "disk_write_iops":   'sum(rate(kubevirt_vmi_storage_iops_write_total[30m])) by (name, namespace)',
}


# ══════════════════════════════════════════════════════════════════════════════
# Data collection helpers
# ══════════════════════════════════════════════════════════════════════════════

def run_oc(args: list) -> Optional[Union[dict, list]]:
    """Run an oc command and return parsed JSON output."""
    cmd = ["oc"] + args + ["-o", "json"]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if result.returncode != 0:
            print(f"  ⚠  oc warning: {result.stderr.strip()[:120]}", file=sys.stderr)
            return None
        return json.loads(result.stdout)
    except FileNotFoundError:
        print("✗  'oc' CLI not found. Please install it or ensure it is on your PATH.", file=sys.stderr)
        sys.exit(1)
    except json.JSONDecodeError:
        return None


def get_oc_token() -> str:
    """Get the current oc session token."""
    result = subprocess.run(["oc", "whoami", "-t"], capture_output=True, text=True)
    if result.returncode != 0:
        print("✗  Could not get oc token. Are you logged in? Run: oc login ...", file=sys.stderr)
        sys.exit(1)
    return result.stdout.strip()


def get_prometheus_route() -> str:
    """Discover the in-cluster Prometheus route."""
    result = subprocess.run(
        ["oc", "get", "route", "prometheus-k8s", "-n", "openshift-monitoring",
         "-o", "jsonpath={.spec.host}"],
        capture_output=True, text=True
    )
    if result.returncode != 0 or not result.stdout.strip():
        # Try thanos-querier (available in newer OCP)
        result = subprocess.run(
            ["oc", "get", "route", "thanos-querier", "-n", "openshift-monitoring",
             "-o", "jsonpath={.spec.host}"],
            capture_output=True, text=True
        )
    if not result.stdout.strip():
        print("✗  Could not find Prometheus/Thanos route in openshift-monitoring.", file=sys.stderr)
        sys.exit(1)
    return result.stdout.strip()


def query_prometheus(prom_url: str, token: str, query: str) -> dict:
    """Query Prometheus HTTP API, return {(namespace,name): value}."""
    encoded = urllib.parse.quote(query)
    url = f"https://{prom_url}/api/v1/query?query={encoded}"
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, context=ctx, timeout=30) as resp:
            data = json.loads(resp.read())
        result = {}
        for item in data.get("data", {}).get("result", []):
            ns = item["metric"].get("namespace", "")
            name = item["metric"].get("name", "")
            val = float(item["value"][1]) if item["value"][1] != "NaN" else 0.0
            result[(ns, name)] = val
        return result
    except Exception as e:
        print(f"  ⚠  Prometheus query failed ({query[:60]}...): {e}", file=sys.stderr)
        return {}


def collect_metrics(prom_url: str, token: str) -> dict:
    """Collect all Prometheus metrics, return nested dict."""
    print("  → Querying Prometheus metrics (CPU, memory, disk, network)...")
    metrics = {}
    for key, query in PROM_QUERIES.items():
        metrics[key] = query_prometheus(prom_url, token, query)
        print(f"    ✓ {key}: {len(metrics[key])} series")
    return metrics


def bytes_to_gib(b: float) -> float:
    return round(b / (1024 ** 3), 2)


def bytes_to_mib(b: float) -> float:
    return round(b / (1024 ** 2), 1)


def format_bps(bps: float) -> str:
    if bps >= 1024 ** 2:
        return f"{bps / (1024**2):.1f} MB/s"
    elif bps >= 1024:
        return f"{bps / 1024:.1f} KB/s"
    return f"{bps:.0f} B/s"


def parse_memory_to_gib(mem_str: str) -> float:
    """Parse a Kubernetes memory string (e.g. '4Gi', '512Mi', '1Ti') to GiB."""
    if not mem_str or mem_str in ("—", "?"):
        return 0.0
    try:
        if mem_str.endswith("Ti"):
            return float(mem_str[:-2]) * 1024
        if mem_str.endswith("Gi"):
            return float(mem_str[:-2])
        if mem_str.endswith("Mi"):
            return float(mem_str[:-2]) / 1024
        if mem_str.endswith("Ki"):
            return float(mem_str[:-2]) / (1024 ** 2)
        if mem_str.endswith("G"):
            return float(mem_str[:-1]) * (10 ** 9 / 1024 ** 3)
        if mem_str.endswith("M"):
            return float(mem_str[:-1]) * (10 ** 6 / 1024 ** 3)
        return float(mem_str) / (1024 ** 3)  # assume bytes
    except (ValueError, TypeError):
        return 0.0


def fmt_storage(raw: str) -> str:
    """Convert a raw Kubernetes storage value (bytes or suffix) to a readable string."""
    if not raw or raw in ("?", "—"):
        return raw or "—"
    gib = parse_memory_to_gib(raw)
    if gib == 0.0:
        return raw
    if gib >= 1024:
        v = round(gib / 1024, 2)
        return f"{int(v) if v == int(v) else v} TiB"
    if gib >= 1:
        v = round(gib, 1)
        return f"{int(v) if v == int(v) else v} GiB"
    v = round(gib * 1024, 1)
    return f"{int(v) if v == int(v) else v} MiB"


# ══════════════════════════════════════════════════════════════════════════════
# Inventory collection
# ══════════════════════════════════════════════════════════════════════════════

def collect_vms(namespace: Optional[str]) -> list:
    """Collect VirtualMachine specs from the cluster."""
    ns_args = ["--all-namespaces"] if not namespace else ["-n", namespace]
    print("  → Fetching VirtualMachine specs...")
    data = run_oc(["get", "vm"] + ns_args)
    if not data:
        return []
    return data.get("items", [])


def collect_vmis(namespace: Optional[str]) -> dict:
    """Collect VirtualMachineInstance runtime state, keyed by (namespace, name)."""
    ns_args = ["--all-namespaces"] if not namespace else ["-n", namespace]
    print("  → Fetching VirtualMachineInstance runtime state...")
    data = run_oc(["get", "vmi"] + ns_args)
    if not data:
        return {}
    result = {}
    for item in data.get("items", []):
        ns = item["metadata"]["namespace"]
        name = item["metadata"]["name"]
        result[(ns, name)] = item
    return result


def collect_pvcs(namespace: Optional[str]) -> dict:
    """Collect PVC sizes keyed by (namespace, name)."""
    ns_args = ["--all-namespaces"] if not namespace else ["-n", namespace]
    print("  → Fetching PersistentVolumeClaims...")
    data = run_oc(["get", "pvc"] + ns_args)
    if not data:
        return {}
    result = {}
    for item in data.get("items", []):
        ns = item["metadata"]["namespace"]
        name = item["metadata"]["name"]
        storage = (
            item.get("spec", {}).get("resources", {}).get("requests", {}).get("storage")
            or item.get("status", {}).get("capacity", {}).get("storage", "?")
        )
        phase = item.get("status", {}).get("phase", "?")
        sc = item.get("spec", {}).get("storageClassName", "?")
        result[(ns, name)] = {"storage": storage, "phase": phase, "storageClass": sc}
    return result


def parse_vm(vm: dict, vmis: dict, pvcs: dict, metrics: dict, include_cloudinit: bool = False) -> dict:
    """Merge VM spec + VMI runtime + PVC data + Prometheus metrics into one record."""
    meta = vm.get("metadata", {})
    spec = vm.get("spec", {})
    tmpl = spec.get("template", {}).get("spec", {})
    domain = tmpl.get("domain", {})
    ns = meta.get("namespace", "")
    name = meta.get("name", "")
    created = meta.get("creationTimestamp", "")

    # ── Spec ──────────────────────────────────────────────────────────────────
    cpu_cores = (
        domain.get("cpu", {}).get("cores", 1) *
        domain.get("cpu", {}).get("sockets", 1) *
        domain.get("cpu", {}).get("threads", 1)
    )
    mem_req = domain.get("resources", {}).get("requests", {}).get("memory", "")
    mem_limits = domain.get("resources", {}).get("limits", {}).get("memory", mem_req)
    mem_guest = domain.get("memory", {}).get("guest", mem_req)

    run_strategy = spec.get("runStrategy", "")
    running = spec.get("running", None)
    if run_strategy:
        configured_state = run_strategy
    elif running is True:
        configured_state = "Always"
    elif running is False:
        configured_state = "Halted"
    else:
        configured_state = "Unknown"

    # ── Disks ─────────────────────────────────────────────────────────────────
    volumes = tmpl.get("volumes", [])
    disk_records = []
    total_disk_gib = 0.0
    for vol in volumes:
        disk_name = vol.get("name", "")
        if "dataVolume" in vol:
            pvc_name = vol["dataVolume"].get("name", disk_name)
            pvc = pvcs.get((ns, pvc_name), {})
            storage = pvc.get("storage", "?")
            phase = pvc.get("phase", "?")
            sc = pvc.get("storageClass", "?")
            disk_records.append({
                "name": pvc_name, "type": "DataVolume/PVC",
                "size": fmt_storage(storage), "phase": phase, "storageClass": sc
            })
            total_disk_gib += parse_memory_to_gib(storage)
        elif "persistentVolumeClaim" in vol:
            pvc_name = vol["persistentVolumeClaim"].get("claimName", disk_name)
            pvc = pvcs.get((ns, pvc_name), {})
            storage = pvc.get("storage", "?")
            phase = pvc.get("phase", "?")
            sc = pvc.get("storageClass", "?")
            disk_records.append({
                "name": pvc_name, "type": "PVC",
                "size": fmt_storage(storage), "phase": phase, "storageClass": sc
            })
            total_disk_gib += parse_memory_to_gib(storage)
        elif "containerDisk" in vol:
            img = vol["containerDisk"].get("image", "?")
            disk_records.append({
                "name": disk_name, "type": "ContainerDisk",
                "size": "—", "phase": "N/A", "storageClass": img
            })
        elif "cloudInitNoCloud" in vol or "cloudInitConfigDrive" in vol:
            if include_cloudinit:
                disk_records.append({
                    "name": disk_name, "type": "CloudInit",
                    "size": "—", "phase": "N/A", "storageClass": "—"
                })

    # ── NICs ──────────────────────────────────────────────────────────────────
    interfaces = domain.get("devices", {}).get("interfaces", [])
    networks = tmpl.get("networks", [])
    net_map = {n["name"]: n for n in networks}
    nic_records = []
    for iface in interfaces:
        iface_name = iface.get("name", "")
        net = net_map.get(iface_name, {})
        net_type = "Pod" if "pod" in net else net.get("multus", {}).get("networkName", "multus")
        model = iface.get("model", "virtio")
        mac = iface.get("macAddress", "—")
        binding = next((k for k in ["masquerade", "bridge", "sriov", "passt"] if k in iface), "?")
        nic_records.append({
            "name": iface_name, "model": model,
            "mac": mac, "binding": binding, "network": net_type
        })

    # ── VMI runtime ───────────────────────────────────────────────────────────
    vmi = vmis.get((ns, name), {})
    vmi_status = vmi.get("status", {}) if vmi else {}
    node = vmi_status.get("nodeName", "—")
    phase = vmi_status.get("phase", "Stopped")
    # KubeVirt keeps VMI phase as "Running" when paused — detect via condition
    if phase == "Running" and any(
        c.get("type") == "Paused" and c.get("status") == "True"
        for c in vmi_status.get("conditions", [])
    ):
        phase = "Paused"
    guest_os = vmi_status.get("guestOSInfo", {})
    os_name = guest_os.get("prettyName", guest_os.get("name", "—"))
    machine_type = vmi_status.get("machine", {}).get("type", "—")

    # Runtime IPs from VMI interfaces
    vmi_ifaces = vmi_status.get("interfaces", [])
    ip_addresses = [i.get("ipAddress", "") for i in vmi_ifaces if i.get("ipAddress")]
    ip_str = ", ".join(ip_addresses) if ip_addresses else "—"

    # ── Error / problematic state ─────────────────────────────────────────────
    error_state = ""
    if phase == "Failed":
        error_state = "Failed"
    else:
        for c in vmi_status.get("conditions", []):
            reason = c.get("reason", "")
            if reason in ("ErrorUnschedulable", "Unschedulable"):
                error_state = "ErrorUnschedulable"
                break
            if (c.get("type") == "Ready" and c.get("status") == "False"
                    and phase not in ("Stopped", "Paused", "")):
                error_state = reason or "NotReady"

    # ── Prometheus metrics ─────────────────────────────────────────────────────
    key = (ns, name)
    cpu_used_cores = round(metrics.get("cpu_usage", {}).get(key, 0.0), 4)
    mem_used_b = metrics.get("mem_used", {}).get(key, 0.0)
    mem_avail_b = metrics.get("mem_available", {}).get(key, 0.0)
    disk_read = metrics.get("disk_read_bps", {}).get(key, 0.0)
    disk_write = metrics.get("disk_write_bps", {}).get(key, 0.0)
    net_rx = metrics.get("net_rx_bps", {}).get(key, 0.0)
    net_tx = metrics.get("net_tx_bps", {}).get(key, 0.0)
    disk_r_iops = metrics.get("disk_read_iops", {}).get(key, 0.0)
    disk_w_iops = metrics.get("disk_write_iops", {}).get(key, 0.0)

    mem_used_gib = bytes_to_gib(mem_used_b) if mem_used_b else "—"
    mem_avail_gib = bytes_to_gib(mem_avail_b) if mem_avail_b else "—"

    # ── Right-sizing utilisation (only meaningful for Running VMs with metrics) ─
    cpu_util_pct: Optional[float] = None
    mem_util_pct: Optional[float] = None
    if phase == "Running" and cpu_used_cores and cpu_cores > 0:
        cpu_util_pct = round(cpu_used_cores / cpu_cores * 100, 1)
    if phase == "Running" and mem_used_b:
        mem_alloc_gib = parse_memory_to_gib(mem_req or mem_guest)
        if mem_alloc_gib > 0:
            mem_util_pct = round(bytes_to_gib(mem_used_b) / mem_alloc_gib * 100, 1)

    return {
        # Identity
        "namespace": ns,
        "name": name,
        "phase": phase,
        "configured_state": configured_state,
        "node": node,
        "created": created,
        # Spec
        "cpu_cores": cpu_cores,
        "mem_requested": mem_req or mem_guest,
        "mem_limit": mem_limits,
        "os_name": os_name,
        "machine_type": machine_type,
        # Network
        "ip_addresses": ip_str,
        "nics": nic_records,
        # Storage
        "disks": disk_records,
        "total_disk_gib": round(total_disk_gib, 2),
        # Actual consumption
        "cpu_used_cores": cpu_used_cores if phase == "Running" else "—",
        "mem_used_gib": mem_used_gib,
        "mem_avail_gib": mem_avail_gib,
        "disk_read_bps": format_bps(disk_read) if disk_read else "—",
        "disk_write_bps": format_bps(disk_write) if disk_write else "—",
        "disk_read_iops": round(disk_r_iops, 1) if disk_r_iops else "—",
        "disk_write_iops": round(disk_w_iops, 1) if disk_w_iops else "—",
        "net_rx_bps": format_bps(net_rx) if net_rx else "—",
        "net_tx_bps": format_bps(net_tx) if net_tx else "—",
        # Error state (empty string = healthy)
        "error_state": error_state,
        # Right-sizing (None = no data)
        "cpu_util_pct": cpu_util_pct,
        "mem_util_pct": mem_util_pct,
    }


# ══════════════════════════════════════════════════════════════════════════════
# CSV export
# ══════════════════════════════════════════════════════════════════════════════

def export_csv(records: list[dict], output_path: str):
    """Export vInfo-style CSV."""
    fields = [
        "namespace", "name", "phase", "configured_state", "node",
        "cpu_cores", "cpu_used_cores", "cpu_util_pct", "mem_requested", "mem_limit",
        "mem_used_gib", "mem_avail_gib", "mem_util_pct", "total_disk_gib",
        "right_sizing", "ip_addresses", "os_name", "machine_type",
        "disk_read_bps", "disk_write_bps", "disk_read_iops", "disk_write_iops",
        "net_rx_bps", "net_tx_bps", "created"
    ]
    # Compute plain-text right-sizing label for CSV (no HTML)
    enriched = []
    for r in records:
        row = dict(r)
        row["right_sizing"] = rs_badge(r.get("cpu_util_pct"), r.get("mem_util_pct"), RS_DEFAULTS, html=False)
        enriched.append(row)
    with open(output_path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(enriched)
    print(f"  ✓ CSV exported: {output_path}")


def export_disk_csv(records: list[dict], output_path: str):
    """Export vDisk-style CSV."""
    with open(output_path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["vm_namespace", "vm_name", "disk_name", "type", "size", "phase", "storageClass"])
        for rec in records:
            for d in rec["disks"]:
                w.writerow([rec["namespace"], rec["name"], d["name"], d["type"], d["size"], d["phase"], d["storageClass"]])
    print(f"  ✓ Disk CSV exported: {output_path}")


def export_nic_csv(records: list[dict], output_path: str):
    """Export vNIC-style CSV."""
    with open(output_path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["vm_namespace", "vm_name", "nic_name", "model", "mac", "binding", "network"])
        for rec in records:
            for n in rec["nics"]:
                w.writerow([rec["namespace"], rec["name"], n["name"], n["model"], n["mac"], n["binding"], n["network"]])
    print(f"  ✓ NIC CSV exported: {output_path}")


# ══════════════════════════════════════════════════════════════════════════════
# HTML report
# ══════════════════════════════════════════════════════════════════════════════

def phase_badge(phase: str) -> str:
    colors = {
        "Running": ("#22c55e", "#052e16"),
        "Stopped": ("#6b7280", "#111827"),
        "Paused":  ("#f59e0b", "#1c1002"),
        "Pending": ("#3b82f6", "#0c1a3a"),
        "Scheduling": ("#a855f7", "#1a0a2e"),
        "Failed":  ("#ef4444", "#2d0a0a"),
    }
    bg, fg = colors.get(phase, ("#64748b", "#0f172a"))
    return f'<span class="badge" style="background:{bg};color:{fg}">{phase}</span>'


def vm_phase_badge(r: dict) -> str:
    """Phase badge for a VM record — shows Error when error_state is set."""
    error_state = r.get("error_state", "")
    if error_state:
        label = "Error" if error_state in ("ErrorUnschedulable", "Unschedulable") else error_state
        return (
            f'<span class="badge" style="background:#ef4444;color:#2d0a0a" '
            f'title="{error_state}">{label}</span>'
        )
    return phase_badge(r["phase"])


def na(val) -> str:
    if val in (None, "", "—", 0, 0.0):
        return '<span class="na">—</span>'
    return str(val)


# Default right-sizing thresholds (overridden by CLI flags)
RS_DEFAULTS = {"cpu_low": 20.0, "cpu_high": 80.0, "mem_low": 20.0, "mem_high": 80.0}


def rs_badge(cpu_pct: Optional[float], mem_pct: Optional[float], thresholds: dict, html: bool = True) -> str:
    """Return a right-sizing recommendation badge.

    Signals:
      Over-provisioned  — both CPU and memory below their low threshold
      Under-provisioned — either CPU or memory above their high threshold
      Right-sized       — everything in range
      No data           — VM not running or metrics unavailable
    """
    if cpu_pct is None and mem_pct is None:
        return '<span class="rs-nodata">—</span>' if html else "—"

    cpu_over  = cpu_pct is not None and cpu_pct < thresholds["cpu_low"]
    cpu_under = cpu_pct is not None and cpu_pct > thresholds["cpu_high"]
    mem_over  = mem_pct is not None and mem_pct < thresholds["mem_low"]
    mem_under = mem_pct is not None and mem_pct > thresholds["mem_high"]

    cpu_str = f"CPU {cpu_pct}%" if cpu_pct is not None else ""
    mem_str = f"Mem {mem_pct}%" if mem_pct is not None else ""
    detail  = ", ".join(filter(None, [cpu_str, mem_str]))

    if cpu_under or mem_under:
        label = "Under-provisioned"
        css   = "rs-under"
        color = "#dc2626"
    elif cpu_over and mem_over:
        label = "Over-provisioned"
        css   = "rs-over"
        color = "#f59e0b"
    elif cpu_over or mem_over:
        label = "Partially over"
        css   = "rs-partover"
        color = "#d97706"
    else:
        label = "Right-sized"
        css   = "rs-ok"
        color = "#16a34a"

    if html:
        return f'<span class="{css}" title="{detail}">{label}</span>'
    return f"{label} ({detail})"


def generate_html(records: list, cluster_name: str, namespace_filter: Optional[str], generated_at: str, logo_b64: Optional[str] = None, logo_mime: str = "image/png", rs_thresholds: Optional[dict] = None) -> str:
    total_vms   = len(records)
    running     = sum(1 for r in records if r["phase"] == "Running")
    paused      = sum(1 for r in records if r["phase"] == "Paused")
    stopped     = sum(1 for r in records if r["phase"] == "Stopped")
    total_vcpu  = sum(r["cpu_cores"] for r in records)
    total_ram   = round(sum(parse_memory_to_gib(r["mem_requested"]) for r in records), 1)
    total_disk  = round(sum(r["total_disk_gib"] for r in records), 1)
    error_vms   = sum(1 for r in records if r.get("error_state"))
    namespaces  = sorted(set(r["namespace"] for r in records))

    def fmt_gib(gib: float) -> str:
        if gib >= 1024:
            return f'{gib/1024:.1f}<span class="stat-unit">TiB</span>'
        return f'{gib}<span class="stat-unit">GiB</span>'

    _rs = rs_thresholds or RS_DEFAULTS

    # ── vInfo rows ────────────────────────────────────────────────────────────
    vinfo_rows = ""
    for r in records:
        vinfo_rows += f"""
        <tr>
          <td><span class="ns-tag">{r['namespace']}</span></td>
          <td class="vm-name">{r['name']}</td>
          <td>{vm_phase_badge(r)}</td>
          <td>{na(r['node'])}</td>
          <td class="num">{r['cpu_cores']}</td>
          <td class="num metric">{na(r['cpu_used_cores'])}</td>
          <td>{na(r['mem_requested'])}</td>
          <td class="num metric">{na(r['mem_used_gib'])} GiB</td>
          <td class="num">{r['total_disk_gib']} GiB</td>
          <td>{rs_badge(r.get('cpu_util_pct'), r.get('mem_util_pct'), _rs)}</td>
          <td>{na(r['ip_addresses'])}</td>
          <td>{na(r['os_name'])}</td>
          <td>{na(r['created'][:10] if r['created'] else '')}</td>
        </tr>"""

    # ── vDisk rows ────────────────────────────────────────────────────────────
    vdisk_rows = ""
    for r in records:
        for d in r["disks"]:
            vdisk_rows += f"""
        <tr>
          <td><span class="ns-tag">{r['namespace']}</span></td>
          <td class="vm-name">{r['name']}</td>
          <td>{d['name']}</td>
          <td><span class="type-tag">{d['type']}</span></td>
          <td class="num">{na(d['size'])}</td>
          <td>{phase_badge(d['phase']) if d['phase'] not in ('N/A','?','—') else na(d['phase'])}</td>
          <td>{na(d['storageClass'])}</td>
          <td class="metric">{na(r['disk_read_bps'])}</td>
          <td class="metric">{na(r['disk_write_bps'])}</td>
          <td class="num metric">{na(r['disk_read_iops'])}</td>
          <td class="num metric">{na(r['disk_write_iops'])}</td>
        </tr>"""

    # ── vNetwork rows ─────────────────────────────────────────────────────────
    vnet_rows = ""
    for r in records:
        for n in r["nics"]:
            vnet_rows += f"""
        <tr>
          <td><span class="ns-tag">{r['namespace']}</span></td>
          <td class="vm-name">{r['name']}</td>
          <td>{n['name']}</td>
          <td>{n['model']}</td>
          <td class="mono">{na(n['mac'])}</td>
          <td><span class="type-tag">{n['binding']}</span></td>
          <td>{na(n['network'])}</td>
          <td>{na(r['ip_addresses'])}</td>
          <td class="metric">{na(r['net_rx_bps'])}</td>
          <td class="metric">{na(r['net_tx_bps'])}</td>
        </tr>"""

    # ── Summary cards ─────────────────────────────────────────────────────────
    logo_html = (
        f'<img src="data:{logo_mime};base64,{logo_b64}" '
        f'style="height:36px;width:auto;max-width:160px;border-radius:6px;" alt="logo" />'
        if logo_b64 else '<div class="logo-icon">OVR</div>'
    )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1.0"/>
  <title>ovr — {cluster_name}</title>
  <style>
    @import url('https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;600&family=IBM+Plex+Sans:wght@300;400;500;600&display=swap');

    :root {{
      --bg:        #0a0e1a;
      --bg2:       #0f1525;
      --bg3:       #151d30;
      --border:    #1e2d4a;
      --accent:    #ee0000;
      --accent2:   #ff6b6b;
      --text:      #c9d3e8;
      --text-dim:  #5a6a8a;
      --text-hdr:  #8ba3cc;
      --green:     #22c55e;
      --blue:      #3b82f6;
      --orange:    #f59e0b;
      --yellow:    #eab308;
      --purple:    #a855f7;
      --cyan:      #06b6d4;
      --gray:      #9ca3af;
      --tab-h:     48px;
    }}

    *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}

    body {{
      font-family: 'IBM Plex Sans', sans-serif;
      background: var(--bg);
      color: var(--text);
      min-height: 100vh;
      font-size: 13px;
    }}

    /* ── Header ── */
    .header {{
      background: var(--bg2);
      border-bottom: 1px solid var(--border);
      padding: 0 32px;
      display: flex;
      align-items: center;
      gap: 24px;
      height: 64px;
    }}
    .logo {{
      display: flex;
      align-items: center;
      gap: 10px;
    }}
    .logo-icon {{
      width: 36px; height: 36px;
      background: var(--accent);
      border-radius: 6px;
      display: flex; align-items: center; justify-content: center;
      font-family: 'IBM Plex Mono', monospace;
      font-weight: 700;
      font-size: 12px;
      color: white;
      letter-spacing: -0.5px;
    }}
    .logo-text {{
      font-family: 'IBM Plex Mono', monospace;
      font-size: 15px;
      font-weight: 600;
      color: white;
      letter-spacing: -0.3px;
    }}
    .logo-sub {{
      font-size: 10px;
      color: var(--text-dim);
      font-weight: 300;
      letter-spacing: 1px;
      text-transform: uppercase;
    }}
    .header-meta {{
      margin-left: auto;
      text-align: right;
      font-size: 11px;
      color: var(--text-dim);
      font-family: 'IBM Plex Mono', monospace;
    }}
    .header-meta strong {{
      color: var(--text-hdr);
      display: block;
      font-size: 12px;
    }}

    /* ── Summary bar ── */
    .summary {{
      background: var(--bg2);
      border-bottom: 1px solid var(--border);
      padding: 20px 32px;
      display: flex;
      gap: 32px;
      align-items: flex-start;
      flex-wrap: wrap;
    }}
    .stat-group {{
      display: flex;
      gap: 20px;
    }}
    .stat {{
      display: flex;
      flex-direction: column;
      gap: 2px;
    }}
    .stat-value {{
      font-family: 'IBM Plex Mono', monospace;
      font-size: 26px;
      font-weight: 600;
      color: white;
      line-height: 1;
    }}
    .stat-value.green {{ color: var(--green); }}
    .stat-value.red {{ color: var(--accent2); }}
    .stat-value.orange {{ color: var(--orange); }}
    .stat-value.yellow {{ color: var(--yellow); }}
    .stat-value.blue {{ color: var(--blue); }}
    .stat-value.purple {{ color: var(--purple); }}
    .stat-value.cyan {{ color: var(--cyan); }}
    .stat-value.gray {{ color: var(--gray); }}
    .stat-unit {{ font-size: 0.52em; color: var(--text-dim); font-weight: 400; margin-left: 2px; }}
    /* Right-sizing badges */
    .rs-ok, .rs-over, .rs-partover, .rs-under, .rs-nodata {{
      display: inline-block; padding: 1px 7px; border-radius: 9px;
      font-size: 11px; font-weight: 600; white-space: nowrap;
    }}
    .rs-ok      {{ background: #dcfce7; color: #15803d; }}
    .rs-over    {{ background: #fef9c3; color: #854d0e; }}
    .rs-partover{{ background: #ffedd5; color: #9a3412; }}
    .rs-under   {{ background: #fee2e2; color: #b91c1c; }}
    .rs-nodata  {{ color: var(--text-dim); }}
    .stat-label {{
      font-size: 10px;
      text-transform: uppercase;
      letter-spacing: 1px;
      color: var(--text-dim);
      font-weight: 500;
    }}
    .stat-divider {{
      width: 1px;
      height: 40px;
      background: var(--border);
      align-self: center;
    }}
    /* ── Tabs ── */
    .tabs {{
      display: flex;
      gap: 0;
      border-bottom: 1px solid var(--border);
      padding: 0 32px;
      background: var(--bg2);
    }}
    .tab {{
      padding: 0 20px;
      height: var(--tab-h);
      display: flex;
      align-items: center;
      gap: 8px;
      cursor: pointer;
      font-size: 12px;
      font-weight: 500;
      color: var(--text-dim);
      border-bottom: 2px solid transparent;
      transition: all 0.15s;
      user-select: none;
      letter-spacing: 0.3px;
      text-transform: uppercase;
    }}
    .tab:hover {{ color: var(--text); }}
    .tab.active {{
      color: var(--accent);
      border-bottom-color: var(--accent);
    }}
    .tab-count {{
      background: var(--bg3);
      border: 1px solid var(--border);
      color: var(--text-dim);
      font-size: 10px;
      padding: 1px 6px;
      border-radius: 10px;
      font-family: 'IBM Plex Mono', monospace;
    }}
    .tab.active .tab-count {{
      background: #3a0a0a;
      border-color: #6a1414;
      color: var(--accent2);
    }}

    /* ── Table controls ── */
    .table-controls {{
      padding: 16px 32px 12px;
      display: flex;
      align-items: center;
      gap: 12px;
    }}
    .search-wrap {{
      position: relative;
    }}
    .search-wrap svg {{
      position: absolute;
      left: 10px;
      top: 50%;
      transform: translateY(-50%);
      opacity: 0.4;
    }}
    .search {{
      background: var(--bg2);
      border: 1px solid var(--border);
      color: var(--text);
      padding: 7px 12px 7px 32px;
      border-radius: 6px;
      font-size: 12px;
      font-family: 'IBM Plex Sans', sans-serif;
      width: 280px;
      outline: none;
      transition: border-color 0.15s;
    }}
    .search:focus {{ border-color: var(--accent); }}
    .search::placeholder {{ color: var(--text-dim); }}
    .filter-info {{
      font-size: 11px;
      color: var(--text-dim);
      font-family: 'IBM Plex Mono', monospace;
    }}
    .export-btn {{
      margin-left: auto;
      background: var(--bg2);
      border: 1px solid var(--border);
      color: var(--text-hdr);
      padding: 6px 14px;
      border-radius: 6px;
      font-size: 11px;
      cursor: pointer;
      font-family: 'IBM Plex Sans', sans-serif;
      letter-spacing: 0.3px;
      transition: all 0.15s;
      display: flex;
      align-items: center;
      gap: 6px;
    }}
    .export-btn:hover {{
      background: var(--bg3);
      border-color: var(--accent);
      color: var(--accent2);
    }}

    /* ── Table ── */
    .table-wrap {{
      padding: 0 32px 32px;
      overflow-x: auto;
    }}
    table {{
      width: 100%;
      border-collapse: collapse;
      font-size: 12px;
    }}
    thead tr {{
      background: var(--bg3);
    }}
    th {{
      padding: 10px 12px;
      text-align: left;
      font-size: 10px;
      font-weight: 600;
      text-transform: uppercase;
      letter-spacing: 0.8px;
      color: var(--text-hdr);
      border-bottom: 1px solid var(--border);
      white-space: nowrap;
      cursor: pointer;
      user-select: none;
    }}
    th:hover {{ color: var(--text); }}
    th.sort-asc::after {{ content: ' ↑'; color: var(--accent); }}
    th.sort-desc::after {{ content: ' ↓'; color: var(--accent); }}

    tbody tr {{
      border-bottom: 1px solid #111827;
      transition: background 0.1s;
    }}
    tbody tr:hover {{ background: var(--bg3); }}
    td {{
      padding: 9px 12px;
      color: var(--text);
      vertical-align: middle;
    }}
    td.num {{ text-align: right; font-family: 'IBM Plex Mono', monospace; }}
    td.metric {{
      font-family: 'IBM Plex Mono', monospace;
      color: var(--orange);
    }}
    td.mono {{ font-family: 'IBM Plex Mono', monospace; font-size: 11px; }}
    .vm-name {{
      font-family: 'IBM Plex Mono', monospace;
      font-size: 12px;
      color: white;
      font-weight: 500;
    }}
    .na {{ color: var(--text-dim); }}
    .ns-tag {{
      background: #0c1a3a;
      border: 1px solid #1e3060;
      color: #60a0d0;
      padding: 2px 7px;
      border-radius: 4px;
      font-size: 10px;
      font-family: 'IBM Plex Mono', monospace;
      white-space: nowrap;
    }}
    .type-tag {{
      background: #1a1a2e;
      border: 1px solid #2a2a4e;
      color: #a78bfa;
      padding: 2px 7px;
      border-radius: 4px;
      font-size: 10px;
      white-space: nowrap;
    }}
    .badge {{
      padding: 2px 8px;
      border-radius: 4px;
      font-size: 10px;
      font-weight: 600;
      letter-spacing: 0.3px;
      text-transform: uppercase;
      white-space: nowrap;
    }}

    /* ── Tab panels ── */
    .panel {{ display: none; }}
    .panel.active {{ display: block; }}

    /* ── Footer ── */
    .footer {{
      text-align: center;
      padding: 24px;
      font-size: 11px;
      color: var(--text-dim);
      border-top: 1px solid var(--border);
      font-family: 'IBM Plex Mono', monospace;
    }}
    .footer a {{ color: var(--accent2); text-decoration: none; }}
    .footer a:hover {{ text-decoration: underline; }}

    /* ── No data ── */
    .no-data {{
      text-align: center;
      padding: 60px;
      color: var(--text-dim);
      font-size: 13px;
    }}
    .no-data-icon {{ font-size: 36px; margin-bottom: 12px; }}
    .hidden {{ display: none !important; }}
  </style>
</head>
<body>

<header class="header">
  <div class="logo">
    {logo_html}
    <div>
      <div class="logo-text">ovr</div>
      <div class="logo-sub">OpenShift Virtualization Reporter</div>
    </div>
  </div>
  <div class="header-meta">
    <strong>{cluster_name}</strong>
    Generated {generated_at}
    <br>ovr v{TOOL_VERSION}
  </div>
</header>

<div class="summary">
  <div class="stat-group">
    <div class="stat">
      <div class="stat-value">{total_vms}</div>
      <div class="stat-label">Total VMs</div>
    </div>
    <div class="stat-divider"></div>
    <div class="stat">
      <div class="stat-value green">{running}</div>
      <div class="stat-label">Running</div>
    </div>
    <div class="stat">
      <div class="stat-value yellow">{paused}</div>
      <div class="stat-label">Paused</div>
    </div>
    <div class="stat">
      <div class="stat-value gray">{stopped}</div>
      <div class="stat-label">Stopped</div>
    </div>
    <div class="stat">
      <div class="stat-value {'red' if error_vms else 'green'}">{error_vms if error_vms else "✓"}</div>
      <div class="stat-label">Errors</div>
    </div>
    <div class="stat-divider"></div>
    <div class="stat">
      <div class="stat-value blue">{total_vcpu}</div>
      <div class="stat-label">Total vCPUs</div>
    </div>
    <div class="stat">
      <div class="stat-value purple">{fmt_gib(total_ram)}</div>
      <div class="stat-label">Total RAM</div>
    </div>
    <div class="stat">
      <div class="stat-value cyan">{fmt_gib(total_disk)}</div>
      <div class="stat-label">Total Disk</div>
    </div>
    <div class="stat-divider"></div>
    <div class="stat">
      <div class="stat-value">{len(namespaces)}</div>
      <div class="stat-label">Namespaces</div>
    </div>
  </div>
</div>

<div class="tabs">
  <div class="tab active" onclick="switchTab('vinfo', this)">
    vInfo <span class="tab-count">{total_vms}</span>
  </div>
  <div class="tab" onclick="switchTab('vdisk', this)">
    vDisk <span class="tab-count">{sum(len(r['disks']) for r in records)}</span>
  </div>
  <div class="tab" onclick="switchTab('vnetwork', this)">
    vNetwork <span class="tab-count">{sum(len(r['nics']) for r in records)}</span>
  </div>
</div>

<!-- ── vInfo Tab ── -->
<div id="panel-vinfo" class="panel active">
  <div class="table-controls">
    <div class="search-wrap">
      <svg width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24">
        <circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/>
      </svg>
      <input class="search" type="text" placeholder="Filter by name, namespace, OS, node..." oninput="filterTable('tbl-vinfo', this.value)" />
    </div>
    <span class="filter-info" id="count-vinfo">{total_vms} VMs</span>
    <button class="export-btn" onclick="exportTableCSV('tbl-vinfo', 'ocpv-vinfo.csv')">
      ↓ Export CSV
    </button>
  </div>
  <div class="table-wrap">
    <table id="tbl-vinfo">
      <thead>
        <tr>
          <th onclick="sortTable('tbl-vinfo',0)">Namespace</th>
          <th onclick="sortTable('tbl-vinfo',1)">VM Name</th>
          <th onclick="sortTable('tbl-vinfo',2)">Status</th>
          <th onclick="sortTable('tbl-vinfo',3)">Node</th>
          <th onclick="sortTable('tbl-vinfo',4)">vCPUs</th>
          <th onclick="sortTable('tbl-vinfo',5)">CPU Used ●</th>
          <th onclick="sortTable('tbl-vinfo',6)">Mem Alloc</th>
          <th onclick="sortTable('tbl-vinfo',7)">Mem Used ●</th>
          <th onclick="sortTable('tbl-vinfo',8)">Disk (GiB)</th>
          <th onclick="sortTable('tbl-vinfo',9)">Sizing</th>
          <th onclick="sortTable('tbl-vinfo',10)">IP Address</th>
          <th onclick="sortTable('tbl-vinfo',11)">Guest OS</th>
          <th onclick="sortTable('tbl-vinfo',12)">Created</th>
        </tr>
      </thead>
      <tbody>{vinfo_rows}</tbody>
    </table>
  </div>
</div>

<!-- ── vDisk Tab ── -->
<div id="panel-vdisk" class="panel">
  <div class="table-controls">
    <div class="search-wrap">
      <svg width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24">
        <circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/>
      </svg>
      <input class="search" type="text" placeholder="Filter by VM, disk, storage class..." oninput="filterTable('tbl-vdisk', this.value)" />
    </div>
    <span class="filter-info" id="count-vdisk">{sum(len(r['disks']) for r in records)} disks</span>
    <button class="export-btn" onclick="exportTableCSV('tbl-vdisk', 'ocpv-vdisk.csv')">
      ↓ Export CSV
    </button>
  </div>
  <div class="table-wrap">
    <table id="tbl-vdisk">
      <thead>
        <tr>
          <th onclick="sortTable('tbl-vdisk',0)">Namespace</th>
          <th onclick="sortTable('tbl-vdisk',1)">VM Name</th>
          <th onclick="sortTable('tbl-vdisk',2)">Disk Name</th>
          <th onclick="sortTable('tbl-vdisk',3)">Type</th>
          <th onclick="sortTable('tbl-vdisk',4)">Size</th>
          <th onclick="sortTable('tbl-vdisk',5)">Phase</th>
          <th onclick="sortTable('tbl-vdisk',6)">Storage Class</th>
          <th onclick="sortTable('tbl-vdisk',7)">Read ●</th>
          <th onclick="sortTable('tbl-vdisk',8)">Write ●</th>
          <th onclick="sortTable('tbl-vdisk',9)">Read IOPS ●</th>
          <th onclick="sortTable('tbl-vdisk',10)">Write IOPS ●</th>
        </tr>
      </thead>
      <tbody>{vdisk_rows}</tbody>
    </table>
  </div>
</div>

<!-- ── vNetwork Tab ── -->
<div id="panel-vnetwork" class="panel">
  <div class="table-controls">
    <div class="search-wrap">
      <svg width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24">
        <circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/>
      </svg>
      <input class="search" type="text" placeholder="Filter by VM, NIC, network..." oninput="filterTable('tbl-vnetwork', this.value)" />
    </div>
    <span class="filter-info" id="count-vnetwork">{sum(len(r['nics']) for r in records)} NICs</span>
    <button class="export-btn" onclick="exportTableCSV('tbl-vnetwork', 'ocpv-vnetwork.csv')">
      ↓ Export CSV
    </button>
  </div>
  <div class="table-wrap">
    <table id="tbl-vnetwork">
      <thead>
        <tr>
          <th onclick="sortTable('tbl-vnetwork',0)">Namespace</th>
          <th onclick="sortTable('tbl-vnetwork',1)">VM Name</th>
          <th onclick="sortTable('tbl-vnetwork',2)">NIC Name</th>
          <th onclick="sortTable('tbl-vnetwork',3)">Model</th>
          <th onclick="sortTable('tbl-vnetwork',4)">MAC Address</th>
          <th onclick="sortTable('tbl-vnetwork',5)">Binding</th>
          <th onclick="sortTable('tbl-vnetwork',6)">Network</th>
          <th onclick="sortTable('tbl-vnetwork',7)">IP Address</th>
          <th onclick="sortTable('tbl-vnetwork',8)">RX ●</th>
          <th onclick="sortTable('tbl-vnetwork',9)">TX ●</th>
        </tr>
      </thead>
      <tbody>{vnet_rows}</tbody>
    </table>
  </div>
</div>

<div class="footer">
  <a href="https://github.com/linusali/ocpv-reporter">ovr</a>
  · Apache 2.0 · ● = live Prometheus metrics (30m avg)
  · Report generated {generated_at}
</div>

<script>
  // ── Tab switching ──────────────────────────────────────────────────────────
  function switchTab(name, el) {{
    document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
    document.querySelectorAll('.panel').forEach(p => p.classList.remove('active'));
    el.classList.add('active');
    document.getElementById('panel-' + name).classList.add('active');
  }}

  // ── Filter ────────────────────────────────────────────────────────────────
  function filterTable(tableId, query) {{
    const tbl = document.getElementById(tableId);
    const rows = tbl.querySelectorAll('tbody tr');
    const q = query.toLowerCase();
    let visible = 0;
    rows.forEach(row => {{
      const match = row.textContent.toLowerCase().includes(q);
      row.classList.toggle('hidden', !match);
      if (match) visible++;
    }});
    const countMap = {{ 'tbl-vinfo': 'count-vinfo', 'tbl-vdisk': 'count-vdisk', 'tbl-vnetwork': 'count-vnetwork' }};
    const countEl = document.getElementById(countMap[tableId]);
    if (countEl) countEl.textContent = visible + ' shown';
  }}

  // ── Sort ──────────────────────────────────────────────────────────────────
  const sortState = {{}};
  function sortTable(tableId, col) {{
    const tbl = document.getElementById(tableId);
    const key = tableId + ':' + col;
    const asc = sortState[key] !== true;
    sortState[key] = asc;
    const tbody = tbl.querySelector('tbody');
    const rows = Array.from(tbody.querySelectorAll('tr'));
    rows.sort((a, b) => {{
      const av = a.cells[col]?.textContent.trim() || '';
      const bv = b.cells[col]?.textContent.trim() || '';
      const an = parseFloat(av), bn = parseFloat(bv);
      if (!isNaN(an) && !isNaN(bn)) return asc ? an - bn : bn - an;
      return asc ? av.localeCompare(bv) : bv.localeCompare(av);
    }});
    rows.forEach(r => tbody.appendChild(r));
    tbl.querySelectorAll('th').forEach((th, i) => {{
      th.classList.remove('sort-asc', 'sort-desc');
      if (i === col) th.classList.add(asc ? 'sort-asc' : 'sort-desc');
    }});
  }}

  // ── CSV export from visible table ─────────────────────────────────────────
  function exportTableCSV(tableId, filename) {{
    const tbl = document.getElementById(tableId);
    const rows = [];
    const headers = Array.from(tbl.querySelectorAll('thead th')).map(th => th.textContent.trim().replace(/[↑↓]/g,'').trim());
    rows.push(headers.join(','));
    tbl.querySelectorAll('tbody tr:not(.hidden)').forEach(tr => {{
      const cells = Array.from(tr.cells).map(td => {{
        const v = td.textContent.trim().replace(/,/g, ';');
        return `"${{v}}"`;
      }});
      rows.push(cells.join(','));
    }});
    const blob = new Blob([rows.join('\\n')], {{ type: 'text/csv' }});
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = filename;
    a.click();
  }}
</script>
</body>
</html>"""


# ══════════════════════════════════════════════════════════════════════════════
# PDF report (landscape A4, light theme, all sections)
# ══════════════════════════════════════════════════════════════════════════════

def generate_pdf_html(records: list, cluster_name: str, namespace_filter: Optional[str], generated_at: str, logo_b64: Optional[str] = None, logo_mime: str = "image/png", rs_thresholds: Optional[dict] = None) -> str:
    """Generate a print-optimised HTML for PDF export (landscape, light, all tabs)."""
    total_vms  = len(records)
    running    = sum(1 for r in records if r["phase"] == "Running")
    paused     = sum(1 for r in records if r["phase"] == "Paused")
    stopped    = sum(1 for r in records if r["phase"] == "Stopped")
    total_vcpu  = sum(r["cpu_cores"] for r in records)
    total_ram   = round(sum(parse_memory_to_gib(r["mem_requested"]) for r in records), 1)
    total_disk  = round(sum(r["total_disk_gib"] for r in records), 1)
    error_vms   = sum(1 for r in records if r.get("error_state"))
    namespaces  = sorted(set(r["namespace"] for r in records))
    _rs = rs_thresholds or RS_DEFAULTS

    def fmt_gib(gib: float) -> str:
        if gib >= 1024:
            return f'{gib/1024:.1f}<span class="stat-unit">TiB</span>'
        return f'{gib}<span class="stat-unit">GiB</span>'

    logo_html = (
        f'<img src="data:{logo_mime};base64,{logo_b64}" style="height:28px;width:auto;max-width:120px;" alt="logo" />'
        if logo_b64 else '<div class="logo-box">OVR</div>'
    )

    def badge(phase: str) -> str:
        colors = {
            "Running":    "#16a34a", "Stopped":  "#6b7280",
            "Paused":     "#d97706", "Pending":  "#2563eb",
            "Scheduling": "#7c3aed", "Failed":   "#dc2626",
        }
        c = colors.get(phase, "#6b7280")
        return (f'<span style="background:{c};color:#fff;padding:1px 6px;'
                f'border-radius:3px;font-size:8px;font-weight:700;'
                f'text-transform:uppercase;white-space:nowrap">{phase}</span>')

    def vm_badge(r: dict) -> str:
        """PDF phase badge — shows Error when error_state is set."""
        error_state = r.get("error_state", "")
        if error_state:
            label = "Error" if error_state in ("ErrorUnschedulable", "Unschedulable") else error_state
            return (f'<span style="background:#dc2626;color:#fff;padding:1px 6px;'
                    f'border-radius:3px;font-size:8px;font-weight:700;'
                    f'text-transform:uppercase;white-space:nowrap" title="{error_state}">{label}</span>')
        return badge(r["phase"])

    def cell(v) -> str:
        return "—" if v in (None, "", "—", 0, 0.0) else str(v)

    # ── vInfo rows ────────────────────────────────────────────────────────────
    vinfo_rows = ""
    for r in records:
        vinfo_rows += (
            f"<tr><td>{r['namespace']}</td><td><b>{r['name']}</b></td>"
            f"<td>{vm_badge(r)}</td>"
            f"<td>{cell(r['node'])}</td>"
            f"<td style='text-align:right'>{r['cpu_cores']}</td>"
            f"<td style='text-align:right'>{cell(r['cpu_used_cores'])}</td>"
            f"<td>{cell(r['mem_requested'])}</td>"
            f"<td style='text-align:right'>{cell(r['mem_used_gib'])} GiB</td>"
            f"<td style='text-align:right'>{r['total_disk_gib']} GiB</td>"
            f"<td>{rs_badge(r.get('cpu_util_pct'), r.get('mem_util_pct'), _rs)}</td>"
            f"<td>{cell(r['ip_addresses'])}</td>"
            f"<td>{cell(r['os_name'])}</td>"
            f"<td>{r['created'][:10] if r['created'] else '—'}</td></tr>"
        )

    # ── vDisk rows ────────────────────────────────────────────────────────────
    vdisk_rows = ""
    for r in records:
        for d in r["disks"]:
            vdisk_rows += (
                f"<tr><td>{r['namespace']}</td><td><b>{r['name']}</b></td>"
                f"<td>{d['name']}</td><td>{d['type']}</td>"
                f"<td style='text-align:right'>{cell(d['size'])}</td>"
                f"<td>{badge(d['phase']) if d['phase'] not in ('N/A','?','—') else cell(d['phase'])}</td>"
                f"<td>{cell(d['storageClass'])}</td>"
                f"<td>{cell(r['disk_read_bps'])}</td><td>{cell(r['disk_write_bps'])}</td>"
                f"<td style='text-align:right'>{cell(r['disk_read_iops'])}</td>"
                f"<td style='text-align:right'>{cell(r['disk_write_iops'])}</td></tr>"
            )

    # ── vNetwork rows ─────────────────────────────────────────────────────────
    vnet_rows = ""
    for r in records:
        for n in r["nics"]:
            vnet_rows += (
                f"<tr><td>{r['namespace']}</td><td><b>{r['name']}</b></td>"
                f"<td>{n['name']}</td><td>{n['model']}</td>"
                f"<td style='font-family:monospace'>{cell(n['mac'])}</td>"
                f"<td>{n['binding']}</td><td>{cell(n['network'])}</td>"
                f"<td>{cell(r['ip_addresses'])}</td>"
                f"<td>{cell(r['net_rx_bps'])}</td><td>{cell(r['net_tx_bps'])}</td></tr>"
            )

    ns_filter_line = f"Namespace filter: <b>{namespace_filter}</b> &nbsp;|&nbsp;" if namespace_filter else ""

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8"/>
  <style>
    @page {{
      size: A4 landscape;
      margin: 0.4cm 0.5cm;
    }}
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{ font-family: Helvetica, Arial, sans-serif; font-size: 8pt; color: #1a1a1a; background: #fff; }}

    /* ── Header ── */
    .header {{
      display: flex; align-items: center; gap: 10px;
      border-bottom: 2px solid #ee0000; padding-bottom: 5px; margin-bottom: 6px;
    }}
    .logo-box {{
      width: 30px; height: 24px; background: #ee0000; border-radius: 3px;
      color: #fff; font-weight: 700; font-size: 8px; display: flex;
      align-items: center; justify-content: center;
    }}
    .header-title {{ font-size: 11pt; font-weight: 700; color: #111; }}
    .header-sub   {{ font-size: 6pt; color: #666; text-transform: uppercase; letter-spacing: 0.8px; }}
    .header-meta  {{ margin-left: auto; text-align: right; font-size: 7pt; color: #444; line-height: 1.4; }}

    /* ── Summary ── */
    .summary {{
      display: flex; gap: 16px; margin-bottom: 6px; align-items: center;
      background: #f5f5f5; border: 1px solid #ddd;
      border-radius: 3px; padding: 4px 10px;
    }}
    .stat {{ text-align: center; }}
    .stat-value {{ font-size: 13pt; font-weight: 700; line-height: 1.1; }}
    .stat-label {{ font-size: 6pt; color: #666; text-transform: uppercase; letter-spacing: 0.5px; }}
    .green {{ color: #16a34a; }} .red {{ color: #dc2626; }} .orange {{ color: #f59e0b; }} .blue {{ color: #2563eb; }} .purple {{ color: #a855f7; }} .cyan {{ color: #0891b2; }} .gray {{ color: #9ca3af; }}
    .stat-unit {{ font-size: 0.55em; color: #aaa; font-weight: 400; margin-left: 2px; }}
    .rs-ok, .rs-over, .rs-partover, .rs-under, .rs-nodata {{
      display: inline-block; padding: 1px 4px; border-radius: 4px; font-size: 6.5pt; font-weight: 700;
    }}
    .rs-ok      {{ background: #dcfce7; color: #15803d; }}
    .rs-over    {{ background: #fef9c3; color: #854d0e; }}
    .rs-partover{{ background: #ffedd5; color: #9a3412; }}
    .rs-under   {{ background: #fee2e2; color: #b91c1c; }}
    .rs-nodata  {{ color: #aaa; }}
    .divider {{ width: 1px; background: #ccc; align-self: stretch; }}

    /* ── Section headings ── */
    .section {{ margin-bottom: 8px; }}
    .section-heading {{
      background: #ee0000; color: #fff; font-size: 7.5pt; font-weight: 700;
      padding: 2px 6px; border-radius: 2px 2px 0 0;
      text-transform: uppercase; letter-spacing: 0.4px;
      display: flex; align-items: center; gap: 6px;
    }}
    .section-count {{
      background: rgba(255,255,255,0.25); padding: 0 4px;
      border-radius: 2px; font-size: 7pt;
    }}

    /* ── Tables ── */
    table {{ width: 100%; border-collapse: collapse; font-size: 7.5pt; table-layout: fixed; }}
    thead tr {{ background: #efefef; }}
    th {{
      padding: 3px 5px; text-align: left; font-size: 6.5pt; font-weight: 700;
      text-transform: uppercase; letter-spacing: 0.4px; color: #444;
      border: 1px solid #d8d8d8; white-space: nowrap; overflow: hidden;
    }}
    td {{
      padding: 2px 5px; border: 1px solid #e5e5e5;
      color: #1a1a1a; vertical-align: middle;
      overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
    }}
    tr:nth-child(even) td {{ background: #f9f9f9; }}
    .page-break {{ page-break-before: always; }}
    .footer {{ font-size: 6.5pt; color: #999; text-align: center; margin-top: 5px; }}
  </style>
</head>
<body>

<div class="header">
  {logo_html}
  <div>
    <div class="header-title">ovr</div>
    <div class="header-sub">OpenShift Virtualization Reporter</div>
  </div>
  <div class="header-meta">
    <b>{cluster_name}</b> &nbsp;|&nbsp; {ns_filter_line}Generated {generated_at} &nbsp;|&nbsp; v{TOOL_VERSION}
  </div>
</div>

<div class="summary">
  <div class="stat"><div class="stat-value">{total_vms}</div><div class="stat-label">Total VMs</div></div>
  <div class="divider"></div>
  <div class="stat"><div class="stat-value green">{running}</div><div class="stat-label">Running</div></div>
  <div class="stat"><div class="stat-value" style="color:#ca8a04">{paused}</div><div class="stat-label">Paused</div></div>
  <div class="stat"><div class="stat-value gray">{stopped}</div><div class="stat-label">Stopped</div></div>
  <div class="stat">
    <div class="stat-value {'red' if error_vms else 'green'}">{error_vms if error_vms else "✓"}</div>
    <div class="stat-label">Errors</div>
  </div>
  <div class="divider"></div>
  <div class="stat"><div class="stat-value blue">{total_vcpu}</div><div class="stat-label">vCPUs</div></div>
  <div class="stat"><div class="stat-value purple">{fmt_gib(total_ram)}</div><div class="stat-label">Total RAM</div></div>
  <div class="stat"><div class="stat-value cyan">{fmt_gib(total_disk)}</div><div class="stat-label">Total Disk</div></div>
  <div class="divider"></div>
  <div class="stat"><div class="stat-value">{len(namespaces)}</div><div class="stat-label">Namespaces</div></div>
  <div class="divider"></div>
  <div style="font-size:7pt;color:#555">{"&nbsp;&nbsp;".join(namespaces)}</div>
</div>

<!-- vInfo -->
<div class="section">
  <div class="section-heading">vInfo <span class="section-count">{total_vms}</span></div>
  <table>
    <colgroup>
      <col style="width:8%"><col style="width:9%"><col style="width:6%"><col style="width:8%">
      <col style="width:4%"><col style="width:6%"><col style="width:6%"><col style="width:6%">
      <col style="width:6%"><col style="width:9%"><col style="width:8%"><col style="width:12%"><col style="width:7%">
    </colgroup>
    <thead><tr>
      <th>Namespace</th><th>VM Name</th><th>Status</th><th>Node</th>
      <th>vCPUs</th><th>CPU Used ●</th><th>Mem Alloc</th><th>Mem Used ●</th>
      <th>Disk (GiB)</th><th>Sizing</th><th>IP Address</th><th>Guest OS</th><th>Created</th>
    </tr></thead>
    <tbody>{vinfo_rows}</tbody>
  </table>
</div>

<!-- vDisk -->
<div class="section page-break">
  <div class="section-heading">vDisk <span class="section-count">{sum(len(r['disks']) for r in records)}</span></div>
  <table>
    <colgroup>
      <col style="width:9%"><col style="width:11%"><col style="width:11%"><col style="width:9%">
      <col style="width:6%"><col style="width:7%"><col style="width:15%">
      <col style="width:8%"><col style="width:8%"><col style="width:8%"><col style="width:8%">
    </colgroup>
    <thead><tr>
      <th>Namespace</th><th>VM Name</th><th>Disk Name</th><th>Type</th>
      <th>Size</th><th>Phase</th><th>Storage Class</th>
      <th>Read ●</th><th>Write ●</th><th>Read IOPS ●</th><th>Write IOPS ●</th>
    </tr></thead>
    <tbody>{vdisk_rows}</tbody>
  </table>
</div>

<!-- vNetwork -->
<div class="section page-break">
  <div class="section-heading">vNetwork <span class="section-count">{sum(len(r['nics']) for r in records)}</span></div>
  <table>
    <colgroup>
      <col style="width:9%"><col style="width:11%"><col style="width:9%"><col style="width:7%">
      <col style="width:12%"><col style="width:8%"><col style="width:12%">
      <col style="width:11%"><col style="width:10%"><col style="width:11%">
    </colgroup>
    <thead><tr>
      <th>Namespace</th><th>VM Name</th><th>NIC Name</th><th>Model</th>
      <th>MAC Address</th><th>Binding</th><th>Network</th>
      <th>IP Address</th><th>RX ●</th><th>TX ●</th>
    </tr></thead>
    <tbody>{vnet_rows}</tbody>
  </table>
</div>

<p class="footer">● = live Prometheus metrics (30-minute average) &nbsp;|&nbsp; ovr v{TOOL_VERSION}</p>

</body>
</html>"""


# ══════════════════════════════════════════════════════════════════════════════
# Main
# ══════════════════════════════════════════════════════════════════════════════

def get_cluster_name() -> str:
    result = subprocess.run(
        ["oc", "config", "current-context"],
        capture_output=True, text=True
    )
    return result.stdout.strip() or "unknown-cluster"


def main():
    parser = argparse.ArgumentParser(
        description="ovr — RVTools-equivalent for OpenShift Virtualization",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Report all namespaces
  python3 ovr.py

  # Report a specific namespace
  python3 ovr.py -n my-vms

  # Output to a custom directory
  python3 ovr.py -o /tmp/reports

  # Export PDF report
  python3 ovr.py --pdf

  # Export PDF with a custom logo
  python3 ovr.py --pdf --logo /path/to/logo.png

  # Skip Prometheus metrics (config/inventory only)
  python3 ovr.py --no-metrics

  # Use a custom Prometheus URL (e.g., port-forwarded)
  python3 ovr.py --prom-url localhost:9090
        """
    )
    parser.add_argument("-n", "--namespace", help="Namespace to report on (default: all namespaces)")
    parser.add_argument("-o", "--output", default=".", help="Output directory for report files (default: current dir)")
    parser.add_argument("--no-metrics", action="store_true", help="Skip Prometheus metrics collection (faster, config-only)")
    parser.add_argument("--prom-url", help="Override Prometheus URL (host:port, no scheme)")
    parser.add_argument("--no-html", action="store_true", help="Skip HTML report generation")
    parser.add_argument("--no-csv", action="store_true", help="Skip CSV export")
    parser.add_argument("--pdf", action="store_true", help="Export a PDF report (requires: pip install weasyprint)")
    parser.add_argument("--logo", help="Path to a custom logo image (PNG/JPEG/SVG) embedded in the report header")
    parser.add_argument("--show-cloudinit", action="store_true", help="Include CloudInit config drives in the vDisk report (hidden by default)")
    parser.add_argument("--rs-cpu-low",  type=float, default=20.0, metavar="PCT",
                        help="CPU utilisation %% below which a VM is flagged as over-provisioned (default: 20)")
    parser.add_argument("--rs-cpu-high", type=float, default=80.0, metavar="PCT",
                        help="CPU utilisation %% above which a VM is flagged as under-provisioned (default: 80)")
    parser.add_argument("--rs-mem-low",  type=float, default=20.0, metavar="PCT",
                        help="Memory utilisation %% below which a VM is flagged as over-provisioned (default: 20)")
    parser.add_argument("--rs-mem-high", type=float, default=80.0, metavar="PCT",
                        help="Memory utilisation %% above which a VM is flagged as under-provisioned (default: 80)")
    parser.add_argument("--version", action="version", version=f"ovr {TOOL_VERSION}")
    args = parser.parse_args()

    print(f"\n  ovr v{TOOL_VERSION}")
    print("  ══════════════════════════════════")

    # Verify oc login
    result = subprocess.run(["oc", "whoami"], capture_output=True, text=True)
    if result.returncode != 0:
        print("✗  Not logged in. Run: oc login <cluster-api-url>", file=sys.stderr)
        sys.exit(1)
    print(f"  ✓ Logged in as: {result.stdout.strip()}")

    cluster_name = get_cluster_name()
    print(f"  ✓ Cluster context: {cluster_name}")

    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)
    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")

    # ── Collect inventory ─────────────────────────────────────────────────────
    print("\n[1/3] Collecting inventory...")
    vms = collect_vms(args.namespace)
    vmis = collect_vmis(args.namespace)
    pvcs = collect_pvcs(args.namespace)
    print(f"  ✓ Found {len(vms)} VirtualMachines, {len(vmis)} running VMIs, {len(pvcs)} PVCs")

    if not vms:
        print("\n  No VirtualMachines found. Check namespace or permissions.")
        sys.exit(0)

    # ── Collect metrics ───────────────────────────────────────────────────────
    metrics = {}
    if not args.no_metrics:
        print("\n[2/3] Collecting Prometheus metrics...")
        token = get_oc_token()
        prom_url = args.prom_url or get_prometheus_route()
        print(f"  → Prometheus: {prom_url}")
        metrics = collect_metrics(prom_url, token)
    else:
        print("\n[2/3] Skipping Prometheus metrics (--no-metrics)")

    # ── Build records ─────────────────────────────────────────────────────────
    print("\n[3/3] Building report...")
    records = [parse_vm(vm, vmis, pvcs, metrics, include_cloudinit=args.show_cloudinit) for vm in vms]
    records.sort(key=lambda r: (r["namespace"], r["name"]))
    print(f"  ✓ Processed {len(records)} VM records")

    # ── Load custom logo ──────────────────────────────────────────────────────
    logo_b64, logo_mime = None, "image/png"
    if args.logo:
        logo_path = Path(args.logo)
        if not logo_path.exists():
            print(f"  ✗ Logo file not found: {args.logo}", file=sys.stderr)
            sys.exit(1)
        ext = logo_path.suffix.lower().lstrip(".")
        logo_mime = {"svg": "image/svg+xml", "jpg": "image/jpeg", "jpeg": "image/jpeg",
                     "gif": "image/gif", "webp": "image/webp"}.get(ext, "image/png")
        logo_b64 = base64.b64encode(logo_path.read_bytes()).decode()
        print(f"  ✓ Logo loaded: {args.logo}")

    # ── Right-sizing thresholds ───────────────────────────────────────────────
    rs_thresholds = {
        "cpu_low":  args.rs_cpu_low,
        "cpu_high": args.rs_cpu_high,
        "mem_low":  args.rs_mem_low,
        "mem_high": args.rs_mem_high,
    }

    # ── Export ────────────────────────────────────────────────────────────────
    html = generate_html(records, cluster_name, args.namespace, generated_at, logo_b64, logo_mime, rs_thresholds)

    if not args.no_html:
        html_path = output_dir / f"ocpv-report-{ts}.html"
        html_path.write_text(html)
        print(f"  ✓ HTML report: {html_path}")

    if args.pdf:
        try:
            from weasyprint import HTML as WeasyprintHTML
            pdf_path = output_dir / f"ocpv-report-{ts}.pdf"
            pdf_html = generate_pdf_html(records, cluster_name, args.namespace, generated_at, logo_b64, logo_mime, rs_thresholds)
            with open(pdf_path, "wb") as pdf_fh:
                WeasyprintHTML(string=pdf_html, base_url=str(output_dir.resolve())).write_pdf(pdf_fh)
            print(f"  ✓ PDF report:  {pdf_path}")
        except ImportError:
            print("  ✗ PDF export requires weasyprint: pip install weasyprint", file=sys.stderr)
        except Exception as e:
            print(f"  ✗ PDF export failed: {e}", file=sys.stderr)

    if not args.no_csv:
        export_csv(records, str(output_dir / f"ocpv-vinfo-{ts}.csv"))
        export_disk_csv(records, str(output_dir / f"ocpv-vdisk-{ts}.csv"))
        export_nic_csv(records, str(output_dir / f"ocpv-vnetwork-{ts}.csv"))

    print(f"\n  ✓ Done. {len(records)} VMs reported.\n")


if __name__ == "__main__":
    main()
