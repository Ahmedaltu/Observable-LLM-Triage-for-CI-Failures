from exporter import metrics


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
print("ok")
