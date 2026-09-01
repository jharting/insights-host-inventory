# V2 Dependency Endpoints

This document is the complete reference for Clowder's V2 dependency endpoint system — the data
model, resolution rules, CRD configuration, and the migration path from V1. It is intended as
self-contained context for automated migration of services consuming Clowder-provided
dependency configuration.

## Why V2

V1 dependency endpoints expose raw connection primitives (`hostname`, `port`, `tlsPort`,
`h2cPort`, `h2cTLSPort`, `tlsCAPath`) and leave every consuming service to independently decide
which protocol to use, how to construct a URI, and whether to configure TLS. This logic is
duplicated across Python, Go, Ruby, and JavaScript services. There is also no signal for whether
a dependency requires client authentication.

V2 replaces this with an opinionated, pre-resolved endpoint: a single `uri` (with the correct
scheme, host, and port already chosen), a `ca_certificate` path when TLS requires a non-system
CA, and an `authenticated` boolean indicating whether the client should present credentials.
The consuming service uses what it's given — no connection logic needed.

Both V1 and V2 are emitted simultaneously in `cdappconfig.json`. Services migrate to V2
independently, at their own pace.

## Data Model

### cdappconfig.json structure

```json
{
  "endpoints": [ ... ],
  "privateEndpoints": [ ... ],

  "dependencyEndpoints": {
    "v2": {
      "<app-name>": {
        "<deployment-name>": {
          "uri": "https://rbac-service.prod-ns.svc:8443",
          "authenticated": false,
          "ca_certificate": "/cdapp/certs/service-ca.crt"
        }
      }
    }
  },
  "privateDependencyEndpoints": {
    "v2": {
      "<app-name>": {
        "<deployment-name>": {
          "uri": "http://rbac-service.prod-ns.svc:10000",
          "authenticated": false
        }
      }
    }
  }
}
```

Access pattern: `dependencyEndpoints.v2[appName][deploymentName]` (public) and
`privateDependencyEndpoints.v2[appName][deploymentName]` (private).

There is exactly one V2 entry per deployment endpoint. The V1 pattern of emitting separate
entries for each protocol variant (http, https, h2c, h2c+tls) is eliminated — Clowder picks the
best protocol (TLS preferred over plaintext) and emits a single entry.

### V2 endpoint fields

| Field | Type | Required | Description |
|---|---|---|---|
| `uri` | string | yes | Complete endpoint URI: `scheme://host:port`. Scheme is `http` for plaintext or `https` for TLS. |
| `authenticated` | bool | yes | Whether the client should attach workload identity credentials when calling this endpoint. Always present (never omitted). |
| `ca_certificate` | string | no | Filesystem path to a CA certificate file mounted by Clowder onto the consuming service's pods. Present only for in-cluster TLS (`ClowdApp` with TLS enabled via Caddy sidecars). Absent for `ClowdAppRef` endpoints (use system trust store) and for plaintext endpoints. |

### V1 vs V2 comparison

| Aspect | V1 | V2 |
|---|---|---|
| Access pattern | Array scan or client helper map | Direct nested map lookup |
| Protocol decision | Client picks from port/tlsPort/h2cPort/h2cTLSPort | Clowder picks, emits single `uri` |
| TLS trust | Global `tlsCAPath` or per-endpoint `tlsCAPath`; same path always | Per-endpoint `ca_certificate` reflecting actual trust chain; absent when system trust suffices |
| Auth signaling | Not provided | Explicit `authenticated` boolean per endpoint |
| Entries per deployment | Up to 4 (one per protocol variant) | Exactly 1 |

## URI and CA Certificate Resolution

Clowder determines the `uri` scheme, port, and `ca_certificate` based on the ClowdEnvironment
web provider configuration, whether the dependency is a ClowdApp or ClowdAppRef, and any
per-deployment TLS overrides.

### Resolution table

