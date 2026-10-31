from exporter import filter_metrics, llm_metrics, metrics, watch_metrics


def job(build, name, state, refs_key="main:a,1:b", repo="bmo", type="presubmit"):
    return {"build_id": build, "job": name, "state": state, "refs_key": refs_key, "type": type, "refs": {"repo": repo}}


m = metrics([
    job(1, "unit", "failure"), job(2, "unit", "success"),            # rerun passed on same commit -> flaky
    job(3, "lint", "failure"), job(4, "lint", "success", "main:a,1:c"),  # passed on a new commit -> not flaky
    job(5, "stale", "failure", "", "", "periodic"),
])
assert 'metal3_prow_flaky{repo="bmo",job="unit"} 1' in m
assert 'job="lint"' not in m.split("metal3_prow_flaky")[-1]
assert 'metal3_prow_jobs{repo="periodic",job="stale",type="periodic",state="failure"} 1' in m
res = [{"job": "j1", "model": "m", "category": "code_bug", "latency_s": 2, "ttft_s": 0.5, "input_tokens": 10, "output_tokens": 5,
        "cost_usd": 0.0, "citation_validity": 1.0},
       {"job": "j2", "model": "m", "category": "config_error", "latency_s": 4, "input_tokens": 10, "output_tokens": 5,
        "cost_usd": 0.0, "citation_validity": None}]                 # no citations: left out of the mean
runs = {"failover-x": [{"served_by": "llama3", "fallback": False}, {"served_by": "llama3.2:3b", "fallback": True},
                       {"served_by": None, "fallback": True}]}
m = llm_metrics(res, {"j1": "code_bug", "j2": "code_bug"}, runs)
assert 'metal3_triage_latency_seconds{model="m",stat="median"} 3.0' in m
assert 'metal3_triage_latency_seconds{model="m",stat="ttft_median"} 0.5' in m  # only results that have it
assert 'metal3_triage_citation_validity{model="m"} 1.0000' in m
assert 'metal3_triage_accuracy{model="m"} 0.5000' in m
assert 'metal3_serve_answers{run="failover-x",served_by="none"} 1' in m
assert 'metal3_serve_fallbacks{run="failover-x"} 2' in m
w = watch_metrics({"last_poll": 200, "last_success": 100, "error": "URLError: down"})
assert "metal3_watch_poll_ok 0" in w and "metal3_watch_last_success_timestamp_seconds 100" in w
assert watch_metrics(None) == ""
f = filter_metrics({"a": (4000, 400), "b": (800, 800)})
assert 'metal3_filter_bytes{job="a",stage="out"} 400' in f
assert 'metal3_filter_tokens_estimate{stage="in"} 1200' in f and 'metal3_filter_tokens_estimate{stage="out"} 300' in f
print("ok")
