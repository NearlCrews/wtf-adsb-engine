# Documentation

Start with the [project README](../README.md) for the quick start. The
[development guide](../DEVELOPMENT.md) covers the development setup, checks,
image builds, and the synthetic replay. All commands assume a source checkout
unless a guide says otherwise.

| Task | Guide |
| --- | --- |
| Configure a receiver, replay, and output | [Configuration](configuration.md) |
| Run, inspect health, back up, and troubleshoot | [Operations](operations.md) |
| Build a container, migrate a journal, or serve the private viewer | [Deployment](deployment.md) |
| Understand bundled data and optional providers | [Reference data](reference-data.md) |
| Freeze and analyze historical archives | [Archive operations](archive-operations.md) |
| Write an installed plugin | [Plugin SDK](plugin-architecture.md) and [example](../examples/plugins/reference-label/README.md) |
| Consume the JSON feed | [Evidence format](evidence-format.md) and [JSON Schemas](../contracts/README.md) |
| Set up development and submit a change | [Development](../DEVELOPMENT.md) and [contributing](../CONTRIBUTING.md) |
| Find help or report a vulnerability | [Support](../SUPPORT.md) and [security](../SECURITY.md) |
| Prepare a release and maintain dependencies | [Release maintenance](releasing.md) |
| Inspect the dependency baseline and source terms | [Dependency audit](dependency-audit-2026-10-08.md) and [third-party notices](../THIRD_PARTY_NOTICES.md) |

The engine is a standalone analysis and export application. A consumer can be
written in another language or run on another host. The repository does not
require access to a site-specific publisher, a private source checkout, or a
production database.
