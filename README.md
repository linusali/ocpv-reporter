# ocpv-reporter

**RVTools for OpenShift Virtualization.**

A lightweight, zero-dependency\* CLI tool that collects VM inventory and real consumption data from an OpenShift Virtualization cluster and produces a beautiful, filterable HTML report + CSV exports — no ACM, no Grafana, no extra operators required.

> \* Requires only `oc` CLI and Python 3.6+. An optional `kubernetes` Python client is supported but not required.

---

## What it looks like

The generated HTML report has three tabs, mirroring the RVTools experience:

| Tab | What's in it |
|-----|-------------|
| **vInfo** | VM name, namespace, status, node, vCPUs, allocated memory, **actual CPU/memory usage**, IP, OS, creation date |
| **vDisk** | All disks per VM — type (DataVolume, PVC, ContainerDisk), size, storage class, PVC phase, **live read/write throughput and IOPS** |
| **vNetwork** | All NICs per VM — model, MAC address, binding type (masquerade/bridge/SR-IOV), network attachment, **live RX/TX throughput** |

Every column is **sortable**. Every table has a **live filter** search box. Every tab has a **one-click CSV export** of visible rows.

Metrics marked with **●** are live 30-minute averages pulled from the in-cluster Prometheus/Thanos endpoint.

---

## Requirements

- Python **3.6+**
- `oc` CLI, logged in to the target cluster (`oc login ...`)
- `cluster-admin` role **or** `view` on VirtualMachine, VirtualMachineInstance, PVC resources + `cluster-monitoring-view` for Prometheus access

Optional:
```bash
pip install kubernetes   # enables direct k8s API access (faster for large clusters)
pip install weasyprint   # required for --pdf export
```

---

## Quick Start

```bash
# Clone
git clone https://github.com/ocpv-reporter/ocpv-reporter.git
cd ocpv-reporter

# Log in to your cluster
oc login https://api.your-cluster.example.com:6443

# Run (all namespaces)
python3 ocpv_reporter.py

# Run for a specific namespace
python3 ocpv_reporter.py -n my-vm-namespace

# Output to a specific directory
python3 ocpv_reporter.py -o /tmp/reports

# Export a PDF report (requires: pip install weasyprint)
python3 ocpv_reporter.py --pdf

# PDF with a custom logo in the header
python3 ocpv_reporter.py --pdf --logo /path/to/company-logo.png

# Skip Prometheus metrics (faster, inventory/config only)
python3 ocpv_reporter.py --no-metrics
```

The report files are created in the current directory (or `-o`):

```
ocpv-report-20250403-143022.html     ← Open in browser
ocpv-report-20250403-143022.pdf      ← Generated with --pdf
ocpv-vinfo-20250403-143022.csv
ocpv-vdisk-20250403-143022.csv
ocpv-vnetwork-20250403-143022.csv
```

---

## All Options

```
usage: ocpv_reporter.py [-h] [-n NAMESPACE] [-o OUTPUT] [--no-metrics]
                        [--prom-url PROM_URL] [--no-html] [--no-csv]
                        [--pdf] [--logo LOGO] [--version]

options:
  -n, --namespace   Namespace to report on (default: all namespaces)
  -o, --output      Output directory (default: current directory)
  --no-metrics      Skip Prometheus metrics — inventory/config only (faster)
  --prom-url        Override Prometheus URL, e.g. localhost:9090 for port-forwarding
  --no-html         Skip HTML report
  --no-csv          Skip CSV exports
  --pdf             Export a PDF report (requires: pip install weasyprint)
  --logo PATH       Path to a custom logo image (PNG/JPEG/SVG/GIF/WebP)
                    embedded inline in the HTML and PDF report header
  --version         Show version
```

---

## Data Sources

| Data | Source |
|------|--------|
| VM spec (vCPU, memory, run strategy) | `VirtualMachine` CR |
| Runtime state (phase, node, IP, guest OS) | `VirtualMachineInstance` CR |
| Disk definitions and sizes | `DataVolume` / `PVC` |
| **Actual CPU usage** | `kubevirt_vmi_cpu_usage_seconds_total` |
| **Actual memory used** | `kubevirt_vmi_memory_used_bytes` |
| **Disk throughput / IOPS** | `kubevirt_vmi_storage_*` |
| **Network throughput** | `kubevirt_vmi_network_*` |

All Prometheus metrics are 30-minute rate averages at the time of report generation.

---

## Prometheus Access

The tool auto-discovers the `prometheus-k8s` or `thanos-querier` route in the `openshift-monitoring` namespace. Your user needs the `cluster-monitoring-view` ClusterRole:

```bash
oc adm policy add-cluster-role-to-user cluster-monitoring-view <your-user>
```

If you're running in an environment where the route is not reachable directly (e.g., behind a jump host), use port-forwarding and `--prom-url`:

```bash
oc port-forward svc/thanos-querier 9091:9091 -n openshift-monitoring &
python3 ocpv_reporter.py --prom-url localhost:9091
```

---

## RBAC — Minimum Permissions

If you don't want to use `cluster-admin`, create a dedicated role:

```yaml
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata:
  name: ocpv-reporter
rules:
- apiGroups: ["kubevirt.io"]
  resources: ["virtualmachines", "virtualmachineinstances"]
  verbs: ["get", "list"]
- apiGroups: [""]
  resources: ["persistentvolumeclaims", "namespaces"]
  verbs: ["get", "list"]
- apiGroups: ["cdi.kubevirt.io"]
  resources: ["datavolumes"]
  verbs: ["get", "list"]
```

```bash
oc apply -f examples/ocpv-reporter-clusterrole.yaml
oc adm policy add-cluster-role-to-user ocpv-reporter <your-user>
oc adm policy add-cluster-role-to-user cluster-monitoring-view <your-user>
```

---

## Running in a Container (no local Python needed)

```bash
docker run --rm \
  -v ~/.kube:/root/.kube:ro \
  -v $(pwd)/reports:/reports \
  ghcr.io/ocpv-reporter/ocpv-reporter:latest \
  -o /reports
```

*(Container image coming soon — contributions welcome!)*

---

## Roadmap

- [ ] Container image (ghcr.io)
- [ ] `--format json` output for pipeline integration
- [ ] Instance type / preference reporting (KubeVirt InstanceTypes)
- [ ] Snapshot inventory tab (vSnapshot)
- [ ] Live migration history tab
- [ ] Helm chart / CronJob for scheduled reporting
- [ ] VM right-sizing recommendations (based on Prometheus data)
- [ ] Multi-cluster report aggregation

---

## Contributing

PRs and issues welcome. Please open an issue before large changes.

```bash
git clone https://github.com/ocpv-reporter/ocpv-reporter.git
cd ocpv-reporter
python3 -m pytest tests/   # run unit tests
```

---

## License

Apache 2.0 — see [LICENSE](LICENSE).

---

## Acknowledgements

Inspired by [RVTools](https://www.robware.net/rvtools/) by Robware.  
Built on [KubeVirt](https://kubevirt.io/) metrics and the OpenShift Virtualization operator.  
Prometheus queries reference: [KubeVirt monitoring docs](https://github.com/kubevirt/monitoring).
