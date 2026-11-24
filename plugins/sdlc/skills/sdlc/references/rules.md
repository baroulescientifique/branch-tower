# Shared SDLC rules

- Keep each change traceable from a user outcome to a stable acceptance ID, owning task, and verification evidence.
- Review the spec, plan, and task assignments at a named starting commit before implementation begins.
- Assign each path to one task. Independent tasks can run together. A task with prerequisites starts after their changes have been integrated.
- Ask a fresh verifier to exercise the integrated commit and record observed outcomes for every acceptance ID.
- Limit repairs to approved paths. A change in scope requires a new plan review.
- Hand verification evidence to the application's PR and release process. Link the shipped artifact to its exact commit and record the post-deploy check and owner.
