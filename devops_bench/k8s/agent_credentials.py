# Copyright 2026 The Kubernetes Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Mint the scoped cluster credential a sandboxed agent is given.

The sandbox's first iteration mounted the operator's own kubeconfig
credential, which on every provider this benchmark uses is cluster-admin. The
container boundary was therefore doing all the work and the RBAC boundary
none: an agent that got a shell out of the container — or simply used the
credential as intended — held the whole cluster. This module replaces that
with a ServiceAccount token minted for the run: scoped by RBAC, short-lived,
and expired by the time anyone could reuse it.

It is also what makes GKE reachable from inside a container at all. A GKE
kubeconfig authenticates through ``gke-gcloud-auth-plugin``, an ``exec:``
credential plugin needing a ``gcloud`` binary and Application Default
Credentials — both of which the container deliberately lacks, and neither of
which it can be given without handing back the cloud identity the sandbox
exists to withhold. A bearer token needs no plugin, so the rendered kubeconfig
is self-contained.

Everything here runs HOST-SIDE, under the operator's credentials, before the
agent starts. Every call is pinned to the run's own kubectl context, which is
what puts the identity in the right cluster: under vcluster the host and
virtual clusters both appear in the operator's kubeconfig, and creating the
ServiceAccount in the virtual one is what makes its token cryptographically
useless against the host.

Review rule (see ``docs/proposals/agent-sandboxing.md``): no cloud CLI —
``gcloud``, ``aws``, ``az`` — is invoked anywhere in this module. Everything
it does is plain Kubernetes API surface reached through ``kubectl``, so it
behaves identically on every provider and adds no cloud dependency to the
credential path.
"""

from __future__ import annotations

from pathlib import Path

from devops_bench.core import NetworkPlan, SandboxError, SubprocessError, get_bool, get_logger
from devops_bench.k8s import kubectl

__all__ = [
    "AGENT_NAMESPACE",
    "AGENT_SA_NAME",
    "ALLOW_ADMIN_ENV",
    "POD_SECURITY_BASELINE",
    "POD_SECURITY_PRIVILEGED",
    "ensure_agent_identity",
    "enforce_pod_security",
    "mint_agent_token",
    "provision_agent_credentials",
    "render_agent_kubeconfig",
    "token_ttl_for",
]

_log = get_logger("k8s.agent_credentials")

# The agent's identity. It gets its own namespace so the ServiceAccount is not
# mistaken for part of a task's workload, and so a task that deletes its own
# namespace cannot delete the credential out from under the running agent.
AGENT_NAMESPACE = "bench-system"
AGENT_SA_NAME = "bench-agent"

# Escape hatch back to the previous behaviour: reuse the operator's admin
# certificate when a scoped credential cannot be minted. Opt-in and loudly
# warned, because it gives up the RBAC boundary entirely. Being ``BENCH_``
# prefixed it is also on the sandbox's env deny list, so it cannot itself
# reach the container.
ALLOW_ADMIN_ENV = "BENCH_SANDBOX_ALLOW_ADMIN_CREDS"

# Slack added to the agent's own timeout so its token outlasts the work it is
# for, covering provisioning, teardown, and clock skew against the apiserver.
TOKEN_TTL_SLACK_SEC = 900

# Pod-security levels a task may declare via ``agent_pod_security:``.
POD_SECURITY_BASELINE = "baseline"
POD_SECURITY_PRIVILEGED = "privileged"

# Namespaces left alone by both halves of the pod-security enforcement. These
# hold the cluster's own control-plane and storage components, which
# legitimately run privileged and with host mounts; enforcing on them would
# break the cluster rather than the agent. ``bench-system`` is exempt because
# it is the harness's own, not the agent's to deploy into.
_PSA_EXEMPT_NAMESPACES = frozenset(
    {
        "kube-system",
        "kube-public",
        "kube-node-lease",
        "local-path-storage",
        "gke-managed-system",
        "gmp-system",
        "vcluster",
        AGENT_NAMESPACE,
    }
)

_PSA_ENFORCE_LABEL = "pod-security.kubernetes.io/enforce"

# The admission-policy backstop, denying the escape the proposal observed:
# a privileged pod with a hostPath mount, used to read the bench checkout off
# the node's disk.
#
# PSA labels alone cannot cover this. A label is per-namespace, applied to the
# namespaces that exist when the agent starts — but the agent can create a
# namespace afterwards (``deploy-hello-app`` literally asks it to) and that
# one carries no label. A ValidatingAdmissionPolicy is cluster-wide and
# therefore proof against namespaces that do not exist yet. It also sidesteps
# a collision: ``deploy-hello-app``'s verifier asserts ``enforce=restricted``
# on its namespace, which the labeller must not clobber.
#
# ``failurePolicy: Fail`` because a control that fails open is not a control.
# The binding exempts the system namespaces by name, so a CEL evaluation error
# can cost the agent a pod but cannot wedge the cluster's own components.
_POD_SECURITY_POLICY_MANIFEST = f"""\
apiVersion: admissionregistration.k8s.io/v1
kind: ValidatingAdmissionPolicy
metadata:
  name: bench-agent-pod-security
