#!/usr/bin/env python3
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

"""Boundary probes for the sandboxed agent, run against a live cluster.

A passing task run proves the agent can WORK inside the sandbox. It says
nothing about whether the boundary HOLDS -- the pod-security policy could be
absent entirely and the run would look identical. This script probes the
boundary directly: it provisions the agent's real credential through the real
code path, then runs each escape from inside a real sandbox container and
checks it is refused.

Control probes come first and are not optional. If the token is broken, every
escape probe "passes" for the wrong reason, and a green run would mean nothing.

Usage:
    uv run python hack/sandbox_probe.py --context <kubectl-context> \\
        --image <sandbox-image> [--host-apiserver https://...]

This is scratch validation tooling, not part of the sandboxing PR stack. It is
the seed of the e2e boundary test planned for PR 4.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

from devops_bench.agents.sandbox import SandboxExecutor, SandboxSpec
from devops_bench.core.context import NetworkPlan
from devops_bench.k8s import agent_credentials as creds

# Written into the workspace rather than piped: the container runs without
# ``-i`` by design, so ``kubectl apply -f -`` would read an empty stdin and
# the probe would "pass" without ever reaching admission.
_HOSTPATH_POD = """\
apiVersion: v1
kind: Pod
metadata:
  name: bench-probe-hostpath
spec:
  containers:
    - name: c
      image: busybox
      command: ["sleep", "1d"]
      volumeMounts:
        - name: host
          mountPath: /host
  volumes:
    - name: host
      hostPath:
        path: /