| ClowdEnvironment | Dependency type | Configuration | Scheme | Port source | `ca_certificate` |
|---|---|---|---|---|---|
| TLS enabled (Caddy sidecars, `web.tls.enabled: true`) | **ClowdApp** | default | `https` | `web.tls.port` / `web.tls.privatePort` | `/cdapp/certs/service-ca.crt` (OpenShift service-serving CA) |
| TLS disabled (`web.tls` absent) | **ClowdApp** | default | `http` | `web.port` / `web.privatePort` | absent |
| any | **ClowdAppRef** | TLS enabled on remote env | `https` | `remoteEnvironment.tls.port` | absent (system trust store) |
| any | **ClowdAppRef** | TLS not configured | `http` | `remoteEnvironment.port` | absent |

Key rules:
- When both TLS and plaintext ports are configured, TLS always takes priority.
- `ca_certificate` is only set for in-cluster `ClowdApp` with TLS. `ClowdAppRef` endpoints
  never get `ca_certificate` because they are accessed through gateways with publicly trusted
  certificates.
- When `ca_certificate` is absent, the client should use its platform's system trust bundle.
- When `ca_certificate` is absent and `uri` uses `http://`, no TLS configuration is needed.

## Authentication

### Purpose

The `authenticated` field tells the client whether it should present workload identity
credentials (e.g., SSO token from Kessel onboarding, projected Kubernetes SA token) when calling
this dependency. Clowder does not distribute or manage credentials — it only signals whether the
client should use credentials it already has.

### Defaults

| Dependency type | Default `authenticated` | Rationale |
|---|---|---|
| `ClowdApp` (in-cluster) | `false` | In-cluster communication relies on network isolation |
| `ClowdAppRef` (cross-cluster) | `true` | Cross-cluster traffic routes through gateways requiring authentication |

### Per-deployment override

The default can be overridden on the dependency's deployment definition:

- `webServices.public.authenticated` — overrides for public endpoints
- `webServices.private.authenticated` — overrides for private endpoints

These are optional boolean fields:
- Omitted → use the type-based default above
- `true` → mark as requiring authentication (e.g., opt-in for a `ClowdApp` like RBAC)
- `false` → mark as not requiring authentication (e.g., opt-out for a `ClowdAppRef` in an mTLS environment)

**Example — ClowdApp opt-in** (in-cluster service that requires authentication):

```yaml
apiVersion: cloud.redhat.com/v1alpha1
kind: ClowdApp
metadata:
  name: rbac
spec:
  deployments:
    - name: service
      webServices:
        public:
          enabled: true
          authenticated: true
        private:
          enabled: true
          authenticated: true
```

**Example — ClowdAppRef opt-out** (service mesh handles auth transparently):

```yaml
apiVersion: cloud.redhat.com/v1alpha1
kind: ClowdAppRef
metadata:
  name: rbac
spec:
  deployments:
    - name: service
      hostname: rbac.remote.example.com
      webServices:
        public:
          enabled: true
          authenticated: false
```

## ClowdApp + ClowdAppRef Coexistence (Gradual Migration)

### The problem

During multi-cluster migration, a service (e.g., RBAC) may run on both the local cluster
(as a `ClowdApp`) and a remote cluster (reachable via `ClowdAppRef`). Different consumers
need to independently switch between local and remote without coordinating with each other.

### The `serves` mechanism

`ClowdAppRef` has an optional `serves` field: a list of consumer `ClowdApp` names that should
prefer this `ClowdAppRef` over the local `ClowdApp` when both exist for the same dependency
name.

Resolution logic:

- Both `ClowdApp` and `ClowdAppRef` exist for dependency X:
  - Consumer is listed in `ClowdAppRef.spec.serves` → consumer gets `ClowdAppRef` endpoints (remote)
  - Consumer is **not** listed → consumer gets `ClowdApp` endpoints (local)
- Only `ClowdAppRef` exists → all consumers get `ClowdAppRef` endpoints
- Only `ClowdApp` exists → all consumers get `ClowdApp` endpoints

