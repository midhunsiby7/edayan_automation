# Development Agent Instructions & Standing Policies

## Project Overview
* **Project**: Cashbook AI & e-Dayan Automation
* **Repository**: `midhunsiby7/edayan_automation`
* **Default Branch**: `main`
* **Root Directory**: `D:/edayan_data_pro`

---

## Mandatory GitHub Synchronization Policy

After every successfully completed implementation task, feature request, or architectural milestone:

1. **Complete Implementation**: Ensure all code changes, configurations, and documentation updates are thoroughly completed.
2. **Execute Targeted Tests**: Run targeted tests for the modified modules.
3. **Run Regression Suite**: Run the complete offline test suite (`pytest tests` inside `cashbook_ai`) to confirm that zero regressions exist and no existing assertions were weakened.
4. **Inspect Git Diff**: Run `git diff` and `git status` to inspect all modifications and untracked files across the repository.
5. **Privacy & Secrets Audit**:
   * Verify that no `.env` files, API keys, credentials, or secrets are present or staged.
   * Verify that no real cashbook photographs (`*.jpg`, `*.png`), original OCR extraction JSONs, real monthly workbooks (`*.xlsx`), journals, manifests, or real transaction records are staged.
   * Verify that test fixtures in `tests/fixtures/` contain only sanitized synthetic benchmark data.
6. **Verify `.gitignore`**: Ensure `.gitignore` protects all confidential monthly folders, models, logs, and artifacts (`data/monthly/`, `data/output/`, `data/jobs/`, `data/archive/`, `models/`, `scratch/`, etc.).
7. **Selective Safe Staging**: Stage only intended safe source files, tests, configuration templates, and documentation. Never use blind wildcards like `git add .` without prior diff review.
8. **Descriptive Commit**: Create a meaningful, conventional commit summarizing the technical achievements. Never include real transaction amounts, voucher numbers, church details, or customer names in commit messages.
9. **Push to Remote**: Push the commit to `origin/main` using a safe non-force push.
10. **Verify Remote Head**: Verify that `origin/main` has been successfully updated with the new commit hash.
11. **Comprehensive Reporting**: Include the commit SHA, changed files, test results, and push confirmation in the final turn summary.

---

## Standing Development Rules & Guardrails

* **No Incomplete or Failing Pushes**: Never push half-finished code or failing tests to GitHub.
* **No Secret or Private Data Exposure**: The GitHub repository is public. Absolute confidentiality of financial records, ledger images, and credentials must be maintained at all times.
* **No Force-Pushing or History Rewriting**: Never force-push (`git push --force`) or rewrite public Git history without explicit, documented user authorization.
* **No Artificial Empty Commits**: Never commit empty changes merely to create activity.
* **Preserve Git Author Identity**: Preserve existing configured author identity; never invent or modify Git author email/username without user instructions. If GitHub authentication fails, report the error immediately.
* **Safe Remote Reconciliation**: If the remote branch diverges, reconcile safely using standard non-destructive merging/rebasing without force-pushing.
* **Automatic Execution**: This policy applies to all future successful implementation milestones automatically. Do not wait for the user to request a Git synchronization after successfully delivering a task.
