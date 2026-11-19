# Copilot instructions

- Keep the runtime dependency-free unless an external package is necessary and documented.
- Route model requests through `omni_router.py`; do not embed provider credentials in code or manifests.
- Keep workflow execution behind `jroc_bridge.dispatch_workflow()` and persist run state through `StateStore`.
- Validate workflow inputs explicitly and propagate execution failures after recording them.
- Add focused `unittest` coverage for behavior changes; run `python -m unittest discover -s tests -v`.
- Preserve the Windows launchers and support Python 3.10 or later.
