# Chaos Engineering Faults & Triggers Proposal

This document outlines proposed chaos triggers and a curated bank of **10 chaos faults** for `devops-bench`, designed to test the resilience, diagnosis, and remediation capabilities of autonomous AI agents (such as Claude Fable 5, GPT-5 series, and Gemini).

---

## 1. Trigger Strategies

To benchmark agents fairly, chaos injection should occur deterministically relative to the agent's progress rather than solely on static timers.

| Trigger Type | Description | Benchmark Value |
| :--- | :--- | :--- |
| **`TimeTrigger`** *(Existing)* | Fires after a fixed elapsed time ($N$ seconds). | Basic baseline; can introduce variance across models of differing inference speeds. |
| **`ResourceStateTrigger`** | Fires when a Kubernetes resource reaches a target condition (e.g., `Deployment` is `Ready`). | Tests whether the agent continuously verifies stability after initial deployment or terminates prematurely. |
| **`AgentActionTrigger`** | Fires when the agent mutates a resource or executes a specific API command. | Allows testing race conditions, conflicting configurations, and override handling. |
| **`MetricThresholdTrigger`** | Fires when resource utilization (CPU, memory, QPS) crosses a threshold. | Ideal for testing auto-scaling (HPA) and resource limit handling. |
| **`MilestoneTrigger`** | Fires when an intermediate verification check evaluates to `True`. | Enables multi-stage evaluation pipelines for complex, multi-step tasks. |
| **`RandomFuzzTrigger`** | Fires at randomized intervals within a bounded time window. | Evaluates robustness under unpredictable, fluctuating infrastructure environments. |

---

## 2. Proposed Bank of 10 Chaos Faults

The faults range from foundational operational tasks (Level 1) to multi-layered, research-backed cognitive challenges (Level 5).

```
Level 1: Foundational ────► Level 3: Non-Crashing / Implicit ────► Level 5: High Distraction / Metastable
(kill_pod)                 (cfs_cpu_throttle, rbac_degrade)        (ambient_noise, downstream_latency)
```

---

### Fault 1: `kill_pod` (Pod CrashLoop / Termination)
* **Difficulty:** **Level 1 (Low)**
* **Description & Mechanism:** Terminates target pods or mutates the container entrypoint/args to exit with a non-zero status code (`1` or `137`).
* **Benchmark Value:** Evaluates whether the agent understands Kubernetes controller lifecycle semantics (e.g., ReplicaSets recreating pods) or erroneously attempts to interact with or edit terminated pods directly.
* **Potential Verification Checks:**
  * `pod_healthy`: Asserts replacement pods achieve `Ready` status and container restart counts stabilize.
  * `resource_property`: Verifies `Deployment.status.readyReplicas == Deployment.spec.replicas`.

---

### Fault 2: `cordon_drain_node` (Node Eviction & Unschedulability)
* **Difficulty:** **Level 2 (Low–Medium)**
* **Description & Mechanism:** Marks a worker node unschedulable (`kubectl cordon`) and evicts non-daemonset workloads (`kubectl drain`).
* **Benchmark Value:** Evaluates agent handling of node selectors, taints/tolerations, pod disruption budgets (PDBs), and anti-affinity rules during rescheduling.
* **Potential Verification Checks:**
  * `pod_healthy`: All evicted pods achieve `Running`/`Ready` states on surviving nodes.
  * `resource_property`: Checks that `Pending` pod counts return to `0`.

---

### Fault 3: `resource_quota_exhaustion` (Namespace Quota Lockout)
* **Difficulty:** **Level 2 (Medium)**
* **Description & Mechanism:** Patches a namespace `ResourceQuota` to cap CPU/memory below current or requested usage, causing subsequent pod rollouts to hang in `Pending`.
* **Benchmark Value:** Tests if the agent can diagnose admission-level quota rejection vs. node scheduling capacity issues.
* **Potential Verification Checks:**
  * `resource_property`: Checks `ResourceQuota` status vs. `hard` limits.
  * `pod_healthy`: All namespace pods achieve `Running` after quota remediation.

---

### Fault 4: `generate_load` (Targeted Traffic Spike)
* **Difficulty:** **Level 2 (Medium)**
* **Description & Mechanism:** Uses `fortio` via the `ChaosAgent` to generate high-QPS traffic against an exposed service or port-forwarded workload.
* **Benchmark Value:** Evaluates autoscaling responsiveness, Horizontal Pod Autoscaler (HPA) policies, and connection pool behavior under load.
* **Potential Verification Checks:**
  * `scaling_complete`: Deployment scales up to target replica count via HPA.
  * `http_probe` / `perf_report`: Workload maintains $\ge 99\%$ HTTP 200 responses with p95 latency under target threshold.

---

### Fault 5: `cfs_cpu_throttle` (Silent CFS Quota Starvation)
* **Difficulty:** **Level 3 (Medium–High)**
* **Description & Mechanism:** Sets restrictive `resources.limits.cpu` (e.g., `10m`) while leaving `requests` normal. This triggers Linux Completely Fair Scheduler (CFS) throttling without crashing the pod or producing `OOMKilled` (Exit 137) logs.
* **Benchmark Value (LLM Metric-Blindness):** LLMs rely heavily on textual logs (`kubectl logs`). Because the container remains `Running` with clean logs, models often hallucinate that the workload is healthy. Tests if the agent inspects multidimensional telemetry (cgroup throttles, Prometheus metrics).
* **Potential Verification Checks:**
  * `resource_property`: Asserts container `resources.limits.cpu` is adjusted or removed.
  * `http_probe`: Request latency returns to baseline under steady traffic.

