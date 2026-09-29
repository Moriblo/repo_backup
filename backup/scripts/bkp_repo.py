#!/usr/bin/env python3
"""Deterministic helpers for the BKP_REPO workflow (backup-repository.yml).

Subcommands:
  validate-inputs       revalidate workflow_call inputs against commands-log-line schema
  preflight             Capability Preflight; exit 10 = BLOCKED (HITL decision required)
  validate-destination  fail-closed destination check; exit 20 = not validated
  build-evidence        build and schema-validate evidence.json and manifest.json

Every outcome is also printed as one `BKP_RESULT {json}` line so the engine can
read it from the job log.
"""
import datetime
import hashlib
import json
import os
import pathlib
import sys

import jsonschema
import yaml

ROOT = pathlib.Path(__file__).resolve().parents[2]
EVIDENCE = pathlib.Path(os.environ.get("EVIDENCE_DIR", "evidence"))
GAP_ACCESS = "ACCESS_PERMISSION_GAP"
GAP_EXEC = "EXECUTION_CAPABILITY_GAP"

# Object classes this executor actually reads and preserves. Everything else in
# the capability's `includes` list is an EXECUTION_CAPABILITY_GAP until an
# executor route is implemented (never treated as absent or preserved).
IMPLEMENTED_CLASSES = {"git"}


