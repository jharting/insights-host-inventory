# /clowder-v2-analyze - Analyze a service for Clowder V2 dependency endpoint migration

Analyze a service's source code to find all Clowder-based HTTP client configurations and produce a
migration report for switching from Clowder V1 dependency endpoints to V2.

## Arguments

The user will pass the path to the target repository: $ARGUMENTS

If no arguments are provided, ask the user for the repository path. The path is required — do not
default to the current directory.

## Reference: V2 Dependency Endpoint Schema

Read the Clowder V2 specification before starting the analysis. It is bundled with this skill:

!`cat .claude/skills/clowder-v2-analyze/DependencyV2.md`

## Analysis Steps

Work from the target repository. Run steps 1-4 in parallel where possible.

### 1. Identify Clowder dependencies

- Read the ClowdApp manifest (typically `deploy/clowdapp.yml` or `deploy/clowdapp.yaml` or
  similar). Look for `dependencies:` and `optionalDependencies:` lists.
- Note which dependencies are declared and whether they are required or optional.

### 2. Find all HTTP client configurations

Search the entire codebase for outbound HTTP/gRPC client usage:

- `requests.Session`, `requests.get`, `requests.post`, `httpx`, `urllib`, `aiohttp`, `grpc`
- URL construction patterns: `f"https://{hostname}:{port}"`, string concatenation building URLs
- TLS/SSL/CA configuration: `verify=`, `cert=`, `ssl`, `ca_cert`, `tls`, `CAPath`
- Auth token attachment: `Authorization`, `Bearer`, headers with credentials
- Any reference to `clowder`, `app_common_python`, `app-common-go`, `LoadedConfig`
- Environment variables that set URLs for external services

For each find, record: file path, line numbers, what service it connects to, how the URL is built,
how TLS is configured, how auth is configured.

### 3. Analyze Clowder endpoint resolution

- Find all imports/usage of `app_common_python` (Python), `app-common-go` (Go), or equivalent
- Identify V1 patterns: `cfg.endpoints` array scans, `cfg.privateEndpoints` scans,
  `DependencyEndpoints` map lookups, `cfg.tlsCAPath` as TLS decision signal
- Check for any existing V2 usage (`DependencyEndpointsV2`, `get_v2_dependency_endpoint`)
- Note the Clowder SDK version in the dependency file (pyproject.toml, go.mod, package.json)

### 4. Check for Kessel SDK and workload identity

- Search for `kessel`, `Kessel`, `KESSEL` across all files
- Check dependency manifests for kessel-related packages
- Look for OAuth2 client credentials, OIDC token acquisition, service account patterns
- Look for gRPC client configuration (Kessel uses gRPC)
- Determine whether there is an established workload identity that could be used when
  Clowder V2 reports `authenticated: true`

### 5. Classify each dependency

For each Clowder dependency found in step 1, classify it:

- **HTTP dependency endpoint** — consumed via `cfg.endpoints` or `cfg.privateEndpoints` or
  `DependencyEndpoints` map for HTTP calls → V2 migration candidate
- **gRPC dependency** — consumed for gRPC, typically via SDK with its own channel management →
  NOT applicable for V2 (V2 emits HTTP URIs only). Note: gRPC dependencies configured via env
  vars is the expected pattern.
- **Kafka-only** — consumed only for Kafka topics, no HTTP/gRPC endpoint calls → not affected
- **Not consumed** — declared but no code references found → note for completeness

### 6. Non-Clowder HTTP dependencies

Identify HTTP clients that get their URLs from environment variables, hardcoded strings, or
other non-Clowder sources. These are not affected by V2 migration but should be listed for
completeness.

## Report Format

Produce the report with these sections:

### Executive Summary

2-3 sentences: how many Clowder dependencies exist, how many are HTTP (V2 candidates),
whether workload identity exists, top-level blockers if any.

### Per-Dependency Reports

For each HTTP dependency that is a V2 migration candidate, produce a report with this structure:

