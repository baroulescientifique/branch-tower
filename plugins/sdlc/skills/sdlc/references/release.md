# Release and operation

The verification handoff identifies the app repo, reviewed spec digest, exact tested commit, and results. Attach it to the PR and use the application's existing review and CI/CD process.

If the commit that will ship changes, verify that exact commit with `verify-commit RUN_ID --commit COMMIT`. After deployment, record the shipped commit, artifact ID, owner, rollback procedure, and observed smoke check with the `release` command. A failed smoke check records the follow-up work for the next intake.
