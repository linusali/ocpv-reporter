# ── Stage 1: download oc CLI ──────────────────────────────────────────────────
FROM python:3.11-slim AS oc-downloader

RUN apt-get update && apt-get install -y --no-install-recommends curl tar \
    && rm -rf /var/lib/apt/lists/*

# Download the latest stable OpenShift client and extract only the oc binary
RUN curl -sSL \
    https://mirror.openshift.com/pub/openshift-v4/clients/ocp/stable/openshift-client-linux.tar.gz \
    | tar -xz -C /usr/local/bin oc \
    && chmod +x /usr/local/bin/oc

# ── Stage 2: runtime ──────────────────────────────────────────────────────────
FROM python:3.11-slim

LABEL org.opencontainers.image.title="ovr" \
      org.opencontainers.image.description="RVTools-equivalent for OpenShift Virtualization" \
      org.opencontainers.image.source="https://github.com/linusali/ocpv-reporter" \
      org.opencontainers.image.licenses="Apache-2.0"

# weasyprint runtime dependencies (PDF export support)
RUN apt-get update && apt-get install -y --no-install-recommends \
      libpango-1.0-0 \
      libpangoft2-1.0-0 \
      libpangocairo-1.0-0 \
      libcairo2 \
      libgdk-pixbuf-2.0-0 \
      shared-mime-info \
      fonts-liberation \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir weasyprint

# Copy oc binary from the downloader stage
COPY --from=oc-downloader /usr/local/bin/oc /usr/local/bin/oc

WORKDIR /app
COPY ovr.py .

# /output  → mount a host directory here to receive generated reports
# /root/.kube → mount your kubeconfig here (or pass KUBECONFIG env var)
VOLUME ["/output", "/root/.kube"]

ENTRYPOINT ["python3", "/app/ovr.py"]
CMD ["-o", "/output"]
