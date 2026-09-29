# Backup Repository — Portable Execution Protocol

## Purpose
Authoritative portable entry point for the Full Repository Preservation Process. An AI engine MUST be able to start without prior conversation memory.

## Mandatory execution sequence

```text
Capability
    ↓
Parameters
    ↓
Exact Command Request
    ↓
HITL GO
    ↓
Capability Preflight
    ↓
Assess effective execution capability for everything that may affect preservation
    ↓
        MATERIAL GAP?
       /             \
     NO              YES
      |                |
      |          disclose exactly:
      |          - what cannot be read/discovered
      |          - gap classification
      |          - required capability
      |          - routes assessed
      |          - cause
      |          - preservation impact
      |          - resulting restriction
      |                ↓
      |              HITL
      |          /             \
      |        STOP   CONTINUE_WITH_RESTRICTIONS
      |          |              |
      |          |      record accepted restrictions
      |          |              |
      |          └──────┬───────┘
      |                 |
      └─────────────────┘
                        ↓
                 Source Inventory
```

This sequence MUST NOT be shortened, reordered, or silently bypassed for `backup_repository`.

## Non-negotiable rules
1. Never infer authorization.
2. Never mutate `SOURCE_REPOSITORY`.
3. Never inherit execution-specific parameters from a prior run.
4. Never treat missing evidence as success.
5. Never expose secret plaintext in chat, logs, plaintext evidence, or ordinary preservation artifacts.
6. HITL applies only to the exact Command Request presented.
7. Authorization is non-transitive and non-reusable.
8. Backup never authorizes cleanup, deletion, rename, archive, or source modification.
9. Inability to read or discover an object MUST NOT be interpreted as evidence that the object is absent.
10. Any restriction accepted by HITL MUST remain traceable through Source Inventory, evidence, reconciliation, manifest, and final reporting.
11. A current engine or connector limitation MUST NOT by itself reopen an architectural gap for which an approved route already exists.
12. `FACTUAL_INVENTORY_PENDING` is not a Capability Preflight gap.
13. No preservation capability depends for its architectural existence on a single engine, connector, workflow, or deterministic executor.
14. Source Inventory MUST NOT begin before the Capability Preflight gate is resolved.

## Start procedure
1. Read this document and `backup/capabilities.yaml`.
2. Present the Capability Menu.
3. Wait for the human to select a capability.
4. Collect or explicitly confirm every required execution parameter.
5. Construct the Exact Command Request using `backup/schemas/command-request.schema.yaml`.
6. Present that Exact Command Request to HITL when required.
7. Execute only after `GO`.
8. Reject on `NO-GO`, missing parameters, scope mismatch, or missing authorization.
9. For `backup_repository`, execute the mandatory Capability Preflight after `GO` and before Source Inventory.
10. Assess the effective execution capability for every applicable object class.
11. If no material gap exists, proceed to Source Inventory.
12. If a material gap exists, disclose it and request `STOP` or `CONTINUE_WITH_RESTRICTIONS`.
13. On `STOP`, do not start Source Inventory.
14. On `CONTINUE_WITH_RESTRICTIONS`, record each accepted restriction with a stable `restriction_id` and proceed only under those restrictions.
15. Preserve accepted restrictions through evidence, reconciliation, manifest, and final reporting.
16. Collect and validate evidence.
17. Report preservation state, limitations, accepted restrictions, reconciliation result, and next applicable HITL.

## Governed command flow (engine → commands.log → dispatcher → workflow)
1. The engine reads this document and presents the Capability Menu; HITL selects one capability.
2. `backup/capabilities.yaml` maps the capability to its Mnemonic (`backup_repository` → `BKP_REPO`) and required parameters; HITL supplies the values.
3. The workflow bound to the Mnemonic (`command_workflow.path`) fully defines the Exact Command Request: the `on: workflow_call` inputs plus the constraints stated in their descriptions. The engine reads the workflow and presents the ECR exactly as defined.
4. Only after `GO`, the engine appends one JSON line to `commands.log` on `main` (`backup/schemas/commands-log-line.schema.yaml`): `request_id`, `mnemonic`, `ts`, `authorization` and the authorized `params`. The set of `params` varies per Mnemonic schema. `commands.log` is append-only, and it is the only file written directly on `main`.
5. `dispatcher.yml` runs on pushes touching `commands.log`, reads only the new lines, validates them (schema, unique `request_id`, no edits to existing lines) and calls the Mnemonic's workflow through a fixed mapping (`workflow_call`, `secrets: inherit`).
6. The workflow revalidates its inputs, runs Capability Preflight, executes and produces schema 2.0 evidence. The engine reports the result to HITL.

