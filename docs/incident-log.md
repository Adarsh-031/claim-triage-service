# Incident log

| Date | Symptom | Cause | Fix | PR |
|---|---|---|---|---|
| 2026-10-24 | PR #11 CI stopped at `uv run ruff check .`; tests were skipped. | Ruff rejected import ordering in the new LLM stub tests. | Reordered the imports in commit `c542539` (`Fix import ordering in LLM stub tests`); the next CI run passed lint and tests. | [#11](https://github.com/Adarsh-031/claim-triage-service/pull/11) · [failed run](https://github.com/Adarsh-031/claim-triage-service/actions/runs/40110128079) · [passing run](https://github.com/Adarsh-031/claim-triage-service/actions/runs/40110369186) |
| 2026-10-24 | PR #10 could not merge cleanly with the updated `main` branch. | `main` advanced while the clause-ingestion work and review revisions were in progress. | Merged `main` into the feature branch (commit `06e3783`), resolved the branch divergence, completed review revisions, and merged after approval. | [#10](https://github.com/Adarsh-031/claim-triage-service/pull/10) · [sync commit](https://github.com/Adarsh-031/claim-triage-service/commit/06e37832ee6d6e55ab4c5cc5bb763af6e793160a) |
