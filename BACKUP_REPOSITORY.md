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

## Start procedure
1. Read this document and `backup/capabilities.yaml`.
2. Present the Capability Menu.
3. Wait for the human to select a capability.
4. Collect or explicitly confirm every required execution parameter.
5. Construct the exact Command Request using `backup/schemas/command-request.schema.yaml`.
6. Present that request for HITL when required.
7. Execute only after `GO`.
8. Reject on `NO-GO`, missing parameters, scope mismatch, or missing authorization.
9. Collect and validate evidence.
10. Report preservation state and the next applicable HITL.

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

## Execution boundary
`Moriblo/repo_backup` hosts deterministic executors. Executors may READ the authorized source and WRITE to the authorized preservation destination. They MUST NOT write to `SOURCE_REPOSITORY`.

## Preservation states
Every discovered source object ultimately receives one of:
`PRESERVED`, `PRESERVED-AS-EQUIVALENT-REPRESENTATION`, `PARTIALLY-PRESERVED`, `NON-EXPORTABLE`, `FAILED`, `NOT-VERIFIED`.

No material gap may be silently omitted. Secret plaintext is never required; preserve permitted metadata, provenance, references, and reconstruction/reissuance treatment.

## Backup Repository
After HITL authorization, prioritize volatile/temporal objects when applicable, then preserve the complete applicable repository/platform state defined by the governing specification. Source remains READ-only.

## Backup Projects
Independently callable and also included by `backup_repository` when related Projects exist. Approved scopes: `SOURCE_REPOSITORY`, `PROJECT`, `OWNER_PROJECT_SET`. Use authorized READ surfaces including REST/GraphQL as required and produce machine-readable reconstructible equivalent representation.

## Evidence and fail-closed behavior
Workflow completion alone is not proof of backup completion. Evidence must reconcile requested scope against preserved scope.

Until `backup/schemas/evidence.schema.yaml` and `backup/schemas/backup-manifest.schema.yaml` are materialized and validated, no production backup may claim completion.

Stop rather than infer if parameters are absent, HITL is ambiguous, authorization differs from the request, scope exceeds authorization, READ-only cannot be guaranteed, destination cannot be validated, or evidence is insufficient.

## Portability
No engine may rely on hidden conversation state, prior runs, or remembered repository identifiers as authorization. Execution context is established by the current Command Request and HITL.