The workflow reports every outcome as one `BKP_RESULT {json}` line in its job log (statuses `REJECTED`, `BLOCKED`, `PREFLIGHT_OK`, `COMPLETE`, `COMPLETE_WITH_EXCEPTIONS`, `FAILED`); the engine reads that line to report to HITL. A `BLOCKED` result for preflight gaps lists `required_restrictions`; the follow-up `commands.log` line must accept every one of them. Destination validation fails closed before Source Inventory, and evidence is built only after it passes.

Capability Preflight gaps: the workflow is unattended, so a material gap ends the run as `BLOCKED` with `GAPS_IDENTIFIED` evidence. HITL then decides `STOP` or `CONTINUE_WITH_RESTRICTIONS`; the latter requires a new `commands.log` line carrying `preflight_decision` with stable `restriction_id` values. Authorization is never reused.

Boundaries: code changes only through pull request; direct writes to `main` only to `commands.log` and only after `GO`; the source is always READ-ONLY; OneDrive uses delegated OAuth (`Files.ReadWrite.AppFolder`), never OIDC.

## Capability Menu
Canonical definitions are in `backup/capabilities.yaml`.

- `backup_repository`
- `backup_projects`
- `list_capabilities`
- `show_status`
- `validate_evidence`
- `help`

The engine MUST NOT silently select a preservation capability.

## HITL parameter acquisition
Execution-specific values MUST be supplied or explicitly confirmed by the human. Conversation context MAY propose a value but MUST NOT replace confirmation.

`backup_repository` requires at minimum:
- `source_repository`;
- `destination`.

`backup_projects` requires:
- `scope` — `SOURCE_REPOSITORY`, `PROJECT`, or `OWNER_PROJECT_SET`;
- the corresponding `scope_identifiers`;
- `destination`.

Changing any execution parameter after authorization requires a new Exact Command Request and new HITL authorization.

## Capability Preflight

### Purpose
Capability Preflight answers one question before Source Inventory:

> Does the authorized execution process have effective READ/discovery capability for everything that may materially affect the requested preservation scope?

It does not ask only what the current AI engine or current connector can do.

### Effective execution capability
Effective execution capability is evaluated from the authorized combination of routes available to the execution, including, when applicable:

- engine-native capability;
- connectors;
- Git;
- REST APIs;
- GraphQL APIs;
- authorized API/identity routes;
- specialized capabilities;
- deterministic executors.

Each route MUST be assessed separately. The aggregate assessment determines whether the required READ/discovery capability is available.

A deterministic executor MAY implement a route but is not the definition of the capability itself.

References to `github_actions:backup-repository.yml` and `github_actions:backup-projects.yml` describe deterministic executor references whose current materialization is `NOT_MATERIALIZED` and whose historical existence is `NOT_DETERMINED`. Their absence MUST NOT be treated by itself as proof that an architectural capability is absent.

### Routing terminology
Routing outcomes are:

- `DIRECT`
- `ROUTED`
- `PARTIAL`
- `LIMITATION`

`WORKFLOW` is preserved as a historical/specialized route mechanism. It is not a synonym for the generalized `ROUTED` outcome.

### Classification
Capability Preflight distinguishes:

- `ARCHITECTURAL_GAP` — no sufficient preservation/read architecture has been defined for a material requirement;
- `EXECUTION_CAPABILITY_GAP` — architecture exists, but the currently authorized execution has no sufficient operational route;
- `ACCESS_PERMISSION_GAP` — an otherwise applicable route is blocked by access, authorization, identity, or permission;
- `FACTUAL_INVENTORY_PENDING` — the architecture/route exists, but a source-instance fact can only be established during Source Inventory.

Only the first three are material Capability Preflight gaps.

`FACTUAL_INVENTORY_PENDING` MUST be recorded separately. It MUST NOT be placed in `gaps[]`, MUST NOT change `PASS` to `GAPS_IDENTIFIED`, and MUST NOT trigger the second HITL by itself.

### Material-gap HITL
For every material gap, disclose:

- object class;
- what cannot be read or discovered;
- gap classification;
- required capability;
- routes assessed;
- cause;
- preservation impact;
- resulting restriction.

HITL MUST explicitly decide:

- `STOP`; or
- `CONTINUE_WITH_RESTRICTIONS`.

`STOP` prevents Source Inventory from starting.

`CONTINUE_WITH_RESTRICTIONS` authorizes Source Inventory only under the restrictions explicitly accepted by HITL. Each accepted restriction MUST receive a stable `restriction_id`.

A limitation MUST NEVER be converted into evidence that an object is absent, not applicable, preserved, or successfully verified.

## Capability Registry and routing requirements
`backup/capabilities.yaml` is the primary machine-readable policy/control registry for:

