# 0006 Placeholder tests are strict xfail

**Decision.** Wave 0 ships the domain and renderer signatures with tests that describe
their behaviour, marked `pytest.mark.xfail(raises=NotImplementedError, strict=True)`.

**Why.** CI stays green while the functions raise `NotImplementedError`. A wrong
implementation fails (it raises something else), and a right one makes the test
XPASS, which strict mode turns into a failure, so the workstream must remove the
marker. No placeholder can be left behind silently.
