"""Predeclared gates. Elapsed time alone never constitutes acceptance."""

import statistics

REQUIRED = (
    "native_media_uploads",
    "takeover_checks",
    "lost_ack_reconciliations",
    "controlled_model_wait_checks",
    "profile_reopens",
    "isolated_fault_checks",
)
THRESHOLDS = {
    "minimum_observed_seconds": 72 * 3600,
    "minimum_rounds": 12,
    "minimum_idle_samples_after_warmup": 12,
    "warmup_seconds": 1800,
    "api_p95_ms": 500,
    "idle_rss_growth_bytes": 256 * 1024**2,
    "idle_fd_growth": 64,
    "minimum_free_bytes": 512 * 1024**2,
    "task_deadline_seconds": 180,
}


def percentile(values, p=0.95):
    return (
        sorted(values)[max(0, min(len(values) - 1, int(len(values) * p)))]
        if values
        else None
    )


def assess(report, samples):
    failures = []
    if report.get("workload_version") != "mixed-v2":
        failures.append("workload_version")
    for name in REQUIRED:
        if not report.get(name):
            failures.append("missing_" + name)
    if len(report.get("mixed_contracts", {})) != 21:
        failures.append("media_format_coverage")
    if report.get("rounds", 0) < THRESHOLDS["minimum_rounds"]:
        failures.append("round_count")
    if report.get("observed_seconds", 0) < THRESHOLDS["minimum_observed_seconds"]:
        failures.append("duration")
    if report.get("observation_gaps", 0):
        failures.append("observation_gaps")
    if not report.get("last_overlap"):
        failures.append("parallel_overlap")
    windows = report.get("native_upload_overlap", [])
    if len(windows) != 3 or max(s for s, e in windows) >= min(e for s, e in windows):
        failures.append("native_upload_overlap")
    if report.get("source_hash_end") != report.get(
        "source_hash_start"
    ) or not report.get("source_hash_start"):
        failures.append("code_changed")
    usable = [
        s
        for s in samples
        if s.get("phase") == "idle"
        and s.get("elapsed_seconds", 0) >= THRESHOLDS["warmup_seconds"]
    ]
    rss_growth = fd_growth = None
    if len(usable) < THRESHOLDS["minimum_idle_samples_after_warmup"]:
        failures.append("resource_sample_count")
    else:
        window = max(3, len(usable) // 5)
        rss_growth = statistics.median(
            s["rss_bytes"] for s in usable[-window:]
        ) - statistics.median(s["rss_bytes"] for s in usable[:window])
        fd_growth = statistics.median(
            s["file_descriptors"] for s in usable[-window:]
        ) - statistics.median(s["file_descriptors"] for s in usable[:window])
        if rss_growth > THRESHOLDS["idle_rss_growth_bytes"]:
            failures.append("rss_growth")
        if fd_growth > THRESHOLDS["idle_fd_growth"]:
            failures.append("fd_growth")
    latency = percentile([s["api_ms"] for s in samples if s.get("api_ms") is not None])
    if latency is None or latency >= THRESHOLDS["api_p95_ms"]:
        failures.append("api_latency")
    if any(s.get("api_error") for s in samples):
        failures.append("api_availability")
    if (
        samples
        and min(s["disk_free_bytes"] for s in samples)
        < THRESHOLDS["minimum_free_bytes"]
    ):
        failures.append("disk_capacity")
    return {
        "accepted": not failures,
        "failures": failures,
        "api_probe": "authenticated_internal_status",
        "api_p95_ms": latency,
        "idle_rss_growth_bytes": rss_growth,
        "idle_fd_growth": fd_growth,
        "thresholds": THRESHOLDS,
        "external_social_acceptance": False,
        "real_provider_soak": False,
    }
