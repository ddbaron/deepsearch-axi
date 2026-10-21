# Synthetic evidence fixture: complete

This directory is a **synthetic** evidence tree used only by `tests/test_delivery_contract.py` and `scripts/verify_evidence.py`. It is not real acceptance evidence, it makes no claim that any product gate ran, and it must never be cited in a real handoff.

The tree contains:

- `manifest.json` — gate records for AC-01 through AC-45, including command, status, exit code, log path, test summary and the tested commit/tree.
- `logs/AC-XX.log` — one non-empty synthetic log per gate. AC-43 is recorded as pending because the verifier computes that result itself (PRD §13 self-record bootstrap).
- `reports/review.json` — independent review with concrete cases and resolved findings.
- `reports/qa.json` — deeper QA from a distinct reviewer role.
- `reports/synthesis.json` — resolved synthesis covering every finding.
- `refs.json`, `action_log.jsonl`, `handoff.md` — recorded base/default refs, an audited execution log, and the delivery handoff fields required by AC-45.
- `prd.md` — a copied excerpt of the PRD §13 gate table, used when `--prd` is omitted.

Run it against a temporary copy, because a successful non-confirm run writes `ac43_result.json` into the evidence directory:

```sh
tmp=$(mktemp -d)
cp -R tests/fixtures/evidence/complete "$tmp/complete"
python scripts/verify_evidence.py --artifacts "$tmp/complete"
python scripts/verify_evidence.py --artifacts "$tmp/complete" --confirm
rm -rf "$tmp"
```

The second command is the read-only confirmation mode.

Local refs and the action log are audited process assertions. They cannot prove the absence of remote actions; only the audited record is checked.
