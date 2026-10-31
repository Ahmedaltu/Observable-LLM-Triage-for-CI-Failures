from labels import accuracy

assert accuracy({"a": "code_bug", "b": "config_error"}, {"a": "code_bug", "b": "code_bug", "c": "x"}) == (1, 2)  # c unlabelled: ignored
assert accuracy({}, {"a": "code_bug"}) == (0, 0)
print("ok")