This logic is identical for both V1 and V2 endpoints.

### Gradual migration workflow

1. RBAC runs as a `ClowdApp` on cluster A (crcp). All consumers call it locally.
2. RBAC is deployed on cluster B (hccp). A `ClowdAppRef` is created on cluster A pointing to
   cluster B, with `serves: [app1, app2]`.
3. `app1` and `app2` now receive endpoints pointing to cluster B. All other consumers continue
   using the local `ClowdApp` on cluster A.
4. More consumers are added to `serves` over time.
5. Once all consumers are migrated, the local `ClowdApp` on cluster A is decommissioned.

### ClowdAppRef with serves example

```yaml
apiVersion: cloud.redhat.com/v1alpha1
kind: ClowdAppRef
metadata:
  name: rbac
  namespace: prod-env
spec:
  envName: prod-env
  remoteEnvironment:
    name: hccp
    port: 8000
    tls:
      enabled: true
      port: 443
  deployments:
    - name: service
      hostname: rbac.hccp.example.com
      webServices:
        public:
          enabled: true
  serves:
    - app1
    - app2
```

**Result for `app1`** (in serves list — gets remote):
```json
{
  "dependencyEndpoints": {
    "v2": {
      "rbac": {
        "service": {
          "uri": "https://rbac.hccp.example.com:443",
          "authenticated": true
        }
      }
    }
  }
}
```

**Result for `app3`** (not in serves list — gets local ClowdApp):
```json
{
  "dependencyEndpoints": {
    "v2": {
      "rbac": {
        "service": {
          "uri": "https://rbac-service.prod-ns.svc:8443",
          "authenticated": false,
          "ca_certificate": "/cdapp/certs/service-ca.crt"
        }
      }
    }
  }
}
```

## Full CRD Configuration Examples

### Example 1: In-cluster with TLS (Caddy sidecars, web.mode=operator)

**ClowdEnvironment:**
```yaml
apiVersion: cloud.redhat.com/v1alpha1
kind: ClowdEnvironment
metadata:
  name: prod-env
spec:
  providers:
    web:
      mode: operator
      port: 8000
      privatePort: 10000
      tls:
        enabled: true
        port: 8443
        privatePort: 10443
```

**ClowdApp (dependency):**
```yaml
apiVersion: cloud.redhat.com/v1alpha1
kind: ClowdApp
metadata:
  name: rbac
  namespace: prod-env
spec:
  envName: prod-env
  deployments:
    - name: service
      webServices:
        public:
          enabled: true
        private:
          enabled: true
      podSpec:
        image: quay.io/cloudservices/rbac:latest
```

**Consumer's cdappconfig.json (V2 section):**
```json
{
  "dependencyEndpoints": {
    "v2": {
      "rbac": {
        "service": {
          "uri": "https://rbac-service.prod-ns.svc:8443",
          "authenticated": false,
          "ca_certificate": "/cdapp/certs/service-ca.crt"
        }
      }
    }
  },
  "privateDependencyEndpoints": {
    "v2": {
      "rbac": {
        "service": {
          "uri": "https://rbac-service.prod-ns.svc:10443",
          "authenticated": false,
          "ca_certificate": "/cdapp/certs/service-ca.crt"
        }
      }
    }
  }
}
```

### Example 2: In-cluster without TLS (web.mode=default)

**ClowdEnvironment:**
```yaml
apiVersion: cloud.redhat.com/v1alpha1
kind: ClowdEnvironment
metadata:
  name: stage-env
spec:
  providers:
    web:
      mode: default
      port: 8000
      privatePort: 10000
```

**Consumer's cdappconfig.json (V2 section):**
```json
{
  "dependencyEndpoints": {
    "v2": {
      "rbac": {
        "service": {
          "uri": "http://rbac-service.stage-ns.svc:8000",
          "authenticated": false
        }
      }
    }
  }
}
```

`ca_certificate` is absent — no TLS.