"""

_PRIVILEGED_OVERRIDE = json.dumps(
    {
        "spec": {
            "containers": [
                {
                    "name": "c",
                    "image": "busybox",
                    "command": ["sleep", "1d"],
                    "securityContext": {"privileged": True},
                }
            ]
        }
    }
)

_ORDINARY_POD = json.dumps(
    {"spec": {"containers": [{"name": "c", "image": "busybox", "command": ["sleep", "1d"]}]}}
)

_METADATA_URL = "http://169.254.169.254/computeMetadata/v1/instance/service-accounts/default/token"


@dataclass
class Probe:
    """One boundary check run inside the sandbox container.

    Attributes:
        name: Short label printed in the report.
        why: What a failure of this probe would mean.
        argv: Command line run inside the container.
        expect_denied: True when a non-zero exit is the passing outcome.
        expect_stderr: Substring the refusal must mention, so a probe that
            fails for an unrelated reason (typo, missing binary) is not
            mistaken for the boundary doing its job.
    """

    name: str
    why: str
    argv: list[str]
    expect_denied: bool = True
    expect_stderr: str = ""


def _probes(workspace_pod_path: str) -> list[Probe]:
    """Build the probe list.

    Args:
        workspace_pod_path: Container path of the hostPath manifest.

    Returns:
        Controls first, then the escapes, then the informational checks.
    """
    return [
        # -- controls: prove the credential works before trusting any refusal --
        Probe(
            name="control:credential-works",
            why="a dead token makes every deny probe pass for the wrong reason",
            argv=["kubectl", "get", "namespaces", "-o", "name"],
            expect_denied=False,
        ),
        Probe(
            name="control:curl-present",
            why="an image without curl makes the metadata probe below pass "
            "without ever reaching the network",
            argv=["curl", "--version"],
            expect_denied=False,
        ),
        Probe(
            name="control:ordinary-namespace",
            why="the namespace guard must deny reserved names, not all names",
            argv=["kubectl", "create", "namespace", "bench-probe-ok"],
            expect_denied=False,
        ),
        Probe(
            name="control:ordinary-pod",
            why="baseline pods must still run, or the policy is too broad to ship",
            argv=[
                "kubectl",
                "run",
                "bench-probe-ok-pod",
                "-n",
                "bench-probe-ok",
                "--image=busybox",
                "--restart=Never",
                f"--overrides={_ORDINARY_POD}",
            ],
            expect_denied=False,
        ),
        # -- incident 1: privileged pod + hostPath, reading the host disk --
        Probe(
            name="escape:privileged-pod",
            why="observed incident 1; the escape this PR exists to deny",
            argv=[
                "kubectl",
                "run",
                "bench-probe-priv",
                "-n",
                "bench-probe-ok",
                "--image=busybox",
                "--restart=Never",
                f"--overrides={_PRIVILEGED_OVERRIDE}",
            ],
            expect_stderr="privileged",
        ),
        Probe(
            name="escape:hostpath-volume",
            why="the other half of incident 1: mounting the node's filesystem",
            argv=["kubectl", "apply", "-n", "bench-probe-ok", "-f", workspace_pod_path],
            expect_stderr="hostPath",
        ),
        # -- incident 2: the VM service account via the metadata server --
        Probe(
            name="escape:metadata-server",
            why="observed incident 2; mined the bastion VM's service account",
            argv=["curl", "-sS", "-m", "5", "-H", "Metadata-Flavor: Google", _METADATA_URL],
        ),
        # -- the three holes the code review found, none of them ever run live --
        Probe(
            name="review:ephemeral-container",
            why="pods/ephemeralcontainers is a distinct subresource; a rule "
            "naming only 'pods' never sees kubectl debug --profile=sysadmin",
            argv=[
                "kubectl",
                "debug",
                "-n",
                "bench-probe-ok",
                "pod/bench-probe-ok-pod",
                "--image=busybox",
                "--profile=sysadmin",
                "--attach=false",
                "-q",
            ],
        ),
        Probe(
            name="review:bench-system-pod",
            why="bench-system was policy-exempt and the agent holds edit "
            "cluster-wide, so the escape was one -n bench-system away",
            argv=[
                "kubectl",
                "run",
                "bench-probe-sys",
                "-n",
                creds.AGENT_NAMESPACE,
                "--image=busybox",
                "--restart=Never",
                f"--overrides={_PRIVILEGED_OVERRIDE}",
            ],
            expect_stderr="privileged",
        ),
        Probe(
            name="review:claim-exempt-namespace",
            why="exemptions are by name and several do not exist on every "
            "provider, so the agent could claim one and deploy there freely",
            argv=["kubectl", "create", "namespace", "gmp-system"],
            expect_stderr="reserved",
        ),
        # -- informational: read the output, there is no pass/fail here --
        Probe(
            name="info:visible-nodes",
            why="on vcluster this must list only virtual nodes",
            argv=["kubectl", "get", "nodes", "-o", "name"],
            expect_denied=False,
        ),
    ]


def _run_probe(executor: SandboxExecutor, probe: Probe) -> tuple[bool, str]:
    """Run one probe and judge it.

    Args:
        executor: Executor wrapping the same sandbox the agent would get.
        probe: The probe to run.

    Returns:
        ``(passed, detail)``; ``detail`` is the output worth printing.
    """
    completed = executor.run(probe.argv, check=False, timeout=120)
    out = ((completed.stdout or "") + (completed.stderr or "")).strip()
    denied = completed.returncode != 0

    if probe.expect_denied != denied:
        return False, out
    if probe.expect_denied and probe.expect_stderr and probe.expect_stderr not in out:
        # Refused, but not by the control we are testing -- a typo, a missing
        # binary, or an RBAC denial standing in for an admission denial.
        return False, f"[refused, but not by the expected control]\n{out}"
    return True, out


def _check_policies(context: str) -> list[str]:
    """Host-side: confirm both policies exist and their CEL compiled.

    A ValidatingAdmissionPolicy whose expression does not type-check is
    accepted by the apiserver and then, under ``failurePolicy: Fail``, denies
    everything it matches. On a shared cluster that is a bad afternoon, so it
    is worth reading the status rather than assuming the apply succeeded.

    Args:
        context: kubectl context to query.

    Returns:
        Human-readable problem lines; empty when both policies are healthy.
    """
    problems: list[str] = []
    for name in ("bench-agent-pod-security", "bench-agent-namespace-guard"):
        completed = subprocess.run(
            [
                "kubectl",
                "--context",
                context,
                "get",
                "validatingadmissionpolicy",
                name,
                "-o",
                "json",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode != 0:
            problems.append(f"{name}: not present ({completed.stderr.strip()})")
            continue
        status = json.loads(completed.stdout).get("status", {})
        for condition in status.get("typeChecking", {}).get("expressionWarnings", []):
            problems.append(f"{name}: CEL warning on {condition}")
        for condition in status.get("conditions", []):
            if condition.get("type") == "TypeChecking" and condition.get("status") != "True":
                problems.append(f"{name}: type checking failed: {condition.get('message')}")
    return problems


def _check_token_is_useless_against_host(kubeconfig: Path, host_apiserver: str) -> str:
    """Replay the agent's token against the HOST apiserver; expect a 401.

    The whole point of pinning to the virtual cluster's context is that the
    minted ServiceAccount lives inside the vcluster and its token is signed by
    a key the host apiserver does not trust.

    Args:
        kubeconfig: The generated agent kubeconfig.
        host_apiserver: Base URL of the host cluster's apiserver.

    Returns:
        A problem line, or ``""`` when the token was correctly rejected.
    """
    import yaml  # local: only this check needs it

    token = yaml.safe_load(kubeconfig.read_text())["users"][0]["user"].get("token")
    if not token:
        return "the agent kubeconfig carries no token -- it fell back to a certificate"
    completed = subprocess.run(
        [
            "curl",
            "-sk",
            "-o",
            "/dev/null",
            "-w",
            "%{http_code}",
            "-H",
            f"Authorization: Bearer {token}",
            f"{host_apiserver.rstrip('/')}/api/v1/nodes",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    code = completed.stdout.strip()
    if code in {"401", "403"}:
        return ""
    return f"the vcluster token got HTTP {code} from the HOST apiserver; expected 401/403"


def main() -> int:
    """Provision, probe, report.

    Returns:
        0 when every probe passed, 1 otherwise.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--context", required=True, help="kubectl context of the run's cluster")
    parser.add_argument("--image", required=True, help="sandbox image (BENCH_SANDBOX_IMAGE)")
    parser.add_argument("--docker-network", default=None, help="docker network to join (kind)")
    parser.add_argument(
        "--host-apiserver",
        default=None,
        help="host cluster apiserver URL; enables the vcluster token-replay check",
    )
    parser.add_argument(
        "--keep", action="store_true", help="leave the probe namespace behind for inspection"
    )
    args = parser.parse_args()

    plan = NetworkPlan(kubectl_context=args.context, docker_network=args.docker_network)

    with tempfile.TemporaryDirectory(prefix="bench-probe-") as tmp:
        workspace = Path(tmp) / "workspace"
        (workspace / "home").mkdir(parents=True)
        (workspace / "probe-hostpath.yaml").write_text(_HOSTPATH_POD)
        creds_dir = Path(tmp) / "creds"
        creds_dir.mkdir()

        print(f"==> provisioning the agent credential against {args.context}")
        kubeconfig = creds.provision_agent_credentials(plan, creds_dir, token_ttl_sec=3600)
        print(f"    kubeconfig: {kubeconfig}")

        print("==> checking the admission policies compiled")
        problems = _check_policies(args.context)
        for line in problems:
            print(f"    FAIL {line}")

        executor = SandboxExecutor(
            SandboxSpec(image=args.image, network=plan, workspace=workspace, kubeconfig=kubeconfig)
        )

        failures = list(problems)
        for probe in _probes("/workspace/probe-hostpath.yaml"):
            passed, detail = _run_probe(executor, probe)
            verdict = "PASS" if passed else "FAIL"
            print(f"\n==> [{verdict}] {probe.name}")
            print(f"    why: {probe.why}")
            for line in detail.splitlines()[:12]:
                print(f"    | {line}")
            if not passed:
                failures.append(probe.name)

        if args.host_apiserver:
            print("\n==> replaying the agent token against the host apiserver")
            problem = _check_token_is_useless_against_host(kubeconfig, args.host_apiserver)
            print(f"    {problem or 'PASS: rejected, as it must be'}")
            if problem:
                failures.append("vcluster:token-replay")

        if not args.keep:
            for namespace in ("bench-probe-ok",):
                subprocess.run(
                    [
                        "kubectl",
                        "--context",
                        args.context,
                        "delete",
                        "namespace",
                        namespace,
                        "--ignore-not-found",
                        "--wait=false",
                    ],
                    capture_output=True,
                    check=False,
                )

    print("\n" + "=" * 60)
    if failures:
        print(f"FAILED: {', '.join(failures)}")
        return 1
    print("all probes passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
