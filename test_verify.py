from verify import shown_lines, verify

shown = shown_lines("...\n49: go: downloading x\n51: go: go.mod requires go >= 1.27.0\n...\n52: make: *** Error 1")
assert shown == {49: "go: downloading x", 51: "go: go.mod requires go >= 1.27.0", 52: "make: *** Error 1"}

citations, validity = verify([51, 52, 999, 50], shown)
assert [c["found"] for c in citations] == [True, True, False, False]   # 50 was cut out, 999 is made up
assert validity == 0.5
assert citations[0]["text"] == "go: go.mod requires go >= 1.27.0"
assert verify([], shown) == ([], None)                                # no citations: undefined, not 0
print("ok")
