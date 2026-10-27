"""Metadata validation only; no media decoding or physical verification."""

import json
from pathlib import Path, PurePosixPath
import re

from .core import CELLS, ValidationError, token


def fields(value, keys, label):
    if not isinstance(value, dict) or set(value) != set(keys.split()):
        raise ValidationError(f"{label}: expected fields {keys}")


def integer(value, label, minimum=0):
    if type(value) is not int or not minimum <= value <= 10**15:
        raise ValidationError(f"{label}: expected bounded integer >= {minimum}")


def items(value, label, minimum=1, maximum=100):
    if not isinstance(value, list) or not minimum <= len(value) <= maximum:
        raise ValidationError(f"{label}: expected list of {minimum}-{maximum} entries")


def ids(value, label, minimum=1):
    items(value, label, minimum)
    for item in value:
        token(item, label)
    if len(set(value)) != len(value):
        raise ValidationError(f"{label}: duplicate identifier")


def validate_manifest(data):
    fields(data, "schema_version synthetic kit_id parts sources evidence graphs", "manifest")
    if type(data["schema_version"]) is not int or data["schema_version"] != 1:
        raise ValidationError("unsupported schema_version")
    if type(data["synthetic"]) is not bool:
        raise ValidationError("synthetic must be boolean")
    token(data["kit_id"], "kit_id")
    ids(data["parts"], "parts")
    if not 10 <= len(data["parts"]) <= 15:
        raise ValidationError("kit inventory must contain 10-15 individually tracked IDs")
    parts = set(data["parts"])
    items(data["sources"], "sources")
    sources = {}
    for source in data["sources"]:
        fields(source, "id relative_path sha256 segment duration_pts timebase_num timebase_den", "source")
        token(source["id"], "source.id")
        token(source["segment"], "source.segment")
        if source["id"] in sources:
            raise ValidationError("duplicate source ID")
        path = source["relative_path"]
        if (not isinstance(path, str) or not path or len(path) > 240
                or "\\" in path or ":" in path or any(ord(c) < 32 for c in path)
                or PurePosixPath(path).is_absolute() or ".." in path.split("/")):
            raise ValidationError("source path must be a relative portable path without traversal")
        if not isinstance(source["sha256"], str) or not re.fullmatch("[0-9a-f]{64}", source["sha256"]):
            raise ValidationError("source.sha256 must be a lowercase SHA256 digest")
        for key in ("duration_pts", "timebase_num", "timebase_den"):
            integer(source[key], "source." + key, 1)
        sources[source["id"]] = source
    items(data["evidence"], "evidence")
    evidence = {}
    for ref in data["evidence"]:
        fields(ref, "id source_id start_pts end_pts part_id tray_cell", "evidence")
        token(ref["id"], "evidence.id")
        token(ref["source_id"], "evidence.source_id")
        if ref["id"] in evidence:
            raise ValidationError("duplicate evidence ID")
        if ref["source_id"] not in sources:
            raise ValidationError("evidence references unknown source")
        source = sources[ref["source_id"]]
        integer(ref["start_pts"], "start_pts")
        integer(ref["end_pts"], "end_pts", 1)
        if not ref["start_pts"] < ref["end_pts"] <= source["duration_pts"]:
            raise ValidationError("evidence interval must satisfy 0 <= start < end <= duration")
        if not isinstance(ref["part_id"], str) or ref["part_id"] not in parts:
            raise ValidationError("evidence references unknown part")
        if not isinstance(ref["tray_cell"], str) or ref["tray_cell"] not in CELLS:
            raise ValidationError("evidence references unknown tray cell")
        evidence[ref["id"]] = {
            **ref,
            "start_ms": ref["start_pts"] * source["timebase_num"] * 1000 // source["timebase_den"],
            "end_ms": ref["end_pts"] * source["timebase_num"] * 1000 // source["timebase_den"],
        }
    fields(data["graphs"], "disassembly reassembly", "graphs")
    for direction, graph in data["graphs"].items():
        fields(graph, "version reviewed reviewed_by nodes", direction)
        token(graph["version"], "graph.version")
        if graph["reviewed"] is not True:
            raise ValidationError("each graph needs an explicit human review declaration")
        token(graph["reviewed_by"], "graph.reviewed_by")
        items(graph["nodes"], "graph.nodes")
        nodes = {}
        for node in graph["nodes"]:
            fields(node, "id action parts prerequisites evidence_ids", "node")
            token(node["id"], "node.id")
            if node["id"] in nodes:
                raise ValidationError("duplicate node ID")
            action = node["action"]
            if (not isinstance(action, str) or not action.strip() or len(action) > 160
                    or any(ord(c) < 32 for c in action)):
                raise ValidationError("node.action must be nonblank text up to 160 characters")
            ids(node["parts"], "node.parts")
            ids(node["prerequisites"], "node.prerequisites", 0)
            ids(node["evidence_ids"], "node.evidence_ids")
            if not set(node["parts"]) <= parts:
                raise ValidationError("node references unknown part")
            if not set(node["evidence_ids"]) <= evidence.keys():
                raise ValidationError("node references unknown evidence")
            if any(evidence[e]["part_id"] not in node["parts"] for e in node["evidence_ids"]):
                raise ValidationError("node evidence part does not match node parts")
            nodes[node["id"]] = node
        if any(not set(n["prerequisites"]) <= nodes.keys() for n in nodes.values()):
            raise ValidationError("graph references missing prerequisite node")
        pending = set(nodes)
        while pending:
            ready = {key for key in pending if not set(nodes[key]["prerequisites"]) & pending}
            if not ready:
                raise ValidationError("graph contains a cycle")
            pending -= ready
    return {
        "schema_version": 1, "synthetic": data["synthetic"], "kit_id": data["kit_id"],
        "evidence": list(evidence.values()),
        "media_status": "not_inspected", "physical_verification": "unknown",
        "review_status": "human_review_declared_only",
        "graph_versions": {k: v["version"] for k, v in data["graphs"].items()},
    }


def load_manifest(path):
    def unique_pairs(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValidationError("duplicate JSON key")
            result[key] = value
        return result
    with Path(path).open("rb") as stream:
        raw = stream.read(262145)
    if len(raw) > 262144:
        raise ValidationError("manifest exceeds 256 KiB limit")
    try:
        data = json.loads(raw.decode("utf-8"), object_pairs_hook=unique_pairs)
    except (UnicodeError, ValueError, RecursionError) as error:
        raise ValidationError("invalid manifest JSON: " + str(error)) from error
    return validate_manifest(data)