def load(path):
    with open(ROOT / path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def now():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def result(status, **extra):
    payload = {"status": status, "request_id": os.environ.get("REQUEST_ID", ""), **extra}
    print("BKP_RESULT " + json.dumps(payload, sort_keys=True), flush=True)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as f:
            f.write("```json\n" + json.dumps(payload, indent=2, sort_keys=True) + "\n```\n")


def set_output(key, value):
    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a", encoding="utf-8") as f:
            f.write(f"{key}={value}\n")


def parse_decision():
    raw = os.environ.get("PREFLIGHT_DECISION", "").strip()
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        print(f"::error::preflight_decision is not valid JSON: {exc}")
        sys.exit(2)


def cmd_validate_inputs():
    params = {
        "source_repository": os.environ.get("SOURCE_REPOSITORY", ""),
        "destination": os.environ.get("DESTINATION", ""),
    }
    decision = parse_decision()
    if decision is not None:
        params["preflight_decision"] = decision
    line = {
        "request_id": os.environ.get("REQUEST_ID", ""),
        "mnemonic": "BKP_REPO",
        "ts": now(),
        "authorization": {"decision": "GO", "authorized_scope": "EXACT_COMMAND_REQUEST", "reusable": False},
        "params": params,
    }
    schema = load("backup/schemas/commands-log-line.schema.yaml")
    errors = sorted(jsonschema.Draft202012Validator(schema).iter_errors(line), key=str)
    if errors:
        for e in errors:
            print(f"::error::input validation: {e.message}")
        result("REJECTED", reason="INPUT_VALIDATION")
        sys.exit(2)
    if decision and decision["previous_request_id"] == line["request_id"]:
        print("::error::preflight_decision.previous_request_id must differ from request_id")
        result("REJECTED", reason="AUTHORIZATION_REUSE")
        sys.exit(2)
    print(f"Inputs valid for {params['source_repository']}")


def build_preflight():
    caps = load("backup/capabilities.yaml")
    cap = next(c for c in caps["capabilities"] if c["id"] == "backup_repository")
    token_ok = os.environ.get("TOKEN_OUTCOME") == "success"
    assessments, gaps, pending = [], [], []
    for cls in cap["includes"]:
        spec = caps["object_classes"][cls]
        implemented = cls in IMPLEMENTED_CLASSES
        routes = []
        for rt in spec["approved_route_types"]:
            if rt == "DETERMINISTIC_EXECUTOR":
                if implemented and token_ok:
                    state, lim = "AVAILABLE", None
                elif implemented:
                    state, lim = "UNAVAILABLE", "Read-only source token could not be issued for the source repository."
                else:
                    state, lim = "UNAVAILABLE", "This executor has no implemented route for the object class."
                routes.append({"route_type": rt, "route_reference": "github_actions:backup-repository.yml",
                               "state": state, "authorized": True, "limitation": lim})
            else:
                routes.append({"route_type": rt, "route_reference": None, "state": "NOT_VERIFIED",
                               "authorized": False, "limitation": "Route not assessed; only the deterministic executor is in scope of this execution."})
        effective = "AVAILABLE" if implemented and token_ok else "UNAVAILABLE"
        assessments.append({
            "object_class": cls,
            "architectural_status": spec["architectural_status"],
            "required_read_capabilities": spec.get("required_read_capabilities") or spec.get("required_behavior"),
            "routes": routes,
            "effective_read_capability": effective,
        })
        if effective != "AVAILABLE":
            cls_gap = GAP_ACCESS if implemented else GAP_EXEC
            cause = ("Read-only GitHub App token could not be issued for the source repository."
                     if implemented else "Executor route for this object class is not implemented yet.")
            gaps.append({
                "object_class": cls,
                "gap_classification": cls_gap,
                "limitation": f"{cls} cannot be read or discovered by this execution.",
                "required_capability": ", ".join(spec.get("required_read_capabilities") or spec.get("required_behavior")),
                "routes_assessed": [r["route_type"] for r in routes],
                "cause": cause,
                "preservation_impact": f"{cls} is not preserved; its state is NOT-VERIFIED, never treated as absent.",
                "resulting_restriction": f"{cls} excluded from this preservation and reported NOT-VERIFIED.",
            })
        if cls == "git":
            pending.append({"object_class": "git", "pending_fact": "Git LFS usage and submodule presence in the source", "route_defined": True})
    return assessments, gaps, pending


def restriction_id(gap):
    return f"RST-{gap['object_class']}-{gap['gap_classification']}"


def cmd_preflight():
    assessments, gaps, pending = build_preflight()
    pf = {"required": True, "status": "PASS", "assessments": assessments, "gaps": gaps,
          "factual_inventory_pending": pending, "hitl_decision": "NOT_REQUIRED", "accepted_restrictions": []}
    exit_code = 0
    if gaps:
        pf["status"] = "GAPS_IDENTIFIED"
        pf["hitl_decision"] = "PENDING"
        needed = {restriction_id(g): g for g in gaps}
        decision = parse_decision()
        accepted = {a["restriction_id"]: a for a in (decision or {}).get("accepted_restrictions", [])}
        if decision and set(needed) <= set(accepted):
            pf["hitl_decision"] = "CONTINUE_WITH_RESTRICTIONS"
            pf["accepted_restrictions"] = [
                {"restriction_id": rid, "object_class": g["object_class"],
                 "restriction": g["resulting_restriction"], "source_gap_classification": g["gap_classification"]}
                for rid, g in needed.items()
            ]
        else:
            exit_code = 10
    schema = load("backup/schemas/command-request.schema.yaml")["properties"]["capability_preflight"]
    jsonschema.validate(pf, schema)
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    (EVIDENCE / "preflight.json").write_text(json.dumps(pf, indent=2, sort_keys=True) + "\n")
    required = [{"restriction_id": restriction_id(g), "object_class": g["object_class"],
                 "restriction": g["resulting_restriction"], "source_gap_classification": g["gap_classification"]} for g in gaps]
    if exit_code:
        result("BLOCKED", reason="CAPABILITY_PREFLIGHT_GAPS", hitl_decision="PENDING",
               required_restrictions=required)
        print("::error::Material Capability Preflight gaps require HITL: STOP or CONTINUE_WITH_RESTRICTIONS.")
    else:
        result("PREFLIGHT_OK", preflight_status=pf["status"], hitl_decision=pf["hitl_decision"],
               accepted_restrictions=[a["restriction_id"] for a in pf["accepted_restrictions"]])
    sys.exit(exit_code)


def cmd_validate_destination():
    # PR 4 implements the OneDrive (delegated OAuth, Files.ReadWrite.AppFolder)
    # destination check. Until then destination validation always fails closed.
    if os.environ.get("ONEDRIVE_DESTINATION_VALIDATED") == "true":
        set_output("destination_validated", "true")
        return
    result("BLOCKED", reason="DESTINATION_NOT_VALIDATED",
           detail="No OneDrive destination route is implemented in this executor version.")
    print("::error::Destination cannot be validated; failing closed before Source Inventory.")
    sys.exit(20)


def lines(name):
    p = EVIDENCE / name
    return [x for x in p.read_text(errors="replace").splitlines() if x] if p.exists() else []


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def cmd_build_evidence():
    if os.environ.get("DESTINATION_VALIDATED") != "true":
        print("::error::Refusing to build evidence: destination was not validated.")
        sys.exit(21)
    request_id = os.environ["REQUEST_ID"]
    source = os.environ["SOURCE_REPOSITORY"]
    pf = json.loads((EVIDENCE / "preflight.json").read_text())
    accepted = {a["object_class"]: a for a in pf["accepted_restrictions"]}
    started = os.environ.get("STARTED_AT", now())

    objects = []
    git_disp, git_lim = "PRESERVED", None
    if lines("gitlinks-history.tsv"):
        git_disp = "PARTIALLY-PRESERVED"
        git_lim = "Submodule gitlinks recorded; submodule contents are not preserved by this executor."
    if not lines("refs.tsv"):
        git_disp, git_lim = "FAILED", "No refs enumerated from the mirror."
    objects.append({"object_class": "git", "object_id": source, "disposition": git_disp,
                    "evidence": ["refs.tsv", "git-fsck.txt", "git-count-objects.txt", "lfs-files.txt",
                                 "lfs-verification.tsv", "gitmodules-history.tsv", "gitlinks-history.tsv"],
                    "limitation": git_lim})
    limitations = []
    if git_lim:
        limitations.append({"limitation_id": "LIM-git-submodules", "description": git_lim,
                            "restriction_id": None, "object_class": "git"})
    for cls, a in accepted.items():
        if cls == "git":
            continue
        objects.append({"object_class": cls, "object_id": f"{source}#{cls}", "disposition": "NOT-VERIFIED",
                        "evidence": ["preflight.json"], "limitation": a["restriction"],
                        "restriction_ids": [a["restriction_id"]]})
        limitations.append({"limitation_id": f"LIM-{cls}", "description": a["restriction"],
                            "restriction_id": a["restriction_id"], "object_class": cls})

    pf_ev = {"status": pf["status"],
             "gaps": [{k: g[k] for k in ("object_class", "gap_classification", "limitation", "preservation_impact", "resulting_restriction")} for g in pf["gaps"]],
             "factual_inventory_pending": [{"object_class": p["object_class"], "pending_fact": p["pending_fact"]} for p in pf["factual_inventory_pending"]],
             "hitl_decision": pf["hitl_decision"], "accepted_restrictions": pf["accepted_restrictions"]}
    evidence = {
        "schema_version": "2.0", "request_id": request_id, "capability_id": "backup_repository",
        "source_scope": {"source_repository": source, "destination": os.environ["DESTINATION"]},
        "captured_at": now(), "capability_preflight": pf_ev, "objects": objects,
        "execution": {"source_repository_mode": "READ_ONLY", "destination_validated": True,
                      "authorization_request_id": request_id, "restrictions_reconciled": True,
                      "temporal_preservation": {"mrbi": None, "crr": None,
                                                "notes": ["Temporal classes are not read by this executor version."]}},
    }
    jsonschema.validate(evidence, load("backup/schemas/evidence.schema.yaml"))
    (EVIDENCE / "evidence.json").write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n")

    count = lambda d: sum(o["disposition"] == d for o in objects)
    arts = [{"path": p.name, "sha256": sha256(p), "object_class": "evidence", "bytes": p.stat().st_size}
            for p in sorted(EVIDENCE.iterdir()) if p.is_file() and p.name != "manifest.json"]
    exceptions = bool(limitations or pf["accepted_restrictions"] or count("FAILED"))
    status = "FAILED" if count("FAILED") else ("COMPLETE_WITH_EXCEPTIONS" if exceptions else "COMPLETE")
    manifest = {
        "schema_version": "2.0", "request_id": request_id, "capability_id": "backup_repository",
        "scope": evidence["source_scope"], "started_at": started, "completed_at": now(), "status": status,
        "capability_preflight": {"status": pf["status"], "hitl_decision": pf["hitl_decision"],
                                 "accepted_restrictions": pf["accepted_restrictions"]},
        "artifacts": arts,
        "reconciliation": {"preserved": count("PRESERVED"), "equivalent": count("PRESERVED-AS-EQUIVALENT-REPRESENTATION"),
                           "partial": count("PARTIALLY-PRESERVED"), "non_exportable": count("NON-EXPORTABLE"),
                           "failed": count("FAILED"), "not_verified": count("NOT-VERIFIED"),
                           "unreconciled_object_classes": [], "restrictions_reconciled": True},
        "limitations": limitations,
    }
    jsonschema.validate(manifest, load("backup/schemas/backup-manifest.schema.yaml"))
    (EVIDENCE / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    result(status, evidence_sha256=sha256(EVIDENCE / "evidence.json"),
           manifest_sha256=sha256(EVIDENCE / "manifest.json"), reconciliation=manifest["reconciliation"])
    if status == "FAILED":
        sys.exit(1)


COMMANDS = {"validate-inputs": cmd_validate_inputs, "preflight": cmd_preflight,
            "validate-destination": cmd_validate_destination, "build-evidence": cmd_build_evidence}

if __name__ == "__main__":
    if len(sys.argv) != 2 or sys.argv[1] not in COMMANDS:
        print(__doc__)
        sys.exit(64)
    COMMANDS[sys.argv[1]]()
