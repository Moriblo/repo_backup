# Backup Repository — Portable Execution Protocol

## Purpose
Authoritative portable entry point for the Full Repository Preservation Process. An AI engine MUST be able to start without prior conversation memory.

## Non-negotiable rules
1. Never infer authorization.
2. Never mutate `SOURCE_REPOSITORY`.
3. Never inherit execution-specific parameters from a prior run.
4. Never treat missing evidence as success.
5. Never expose secret plaintext.
6. HITL applies only to the exact Command Request presented.
7. Authorization is non-transitive and non-reusable.
8. Backup never authorizes cleanup, deletion, rename, archive, or source modification.
9. Inability to read or discover an object MUST NOT be interpreted as evidence that the object is absent.
10. Any restriction accepted by HITL MUST remain traceable through inventory, evidence, reconciliation, and final reporting.

## Start procedure
1. Read this document and `backup/capabilities.yaml`.
2. Present the Capability Menu.
3. Wait for the human to select a capability.
4. Collect or explicitly confirm every required execution parameter.
5. Construct the exact Command Request using `backup/schemas/command-request.schema.yaml`.
6. Present that request for HITL when required.
7. Execute only after `GO`.
8. Reject on `NO-GO`, missing parameters, scope mismatch, or missing authorization.
9. For `backup_repository`, execute the mandatory Capability Preflight before Source Inventory.
10. If the Capability Preflight has no gaps, proceed to Source Inventory.
11. If gaps exist, present each gap, its cause, impact, and resulting restriction to HITL and request `STOP` or `CONTINUE_WITH_RESTRICTIONS`.
12. On `STOP`, do not start Source Inventory. On `CONTINUE_WITH_RESTRICTIONS`, record the explicitly accepted restrictions and proceed without treating any gap as absence or success.
13. Collect and validate evidence.
14. Report preservation state, all accepted restrictions, and the next applicable HITL.

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

`backup_repository` requires at minimum `source_repository` and `destination`.

`backup_projects` requires `scope` (`SOURCE_REPOSITORY`, `PROJECT`, or `OWNER_PROJECT_SET`), the corresponding identifiers, and `destination`.

After authorization, changing any execution parameter requires a new HITL.

## Capability Preflight
For `backup_repository`, Capability Preflight is mandatory and MUST occur after execution authorization and before Source Inventory.

The engine MUST assess its actual READ capability for every applicable object class declared by the selected capability. The assessment MUST distinguish successful discovery/read capability from tool, connector, API, permission, or other access limitations.

If no gap is identified, Source Inventory may proceed.

If one or more gaps are identified, the engine MUST present to HITL, for every gap:
- what cannot be read or discovered;
- the cause;
- the impact on preservation;
- the resulting restriction.

HITL MUST explicitly decide:
- `STOP`; or
- `CONTINUE_WITH_RESTRICTIONS`.

`STOP` prevents Source Inventory from starting.

`CONTINUE_WITH_RESTRICTIONS` authorizes Source Inventory to proceed only under the restrictions explicitly accepted by HITL. Accepted restrictions MUST remain traceable throughout Source Inventory, evidence, reconciliation, and final reporting.

A read/discovery limitation MUST NEVER be interpreted as evidence that an object is absent, not applicable, preserved, or successfully verified.

## Execution boundary
`Moriblo/repo_backup` hosts deterministic executors. Executors may READ the authorized source and WRITE to the authorized preservation destination. They MUST NOT write to `SOURCE_REPOSITORY`.

## Preservation states
Every discovered source object ultimately receives one of:
`PRESERVED`, `PRESERVED-AS-EQUIVALENT-REPRESENTATION`, `PARTIALLY-PRESERVED`, `NON-EXPORTABLE`, `FAILED`, `NOT-VERIFIED`.

No material gap may be silently omitted. Secret plaintext is never required; preserve permitted metadata, provenance, references, and reconstruction/reissuance treatment.

## Backup Repository
After HITL authorization and the mandatory Capability Preflight decision, prioritize volatile/temporal objects when applicable, then preserve the complete applicable repository/platform state defined by the governing specification. Source remains READ-only.

## Backup Projects
Independently callable and also included by `backup_repository` when related Projects exist. Approved scopes: `SOURCE_REPOSITORY`, `PROJECT`, `OWNER_PROJECT_SET`. Use authorized READ surfaces including REST/GraphQL as required and produce machine-readable reconstructible equivalent representation.

## Evidence and fail-closed behavior
Workflow completion alone is not proof of backup completion. Evidence must reconcile requested scope against preserved scope.

Until `backup/schemas/evidence.schema.yaml` and `backup/schemas/backup-manifest.schema.yaml` are materialized and validated, no production backup may claim completion.

Stop rather than infer if parameters are absent, HITL is ambiguous, authorization differs from the request, scope exceeds authorization, READ-only cannot be guaranteed, destination cannot be validated, or evidence is insufficient.

A Capability Preflight gap is not silently fatal: the engine MUST disclose it and obtain the explicit HITL decision defined above. If HITL chooses `CONTINUE_WITH_RESTRICTIONS`, those restrictions remain material limitations of the execution and MUST be carried through final reporting.

## Portability
No engine may rely on hidden conversation state, prior runs, or remembered repository identifiers as authorization. Execution context is established by the current Command Request and HITL.