### Example 3: Cross-cluster ClowdAppRef with TLS (default)

**ClowdAppRef:**
```yaml
apiVersion: cloud.redhat.com/v1alpha1
kind: ClowdAppRef
metadata:
  name: rbac
  namespace: prod-env
spec:
  envName: prod-env
  remoteEnvironment:
    name: hccp
    port: 8000
    tls:
      enabled: true
      port: 443
  deployments:
    - name: service
      hostname: rbac.hccp.example.com
      webServices:
        public:
          enabled: true
```

**Consumer's cdappconfig.json (V2 section):**
```json
{
  "dependencyEndpoints": {
    "v2": {
      "rbac": {
        "service": {
          "uri": "https://rbac.hccp.example.com:443",
          "authenticated": true
        }
      }
    }
  }
}
```

`ca_certificate` is absent — gateway uses a publicly trusted certificate; the client uses the
system trust store.

### Example 4: Cross-cluster ClowdAppRef plaintext (local testing)

**ClowdAppRef:**
```yaml
apiVersion: cloud.redhat.com/v1alpha1
kind: ClowdAppRef
metadata:
  name: rbac
  namespace: dev-env
spec:
  envName: dev-env
  remoteEnvironment:
    name: development
    port: 8000
  deployments:
    - name: service
      hostname: rbac-dev.local
      webServices:
        public:
          enabled: true
```

**Consumer's cdappconfig.json (V2 section):**
```json
{
  "dependencyEndpoints": {
    "v2": {
      "rbac": {
        "service": {
          "uri": "http://rbac-dev.local:8000",
          "authenticated": true
        }
      }
    }
  }
}
```

## Migrating a Consuming Service from V1 to V2

### No CRD changes needed

V2 endpoints are populated automatically by Clowder for all dependencies. No changes to
`ClowdApp`, `ClowdAppRef`, or `ClowdEnvironment` resources are needed. The migration is purely
in the consuming service's application code.

### V1 client pattern (before)

Typical V1 logic — duplicated across every consumer in every language:

```python
for endpoint in config.endpoints:
    if endpoint.app == "rbac" and endpoint.name == "service":
        if config.tlsCAPath:
            url = f"https://{endpoint.hostname}:{endpoint.tlsPort}"
            client = make_tls_client(ca_path=config.tlsCAPath)
        else:
            url = f"http://{endpoint.hostname}:{endpoint.port}"
            client = make_plaintext_client()
```

Problems with this pattern:
- Protocol decision logic duplicated in every consumer
- `tlsCAPath` presence used as a proxy for "use TLS" (tight coupling)
- No signal for authentication — each consumer hard-codes when to attach tokens
- Cross-cluster endpoints (`ClowdAppRef`) use a different CA trust chain than in-cluster, but
  the same `tlsCAPath` is unconditionally applied

### V2 client pattern (after)

```python
endpoint = config.dependencyEndpoints["v2"]["rbac"]["service"]
url = endpoint["uri"]

client = make_client()
if "ca_certificate" in endpoint:
    client.set_ca(endpoint["ca_certificate"])

if endpoint["authenticated"]:
    client.set_credentials(get_workload_identity())
```

- No protocol/port decision logic
- CA trust is per-endpoint, not global
- Authentication is explicit

### Client library readiness

Not all client libraries have full V2 support. Check this table before migrating a service.

| Language | Library | Public V2 | Private V2 | Helper function | Notes |
|---|---|---|---|---|---|
| Go | `app-common-go` | yes | yes | `GetV2DependencyEndpoint(app, name)`, `GetV2PrivateDependencyEndpoint(app, name)` | Full support. Package-level `DependencyEndpointsV2` and `PrivateDependencyEndpointsV2` maps also available. |
| Python | `app-common-python` | yes | **no** | `get_v2_dependency_endpoint(app, endpoint)` | Private V2 endpoints (`privateDependencyEndpoints.v2`) are not parsed. Module-level `DependencyEndpointsV2` dict also available. |
| JavaScript | `app-common-js` | not checked | not checked | — | Audit needed before migrating JS services. |
| Ruby | `app-common-ruby` | not checked | not checked | — | Audit needed before migrating Ruby services. |

