# CI Dashboard Pod Watcher Design

Status: Draft v0.1

Last updated: 2026-05-01

## 1. Problem

The current pod ingestion path combines two imperfect sources:

- Cloud Logging Kubernetes events have better historical coverage, but they do not carry the full Pod object metadata. In particular, Cloud Logging `Created` is a container-created event, not the Pod `metadata.creationTimestamp`, so it must not be used as pod creation time.
- Ad hoc Kubernetes API lookup has complete Pod metadata, labels, annotations, and `metadata.creationTimestamp`, but it can only read Pods that still exist when the sync job runs. Short-lived CI Pods are often gone by then.

This makes scheduling metrics unreliable. We can safely show lifecycle metrics only when we have the actual Pod creation time and the `Scheduled` event for the same Pod identity.

## 2. Goal

Run a long-lived watcher inside the cluster so CI Dashboard sees Pods while they are alive.

The watcher should:

- watch target namespaces for Pod `ADDED` and `MODIFIED` events
- persist full Pod labels, annotations, uid, and `metadata.creationTimestamp`
- watch Kubernetes Events for lifecycle reasons such as `Scheduled`, `Pulling`, `Pulled`, `Created`, `Started`, `ErrImagePull`, and `ImagePullBackOff`
- upsert existing `ci_l1_pod_events` and `ci_l1_pod_lifecycle` rows idempotently
- keep the dashboard's scheduling wait definition strict: `scheduled_at - pod_created_at`

## 3. Non-Goals

- Do not create a new dashboard tab in this change.
- Do not invent historical pod creation time from Cloud Logging.
- Do not replace Jenkins build ingestion or error classification.
- Do not backfill Pods that were deleted before the watcher rollout. Old data can stay incomplete.

## 4. Data Contract

The source of truth for Pod creation time is Kubernetes Pod `metadata.creationTimestamp`.

The source of truth for lifecycle milestones is Kubernetes Event reason timestamps:

- `Scheduled`: scheduler accepted the Pod onto a Node
- `Pulling` / `Pulled`: image pull start and completion
- `Created` / `Started`: container creation and start, not Pod creation
- `FailedScheduling`: intermediate retry signal, not final CI failure
- `ErrImagePull` / `ImagePullBackOff`: image pull failure/backoff signal

Rows remain keyed by the existing lifecycle identity:

```text
(source_project, namespace_name, pod_uid, pod_name)
```

The watcher writes the existing tables:

- `ci_l1_pod_events`
- `ci_l1_pod_lifecycle`
- `ci_job_state`

## 5. Runtime Shape

The watcher is a Deployment, not a CronJob.

Recommended first deployment:

- namespace: `apps`
- replicas: `1`
- service account: `ci-dashboard`
- target namespaces: `prow-test-pods,jenkins-tidb,jenkins-tiflow`
- RBAC: `get`, `list`, `watch` on `pods` and `events` in target namespaces

The watcher uses Kubernetes watch streams. On startup it first lists current Pods in each namespace, then starts watches from the returned `resourceVersion`. If a watch stream disconnects or the resource version expires, the worker relists and resumes from a fresh resource version.

Rollout precondition: migration `017_alter_ci_l1_pod_lifecycle_add_pod_created_at.sql`
must be applied before `watch-pods` starts, otherwise metadata writes will fail because the
watcher persists Pod `metadata.creationTimestamp` into `ci_l1_pod_lifecycle.pod_created_at`.

## 6. Health And Self-Healing

The watcher exposes HTTP health endpoints from the same process:

- `/livez`: returns success only when every registered Pod/Event watch stream has a recent heartbeat
- `/readyz`: uses the same stream-heartbeat check so the Pod does not receive traffic until every namespace stream has completed at least one successful list/watch cycle

Each namespace has two registered streams:

- `<namespace>/pods`
- `<namespace>/events`

The heartbeat is updated after each successful list and after each watch response, including Kubernetes watch bookmarks. The Kubernetes watch request also sets `allowWatchBookmarks=true` and `timeoutSeconds`, so healthy long connections are expected to either receive events/bookmarks or reconnect periodically. If a stream is stuck beyond `CI_DASHBOARD_POD_WATCH_STALE_AFTER_SECONDS`, `/livez` fails and Kubernetes restarts the Pod.

Recommended first probe settings:

- health port: `8081`
- watch timeout: `300s`
- stale-after: `720s`
- startup probe: `/livez`, up to 3 minutes
- liveness probe: `/livez`
- readiness probe: `/readyz`

## 7. Dashboard Semantics

Scheduling wait should only use rows where:

- `pod_created_at IS NOT NULL`
- `scheduled_at IS NOT NULL`
- `scheduled_at >= pod_created_at`

`FailedScheduling` should not be displayed as a CI failure rate. It is useful as a debug signal, but in an autoscaled GKE cluster it is commonly an intermediate retry event. The high-level dashboard should emphasize:

- final unscheduled Pods, if any
- scheduling wait distribution
- image pull latency distribution
- image pull error/backoff counts

## 8. Failure Handling

Writes are idempotent:

- pod events are deduped by `(source_project, source_insert_id)`
- lifecycle rows are upserted by `(source_project, namespace_name, pod_uid, pod_name)`

The worker records a job state under `ci-watch-pods`. A restart is safe because the startup relist refreshes current Pod metadata and new watch streams continue from Kubernetes resource versions.

Watch streams are not a durable historical log. If the watcher is down while short-lived Pods are created and deleted, those Pods can still be missed. This is an operational reliability problem, so alerts should eventually watch for worker restarts and DB write failures.

## 9. Rollout Plan

1. Deploy with `replicas=1` and a narrow namespace list.
2. Validate that fresh lifecycle rows have `pod_created_at`, labels, annotations, and `Scheduled`.
3. Compare new rows against existing Cloud Logging rows for a recent window.
4. Update dashboard cards to count scheduling wait only for watcher-backed rows.
5. Keep `sync-pods` temporarily as a compatibility/backfill path, but do not use Cloud Logging `Created` as Pod creation time.

## 10. Open Follow-Ups

- Add a small freshness monitor: latest `metadata_observed_at` by namespace.
- Consider a dedicated `ci_l1_pod_watch_state` table only if Kubernetes resource versions need to survive restarts more precisely than startup relist.
