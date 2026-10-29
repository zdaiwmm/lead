from scripts.soak_assessment import assess


def test_timer_and_partial_coverage_can_never_pass_endurance():
    result = assess({"observed_seconds": 259200, "rounds": 300}, [])
    assert not result["accepted"]
    assert (
        "workload_version" in result["failures"]
        and "media_format_coverage" in result["failures"]
    )


def test_resource_growth_and_latency_are_hard_failures():
    report = {
        "workload_version": "mixed-v2",
        "native_media_uploads": 21,
        "takeover_checks": 12,
        "lost_ack_reconciliations": 12,
        "controlled_model_wait_checks": 12,
        "profile_reopens": 12,
        "isolated_fault_checks": 2,
        "mixed_contracts": {str(i): 1 for i in range(21)},
        "rounds": 12,
        "observed_seconds": 259200,
        "last_overlap": True,
        "native_upload_overlap": [(1, 4), (2, 5), (3, 6)],
        "source_hash_start": "same",
        "source_hash_end": "same",
    }
    samples = [
        {
            "phase": "idle",
            "elapsed_seconds": 2000 + i * 300,
            "rss_bytes": 1000000000,
            "file_descriptors": 200,
            "api_ms": 100,
            "disk_free_bytes": 1024**3,
        }
        for i in range(15)
    ]
    assert assess(report, samples)["accepted"]
    for sample in samples[-3:]:
        sample.update(rss_bytes=2000000000, file_descriptors=400, api_ms=900)
    result = assess(report, samples)
    assert {"rss_growth", "fd_growth", "api_latency"}.issubset(result["failures"])
    report["source_hash_end"] = "changed"
    assert "code_changed" in assess(report, samples)["failures"]