**Implication for migration agents:** If a service uses private dependency endpoints and is
written in Python, the migration to V2 is blocked until `app-common-python` adds private V2
parsing. Go services can migrate both public and private endpoints immediately.

### Client library API reference

**Go** (`app-common-go`):
```go
import clowder "github.com/redhatinsights/app-common-go/pkg/api/v1"

// Direct map access
endpoint := clowder.DependencyEndpointsV2["rbac"]["service"]
fmt.Println(endpoint.Uri, endpoint.Authenticated, endpoint.CaCertificate)

// Helper function (returns value + ok bool)
endpoint, ok := clowder.GetV2DependencyEndpoint("rbac", "service")
endpoint, ok := clowder.GetV2PrivateDependencyEndpoint("rbac", "service")
```

**Python** (`app-common-python`):
```python
from app_common_python import DependencyEndpointsV2, get_v2_dependency_endpoint

# Direct dict access
endpoint = DependencyEndpointsV2["rbac"]["service"]
print(endpoint.uri, endpoint.authenticated, endpoint.ca_certificate)

# Helper function (returns None for missing keys)
endpoint = get_v2_dependency_endpoint("rbac", "service")
```

### Migration checklist

1. **Read V2 endpoint** instead of scanning the V1 `endpoints` array.
   Access: `config.dependencyEndpoints.v2[appName][deploymentName]`

2. **Use `uri` directly** as the base URL. Remove any logic that constructs URLs from
   hostname + port + scheme.

3. **Handle `ca_certificate` for TLS trust.** If present, configure the HTTP client to trust
   that specific CA. If absent, rely on the system trust store. Remove any logic that checks the
   global `tlsCAPath` or uses its presence to decide whether to enable TLS.

4. **Handle `authenticated` for credential attachment.** If `true`, attach workload identity
   credentials (SSO token, SA token, etc.). If `false`, do not. Remove any hard-coded lists of
   "which dependencies need auth."

   **When the service already has a preexisting auth mechanism (PSK, existing token, etc.):** keep
   that behavior untouched for `authenticated: false`, and switch to the kessel-sdk OAuth2 token
   for `authenticated: true`. Branch on the endpoint's `authenticated` flag:

   ```python
   if endpoint.authenticated:
       headers["Authorization"] = f"Bearer {get_kessel_access_token(config)}"
   else:
       headers["x-rh-<service>-psk"] = config.<service>_token   # preexisting auth, unchanged
   ```

   `authenticated: true` must always yield a kessel-sdk token — never key the switch on a separate
   migration flag, and never raise. If a separate flag (e.g. `kessel_auth_enabled`) already selects
   kessel-vs-PSK, it remains authoritative only within the `authenticated: false` branch.

5. **Remove V1 fallback logic** once migration is complete. This is optional — V1 continues to
   be emitted indefinitely.

### What to watch for during migration

- **Do not use `ca_certificate` presence as a TLS signal.** An `https://` URI with no
  `ca_certificate` means "use TLS with system trust." The presence of `ca_certificate` only
  means "trust this specific non-system CA." Check the URI scheme to determine if TLS is in use.

- **`authenticated` is always present.** Unlike `ca_certificate`, it is never omitted. Do not
  default or infer — read the explicit value.

- **Private endpoints use a separate top-level key.** Public dependencies are under
  `dependencyEndpoints.v2`, private under `privateDependencyEndpoints.v2`. The nesting structure
  within each is identical.

- **Check client library support before migrating private endpoints.** As of this writing,
  `app-common-python` does not parse `privateDependencyEndpoints.v2`. Python services using
  private dependency endpoints cannot migrate to V2 until the library is updated. See the
  client library readiness table above.
