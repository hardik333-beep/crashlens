# Security Policy

Crashlens is a self-hosted, open source error monitoring platform. Because
it holds error data from your applications (stack traces, tags, breadcrumbs,
and sometimes user identifiers) and enforces multi-tenant isolation at the
database layer, we take vulnerability reports seriously even though this is
a small, early-stage project.

## Supported versions

Crashlens is at v0.1.0. Only the latest released version receives security
fixes right now; there is no support matrix for older versions yet.

## Reporting a vulnerability

Please report suspected vulnerabilities privately, using GitHub's private
vulnerability reporting feature:

1. Go to the [Security tab](https://github.com/hardik333-beep/crashlens/security) on this repository.
2. Click "Report a vulnerability."

This opens a private draft security advisory that only the maintainer can
see. It is not a public GitHub issue, and nothing in it is visible to other
users of the repository. That matters because publishing details of a real
vulnerability before a fix ships would tell attackers how to exploit it
against everyone still running the affected version.

This is a solo, early-stage open source project, so there is no dedicated
security team and no formal SLA. Reports are handled best effort, typically
within a few days. You will get a reply acknowledging the report and, once
the issue is understood, an idea of what fix or workaround to expect.

## Scope

In scope: the Crashlens application itself, meaning the server (ingest
endpoint, authentication, session handling, the multi-tenant isolation
code), the dashboard, the SDKs (Python, browser, Node), and the database
migrations that define the tenant isolation model. Tenant isolation is a
real security boundary here, not just an internal convenience: organization
data is separated with PostgreSQL Row Level Security, enforced by running
the application as a non-superuser database role so RLS actually applies.
Any way to read or modify another organization's data, or to bypass RLS
from the application's database role, is very much in scope and will be
taken seriously.

Out of scope: security issues that come from how you choose to self-host
Crashlens rather than from the application itself. Examples: not putting
your deployment behind TLS, exposing the Postgres or Redis ports to the
public internet, or choosing a weak password for a generated secret instead
of a strong one. See [docs/self-hosting.md](docs/self-hosting.md) for the
hardening guidance on those points; if you find a way the application
itself makes an insecure default unavoidable, that is in scope, but the
deployment choice itself is on the operator.

## What not to do

Please do not open a public GitHub issue for a suspected vulnerability,
even a vague one; use the private reporting flow above instead. Please do
not test against anyone else's running Crashlens instance without their
permission; test against an instance you control.

## Coordinated disclosure

There is no bug bounty program at this stage. If you would like credit for
a reported and fixed vulnerability, it will be given in the release notes
for the fix.
