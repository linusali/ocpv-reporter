# ovr

**RVTools for OpenShift Virtualization.**

A lightweight, zero-dependency\* CLI tool that collects VM inventory and real consumption data from an OpenShift Virtualization cluster and produces a beautiful, filterable HTML report + CSV exports — no ACM, no Grafana, no extra operators required.

> \* Requires only `oc` CLI and Python 3.6+. An optional `kubernetes` Python client is supported but not required.

---

## What it looks like

The generated HTML report has three tabs, mirroring the RVTools experience:

| Tab | What's in it |
|-----|-------------|
| **vInfo** | VM name, namespace, status, node, vCPUs, allocated memory, **actual CPU/memory usage**, right-sizing recommendation, IP, OS, creation date |
| **vDisk** | All disks per VM — type (DataVolume, PVC, ContainerDisk), size, storage class, PVC phase, **live read/write throughput and IOPS** |
| **vNetwork** | All NICs per VM — model, MAC address, binding type (masquerade/bridge/SR-IOV), network attachment, **live RX/TX throughput** |

Every column is **sortable**. Every table has a **live filter** search box. Every tab has a **one-click CSV export** of visible rows.

Metrics marked with **●** are live 30-minute averages pulled from the in-cluster Prometheus/Thanos endpoint.

The summary bar at the top shows Total VMs, Running/Stopped/Error counts, total vCPUs, RAM, and Disk allocated across the cluster.

---

## Requirements

- Python **3.6+**
- `oc` CLI, logged in to the target cluster (`oc login ...`)
- `cluster-admin` role **or** `view` on VirtualMachine, VirtualMachineInstance, PVC resources + `cluster-monitoring-view` for Prometheus access

Optional:
```bash
pip install weasyprint   # required for --pdf export
```

---

## Quick Start

```bash
# Clone
git clone https://github.com/linusali/ocpv-reporter.git
cd ocpv-reporter

# Log in to your cluster
oc login https://api.your-cluster.example.com:6443

# Run (all namespaces)
python3 ovr.py

# Run for a specific namespace
python3 ovr.py -n my-vm-namespace

# Output to a specific directory
python3 ovr.py -o /tmp/reports

# Export a PDF report (requires: pip install weasyprint)
python3 ovr.py --pdf

# PDF with a custom logo in the header
python3 ovr.py --pdf --logo /path/to/company-logo.png

# Skip Prometheus metrics (faster, inventory/config only)
python3 ovr.py --no-metrics

# Include CloudInit config drives in the vDisk tab (hidden by default)
python3 ovr.py --show-cloudinit
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
usage: ovr.py [-h] [-n NAMESPACE] [-o OUTPUT] [--no-metrics]
                        [--prom-url PROM_URL] [--no-html] [--no-csv]
                        [--pdf] [--logo LOGO] [--show-cloudinit]
                        [--rs-cpu-low PCT] [--rs-cpu-high PCT]
                        [--rs-mem-low PCT] [--rs-mem-high PCT] [--version]

options:
  -n, --namespace     Namespace to report on (default: all namespaces)
  -o, --output        Output directory (default: current directory)
  --no-metrics        Skip Prometheus metrics — inventory/config only (faster)
  --prom-url          Override Prometheus URL, e.g. localhost:9090 for port-forwarding
  --no-html           Skip HTML report
  --no-csv            Skip CSV exports
  --pdf               Export a PDF report (requires: pip install weasyprint)
  --logo PATH         Path to a custom logo image (PNG/JPEG/SVG/GIF/WebP)
                      embedded inline in the HTML and PDF report header
                      (see Logo Guidelines below)
  --show-cloudinit    Include CloudInit config drives in the vDisk report
                      (hidden by default — they carry no real storage)
  --rs-cpu-low PCT    CPU utilisation % below which a VM is flagged as
                      over-provisioned (default: 20)
  --rs-cpu-high PCT   CPU utilisation % above which a VM is flagged as
                      under-provisioned (default: 80)
  --rs-mem-low PCT    Memory utilisation % below which a VM is flagged as
                      over-provisioned (default: 20)
  --rs-mem-high PCT   Memory utilisation % above which a VM is flagged as
                      under-provisioned (default: 80)
  --version           Show version
```

---

## Right-Sizing

When Prometheus metrics are available, the **Sizing** column in the vInfo tab shows a colour-coded recommendation for each running VM based on its current CPU and memory utilisation:

| Badge | Meaning |
|-------|---------|
| **Right-sized** (green) | CPU and memory utilisation both within thresholds |
| **Over-provisioned** (yellow) | Both CPU and memory below the low threshold |
| **Partially over** (orange) | One resource below the low threshold, the other in range |
| **Under-provisioned** (red) | CPU or memory above the high threshold |
| **—** | VM is stopped, or no Prometheus data available |

