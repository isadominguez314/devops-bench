# Agents

An **agent harness** is the thing under test. It drives one AI agent against one
task prompt and hands back a typed result the rest of the benchmark can score.
Everything in this layer lives under `devops_bench/agents/`.

The base class is `AgentHarness` (`devops_bench/agents/base.py`). It owns two
concerns so subclasses never have to: the base `run()` stamps wall-clock
**latency** onto every result, and it wraps the agent in a **safety net** — any
crash inside the agent is caught and turned into an errored result, so one faulty
agent never aborts the whole benchmark. Subclasses implement a single method,
`_execute()`, which does the provider-specific work and returns an `AgentResult`.

```text
agent.run(prompt) -> AgentResult     # base: latency + safety net
   └─ agent._execute(prompt)         # subclass: build invocation, parse, return
```

## Supported harnesses

Four harnesses ship today. Each self-registers under a canonical key.

| Key | Wraps | How it runs | Capabilities |
| --- | --- | --- | --- |
| `gemini` | The Google **Gemini CLI** binary | Headless subprocess; trajectory parsed from `--output-format stream-json` on stdout | MCP, skills, rules, allowed-tools |
| `openclaw` | The **Openclaw Agent CLI** | `openclaw agent --local` with per-run isolated state/config; trajectory via `openclaw sessions export-trajectory` | MCP, skills, rules |
| `antigravity` | The **Antigravity CLI** (`agy` binary) | Headless subprocess that keeps the real `HOME` so cached OAuth/ADC credentials work (see the trust-boundary note below); trajectory parsed from the transcript JSONL it writes, token usage read from the conversation DB | MCP, skills, rules |
| `api` | **In-process** model call | Calls `get_model(provider, model)` and runs a model-agnostic MCP tool-use loop (`max_turns`, default 50) | MCP (spawns a stdio server), skills (served as tools), rules (system instruction) |

> `oc` is just a shorthand alias for the `openclaw` CLI; this doc uses `openclaw` throughout.