---

### Fault 6: `rbac_privilege_degradation` (Silent Authorization Drift)
* **Difficulty:** **Level 3 (Medium–High)**
* **Description & Mechanism:** Silently revokes specific verbs (e.g., `get`, `list`, `patch`) from a workload's `ServiceAccount` `RoleBinding` or mutates admission webhook credentials.
* **Benchmark Value (Over-Privileged Hallucination):** When encountering permission denials, agents frequently attempt to escalate permissions recklessly (e.g., creating broad `cluster-admin` bindings) or mistake RBAC rejections for network disconnects. Tests least-privilege diagnosis.
* **Potential Verification Checks:**
  * `resource_property`: Asserts that `Role`/`ClusterRole` contains strictly necessary `verbs` without wildcard `*` permissions.
  * `pod_healthy`: ServiceAccount successfully authenticates with the Kubernetes API.

---

### Fault 7: `downstream_latency_injection` (Cascading Microservice / DB Delay)
* **Difficulty:** **Level 4 (High)**
* **Description & Mechanism:** Injects synthetic delay (e.g., 2000ms latency) or connection pool exhaustion into a downstream database or cache service, causing upstream frontend services to return HTTP 504 Gateway Timeouts.
* **Benchmark Value (Reasoning Drift & Shallow Diagnosis):** Frontier models tend to focus on immediate surface symptoms (restarting frontend pods repeatedly) rather than tracing service dependency graphs to identify the true root cause.
* **Potential Verification Checks:**
  * `http_probe`: End-to-end frontend requests return HTTP 200 within acceptable latency bounds.
  * `resource_property`: Downstream service timeouts and connection pool parameters are properly configured.

---

### Fault 8: `flapping_dns_packet_drop` (Stochastic Network Degradation)
* **Difficulty:** **Level 4 (High)**
* **Description & Mechanism:** Injects 25–35% probabilistic UDP packet loss or intermittent latency spikes into CoreDNS or cluster network interfaces.
* **Benchmark Value (Temporal Reasoning & Premature Convergence):** Single-shot probes (e.g., executing `curl` once) will intermittently succeed by chance, tricking the agent into falsely declaring victory before fixing the underlying network/DNS issue. Tests temporal stability verification.
* **Potential Verification Checks:**
  * `http_probe`: 50 consecutive probe requests over a 30-second window maintain a 0% error rate.
  * `resource_property`: CoreDNS / CNI configurations and network policies are valid.

---

### Fault 9: `latent_io_storage_bottleneck` (Disk & Latent Sector Degradation)
* **Difficulty:** **Level 4 (High)**
* **Description & Mechanism:** Injects I/O throttling, read latency, or simulated latent sector errors onto persistent storage volumes backing stateful workloads.
* **Benchmark Value (Infrastructure vs. Application Blindspots):** Tests whether the agent can differentiate between application thread pool starvation and underlying disk/volume performance constraints.
* **Potential Verification Checks:**
  * `resource_property`: `StorageClass` and `PersistentVolumeClaim` configurations provide adequate IOPS/throughput.
  * `pod_healthy`: StatefulSet pods mount volumes and pass continuous I/O read/write probes.

---

### Fault 10: `ambient_noise_decoy_failure` (Concurrent Multi-Fault Distraction)
* **Difficulty:** **Level 5 (Very High)**
* **Description & Mechanism:** Injects noisy, non-critical background failures (e.g., a test cronjob crashing in an adjacent namespace) simultaneously with the primary root-cause issue in the target namespace.
* **Benchmark Value (Greedy Diagnosis):** Models frequently exhibit greedy heuristic behavior, latching onto the first failing resource in cluster-wide logs while ignoring the primary blast radius. Tests noise filtering and systematic root cause analysis.
* **Potential Verification Checks:**
  * `resource_property`: Primary objective is resolved without the agent wasting turns mutating or deleting irrelevant decoy workloads.
  * `pod_healthy`: Target workload is fully operational and healthy.

---

## 3. Research Citations & Literature Reference

1. **SREGym: A High-Fidelity Benchmark for Autonomous SRE in Kubernetes** ([arXiv:2412.12519](https://arxiv.org/abs/2412.12519))
   * *Key findings:* Identifies model struggles with cascading dependencies, ambient production noise, and hardware/storage-level latent failures.
2. **OpsEval: A Comprehensive Task-Oriented Benchmark for AIOps** ([arXiv:2310.07637](https://arxiv.org/abs/2310.07637))
   * *Key findings:* Highlights severe performance degradation when models diagnose implicit performance bottlenecks lacking explicit error log entries.
3. **Cloud-OpsBench: Evaluating Agentic Root Cause Analysis in Cloud Systems** ([arXiv:2602.04812](https://arxiv.org/abs/2602.04812))
   * *Key findings:* Documents frontier model failure modes including premature convergence, speculative shortcuts without verification, and API schema hallucination.
4. **Alibaba Cloud RCA Benchmark for Agentic Ops**
   * *Key findings:* Evaluates active agentic graph traversal and identifies failures in temporal reasoning under stochastic, flapping network faults.