- applicable object classes;
- required READ/discovery capabilities;
- approved route types;
- temporal preservation controls;
- Protected Information controls;
- alert semantics;
- Projects preservation;
- Codespaces repository-state delta;
- O-24/Open-Set Discovery Control.

Architecturally defined routes remain defined even when a particular execution cannot currently use them. Operational availability is established by Capability Preflight.


## Capability ↔ Implementation Artifact Traceability
`backup/capabilities.yaml` is the single canonical registry for capability-to-artifact traceability. A separate artifact registry MUST NOT be required.

Every declarative or executable implementation artifact that implements, validates, schematizes, configures, or supports the capability system MUST have a stable logical `artifact_id` and explicit relationship(s) to one or more registered `capability_id` values.

The canonical relationship types are:

- `EXECUTES`
- `VALIDATES`
- `SCHEMATIZES`
- `CONFIGURES`
- `SUPPORTS`

The relationship model is N:M. A capability MAY relate to multiple artifacts and an artifact MAY relate to multiple capabilities. The authoritative relationship is stored once in the implementation-artifact registry; reverse Artifact → Capability lookup MUST be derived from those relationships rather than maintained as a second source of truth.

Each registered implementation artifact records, at minimum:

- stable `artifact_id`;
- artifact type;
- repository and path;
- purpose;
- materialization status;
- integrity/version tracking mechanism;
- validation state;
- capability relationships.

### Materialization and integrity
`MATERIALIZED` and `NOT_MATERIALIZED` describe whether the registered implementation artifact currently exists. They are distinct from validation and integrity state.

For Git-versioned materialized artifacts, the current Git blob SHA is the canonical content-integrity/version identifier and the Git commit SHA is change provenance. The current blob SHA MUST be resolved and compared during traceability validation; an artifact MUST NOT be required to embed its own blob SHA.

A `NOT_MATERIALIZED` executor or workflow MAY remain registered as an architectural implementation reference. Its absence does not by itself remove the capability definition.

### Referential Integrity and Broken Reference Detection
For every registered `MATERIALIZED` artifact, the registered repository/path MUST resolve to an existing object. Failure produces `BROKEN_REFERENCE` and MUST NOT be interpreted as successful validation.

A registered `NOT_MATERIALIZED` artifact is permitted to have no object at its future path.

### Orphan Artifact Detection
Implementation-artifact locations MUST be checked for capability-system artifacts that exist without a registered `artifact_id`. Such an object is an `ORPHAN_ARTIFACT`.

Orphan detection applies to implementation artifacts within managed locations and MUST NOT classify every unrelated repository file as an implementation artifact merely because it exists.

### Impact Analysis and Revalidation
Whenever a materialized implementation artifact changes:

1. identify its stable `artifact_id`;
2. derive every affected capability from the canonical relationships;
3. mark the affected capability/artifact relationship for revalidation;
4. perform the applicable validation before treating the changed implementation as validated.

A changed Git blob SHA therefore triggers impact analysis; it does not change the logical `artifact_id`.

These traceability rules are generic and apply equally to current and future capabilities. No capability receives a private or weaker traceability model.

## Git preservation
The Git object class includes the requirements to:

- preserve the full Git object graph using full mirror semantics;
- preserve all material refs, including pull-request refs when present;
- detect Git LFS and preserve/verify LFS objects when used;
- detect submodules and record explicit disposition;
- preserve a navigable repository tree separately from the Git mirror;
- reconcile source and preserved refs and target SHAs;
- perform Git object-integrity verification.

These are preservation requirements, not permission to mutate the source.

## Temporal preservation, MRBI and CRR
Temporal/volatile classes MUST be prioritized before stable objects once backup execution is authorized.

`MRBI` — Maximum Recommended Backup Interval — is governed by the shortest applicable deterministic retention window among discovered temporal preservation classes.

A non-time-bound eviction risk MUST NOT be converted into a numeric MRBI. It produces `VOLATILITY_ALERT`.

`CRR` — Current Repository Retention — is the effective applicable retention observed for the source. It MUST be read/preserved as evidence when applicable and MUST NOT be inferred from a product default.

Canonical alerts are:

- `RETENTION_ALERT`
- `VOLATILITY_ALERT`
- `ACCESSIBILITY_ALERT`

Temporal unavailability MUST be evidenced rather than silently treated as source absence.

## Protected Information
Protected Information handling is metadata-first and recoverability-aware.

Secret plaintext is not required to claim architectural completeness when the source platform does not expose it. Preserve permitted metadata, scope, provenance, relationships, authoritative-source references, recoverability, value-preservation status, and reconstruction/reissuance treatment.

Any attempt to recover plaintext from an external authoritative source when not already covered by the Exact Command Request requires operation-specific HITL authorization.

