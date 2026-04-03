"""
Unit tests for ocpv_reporter.py
These test the data parsing logic without needing a live cluster.
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from ocpv_reporter import bytes_to_gib, bytes_to_mib, format_bps, parse_vm, phase_badge, parse_memory_to_gib


# ── Utility tests ─────────────────────────────────────────────────────────────

def test_bytes_to_gib():
    assert bytes_to_gib(1024 ** 3) == 1.0
    assert bytes_to_gib(2 * 1024 ** 3) == 2.0
    assert bytes_to_gib(0) == 0.0


def test_bytes_to_mib():
    assert bytes_to_mib(1024 ** 2) == 1.0
    assert bytes_to_mib(512 * 1024 ** 2) == 512.0


def test_format_bps():
    assert "MB/s" in format_bps(5 * 1024 ** 2)
    assert "KB/s" in format_bps(512 * 1024)
    assert "B/s" in format_bps(100)


def test_phase_badge_running():
    badge = phase_badge("Running")
    assert "Running" in badge
    assert "#22c55e" in badge


def test_phase_badge_stopped():
    badge = phase_badge("Stopped")
    assert "Stopped" in badge


def test_phase_badge_unknown():
    badge = phase_badge("SomeUnknownPhase")
    assert "SomeUnknownPhase" in badge


# ── VM parsing tests ──────────────────────────────────────────────────────────

SAMPLE_VM = {
    "metadata": {
        "name": "test-vm",
        "namespace": "dev-vms",
        "creationTimestamp": "2025-01-15T10:00:00Z",
    },
    "spec": {
        "runStrategy": "Always",
        "template": {
            "spec": {
                "domain": {
                    "cpu": {"cores": 2, "sockets": 2, "threads": 1},
                    "resources": {
                        "requests": {"memory": "4Gi"},
                        "limits": {"memory": "4Gi"},
                    },
                    "devices": {
                        "interfaces": [
                            {"name": "default", "model": "virtio", "masquerade": {}, "macAddress": "02:00:00:aa:bb:cc"}
                        ]
                    }
                },
                "networks": [{"name": "default", "pod": {}}],
                "volumes": [
                    {"name": "rootdisk", "dataVolume": {"name": "test-vm-rootdisk"}},
                    {"name": "cloudinit", "cloudInitNoCloud": {"userData": "#cloud-config"}},
                ]
            }
        }
    }
}

SAMPLE_VMI = {
    "dev-vms/test-vm": {
        "status": {
            "phase": "Running",
            "nodeName": "worker-1.example.com",
            "interfaces": [{"name": "default", "ipAddress": "10.128.0.55"}],
            "guestOSInfo": {"prettyName": "Red Hat Enterprise Linux 9.4"},
            "machine": {"type": "pc-q35-rhel9.4.0"},
        }
    }
}

SAMPLE_PVCS = {
    ("dev-vms", "test-vm-rootdisk"): {
        "storage": "50Gi",
        "phase": "Bound",
        "storageClass": "ocs-storagecluster-ceph-rbd",
    }
}


def test_parse_vm_basic():
    vmis = {("dev-vms", "test-vm"): SAMPLE_VMI["dev-vms/test-vm"]}
    rec = parse_vm(SAMPLE_VM, vmis, SAMPLE_PVCS, {})

    assert rec["name"] == "test-vm"
    assert rec["namespace"] == "dev-vms"
    assert rec["cpu_cores"] == 4   # 2 cores × 2 sockets × 1 thread
    assert rec["mem_requested"] == "4Gi"
    assert rec["phase"] == "Running"
    assert rec["node"] == "worker-1.example.com"
    assert rec["ip_addresses"] == "10.128.0.55"
    assert rec["os_name"] == "Red Hat Enterprise Linux 9.4"
    assert rec["total_disk_gib"] == 50.0
    assert len(rec["disks"]) == 2   # dataVolume + cloudInit
    assert len(rec["nics"]) == 1


def test_parse_vm_no_vmi():
    """Stopped VM — no VMI present."""
    rec = parse_vm(SAMPLE_VM, {}, SAMPLE_PVCS, {})
    assert rec["phase"] == "Stopped"
    assert rec["node"] == "—"
    assert rec["ip_addresses"] == "—"


def test_parse_vm_metrics():
    metrics = {
        "cpu_usage": {("dev-vms", "test-vm"): 1.234},
        "mem_used": {("dev-vms", "test-vm"): 2 * 1024 ** 3},
        "mem_available": {},
        "disk_read_bps": {},
        "disk_write_bps": {},
        "net_rx_bps": {},
        "net_tx_bps": {},
        "disk_read_iops": {},
        "disk_write_iops": {},
    }
    vmis = {("dev-vms", "test-vm"): SAMPLE_VMI["dev-vms/test-vm"]}
    rec = parse_vm(SAMPLE_VM, vmis, SAMPLE_PVCS, metrics)
    assert rec["cpu_used_cores"] == 1.234
    assert rec["mem_used_gib"] == 2.0


def test_parse_vm_run_strategy():
    vm = {**SAMPLE_VM, "spec": {**SAMPLE_VM["spec"], "runStrategy": "Halted"}}
    rec = parse_vm(vm, {}, SAMPLE_PVCS, {})
    assert rec["configured_state"] == "Halted"


# ── Memory parsing tests ──────────────────────────────────────────────────────

def test_parse_memory_to_gib():
    assert parse_memory_to_gib("1Gi")   == 1.0
    assert parse_memory_to_gib("2Gi")   == 2.0
    assert parse_memory_to_gib("512Mi") == 0.5
    assert parse_memory_to_gib("1Ti")   == 1024.0
    assert parse_memory_to_gib("")      == 0.0
    assert parse_memory_to_gib("—")     == 0.0


# ── Error state detection tests ───────────────────────────────────────────────

def test_parse_vm_error_state_clean():
    """Healthy running VM — no error state."""
    vmis = {("dev-vms", "test-vm"): SAMPLE_VMI["dev-vms/test-vm"]}
    rec = parse_vm(SAMPLE_VM, vmis, SAMPLE_PVCS, {})
    assert rec["error_state"] == ""


def test_parse_vm_error_state_failed():
    """VMI in Failed phase → error_state == 'Failed'."""
    failed_vmi = {"status": {"phase": "Failed", "conditions": []}}
    vmis = {("dev-vms", "test-vm"): failed_vmi}
    rec = parse_vm(SAMPLE_VM, vmis, SAMPLE_PVCS, {})
    assert rec["error_state"] == "Failed"


def test_parse_vm_error_state_unschedulable():
    """VMI with ErrorUnschedulable condition → error_state == 'ErrorUnschedulable'."""
    unschedulable_vmi = {
        "status": {
            "phase": "Scheduling",
            "conditions": [
                {"type": "Ready", "status": "False", "reason": "ErrorUnschedulable"}
            ],
        }
    }
    vmis = {("dev-vms", "test-vm"): unschedulable_vmi}
    rec = parse_vm(SAMPLE_VM, vmis, SAMPLE_PVCS, {})
    assert rec["error_state"] == "ErrorUnschedulable"
