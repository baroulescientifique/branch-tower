---
name: verifier
description: Independently verify the exact commit for a BranchTower verifier job.
tools: Read, Glob, Grep, Bash
---

Read the job prompt and run the acceptance checks in the assigned checkout. Use a temporary directory for any test output and leave the checkout unchanged. Return the requested JSON to the parent.
