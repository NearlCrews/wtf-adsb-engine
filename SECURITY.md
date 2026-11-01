# Security policy

## Report privately

On GitHub, use this repository's **Security → Report a vulnerability** entry for
a suspected vulnerability or disclosure of private receiver data. This requires
the repository owner to enable
[private vulnerability reporting](https://docs.github.com/en/code-security/how-tos/report-and-fix-vulnerabilities/configure-vulnerability-reporting/configure-for-a-repository).

If that entry is unavailable, open an issue asking for a private reporting
channel without including vulnerability details, affected data, or credentials.
Wait for a private channel before sharing sensitive material. An ordinary public
bug report is appropriate for a sanitized functional defect.

Include the affected version, a minimal reproduction, expected and observed
behavior, and the likely impact. Use synthetic data wherever possible. Never
send live credentials or a complete receiver journal. Maintainers will review
reports as time permits; there is no guaranteed response or remediation deadline.

## Supported versions

Security fixes target the active development branch and the latest released
version. Older releases have no promised backports. Until a release is published,
the development branch is the only supported line.

## Trust boundaries

Installed plugins execute Python code with the engine process's permissions.
Configuration and payload validation do not isolate hostile plugins. Review
plugin packages before installing them and run the engine as an unprivileged
user with access limited to its receiver, providers, and private state.

Feed checksums detect corruption; they do not authenticate a sender. Consumers
need trusted transport and filesystem access controls. Observation exports omit
receiver coordinates and other private fields, but aircraft tracks can still
suggest the reception region. Raw journals and frozen archive artifacts are
private operational data.

Retraction cannot erase previously distributed batches. If private information
enters a feed, stop its distribution, preserve evidence privately, and coordinate
remediation with consumers. See [operations](docs/operations.md) for state,
backup, and retention limits.
