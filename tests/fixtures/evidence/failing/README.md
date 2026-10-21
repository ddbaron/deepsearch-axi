# Synthetic evidence fixture: failing

This directory is a **synthetic** evidence tree that is intentionally invalid. It exists so `tests/test_delivery_contract.py` can prove the verifier rejects a recorded failure rather than only accepting mutated copies of the complete fixture. It is not real evidence and must never be cited in a handoff.

Relative to `../complete`, this fixture records:

- `AC-07` as **failed** with a non-zero exit code.
- `AC-19` as **skipped**.
- An unresolved adversarial QA **security** finding (`QA-1`).
- A synthesis that is not resolved and still carries the open finding.

Everything else follows the same synthetic layout as `../complete`, including the copied PRD §13 gate table in `prd.md`.

Run it with:

```sh
python scripts/verify_evidence.py --artifacts tests/fixtures/evidence/failing
```

The command must exit non-zero. Local git and the action log are audited process assertions; they cannot prove the absence of remote actions.
