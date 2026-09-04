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

"""Unit tests for devops_bench.k8s.agent_credentials.

The kubectl argv and the rendered YAML are the boundary this module owns, so
that is what the tests assert on — no cluster and no docker daemon needed.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from devops_bench.core import NetworkPlan
from devops_bench.core.errors import SandboxError, SubprocessError
from devops_bench.k8s import agent_credentials as creds
from devops_bench.k8s import kubectl

_CA = "ZmFrZS1jYQ=="
_TOKEN = "eyJhbGciOi.fake.token"


def _patch_kubectl(
    monkeypatch: pytest.MonkeyPatch,
    *,
    ca: str = _CA,
    server: str = "https://127.0.0.1:6443",
    cert: str = "Y2VydA==",
    key: str = "a2V5",
    token: str = _TOKEN,
    mint_fails: bool = False,
    namespaces: dict | None = None,
    calls: list[list[str]] | None = None,
) -> list[list[str]]:
    """Answer every kubectl call this module makes, recording the argv.

    Returns the list the argvs land in, so a test can assert on the exact
    command line the module would have run.
    """
    seen = calls if calls is not None else []
    namespaces = namespaces if namespaces is not None else {"items": []}
    answers = {
        "jsonpath={.clusters[0].cluster.certificate-authority-data}": ca,
        "jsonpath={.clusters[0].cluster.server}": server,
        "jsonpath={.users[0].user.client-certificate-data}": cert,
        "jsonpath={.users[0].user.client-key-data}": key,
    }

    def fake_run(argv, **kwargs):
        seen.append(argv)
        if argv[-1] in answers:
            return SimpleNamespace(returncode=0, stdout=answers[argv[-1]], stderr="")
        if "token" in argv:
            if mint_fails:
                raise SubprocessError(argv, 1, stderr="forbidden: cannot create token")
            return SimpleNamespace(returncode=0, stdout=f"{token}\n", stderr="")
        if "apply" in argv:
            if mint_fails and "bench-agent-rbac.yaml" in argv[-1]:
                raise SubprocessError(argv, 1, stderr="forbidden: cannot create clusterroles")
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if argv[1:3] == ["get", "namespaces"] or argv[3:5] == ["get", "namespaces"]:
            return SimpleNamespace(returncode=0, stdout=json.dumps(namespaces), stderr="")
        if "label" in argv:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        raise AssertionError(f"unexpected kubectl argv: {argv}")

    monkeypatch.setattr(kubectl, "run", fake_run)
    return seen


# -- the agent identity ------------------------------------------------------


def test_ensure_agent_identity_applies_the_rendered_manifest(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls = _patch_kubectl(monkeypatch)

    creds.ensure_agent_identity(tmp_path)

    manifest = tmp_path / "bench-agent-rbac.yaml"
    assert calls == [["kubectl", "apply", "-f", str(manifest)]]
    assert manifest.exists()


def test_ensure_agent_identity_pins_the_apply_to_the_runs_context(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Under vcluster the host and virtual clusters share one kubeconfig, so an
    unpinned apply could seed the agent identity in the wrong one."""
    calls = _patch_kubectl(monkeypatch)

    creds.ensure_agent_identity(tmp_path, "vcluster-c1")

    assert calls[0][:3] == ["kubectl", "--context", "vcluster-c1"]


def _rbac_docs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    _patch_kubectl(monkeypatch)
    creds.ensure_agent_identity(tmp_path)
    text = (tmp_path / "bench-agent-rbac.yaml").read_text()
    return [d for d in yaml.safe_load_all(text) if d]