spec:
  failurePolicy: Fail
  matchConstraints:
    resourceRules:
      - apiGroups: [""]
        apiVersions: ["v1"]
        operations: ["CREATE", "UPDATE"]
        resources: ["pods"]
  validations:
    - expression: "!has(object.spec.hostNetwork) || !object.spec.hostNetwork"
      message: "hostNetwork is not allowed for benchmark workloads"
    - expression: "!has(object.spec.hostPID) || !object.spec.hostPID"
      message: "hostPID is not allowed for benchmark workloads"
    - expression: "!has(object.spec.hostIPC) || !object.spec.hostIPC"
      message: "hostIPC is not allowed for benchmark workloads"
    - expression: >-
        !has(object.spec.volumes) ||
        object.spec.volumes.all(v, !has(v.hostPath))
      message: "hostPath volumes are not allowed for benchmark workloads"
    - expression: >-
        object.spec.containers.all(c,
          !has(c.securityContext) ||
          !has(c.securityContext.privileged) ||
          !c.securityContext.privileged)
      message: "privileged containers are not allowed for benchmark workloads"
    - expression: >-
        !has(object.spec.initContainers) ||
        object.spec.initContainers.all(c,
          !has(c.securityContext) ||
          !has(c.securityContext.privileged) ||
          !c.securityContext.privileged)
      message: "privileged init containers are not allowed for benchmark workloads"
    - expression: >-
        !has(object.spec.ephemeralContainers) ||
        object.spec.ephemeralContainers.all(c,
          !has(c.securityContext) ||
          !has(c.securityContext.privileged) ||
          !c.securityContext.privileged)
      message: "privileged ephemeral containers are not allowed for benchmark workloads"
---
apiVersion: admissionregistration.k8s.io/v1
kind: ValidatingAdmissionPolicyBinding
metadata:
  name: bench-agent-pod-security
spec:
  policyName: bench-agent-pod-security
  validationActions: ["Deny"]
  matchResources:
    namespaceSelector:
      matchExpressions:
        - key: kubernetes.io/metadata.name
          operator: NotIn
          values: [{", ".join(sorted(_PSA_EXEMPT_NAMESPACES))}]
"""

# Ceiling on the lifetime, so a long or unbounded run cannot mint a credential
# that outlives it by hours. There is no matching floor: the slack above is
# already the minimum any run gets. The apiserver may shorten the result
# further, which is fine — a shorter token is never a security problem.
_MAX_TOKEN_TTL_SEC = 7200

# The agent's permissions, as one applyable document.
#
# Built-in ``edit`` bound cluster-wide is the baseline: it is Kubernetes' own
# role for "change workloads, but not permissions", which is what the tasks
# ask for. What it omits is cluster-scoped resources, hence the supplement —
# an agent that cannot create a namespace or read nodes fails ordinary tasks,
# and an agent that fails an ordinary task goes looking for another way, which
# is how the proposal's first observed incident started.
#
# Two omissions are deliberate and load-bearing:
#
# * no write on ``rbac.authorization.k8s.io``, so the agent cannot grant
#   itself anything beyond this. Without that, every other limit is advisory.
# * no write on ``admissionregistration.k8s.io``, so it cannot remove the
#   admission policy that denies privileged pods.
_RBAC_MANIFEST = f"""\
apiVersion: v1
kind: Namespace
metadata:
  name: {AGENT_NAMESPACE}
---
apiVersion: v1
kind: ServiceAccount
metadata:
  name: {AGENT_SA_NAME}
  namespace: {AGENT_NAMESPACE}
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRoleBinding
metadata:
  name: {AGENT_SA_NAME}-edit
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: ClusterRole
  name: edit
