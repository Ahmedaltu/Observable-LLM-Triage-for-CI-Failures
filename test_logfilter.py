from logfilter import filter_log, render

log = "\n".join(
    [f"go: downloading pkg{i}" for i in range(50)]
    + ["go: go.mod requires go >= 1.27.0", "make: *** [Makefile:204: modules] Error 1"]
    + [f"cleanup {i}" for i in range(20)]
)
kept = dict(filter_log(log))
assert kept[51] == "go: go.mod requires go >= 1.27.0"   # context before the hit, original numbering
assert 52 in kept and 72 in kept                         # the hit and the log tail
assert 10 not in kept                                    # noise dropped
assert render(filter_log(log)).startswith("...\n49: ")   # gap marker + line numbers
assert dict(filter_log("\x1b[31mFAIL\x1b[0m x"))[1] == "FAIL x"
for noise in ["run --timeout=10m", "go: downloading github.com/pkg/errors v0.9.1", 'level=info msg="x forbidden y"']:
    assert len(filter_log("\n".join([noise] + ["ok"] * 20))) == 10, noise   # only the tail kept
print("ok")