```
#### N. <Dependency Name> — <public|private> endpoint — <V2 READY | BLOCKED | NEEDS WORK>

| Aspect | Current State |
|---|---|
| **Clowder dependency name** | `<name>` |
| **V2 key** | `dependencyEndpoints.v2["<app>"]["<deployment>"]` or **Unknown — needs confirmation** |
| **Endpoint type** | Public / Private |
| **Config location** | `<file>:<lines>` |
| **HTTP client location** | `<file>:<lines>` |
| **URL construction** | Description of how URL is built |
| **TLS** | How TLS/CA is configured |
| **Auth** | How authentication works |
| **Non-Clowder fallback** | Env var fallback if any |

**V2 migration changes:**
- What needs to change (URL resolution, TLS, auth)

**Action items:**
- [ ] Specific things to do or confirm
```

### Key report rules

Follow these rules strictly:

1. **Do NOT guess V2 deployment names.** The V2 access pattern is
   `dependencyEndpoints.v2[appName][deploymentName]`. The `appName` is usually known from the
   ClowdApp manifest's dependency list. The `deploymentName` comes from the *dependency's own*
   ClowdApp manifest — NOT from this service's code. If the V1 code only matches on
   `endpoint.app` (no `endpoint.name` check), the deployment name is unknown. Flag it:
   "**Unknown — confirm from `<dependency>`'s ClowdApp manifest**"

2. **Flag PSK-to-Kessel transitions, and apply the auth-switch rule.** If a dependency currently
   uses PSK (pre-shared key) authentication (e.g., `x-rh-exports-psk`, `X-RH-RBAC-PSK`), highlight
   that this will transition to Kessel workload identity under V2.

   **Auth-switch rule (recommend this exact behavior in the migration changes):** when a service
   already has a preexisting authentication mechanism (PSK, an existing token, etc.), keep that
   behavior **untouched for `authenticated: false`**, and **switch to the kessel-sdk OAuth2 token
   for `authenticated: true`**. In code this is a single branch on the endpoint's `authenticated`
   flag:

   ```python
   if endpoint_authenticated:
       headers["Authorization"] = f"Bearer {get_kessel_access_token(config)}"
   else:
       # preexisting auth, unchanged
       headers["x-rh-<service>-psk"] = config.<service>_token
   ```

   Do NOT re-key the switch on a separate migration flag (e.g. an existing `kessel_auth_enabled`
   toggle) and do NOT raise/error when `authenticated: true` — `authenticated: true` must always
   result in a kessel-sdk token. If the service has a preexisting flag that already selects
   kessel-vs-PSK, that flag stays authoritative only for the `authenticated: false` branch; the
   `authenticated: true` branch unconditionally uses kessel-sdk.

3. **`authenticated` must always be honored.** Do not assume whether a dependency will be local
   or remote — the service must work under any scenario the V2 schema supports. If the code
   currently cannot honor `authenticated: true` (e.g., no workload identity exists, or a code
   path skips auth), flag that as a gap.

4. **Clowder SDK upgrade is expected.** Do not flag the need to upgrade `app-common-python`,
   `app-common-go`, etc. as a blocker — it is a known prerequisite. Do note the current version
   and whether V2 support exists in that version.

5. **gRPC dependencies configured via env vars are fine.** Kessel and similar gRPC services
   are typically not configured via Clowder dependency endpoints. Note them for completeness
   but mark as "Not applicable for V2."

### Non-Clowder Dependencies

Brief table listing non-Clowder HTTP dependencies (external SaaS, hardcoded URLs, etc.) with
a note that they are unaffected.

### Kafka-only / gRPC / Unused Dependencies

Brief table listing these with their classification and a note on why V2 does not apply.

### Workload Identity Status

Table summarizing:
- Whether Kessel SDK or equivalent is installed (package + version)
- OAuth2 credentials configuration (env vars, K8s secrets)
- OIDC discovery endpoint
- Whether the identity is already used for any HTTP auth today
- Whether it can be reused for V2 `authenticated: true` endpoints

### Prerequisites

Table of things that must happen before or during migration (SDK upgrades, deployment name
confirmations, auth implementation gaps, etc.). Do NOT list SDK upgrade as a blocker — list it
as an expected prerequisite.

### Risks and Observations

Any bugs found (e.g., missing `verify=` on HTTP calls), inconsistencies between clients,
or other issues worth noting during migration.