The default thresholds are **20 % (low) / 80 % (high)**. Override them with `--rs-cpu-low`, `--rs-cpu-high`, `--rs-mem-low`, `--rs-mem-high`.

> **Note:** This is a point-in-time snapshot based on the 30-minute Prometheus average at report generation time. For production right-sizing decisions, compare against peak/average data over a longer window (7 d, 30 d).

The CSV export includes `cpu_util_pct`, `mem_util_pct`, and `right_sizing` columns for further analysis in spreadsheet tools.

---

## Logo Guidelines

A custom logo is embedded inline (base64) in both the HTML and PDF reports, so the output files remain fully self-contained.

| Property | HTML report | PDF report |
|----------|-------------|------------|
| Display height | 36 px | 28 px |
| Max display width | 160 px | 120 px |
| Aspect ratio | Preserved (width: auto) | Preserved (width: auto) |

**Best practices:**

| Format | Recommendation |
|--------|---------------|
| **SVG** | Preferred — scales perfectly at any resolution, smallest file size |
| **PNG** | Use a source at least 72 px tall (2× display height for HiDPI/Retina) |
| **JPEG** | Avoid for logos — lossy compression causes artefacts on text/edges |

- **Landscape/wide logos** (company wordmark, e.g. 4:1 ratio) work best — they sit naturally next to the report title without taking up vertical space
- **Square icons** work well too — they will be scaled to the display height
- Keep the background **transparent** (SVG or PNG with alpha) so the logo blends with both the dark HTML header and the white PDF header
- Avoid excessively wide images (> 400 px source width) — they are capped at the max-width above anyway

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
python3 ovr.py --prom-url localhost:9091
```

---

## RBAC — Minimum Permissions

If you don't want to use `cluster-admin`, create a dedicated role:

```yaml
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata:
  name: ovr
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
oc apply -f deploy/ovr-clusterrole.yaml
oc adm policy add-cluster-role-to-user ovr <your-user>
oc adm policy add-cluster-role-to-user cluster-monitoring-view <your-user>
```

---

## Running in a Container (no local Python needed)

A pre-built image is published to the GitHub Container Registry on every push to `main` and on every version tag.

```bash
# Pull the latest image
docker pull ghcr.io/linusali/ocpv-reporter:latest

# Run — mount your kubeconfig and an output directory
docker run --rm \
  -v ~/.kube:/root/.kube:ro \
  -v $(pwd)/reports:/output \
  ghcr.io/linusali/ocpv-reporter:latest

# Specific namespace, skip metrics
docker run --rm \
  -v ~/.kube:/root/.kube:ro \
  -v $(pwd)/reports:/output \
  ghcr.io/linusali/ocpv-reporter:latest \
  -n my-vm-namespace --no-metrics

# Export PDF as well
docker run --rm \
  -v ~/.kube:/root/.kube:ro \
  -v $(pwd)/reports:/output \
  ghcr.io/linusali/ocpv-reporter:latest \
  --pdf
```

The image includes `weasyprint` so `--pdf` works out of the box. All report files are written to `/output` inside the container — mount a host directory there to retrieve them.

### Available tags

| Tag | Description |
|-----|-------------|
| `latest` | Latest build from `main` |
| `1.2.3` | Specific release version |
| `sha-abc1234` | Exact commit build |

### Build locally

```bash
docker build -t ovr .
docker run --rm \
  -v ~/.kube:/root/.kube:ro \
  -v $(pwd)/reports:/output \
  ovr
```

---

## Roadmap

- [x] Container image (ghcr.io/linusali/ocpv-reporter)
- [ ] `--format json` output for pipeline integration
- [ ] Instance type / preference reporting (KubeVirt InstanceTypes)
- [ ] Snapshot inventory tab (vSnapshot)
- [ ] Live migration history tab
- [ ] Helm chart / CronJob for scheduled reporting
- [x] VM right-sizing recommendations (based on Prometheus data)
- [ ] Right-sizing using Prometheus range queries (historical peak/average)
- [ ] Multi-cluster report aggregation

---

## Contributing

PRs and issues welcome. Please open an issue before large changes.

```bash
git clone https://github.com/linusali/ocpv-reporter.git
cd ocpv-reporter
python3 -m pytest tests/
```

---

## License

Apache 2.0 — see [LICENSE](LICENSE).

---

## Acknowledgements

Inspired by [RVTools](https://www.robware.net/rvtools/) by Robware.  
Built on [KubeVirt](https://kubevirt.io/) metrics and the OpenShift Virtualization operator.  
Prometheus queries reference: [KubeVirt monitoring docs](https://github.com/kubevirt/monitoring).