subjects:
  - kind: ServiceAccount
    name: {AGENT_SA_NAME}
    namespace: {AGENT_NAMESPACE}
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata:
  name: {AGENT_SA_NAME}-cluster-supplement
rules:
  - apiGroups: [""]
    resources: ["namespaces"]
    verbs: ["get", "list", "watch", "create", "update", "patch", "delete"]
  - apiGroups: [""]
    resources: ["nodes", "persistentvolumes"]
    verbs: ["get", "list", "watch"]
  - apiGroups: ["storage.k8s.io"]
    resources: ["storageclasses", "csidrivers", "csinodes", "volumeattachments"]
    verbs: ["get", "list", "watch"]
  - apiGroups: ["apiextensions.k8s.io"]
    resources: ["customresourcedefinitions"]
    verbs: ["get", "list", "watch"]
  - apiGroups: ["apiregistration.k8s.io"]
    resources: ["apiservices"]
    verbs: ["get", "list", "watch"]
  - apiGroups: ["metrics.k8s.io"]
    resources: ["nodes", "pods"]
    verbs: ["get", "list"]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRoleBinding
metadata:
  name: {AGENT_SA_NAME}-cluster-supplement
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: ClusterRole
  name: {AGENT_SA_NAME}-cluster-supplement
subjects:
  - kind: ServiceAccount
    name: {AGENT_SA_NAME}
    namespace: {AGENT_NAMESPACE}