## Webhooks and deliveries
Preserve readable webhook configuration and readable delivery evidence.

Webhook deliveries are temporal evidence and are subject to temporal-preservation controls.

Webhook secret plaintext is not required when the platform does not expose it; preserve presence/metadata and reissuance treatment when observable.

External systems reached by webhooks remain reference-only unless separately and explicitly scoped. Webhook preservation MUST NOT silently expand authorization into external systems.

## Backup Projects
`backup_projects` is independently callable and is also included by `backup_repository` when related Projects exist.

Approved scopes:

- `SOURCE_REPOSITORY`
- `PROJECT`
- `OWNER_PROJECT_SET`

Projects preservation may require combined authorized REST and GraphQL READ surfaces. No single surface is assumed complete.

Preserve, when exposed and applicable:

- identity and metadata;
- repository relationships;
- fields/options/configuration;
- items;
- draft issues;
- issue and pull-request references;
- field values;
- archived item state;
- views/configuration;
- workflows/automations;
- status updates;
- other discovered Project state.

The preserved representation MUST be machine-readable, schema-controlled, reconstructible as an equivalent representation, provenance-aware, and reusable downstream.

A missing Project Graph surface in the current engine/connector is an execution-surface fact, not automatically an architectural gap.

## Codespaces repository-state delta
The authoritative preservation scope for Codespaces is repository-state delta only:

- unpushed commits;
- modified tracked files;
- untracked repository content.

The required order is:

`discover → assess accessibility → verify repository-state delta → record evidence/state → evaluate retention`.

VM/environment state outside repository-state delta is excluded unless separately scoped.

An official export by itself MUST NOT be treated as proof that repository-state delta preservation is complete.

Accessibility risk and retention risk are independent. Use `ACCESSIBILITY_ALERT` and temporal alerts as applicable.

## O-24 — Open-Set Discovery Control
`other_discovered_state` is not an unresolved architectural gap. It is the O-24 open-set discovery control.

Any newly discovered material source/platform state MUST be:

1. classified;
2. assigned an applicable preservation route or explicit disposition;
3. evidenced;
4. reconciled.

It MUST NOT be silently omitted.

## Execution boundary
`Moriblo/repo_backup` is the execution/control repository.

Authorized execution routes may READ the authorized source and WRITE only to the authorized preservation destination. They MUST NOT write to `SOURCE_REPOSITORY`.

No particular GitHub Actions workflow is required for the architectural existence of a capability.

## Preservation states
Every discovered material source object ultimately receives one of:

- `PRESERVED`
- `PRESERVED-AS-EQUIVALENT-REPRESENTATION`
- `PARTIALLY-PRESERVED`
- `NON-EXPORTABLE`
- `FAILED`
- `NOT-VERIFIED`

No material state may be silently omitted.

## Evidence, restrictions, manifest and final reporting
`backup/schemas/evidence.schema.yaml` records preservation evidence and carries Capability Preflight restrictions through stable `restriction_id` references.

`backup/schemas/backup-manifest.schema.yaml` is the authoritative machine-readable reconciliation of preserved artifacts, accepted restrictions, limitations, and reconciliation counts.

Until a dedicated final-report schema exists, the validated Manifest is the authoritative input for accepted restrictions and limitations in Final Reporting. Final Reporting MUST NOT remove, weaken, or silently resolve restrictions present in the Manifest.

A clean `COMPLETE` result is permitted only when there are:

- no accepted restrictions;
- no limitations;
- no partial objects;
- no non-exportable objects;
- no failed objects;
- no not-verified objects;
- no unreconciled object classes;
- and restrictions are reconciled.

Otherwise a completed preservation with explicitly accepted exceptions MUST use `COMPLETE_WITH_EXCEPTIONS`, subject to schema validation.

Workflow, route, or tool completion alone is never proof of preservation completion.

## Fail-closed behavior
Stop rather than infer when:

- parameters are absent;
- HITL is ambiguous;
- authorization differs from the Exact Command Request;
- scope exceeds authorization;
- source READ-only cannot be guaranteed;
- destination cannot be validated;
- evidence is insufficient;
- a material Capability Preflight gap has not received its required HITL decision.

A material Capability Preflight gap is not automatically fatal. It is disclosed to HITL. If HITL chooses `CONTINUE_WITH_RESTRICTIONS`, the accepted restrictions remain material limitations throughout Source Inventory, evidence, reconciliation, Manifest, and Final Reporting.

## Portability
No engine may rely on hidden conversation state, prior runs, remembered repository identifiers, or a prior HITL as authorization.

Execution context is established by the current Exact Command Request and its HITL authorization.

The protocol is engine-agnostic: capability is the effective authorized execution capability, not the native capability set of whichever engine happens to be interacting with the human.
