# Traces

`src/tracing.py` writes one JSONL file per conversation here during a run.

These exist so the repository carries machine-readable evidence that does not
depend on a dashboard login — and so tracing still produces something when
running against a gateway, where dashboard upload is unavailable.

`scripts/build_docs.py` reads these to generate `docs/sample-interactions.md`,
which is why that document can only contain things that actually happened.

Committed after a real run. Regenerate with:

```bash
python -m src.main --demo
python -m src.main --guardrail-demo
python -m scripts.build_docs
```