def test_rbac_binds_edit_to_the_agent_service_account(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    docs = _rbac_docs(tmp_path, monkeypatch)
    by_kind = {(d["kind"], d["metadata"]["name"]): d for d in docs}

    assert ("Namespace", creds.AGENT_NAMESPACE) in by_kind
    assert ("ServiceAccount", creds.AGENT_SA_NAME) in by_kind
    binding = by_kind[("ClusterRoleBinding", f"{creds.AGENT_SA_NAME}-edit")]
    assert binding["roleRef"]["name"] == "edit"
    assert binding["subjects"] == [
        {
            "kind": "ServiceAccount",
            "name": creds.AGENT_SA_NAME,
            "namespace": creds.AGENT_NAMESPACE,
        }
    ]


def test_rbac_supplements_edit_with_the_cluster_scoped_reads_tasks_need(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``edit`` omits cluster-scoped resources, and an agent that cannot create
    a namespace or read nodes fails ordinary tasks — which is how the
    proposal's first observed incident started."""
    docs = _rbac_docs(tmp_path, monkeypatch)
    role = next(
        d
        for d in docs
        if d["kind"] == "ClusterRole"
        and d["metadata"]["name"] == f"{creds.AGENT_SA_NAME}-cluster-supplement"
    )
    granted = {
        (g, r) for rule in role["rules"] for g in rule["apiGroups"] for r in rule["resources"]
    }
    assert ("", "namespaces") in granted
    assert ("", "nodes") in granted
    assert ("storage.k8s.io", "storageclasses") in granted


@pytest.mark.parametrize(
    "forbidden_group",
    ["rbac.authorization.k8s.io", "admissionregistration.k8s.io"],
)
def test_rbac_never_grants_self_escalation_or_admission_control(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, forbidden_group: str
) -> None:
    """Without these two omissions every other limit is advisory: the agent
    could grant itself more, or delete the policy denying privileged pods."""
    docs = _rbac_docs(tmp_path, monkeypatch)
    for role in (d for d in docs if d["kind"] == "ClusterRole"):
        for rule in role["rules"]:
            assert forbidden_group not in rule["apiGroups"]


# -- token minting -----------------------------------------------------------


def test_mint_agent_token_requests_a_bounded_duration(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _patch_kubectl(monkeypatch)

    assert creds.mint_agent_token(1500, "kind-c1") == _TOKEN

    assert calls[0] == [
        "kubectl",
        "--context",
        "kind-c1",
        "create",
        "token",
        creds.AGENT_SA_NAME,
        "--duration=1500s",
        "-n",
        creds.AGENT_NAMESPACE,
    ]


@pytest.mark.parametrize(
    ("timeout_sec", "expected"),
    [
        (600.0, 1500),  # the default: timeout plus slack, under the cap
        (10.0, 910),  # the slack is what keeps a short task's token from expiring mid-run
        (30000.0, 7200),  # capped, so a long run's credential is not left lying around
        (None, 7200),  # unbounded agent: the cap is the whole point
    ],
)
def test_token_ttl_for_adds_slack_and_caps_the_lifetime(
    timeout_sec: float | None, expected: int
) -> None:
    assert creds.token_ttl_for(timeout_sec) == expected


# -- kubeconfig rendering ----------------------------------------------------


def test_render_agent_kubeconfig_emits_one_cluster_and_no_exec_block(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_kubectl(monkeypatch)
    plan = NetworkPlan(docker_network="kind", rewrite_server="https://c1-control-plane:6443")

    path = creds.render_agent_kubeconfig(plan, tmp_path, user_fields=f"token: {_TOKEN}")

    text = path.read_text()
    config = yaml.safe_load(text)
    assert len(config["clusters"]) == 1
    assert len(config["users"]) == 1
    assert len(config["contexts"]) == 1
    assert config["clusters"][0]["cluster"]["server"] == "https://c1-control-plane:6443"
    # No exec-plugin block and no ADC anywhere: the container can never be
    # asked to shell out to a cloud credential helper it does not have. This
    # is also what makes a GKE kubeconfig usable in-container at all.
    assert "exec" not in config["users"][0]["user"]
    assert "exec:" not in text
    assert "application_default" not in text


def test_render_agent_kubeconfig_is_owner_readable_only(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_kubectl(monkeypatch)
    path = creds.render_agent_kubeconfig(NetworkPlan(), tmp_path, user_fields=f"token: {_TOKEN}")
    assert (path.stat().st_mode & 0o777) == 0o600


def test_render_agent_kubeconfig_keeps_the_context_server_without_a_rewrite(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_kubectl(monkeypatch, server="https://34.1.2.3")
    path = creds.render_agent_kubeconfig(NetworkPlan(), tmp_path, user_fields="token: t")
    cluster = yaml.safe_load(path.read_text())["clusters"][0]["cluster"]
    assert cluster["server"] == "https://34.1.2.3"


def test_render_agent_kubeconfig_renders_tls_server_name_when_the_plan_sets_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_kubectl(monkeypatch)
    plan = NetworkPlan(
        rewrite_server="https://host.docker.internal:8443", tls_server_name="localhost"
    )
    path = creds.render_agent_kubeconfig(plan, tmp_path, user_fields="token: t")
    cluster = yaml.safe_load(path.read_text())["clusters"][0]["cluster"]
    assert cluster["tls-server-name"] == "localhost"


def test_render_agent_kubeconfig_pins_reads_to_the_plans_context(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The rendered CA and server must belong to the run's own cluster even if
    the ambient current-context was switched after provisioning."""
    calls = _patch_kubectl(monkeypatch)
    creds.render_agent_kubeconfig(
        NetworkPlan(kubectl_context="kind-c1"), tmp_path, user_fields="token: t"
    )
    assert calls
    for argv in calls:
        assert argv[:3] == ["kubectl", "--context", "kind-c1"]


def test_render_agent_kubeconfig_refuses_without_a_ca(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_kubectl(monkeypatch, ca="")
    with pytest.raises(SandboxError, match="CA"):
        creds.render_agent_kubeconfig(NetworkPlan(), tmp_path, user_fields="token: t")


def test_render_agent_kubeconfig_refuses_without_a_server(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_kubectl(monkeypatch, server="")
    with pytest.raises(SandboxError, match="server URL"):
        creds.render_agent_kubeconfig(NetworkPlan(), tmp_path, user_fields="token: t")


# -- the whole provisioning path ---------------------------------------------


def test_provision_gives_the_agent_a_service_account_token_not_a_certificate(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The point of the module: the credential in the container is a scoped,
    short-lived SA token, so the RBAC boundary does real work."""
    _patch_kubectl(monkeypatch)

    path = creds.provision_agent_credentials(NetworkPlan(), tmp_path, token_ttl_sec=1500)

    user = yaml.safe_load(path.read_text())["users"][0]["user"]
    assert user == {"token": _TOKEN}
    assert "client-certificate-data" not in user


def test_provision_refuses_to_fall_back_to_the_admin_credential(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A silent fallback would look identical in the results while having no
    RBAC boundary at all, so failing to mint must fail the run."""
    monkeypatch.delenv(creds.ALLOW_ADMIN_ENV, raising=False)
    _patch_kubectl(monkeypatch, mint_fails=True)

    with pytest.raises(SandboxError, match=creds.ALLOW_ADMIN_ENV):
        creds.provision_agent_credentials(NetworkPlan(), tmp_path, token_ttl_sec=1500)

    assert not (tmp_path / "kubeconfig").exists()


def test_provision_falls_back_to_the_admin_cert_only_when_told_to(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv(creds.ALLOW_ADMIN_ENV, "1")
    _patch_kubectl(monkeypatch, mint_fails=True)

    path = creds.provision_agent_credentials(NetworkPlan(), tmp_path, token_ttl_sec=1500)

    user = yaml.safe_load(path.read_text())["users"][0]["user"]
    assert user["client-certificate-data"] == "Y2VydA=="
    assert "token" not in user


def test_provision_refuses_the_fallback_for_an_exec_plugin_context(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A GKE context has no static certificate to copy, and its exec plugin
    could not run inside the container anyway."""
    monkeypatch.setenv(creds.ALLOW_ADMIN_ENV, "1")
    _patch_kubectl(monkeypatch, mint_fails=True, cert="", key="")

    with pytest.raises(SandboxError, match="exec"):
        creds.provision_agent_credentials(NetworkPlan(), tmp_path, token_ttl_sec=1500)


# -- pod security ------------------------------------------------------------


def _ns(name: str, **labels: str) -> dict:
    return {"metadata": {"name": name, "labels": labels}}


def _policy_docs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    _patch_kubectl(monkeypatch)
    creds.enforce_pod_security(tmp_path)
    text = (tmp_path / "bench-agent-pod-security.yaml").read_text()
    return [d for d in yaml.safe_load_all(text) if d]


def test_pod_security_policy_denies_the_observed_escape(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The proposal's first incident was a privileged pod with a hostPath mount
    reading the bench checkout off the node's disk. Every ingredient of it must
    have a validation that rejects it."""
    docs = _policy_docs(tmp_path, monkeypatch)
    policy = next(d for d in docs if d["kind"] == "ValidatingAdmissionPolicy")
    expressions = " ".join(v["expression"] for v in policy["spec"]["validations"])

    assert "hostPath" in expressions
    assert "privileged" in expressions
    assert "hostNetwork" in expressions
    assert "hostPID" in expressions
    assert "hostIPC" in expressions


def test_pod_security_policy_denies_rather_than_warns(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Detection is a tripwire; this is meant to be a boundary. A binding in
    Warn mode would let the escape through and merely mention it."""
    docs = _policy_docs(tmp_path, monkeypatch)
    binding = next(d for d in docs if d["kind"] == "ValidatingAdmissionPolicyBinding")
    policy = next(d for d in docs if d["kind"] == "ValidatingAdmissionPolicy")

    assert binding["spec"]["validationActions"] == ["Deny"]
    assert policy["spec"]["failurePolicy"] == "Fail"


def test_pod_security_policy_exempts_the_clusters_own_components(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Control-plane and storage components legitimately run privileged with
    host mounts; enforcing on them would break the cluster, not the agent."""
    docs = _policy_docs(tmp_path, monkeypatch)
    binding = next(d for d in docs if d["kind"] == "ValidatingAdmissionPolicyBinding")
    expr = binding["spec"]["matchResources"]["namespaceSelector"]["matchExpressions"][0]

    assert expr["key"] == "kubernetes.io/metadata.name"
    assert expr["operator"] == "NotIn"
    assert "kube-system" in expr["values"]
    assert creds.AGENT_NAMESPACE in expr["values"]


def test_enforce_pod_security_labels_ordinary_namespaces(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls = _patch_kubectl(monkeypatch, namespaces={"items": [_ns("default"), _ns("kube-system")]})

    creds.enforce_pod_security(tmp_path)

    labelled = [c for c in calls if "label" in c]
    assert len(labelled) == 1
    assert labelled[0][:4] == ["kubectl", "label", "namespace", "default"]
    assert "pod-security.kubernetes.io/enforce=baseline" in labelled[0]


def test_enforce_pod_security_leaves_a_declared_level_alone(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """One task's verifier asserts ``enforce=restricted`` on its own namespace.
    Overwriting it would fail the task this control exists to protect."""
    calls = _patch_kubectl(
        monkeypatch,
        namespaces={
            "items": [_ns("hello-app", **{"pod-security.kubernetes.io/enforce": "restricted"})]
        },
    )

    creds.enforce_pod_security(tmp_path)

    assert [c for c in calls if "label" in c] == []


def test_enforce_pod_security_pins_every_call_to_the_runs_context(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls = _patch_kubectl(monkeypatch, namespaces={"items": [_ns("default")]})

    creds.enforce_pod_security(tmp_path, "vcluster-c1")

    assert calls
    for argv in calls:
        assert argv[:3] == ["kubectl", "--context", "vcluster-c1"]


def test_provision_enforces_pod_security_by_default(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A task author who never heard of the key still gets the control."""
    calls = _patch_kubectl(monkeypatch)

    creds.provision_agent_credentials(NetworkPlan(), tmp_path, token_ttl_sec=1500)

    assert any("bench-agent-pod-security.yaml" in c[-1] for c in calls if "apply" in c)


def test_provision_honours_the_privileged_opt_out(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A task whose subject matter *is* privileged workloads can opt out, and
    then nothing pod-security-related is applied at all."""
    calls = _patch_kubectl(monkeypatch)

    creds.provision_agent_credentials(
        NetworkPlan(),
        tmp_path,
        token_ttl_sec=1500,
        pod_security=creds.POD_SECURITY_PRIVILEGED,
    )

    assert not any("bench-agent-pod-security.yaml" in c[-1] for c in calls if "apply" in c)
    assert [c for c in calls if "label" in c] == []