> [!NOTE]
> The `gemini` key names the CLI **harness** — the program that drives the agent.
> It is not the gemini **model**. You can run the gemini *model* through the `api`
> harness, or run a non-gemini model through the `gemini` CLI, because the harness
> and the model are chosen independently (see [Harness vs model](#harness-vs-model)).
> The alias `gemini-cli` also resolves to `gemini`, and is the default agent type.

## Harness vs model

A harness does **not** hardcode a model. It reads `AGENT_PROVIDER` and
`AGENT_MODEL` from its config and maps them onto whatever it drives.

Every harness resolves `AGENT_PROVIDER` through one shared contract
(`devops_bench/core/model_providers.py`), so the same `AGENT_*` config behaves
identically across them. The `api` harness uses it to pick the adapter family and
backend for `get_model(provider, model)` and runs the tool-use loop in-process.
The CLI harnesses (`gemini`, `openclaw`) use it to route `AGENT_API_KEY` onto the
binary's provider-specific env var(s) and pass the model through: the Gemini CLI
gets `GEMINI_MODEL`, and openclaw gets a `--model provider/id` flag. Either way,
the model is a runtime input, never baked into the harness.

`antigravity` is the exception: it does not go through the shared contract. It
writes `AGENT_API_KEY` straight onto `GEMINI_API_KEY` and `GOOGLE_API_KEY` and
maps the model onto `GEMINI_MODEL` (`agents/cli/antigravity/agent.py`), so it is
Gemini-only in practice — pointing `AGENT_PROVIDER` at another provider will not
route it.

> [!WARNING]
> **`antigravity` runs with the operator's real `HOME`.** That is deliberate, so
> cached OAuth/ADC credentials keep working without a re-login, but it means the
> agent under test inherits read access to everything in that home directory —
> `~/.config/gcloud`, `~/.ssh`, shell history, other tools' tokens. Every other
> harness gets an isolated per-run state directory. Run untrusted agents under a
> dedicated account or an isolated `HOME`, and treat any credential reachable
> from that home as exposed to the agent.

For everything about providers, model ids, and how `get_model` resolves them, see
[Model providers](./model_providers.md).

## Configuring a harness for an eval

Configuration is env-driven. The benchmark reads neutral `AGENT_*` variables and
each harness maps them onto its target.

**Selecting the harness**

| Variable | Default | Notes |
| --- | --- | --- |
| `BENCH_AGENT_TYPE` | `gemini-cli` (resolves to `gemini`) | The canonical key or an alias. The `--agent-type` flag overrides it. |

**Agent config**

| Variable | Default | Notes |
| --- | --- | --- |
| `AGENT_MODEL` | unset | Model id; flows to the harness's target. See [agy model ids](#agy-model-ids) for the one harness that rewrites it. |
| `AGENT_MODEL_EFFORT` | `high` | Reasoning tier for `antigravity`; ignored by every other harness. One of `low`, `medium`, `high` — an unknown value is rejected rather than passed through. |
| `AGENT_PROVIDER` | unset | Provider key (e.g. `gemini`, `anthropic`, `google-vertex`). |
| `AGENT_API_KEY` | unset | Routed onto the provider's key env var(s) via the shared contract; omitted for keyless backends (Vertex/Bedrock ADC). |
| `AGENT_TARGET` | unset | Path to the CLI binary (`gemini` / `oc`). Ignored by `api`. |
| `AGENT_TIMEOUT_SEC` | `600` | Wall-clock budget for each external call. |
| `AGENT_MAX_TURNS` | harness default (50 for `api`) | Caps the `api` tool-use loop. |

**Capabilities**

| Variable | Default | Notes |
| --- | --- | --- |
| `BENCH_USE_MCP` | `true` | Master gate. `false` drops the MCP binding entirely. |
| `AGENT_MCP_SERVER` | unset | Shell-quoted argv for the MCP server (e.g. `"uv run k8s-mcp"`). |
| `AGENT_ALLOWED_TOOLS` | unset | CSV of pre-approved tool names. |
| `AGENT_SKILLS_PATHS` | unset | CSV of directories to discover `SKILL.md` files under. |
| `AGENT_RULES_TEXT` | unset | Operator-brief text handed to the agent. |

### agy model ids

`AGENT_MODEL` is a single value shared by every arm and by the judge, and it is
normally spelled for Vertex — `gemini-3.1-pro-preview`. The `antigravity`
harness is the one exception: `agy` does not recognise the `-preview` suffix,
and it refuses any selection that does not name a reasoning tier exactly once.
Left alone it exits with `invalid model selection` before the run starts.

So the harness rewrites the id rather than requiring the matrix to respell it —
respelling `AGENT_MODEL` for that one arm would desynchronise its label from
every other arm in the same run. `google/gemini-3.1-pro-preview` becomes
`--model gemini-3.1-pro --effort high`, and the rewrite is logged.

The tier is a scoring variable, not a formatting detail: `low` and `high` are
materially different agents. `high` is the default because the other harnesses
run their model with no reasoning throttle. Override it with
`AGENT_MODEL_EFFORT`.

A tier already spelled into `AGENT_MODEL` is honoured and nothing is added —
both `gemini-3.1-pro-low` and the display-name form `Gemini 3.1 Pro (Low)`
work. In that case `AGENT_MODEL_EFFORT` is ignored and no `--effort` is passed,
because `agy` rejects a tiered id and the flag together.

### Example: gemini CLI with MCP + skills

```bash
export BENCH_AGENT_TYPE=gemini
export AGENT_PROVIDER=gemini
export AGENT_MODEL=gemini-2.5-pro
export AGENT_API_KEY="$GEMINI_API_KEY"
export AGENT_TARGET=gemini

export BENCH_USE_MCP=true
export AGENT_MCP_SERVER="uv run k8s-mcp"
export AGENT_ALLOWED_TOOLS="list_clusters,get_pods"
export AGENT_SKILLS_PATHS="/opt/skills/devops,/opt/skills/k8s"
```

### Example: api harness on Claude with MCP off

```bash
export BENCH_AGENT_TYPE=api
export AGENT_PROVIDER=anthropic
export AGENT_MODEL=claude-sonnet-4-5
export AGENT_API_KEY="$ANTHROPIC_API_KEY"

export BENCH_USE_MCP=false      # no MCP server is spawned; tools are dropped
```

## Capabilities

MCP tools, skills, and rules are the three augmentation axes, and they are
independent — an agent may run with any combination, or none. Each is expressed
as a structural Protocol (`SupportsMcp`, `SupportsSkills`, `SupportsRules` in
`devops_bench/agents/capabilities/`): a harness satisfies a Protocol simply by
assigning the matching binding attribute. **MCP** wires the agent to a tool
server, **skills** drop `SKILL.md` files the agent can discover, and **rules**
supply an operator brief. Setting `BENCH_USE_MCP=false` drops the MCP binding
entirely, so the agent sees no tools and the scorer agrees that none ran — skills
and rules are unaffected.

## Sandboxing

Opt-in, off by default. With `BENCH_AGENT_SANDBOX=docker` the agent runs inside
a container (`BENCH_SANDBOX_IMAGE`) that sees the run workspace, the task's
seeded fixtures, a generated kubeconfig, and an explicit env overlay — and not
the repo checkout, `results/`, your `$HOME`, gcloud config, Terraform state, or
the Docker socket. With the switch unset the harness behaves exactly as it did
before the sandbox existed. The design and the incidents behind it are in
`docs/proposals/agent-sandboxing.md`.

### The cluster credential

The agent does **not** get your kubeconfig. Before it starts, the harness
creates a `bench-agent` ServiceAccount in the `bench-system` namespace, binds it
to the built-in `edit` role cluster-wide plus a small cluster-scoped supplement
(namespaces CRUD; nodes, PVs, storage classes, CRDs read-only), mints a
short-lived token for it, and renders a single-cluster kubeconfig containing
that token and nothing else. The RBAC deliberately grants no write on
`rbac.authorization.k8s.io` or `admissionregistration.k8s.io`, so the agent can
neither escalate its own permissions nor remove the admission policy below.

Two consequences worth knowing:

- **GKE works in-container because of this.** A normal GKE kubeconfig
  authenticates through `gke-gcloud-auth-plugin`, which needs a `gcloud` binary
  and Application Default Credentials — neither of which the container has, by
  design. A bearer token needs no plugin.
- **Under vcluster the ServiceAccount lives in the virtual cluster**, because
  every call is pinned to the run's own kubectl context. Its token is
  cryptographically useless against the host cluster.

The token's lifetime is the agent's `timeout_sec` plus 15 minutes of slack,
capped at two hours. If a scoped credential cannot be minted — or if pod
security below cannot be applied — the run **fails**; it never falls back to
your admin credential silently. For local development against a cluster where
you cannot create cluster-scoped objects, set
`BENCH_SANDBOX_ALLOW_ADMIN_CREDS=1` to allow the old behaviour explicitly. Never
use it for a scored run.

**On GKE this is an IAM question, not a Kubernetes RBAC one.** GKE gates the
admission-policy resources behind the `container.thirdPartyObjects.*`
permissions, which `roles/container.developer` does not carry, so an operator
who can otherwise deploy freely still cannot apply the policy below — and the
run refuses. Grant `roles/container.admin`, or a custom role including those
permissions, to whatever identity runs the harness. A vcluster run is
unaffected: the policy is applied inside the virtual cluster, where the
generated kubeconfig is already admin.

Provisioning also refuses a cluster that no provider vouched for. With the no-op
deployer (`BENCH_NO_INFRA`) there is no context to pin to, so "the cluster"
would be whatever your kubeconfig last pointed at — and this step writes a
cluster-wide admission policy and `ClusterRoleBindings`. Set
`BENCH_SANDBOX_ALLOW_AMBIENT_CLUSTER=1` if that is genuinely what you want.

### Pod security

Overlapping controls, applied at the same point:

1. **PSA `baseline` labels** on every namespace that exists when the agent
   starts. Namespaces that already declare an `enforce` level are left alone, so
   a task asserting its own level keeps it.
2. **A `ValidatingAdmissionPolicy`** denying `hostNetwork` / `hostPID` /
   `hostIPC`, privileged containers (including init and ephemeral ones), and
   `hostPath` volumes — cluster-wide, in `Deny` mode, failing closed, with the
   cluster's own system namespaces exempted. It matches
   `pods/ephemeralcontainers` as well as `pods`, so `kubectl debug` cannot
   attach a privileged container to an already-admitted pod.
3. **A second policy guarding the exemption list**, denying the agent the
   creation of a namespace named after one of the exempt namespaces, or the
   labelling of any namespace as the cluster's own. The exemptions are by name
   and several do not exist on every provider (`gmp-system` on kind,
   `local-path-storage` on GKE), so without this the agent could simply claim
   an unused one and deploy there. It covers `UPDATE` as well as `CREATE`,
   because a name is immutable but a label is not.
4. **A third policy denying the agent workloads inside the exempt namespaces.**
   Guarding the names only protects the ones that do not exist yet. `edit` is
   bound cluster-wide, so the agent can write to `kube-system` on every
   provider, and policy 2 deliberately skips it — which made
   `kubectl run --privileged -n kube-system` an admitted request on a cluster
   carrying the full set. A boundary probe caught this; it is not hypothetical.
   The policy matches every kind that can produce a pod, not `pods` alone, since
   a `Deployment` reaches the same place with its pod created by the ReplicaSet
   controller. It matches `pods/exec`, `pods/attach` and `pods/portforward` too,
   because `edit` grants exec and these are precisely the namespaces whose pods
   are legitimately privileged. It does not touch config: a ConfigMap in
   `kube-system` is a blast-radius question rather than an escape.
5. **A fourth policy denying the agent a shell into pods that predate all of
   the above.** Admission only sees requests, so nothing here retracts a pod
   that already exists — and the deployer runs *before* credentials are
   provisioned. Some fixtures deploy privileged workloads on purpose:
   `opa-remediation` ships two, because remediating them is the task. Those pods
   stay, and `edit` grants `pods/exec` cluster-wide, so a shell into one is node
   root by a route policy 2 never sees. Before the agent starts, the harness
   scans every namespace for pods policy 2 would have rejected — skipping the
   ones policy 4 already covers — and renders their `namespace/name` into a
   policy denying the agent `exec`, `attach` and `port-forward` into exactly
   those. They are named individually rather than matched on a property because
   admission cannot see the target pod's spec on a `CONNECT`: the object on an
   exec request is a `PodExecOptions`, so a name list is the only thing there is
   to test. The list stays correct for the run — policy 2 denies these pods on
   `CREATE`, so a name that leaves it cannot come back. It is applied even when
   the list is empty, since nothing here is torn down and a reused cluster would
   otherwise keep the previous run's list.

Policies 3, 4 and 5 are scoped to the agent's own username, so the cluster's own
components keep running — the exemption exists for kube-proxy and the CNI, not
for whoever asks. That scoping is sound for these three and would not be for
policy 2: a namespace or a `Deployment` is always created by whoever asked,
whereas a pod is often created on the agent's behalf by a controller running
under an identity of its own.

The exemption has two halves: the names above, and any namespace carrying
`addonmanager.kubernetes.io/mode`. A name list alone goes stale — a plain GKE
run turned up four managed namespaces it had never heard of
(`gke-managed-cim`, `gke-managed-networking-dra-driver`,
`gke-managed-volumepopulator`, `gmp-public`), all created by the same addon
manager as the two that were listed. Nothing broke there, since three were
empty and the fourth runs an unprivileged metrics scraper, but a cluster using
DRA or TPUs runs a privileged `hostPath` DaemonSet in one of them and a
fail-closed policy would have denied it. The label is the cluster declaring
which namespaces are its own to run, so it covers managed namespaces that do
not exist yet. The names are still needed: nothing carries that label on kind
or vcluster, and on GKE `kube-system` itself does not.

The first policy exists because labels cannot cover a namespace the agent
creates *after* provisioning, and at least one task asks it to create one.
`bench-system` is skipped by the labeller — it holds only a ServiceAccount — but
is deliberately **not** exempt from the policy, since the agent can create pods
there. Together these deny the privileged-pod-plus-`hostPath` escape that was
used to read the benchmark's own answer key off a node's disk.

**The cluster must be Kubernetes 1.30 or newer.** Four of the five controls are
`ValidatingAdmissionPolicy` objects, and `admissionregistration.k8s.io/v1` only
reached GA in 1.30 — 1.29 serves `v1beta1`, behind a feature gate. The harness
checks for the `v1` resource before it applies anything and refuses by name if
it is missing, rather than letting the apply fail with kubectl's `no matches for
kind`, which reads like a typo in our own manifest. This is deliberately *not*
routed through `BENCH_SANDBOX_ALLOW_ADMIN_CREDS`: that hatch is for an operator
whose credential cannot write cluster-scoped objects, and no credential makes a
1.29 apiserver serve a v1 policy. For kind, the floor is why `node_image`
defaults to a digest-pinned `v1.30.0`; lowering it breaks every sandboxed run.

A task whose subject matter genuinely is privileged workloads opts out with
`agent_pod_security: privileged` in its `task.yaml` (see
[Add a task](../how-to/add-a-task.md)). The default is `baseline`, and any other
value is a load-time validation error rather than a silent fall-back.

**None of this is torn down.** `bench-system`, the ClusterRoleBindings, the
admission policies and the PSA labels outlive the run. On a disposable cluster that is
irrelevant; on a reused one it means a second run finds most namespaces already
labelled and skips them, which is correct but makes the labeller look inert.
Read the labels as the state of the cluster, not as the output of the run that
is in front of you.

### Known gaps in the RBAC scope

Three are open, all in how the agent's RBAC is scoped rather than in the
pod-security controls above. None is reachable without a working agent
credential, and all are fixed by replacing the built-in role with a derived one
and narrowing the supplement:

- **`edit` bound cluster-wide reaches the system namespaces.** The built-in role
  carries `impersonate` on `serviceaccounts` and `create` on
  `serviceaccounts/token`, so a cluster-wide binding lets the agent mint a token
  for, or impersonate, any ServiceAccount in `kube-system` — including ones bound
  to `cluster-admin`. That is a path to cluster-admin, and it defeats the
  deliberate omission of write on `rbac.authorization.k8s.io`. RBAC has no deny
  rule, so nothing in the supplement can subtract this; only replacing `edit`
  with a derived role closes it. The neighbouring exec route is closed —
  policy 4 above denies `pods/exec` in exactly those namespaces — but that is a
  patch over one exit, not a fix for the scope.
- **`edit` carries read and write on `secrets`.** The built-in role includes
  them, unlike `view`, which excludes them deliberately; bound cluster-wide that
  reaches every namespace. Nothing in the supplement adds this — it is inherited,
  and it is not narrowed anywhere. What the exposure is worth depends on the run:
  the cluster is disposable and its workloads are synthetic, so ordinarily this
  leaks fixture data. It matters for a task that seeds a real credential into a
  Secret, and for the controller and syncer Secrets a task did not author. Under
  vcluster it stops at the virtual cluster; the host cluster's Secrets are not
  reachable with this token.
- **The supplement grants `update`/`patch` on namespaces**, so the agent can
  strip the `pod-security.kubernetes.io/*` labels the harness just applied. The
  admission policies are unaffected — the agent has no write on
  `admissionregistration.k8s.io` — so this removes the PSA half of the
  enforcement, not the load-bearing half. It is a capability the agent holds
  rather than one it has been seen to use: on a GKE run the namespace labels
  were byte-identical before and after.

Until these are closed, treat the pod-security controls as the boundary and the
RBAC scope as best-effort. Narrowing the role is not a blind edit: the tasks were
authored against admin, so a scope that is too tight fails them in ways that read
as agent error. An A/B soak of sandboxed against ambient runs is what shows which
tasks need which verbs.

### The scope is already too tight for custom resources

`edit` covers Kubernetes' own API groups. It does not cover the CRDs an operator
installs, and the supplement grants only `get`/`list`/`watch` on
`customresourcedefinitions` — the definitions, not the objects. What the agent
gets on a given CRD is therefore whatever that operator chose to aggregate into
`edit`, which is usually nothing.

A task whose objective writes an operator's CRD must grant that in its own
stack. Kyverno, for example, aggregates its policy roles into `view` and
`admin` only, so `opa-remediation` applies a `kyverno-policy-editor`
ClusterRole labelled `aggregate-to-edit` with `update`/`patch` on `kyverno.io`
policies (`tf/prebuilt/opa-remediation/manifests/rbac/`). Grant the task's
minimum there rather than widening the harness supplement for every task; an
objective the scope cannot reach fails silently, since agents rarely attempt a
write they expect to be denied.

### Model credentials

A key-based provider needs nothing special: the key is in the resolved overlay
and crosses the boundary by value like any other variable.

A **keyless** backend does not have that luxury. The sandbox strips
`CLOUDSDK_CONFIG` and `GOOGLE_APPLICATION_CREDENTIALS`, and on the bastion the
link-local metadata endpoint is blocked for containers (see
[infrastructure](infra.md)) — and Application Default Credentials is exactly
that chain. So each keyless backend needs its own *mint-and-inject recipe*: mint
a narrow, short-lived credential host-side, inject it explicitly. The recipes
live in `core/model_providers.py` (`sandbox_credential_env`), keyed off the
provider's backend, never in the sandbox module — cloud-specific code stays out
of the boundary code.

#### Vertex: a metadata-server emulator

Vertex's recipe is a small HTTP server the harness runs on the host, speaking
the subset of the GCE metadata protocol Google's auth libraries use to obtain a
token. The container is pointed at it with `GCE_METADATA_HOST`,
`GCE_METADATA_IP` and `METADATA_SERVER_DETECTION=assume-present`, reaching it at
`host.docker.internal` (the executor `--add-host`s that to the host gateway on
every run). The token behind it is minted by impersonating a service account
that holds `roles/aiplatform.user` and nothing else, refilled in place, and
never minted at all if the run does not call the model.

The route table is fixed and closed: the residency ping, `project/project-id`,
`universe/universe-domain`, and the service-account subtree (the account
listing, plus `email`, `scopes`, `aliases`, `token` and the recursive listing,
under both the `default` alias and the account's own email). Everything else is
a 404 — including `instance/attributes/`, which on a real GKE node carries
`kube-env`; the instance identity paths (`id`, `zone`, `hostname`, `disks`,
`network-interfaces`); any account other than the impersonated one; and the
`identity?audience=` OIDC-JWT issuer. So this is an emulator of an enumerated
set of paths, not a proxy onto the host's real metadata server: a container that
probes it learns nothing about the host it is running on. Requests must carry
`Metadata-Flavor: Google`, as the real server requires.

Two things have to be set up before a sandboxed Vertex run:

| Variable | Meaning |
| --- | --- |
| `BENCH_VERTEX_SANDBOX_SA` | The service account to impersonate. Give it `roles/aiplatform.user` on the project and nothing else. |
| `GOOGLE_CLOUD_PROJECT` (or `GCP_PROJECT`) | The project the run bills to; the emulator serves it to the SDK. |

plus `roles/iam.serviceAccountTokenCreator` for the *host's own* identity on
that service account. Anything missing fails loud — a sandboxed run never
degrades to an unsandboxed one to get a credential.

Bedrock has no recipe yet; a sandboxed `anthropic-bedrock` run is refused with
an error saying so. Ambient, unsandboxed runs of either are unaffected: they are
host processes with the operator's own ADC.

> [!IMPORTANT]
> **`AGENT_API_KEY` is the only variable the host reads the key from**
> (`agents/config.py`). The provider's own names — `GEMINI_API_KEY`,
> `GOOGLE_API_KEY` — are where the key is *written to* inside the container, not
> where it is read from outside. Unsandboxed the distinction never shows: the
> agent subprocess inherits the whole host environment and finds
> `GEMINI_API_KEY` by itself. Sandboxed, only the explicit overlay crosses, so a
> host-side `GEMINI_API_KEY` is simply absent and the CLI exits reporting that no
> auth method is set — naming the very variable you exported. Export
> `AGENT_API_KEY` and the sandboxed run routes it onward for you.

### Task cloud credentials

Some tasks require cloud API calls beyond `kubectl` — secret-rotation adds a
Secret Manager secret version. The sandbox strips the operator's ambient cloud
identity, so those calls get their own: the task's stack provisions a
run-unique service account holding exactly the roles the task needs, scoped to
exactly the resources it provisioned, and names it in an `agent_cloud_identity`
output. When that output is present on a sandboxed run, the harness
impersonates the account host-side, mints a short-lived access token, and
injects it as `CLOUDSDK_AUTH_ACCESS_TOKEN` / `GOOGLE_OAUTH_ACCESS_TOKEN` (plus
the project id). No key file exists, the operator's ADC never crosses, and the
whole loop — account, role bindings, the provisioner's
`serviceAccountTokenCreator` grant on it — tears down with the run's stack.

Two properties to keep in mind. The token lives at most one hour and is not
refreshed inside the container: an agent still making cloud calls past that
gets a clean 401, not a silent widening. And a mint failure fails the run
loudly — the alternative is an agent graded on a failure that was really a
missing credential. Tasks that declare no `agent_cloud_identity` output are
completely unaffected; nothing extra crosses for them.

## Adding your own harness

Want to wrap a different agent? See
[Add an agent harness](../how-to/add-an-agent-harness.md).

### Concurrent sandbox runs and external user IDs

A caller may set `BENCH_AGENT_SANDBOX_OWNER` to a unique alphanumeric attempt ID
(underscores allowed). Agent container names then include that ID; startup
recovery only reaps containers belonging to the same owner. Never reuse an owner
for concurrent attempts. Without an owner, startup recovery is a no-op; legacy
orphans require explicit operator recovery.

For host IDs above Docker's signed 32-bit limit, the sandbox runs as 1000:1000.
Ownership remapping covers the workspace, fixture mounts and the generated
single-cluster kubeconfig, then restores the host ownership after execution.
The agent's kubeconfig mount remains read-only. Provider authentication uses
explicit overlays; host credential files are not copied into the sandbox.


OpenClaw's per-run catalog also registers `gemini-3.8-flash` with the `google`
or `google-vertex` provider, and `claude-fable-5-1` with `anthropic-vertex`.
Select them with `AGENT_MODEL` and `AGENT_PROVIDER`; the harness pins the matching
transport in the isolated run configuration. Model IDs follow the
[Gemini documentation](https://ai.google.dev/gemini-api/docs/latest-model) and
[Claude on Vertex documentation](https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/partner-models/claude/fable-5-1).