"""


def token_ttl_for(agent_timeout_sec: float | None) -> int:
    """Choose a token lifetime for an agent running under this timeout.

    Args:
        agent_timeout_sec: The agent's wall-clock budget, or ``None`` when it
            runs unbounded — which is exactly when the ceiling matters.

    Returns:
        The lifetime to request, in seconds: the timeout plus
        :data:`TOKEN_TTL_SLACK_SEC`, capped at two hours.
    """
    if agent_timeout_sec is None:
        _log.info(
            "agent runs without a timeout; capping its cluster token at %ds", _MAX_TOKEN_TTL_SEC
        )
        return _MAX_TOKEN_TTL_SEC
    requested = int(agent_timeout_sec) + TOKEN_TTL_SLACK_SEC
    if requested > _MAX_TOKEN_TTL_SEC:
        _log.info(
            "capping the agent cluster token lifetime at %ds (%ds requested)",
            _MAX_TOKEN_TTL_SEC,
            requested,
        )
        return _MAX_TOKEN_TTL_SEC
    return requested


def ensure_agent_identity(work_dir: Path, context: str | None = None) -> None:
    """Create or update the agent's ServiceAccount and the RBAC that scopes it.

    Idempotent by ``kubectl apply``, so this is safe to call once per task
    without tracking whether an earlier task on the same cluster already did
    it, and a manifest change takes effect on the next run.

    Args:
        work_dir: Directory to render the manifest into before applying. Must
            not itself be mounted into the container; the harness's
            credentials directory qualifies, since only the kubeconfig file
            within it is bind-mounted.
        context: kubectl context to pin the apply to. ``None`` uses the
            ambient current-context.

    Raises:
        SubprocessError: If the apply fails — most often because the operator
            cannot create cluster roles, or the apiserver is unreachable.
    """
    manifest = work_dir / "bench-agent-rbac.yaml"
    manifest.write_text(_RBAC_MANIFEST)
    kubectl.apply(str(manifest), context=context)
    _log.info(
        "ensured the sandboxed agent identity %s/%s (edit, plus a cluster-scoped supplement)",
        AGENT_NAMESPACE,
        AGENT_SA_NAME,
    )


def enforce_pod_security(work_dir: Path, context: str | None = None) -> None:
    """Deny privileged pods, host namespaces, and hostPath mounts cluster-wide.

    Two halves, because neither alone is enough. PSA ``baseline`` labels go on
    every namespace that exists now, which is the mechanism Kubernetes ships
    and the one an operator can read off a namespace; a
    ValidatingAdmissionPolicy backs them up cluster-wide, covering namespaces
    the agent creates *after* this runs — a label cannot, and one of the tasks
    asks the agent to create a namespace.

    Namespaces that already carry an ``enforce`` label are left alone: a task
    may assert a specific level as part of its own verification (one asserts
    ``restricted``), and overwriting it would fail the task this control is
    supposed to protect.

    Args:
        work_dir: Directory to render the policy manifest into before
            applying. Must not itself be mounted into the container.
        context: kubectl context to pin every call to.

    Raises:
        SubprocessError: If the policy cannot be applied. Namespace labelling
            failures are warned and skipped — the policy is the load-bearing
            half, and one unlabellable namespace must not fail the run.
    """
    manifest = work_dir / "bench-agent-pod-security.yaml"
    manifest.write_text(_POD_SECURITY_POLICY_MANIFEST)
    kubectl.apply(str(manifest), context=context)

    for name in _labellable_namespaces(context):
        try:
            kubectl.label(
                "namespace",
                name,
                {
                    _PSA_ENFORCE_LABEL: POD_SECURITY_BASELINE,
                    "pod-security.kubernetes.io/warn": POD_SECURITY_BASELINE,
                    "pod-security.kubernetes.io/audit": POD_SECURITY_BASELINE,
                },
                overwrite=True,
                context=context,
            )
        except SubprocessError as exc:
            _log.warning("could not label namespace %s for pod security: %s", name, exc)
    _log.info("pod security enforced: baseline labels plus the cluster-wide admission policy")


def _labellable_namespaces(context: str | None) -> list[str]:
    """List the namespaces this run should label, skipping the ones it must not.

    Skips the system namespaces outright, and any namespace that already
    declares an ``enforce`` level — that value belongs to whoever set it.
    """
    try:
        listing = kubectl.get_resource("namespaces", context=context, timeout=60)
    except SubprocessError as exc:
        _log.warning("could not list namespaces for pod-security labelling: %s", exc)
        return []
    names = []
    for item in listing.get("items", []):
        meta = item.get("metadata", {})
        name = meta.get("name", "")
        if not name or name in _PSA_EXEMPT_NAMESPACES:
            continue
        if meta.get("labels", {}).get(_PSA_ENFORCE_LABEL):
            _log.debug("namespace %s already declares a pod-security level; leaving it", name)
            continue
        names.append(name)
    return names


def mint_agent_token(ttl_sec: int, context: str | None = None) -> str:
    """Mint a short-lived bearer token for the agent's ServiceAccount.

    Args:
        ttl_sec: Requested lifetime, already chosen by :func:`token_ttl_for`.
        context: kubectl context to pin the request to.

    Returns:
        The bearer token.

    Raises:
        SubprocessError: If the mint fails.
    """
    return kubectl.create_token(
        AGENT_SA_NAME,
        namespace=AGENT_NAMESPACE,
        duration_sec=ttl_sec,
        context=context,
    )


def render_agent_kubeconfig(plan: NetworkPlan, dest_dir: Path, *, user_fields: str) -> Path:
    """Write the single-cluster kubeconfig the container gets, and return its path.

    Exactly one cluster, one user, one context: the agent cannot switch to
    another cluster the operator's kubeconfig happens to know about. And no
    ``exec:`` block, so nothing in the file can invoke a credential plugin
    that would need the cloud identity the container deliberately lacks.

    Every read is pinned to ``plan.kubectl_context`` when the plan carries
    one, so the rendered CA and server belong to the run's own cluster even if
    the ambient current-context was switched after provisioning — by an
    operator mid-run, or by a parallel harness's ``up()``.

    Args:
        plan: The run's network plan. ``rewrite_server`` replaces the
            context's server URL and ``tls_server_name`` is rendered when set.
        dest_dir: Directory to write into. Callers must keep it OUTSIDE the
            workspace, otherwise the credential would also surface read-write
            under ``/workspace``.
        user_fields: Rendered inline-YAML body of the ``user:`` block, e.g.
            ``"token: <jwt>"``.

    Returns:
        Path of the written kubeconfig (mode 0600).

    Raises:
        SandboxError: When the context carries no CA or no server URL —
            refusing beats handing the container a kubeconfig that cannot
            authenticate.
    """
    ctx = plan.kubectl_context
    ca = kubectl.config_value("{.clusters[0].cluster.certificate-authority-data}", context=ctx)
    if not ca:
        raise SandboxError("could not read the cluster CA from the run's kubectl context")

    server = plan.rewrite_server or kubectl.config_value(
        "{.clusters[0].cluster.server}", context=ctx
    )
    if not server:
        raise SandboxError("could not read the cluster server URL from the run's kubectl context")

    cluster_fields = f"server: {server}, certificate-authority-data: {ca}"
    if plan.tls_server_name:
        cluster_fields += f", tls-server-name: {plan.tls_server_name}"
    path = dest_dir / "kubeconfig"
    path.write_text(
        "apiVersion: v1\n"
        "kind: Config\n"
        f"clusters: [{{name: c, cluster: {{{cluster_fields}}}}}]\n"
        f"users: [{{name: u, user: {{{user_fields}}}}}]\n"
        "contexts: [{name: ctx, context: {cluster: c, user: u}}]\n"
        "current-context: ctx\n"
    )
    path.chmod(0o600)
    return path


def provision_agent_credentials(
    plan: NetworkPlan,
    dest_dir: Path,
    *,
    token_ttl_sec: int,
    pod_security: str = POD_SECURITY_BASELINE,
) -> Path:
    """Seed the agent's identity and pod security, and render its kubeconfig.

    The single entry point the eval harness calls. On any failure to produce a
    scoped credential this raises rather than falling back: a run that quietly
    reverted to the operator's admin certificate would look identical in the
    results while having no RBAC boundary at all. The fallback exists only
    behind :data:`ALLOW_ADMIN_ENV`, for developing against a cluster where the
    operator cannot create cluster roles.

    Args:
        plan: The run's network plan, supplying the context pin and any server
            rewrite.
        dest_dir: Directory (outside the workspace) for the kubeconfig and the
            rendered RBAC manifest.
        token_ttl_sec: Requested token lifetime; see :func:`token_ttl_for`.
        pod_security: The task's declared ``agent_pod_security`` level.
            ``"privileged"`` skips :func:`enforce_pod_security` entirely, for
            a task whose own subject matter is privileged workloads.

    Returns:
        Path of the written kubeconfig (mode 0600).

    Raises:
        SandboxError: When no scoped credential can be minted and the admin
            fallback is not explicitly enabled.
    """
    if pod_security == POD_SECURITY_PRIVILEGED:
        _log.warning(
            "task declares agent_pod_security: %s, so privileged pods, host namespaces "
            "and hostPath mounts are NOT denied for this run",
            POD_SECURITY_PRIVILEGED,
        )
    else:
        enforce_pod_security(dest_dir, plan.kubectl_context)
    try:
        ensure_agent_identity(dest_dir, plan.kubectl_context)
        token = mint_agent_token(token_ttl_sec, plan.kubectl_context)
    except SubprocessError as exc:
        if not get_bool(ALLOW_ADMIN_ENV, False):
            raise SandboxError(
                "could not mint a scoped ServiceAccount credential for the sandboxed "
                f"agent ({exc}); refusing to fall back to the operator's admin "
                f"credential — set {ALLOW_ADMIN_ENV}=1 to allow that explicitly"
            ) from exc
        return _render_admin_fallback_kubeconfig(plan, dest_dir)
    _log.info(
        "sandboxed agent will authenticate as %s/%s with a %ds token",
        AGENT_NAMESPACE,
        AGENT_SA_NAME,
        token_ttl_sec,
    )
    return render_agent_kubeconfig(plan, dest_dir, user_fields=f"token: {token}")


def _render_admin_fallback_kubeconfig(plan: NetworkPlan, dest_dir: Path) -> Path:
    """Give the agent the operator's own client certificate instead of a token.

    The pre-scoping behaviour, kept only for local development against a
    cluster where the operator cannot create cluster roles. It gives up the
    RBAC boundary completely, so it warns every time — and it still refuses
    when the context has no static certificate to copy.

    Raises:
        SandboxError: When the context authenticates through an ``exec:``
            plugin, which has no static credential to copy and could not run
            inside the container anyway.
    """
    ctx = plan.kubectl_context
    cert = kubectl.config_value("{.users[0].user.client-certificate-data}", context=ctx)
    key = kubectl.config_value("{.users[0].user.client-key-data}", context=ctx)
    if not (cert and key):
        raise SandboxError(
            f"{ALLOW_ADMIN_ENV} is set but the run's kubectl context carries no static "
            "client certificate to fall back to; it authenticates through an exec "
            "credential plugin, which cannot run inside the container"
        )
    _log.warning(
        "%s is set: the sandboxed agent is being given the operator's admin client "
        "certificate. The container boundary is doing all the work and the RBAC "
        "boundary none. Never use this for a scored run.",
        ALLOW_ADMIN_ENV,
    )
    return render_agent_kubeconfig(
        plan,
        dest_dir,
        user_fields=f"client-certificate-data: {cert}, client-key-data: {key}",
    )
