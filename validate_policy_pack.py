#!/usr/bin/env python3
"""Validate a policy pack against policy_vault/FORMAT.md.

Checks a pack directory (``pack.config.yml`` + ``*.md`` policy documents) or a
single markdown document, and reports **every** violation as
``filename:line: [VIOLATION|WARNING] code: message``.

Exit status is 1 if there is any VIOLATION, else 0.  Warnings never fail a
pack: they describe things the parser tolerates (and the consequences of
tolerating them) rather than structural defects.

    python validate_policy_pack.py policy_vault/
    python validate_policy_pack.py policy_vault/khda_adek_health_safety_protocols.md

The regexes and truncation limits are imported from ``policy_pack`` so this
checker cannot drift away from what ``build_knowledge_graph.py`` parses.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from policy_pack import (
    CLAUSE_H2_RE,
    COMPETENCY_TEXT_LIMIT,
    DOC_REFERENCE_RE,
    DOC_REFERENCE_WINDOW,
    PACK_CONFIG_FILENAME,
    STRAND_H3_RE,
    TEXT_CONTENT_LIMIT,
    PackConfigError,
    iter_pack_documents,
    load_pack_config,
)

VIOLATION = "VIOLATION"
WARNING = "WARNING"


class Finding:
    __slots__ = ("path", "line", "level", "code", "message")

    def __init__(self, path: Path, line: int, level: str, code: str, message: str) -> None:
        self.path = path
        self.line = line
        self.level = level
        self.code = code
        self.message = message

    def render(self) -> str:
        return f"{self.path}:{self.line}: {self.level} {self.code}: {self.message}"


def _heading_level(line: str) -> int:
    n = 0
    for ch in line:
        if ch == "#":
            n += 1
        else:
            break
    # `#Title` is not a heading the parser recognises; `# Title` is.
    if n and line[n:n + 1] in (" ", "\t"):
        return n
    return 0


def validate_document(path: Path) -> list[Finding]:
    """Check one policy document against the FORMAT.md rules."""
    findings: list[Finding] = []
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()

    # ---- H1 title --------------------------------------------------------
    h1_lines = [i for i, line in enumerate(lines, 1) if _heading_level(line) == 1]
    headings = [(i, _heading_level(line)) for i, line in enumerate(lines, 1)
                if _heading_level(line) >= 1]

    if not h1_lines:
        findings.append(Finding(
            path, 1, VIOLATION, "no-h1",
            "no H1 (`# Title`) line — the loader falls back to the filename stem "
            "as the Policy title, which will not match POLICY_DEPT_RULES"))
    else:
        if headings and headings[0][1] != 1:
            findings.append(Finding(
                path, headings[0][0], VIOLATION, "first-heading-not-h1",
                "first heading is not an H1 — the pack format requires `# Title` "
                "as the document's first heading (-> Policy.title)"))
        if lines[h1_lines[0] - 1][2:].strip() == "":
            findings.append(Finding(
                path, h1_lines[0], VIOLATION, "empty-h1",
                "H1 has no title text — Policy.title would be empty"))
        for extra in h1_lines[1:]:
            findings.append(Finding(
                path, extra, VIOLATION, "multiple-h1",
                "additional H1 — the loader keeps only the first as Policy.title; "
                "this one is silently absorbed into a clause body"))

    # ---- Document reference ---------------------------------------------
    ref_lines = []
    for i, line in enumerate(lines, 1):
        m = DOC_REFERENCE_RE.search(line)
        if m:
            ref_lines.append((i, m.group(1).strip()))

    if not ref_lines:
        findings.append(Finding(
            path, 1, VIOLATION, "missing-doc-ref",
            f"no '**Document Reference:**' line in the first "
            f"{DOC_REFERENCE_WINDOW} lines (-> Policy.reference)"))
    else:
        first_line, first_value = ref_lines[0]
        if first_line > DOC_REFERENCE_WINDOW:
            findings.append(Finding(
                path, first_line, VIOLATION, "doc-ref-late",
                f"'**Document Reference:**' is on line {first_line} but the loader "
                f"only reads the first {DOC_REFERENCE_WINDOW} lines — this "
                f"reference would be dropped"))
        if not first_value:
            findings.append(Finding(
                path, first_line, VIOLATION, "doc-ref-empty",
                "'**Document Reference:**' has no value — Standard codes would "
                "fall back to the Policy title"))
        for dup_line, _ in ref_lines[1:]:
            level = VIOLATION if dup_line <= DOC_REFERENCE_WINDOW else WARNING
            findings.append(Finding(
                path, dup_line, level, "doc-ref-duplicate",
                "additional '**Document Reference:**' line — the loader keeps only "
                "the first one in the reference window"))

    # ---- Clause H2s (`## N. Heading`) ------------------------------------
    clause_starts: list[tuple[int, str, str]] = []  # (line_no, clause_no, heading)
    seen_numbers: dict[int, tuple[str, int]] = {}
    seen_codes: dict[str, int] = {}
    reference_value = ref_lines[0][1] if ref_lines else ""
    title_value = ""
    if h1_lines:
        title_value = lines[h1_lines[0] - 1][2:].strip()
    code_base = (reference_value or title_value).strip()

    for i, line in enumerate(lines, 1):
        stripped = line.strip()
        level = _heading_level(stripped)
        if level != 2:
            continue
        body = stripped[2:].strip()
        m = CLAUSE_H2_RE.match(stripped)
        if m:
            clause_no, heading = m.group(1), m.group(2).strip()
            clause_starts.append((i, clause_no, heading))

            if not heading:
                findings.append(Finding(
                    path, i, VIOLATION, "empty-clause-heading",
                    f"clause {clause_no} has no heading text"))
            elif heading[0].isdigit():
                findings.append(Finding(
                    path, i, WARNING, "suspicious-clause-heading",
                    f"clause {clause_no} heading starts with a digit ('{heading}') — "
                    f"a sub-numbered H2 such as '## 4.1 x' is parsed as clause 4 "
                    f"with heading '1 x'"))
            if len(clause_no) > 1 and clause_no.startswith("0"):
                findings.append(Finding(
                    path, i, WARNING, "leading-zero-clause",
                    f"clause number '{clause_no}' has leading zeros — it codes as "
                    f"'{code_base}§{clause_no}', distinct from '§{int(clause_no)}'"))

            as_int = int(clause_no)
            if as_int in seen_numbers:
                prev_no, prev_line = seen_numbers[as_int]
                findings.append(Finding(
                    path, i, VIOLATION, "duplicate-clause",
                    f"clause number {clause_no} duplicates clause {prev_no} "
                    f"on line {prev_line} (same integer)"))
            else:
                seen_numbers[as_int] = (clause_no, i)

            code = f"{code_base}§{clause_no}"
            if code in seen_codes:
                findings.append(Finding(
                    path, i, VIOLATION, "duplicate-clause-code",
                    f"Standard code '{code}' already produced on line "
                    f"{seen_codes[code]} — the two clauses would MERGE into one node"))
            else:
                seen_codes[code] = i
        else:
            # An H2 that is not a clause.
            if body[:1].isdigit():
                findings.append(Finding(
                    path, i, VIOLATION, "bad-clause-h2",
                    f"H2 '{stripped}' looks like a clause but does not match "
                    f"`## <integer>. <heading>` (integer, period, space) — it will "
                    f"NOT become a Standard and its text is absorbed into the "
                    f"previous clause"))
            elif body.lower().startswith("strand"):
                findings.append(Finding(
                    path, i, VIOLATION, "strand-wrong-level",
                    f"H2 '{stripped}' looks like a competency strand — strands are "
                    f"H3 (`### Strand X — Name`) and this will not become a Competency"))
            else:
                findings.append(Finding(
                    path, i, WARNING, "non-clause-h2",
                    f"H2 '{stripped}' is not a clause (`## N. Heading`) — it becomes "
                    f"no Standard, and its text is absorbed into the previous "
                    f"clause's body (or dropped if it precedes every clause)"))

    if not clause_starts:
        findings.append(Finding(
            path, 1, VIOLATION, "no-clauses",
            "no `## N. Heading` clause H2s — the pack would load a Policy with no "
            "Standards, so no risk can be ADDRESSED_BY anything"))

    # ---- Clause body truncation -----------------------------------------
    for idx, (start, clause_no, _heading) in enumerate(clause_starts):
        end = clause_starts[idx + 1][0] if idx + 1 < len(clause_starts) else len(lines) + 1
        body = "\n".join(lines[start:end - 1]).strip()
        if len(body) > TEXT_CONTENT_LIMIT:
            findings.append(Finding(
                path, start, WARNING, "truncated-section",
                f"clause {clause_no} body is {len(body)} chars — only the first "
                f"{TEXT_CONTENT_LIMIT} are stored on Standard.text_content"))

    # ---- Competency strands (`### Strand X — Name`) ----------------------
    strand_seen: dict[str, int] = {}
    for i, line in enumerate(lines, 1):
        stripped = line.strip()
        if _heading_level(stripped) != 3:
            continue
        body = stripped[3:].strip()
        if not body.lower().startswith("strand"):
            continue
        m = STRAND_H3_RE.match(stripped)
        if not m:
            findings.append(Finding(
                path, i, VIOLATION, "bad-strand",
                f"H3 '{stripped}' looks like a strand but does not match "
                f"`### Strand <A-F> — <Name>` (em-dash U+2014, letter A-F, "
                f"non-empty name) — it will NOT become a Competency"))
            continue
        code = m.group(1).replace(" ", "")
        if code in strand_seen:
            findings.append(Finding(
                path, i, VIOLATION, "duplicate-strand",
                f"strand code '{code}' already produced on line "
                f"{strand_seen[code]} — the two would MERGE into one Competency"))
        else:
            strand_seen[code] = i

        # Body length measured on the parsed description (to the next ## / ###).
        start_idx = i  # 1-based -> slice below starts after the H3
        end_idx = len(lines) + 1
        for j in range(start_idx, len(lines)):
            nxt = lines[j]
            if _heading_level(nxt) in (2, 3):
                end_idx = j + 1
                break
        desc = "\n".join(lines[start_idx:end_idx - 1]).strip()
        if len(desc) > COMPETENCY_TEXT_LIMIT:
            findings.append(Finding(
                path, i, WARNING, "truncated-competency",
                f"strand '{code}' body is {len(desc)} chars — only the first "
                f"{COMPETENCY_TEXT_LIMIT} are stored on Competency.description"))

    return findings


def validate_config(pack_dir: Path, findings: list[Finding]) -> None:
    """Check pack.config.yml and its references into the documents."""
    cfg_path = pack_dir / PACK_CONFIG_FILENAME
    if not cfg_path.is_file():
        findings.append(Finding(
            cfg_path, 1, VIOLATION, "config-missing",
            f"{PACK_CONFIG_FILENAME} not found — a pack must ship its "
            f"DEPARTMENTS / POLICY_DEPT_RULES / RISK_POLICY_MAP here"))
        return

    try:
        config = load_pack_config(pack_dir)
    except PackConfigError as exc:
        findings.append(Finding(cfg_path, 1, VIOLATION, "config-invalid", str(exc)))
        return

    docs = iter_pack_documents(pack_dir)
    stems = {p.stem: p for p in docs}

    # Index each document's clause headings (parsed, i.e. the text after `## N. `)
    headings_by_stem: dict[str, list[tuple[str, str]]] = {}
    for doc in docs:
        pairs: list[tuple[str, str]] = []
        for line in doc.read_text(encoding="utf-8").splitlines():
            m = CLAUSE_H2_RE.match(line.strip())
            if m:
                pairs.append((m.group(1), m.group(2).strip()))
        headings_by_stem[doc.stem] = pairs

    for risk_type, entry in config.risk_policy_map.items():
        stem = entry["policy_file"]
        keyword = entry["clause_keyword"]
        if stem not in stems:
            findings.append(Finding(
                cfg_path, 1, VIOLATION, "config-unknown-policy-file",
                f"RISK_POLICY_MAP.{risk_type}.policy_file '{stem}' has no "
                f"'{stem}.md' in this pack"))
            continue
        headings = [h for _n, h in headings_by_stem.get(stem, [])]
        if not any(keyword.lower() in h.lower() for h in headings):
            findings.append(Finding(
                cfg_path, 1, VIOLATION, "config-keyword-unmatched",
                f"RISK_POLICY_MAP.{risk_type}.clause_keyword '{keyword}' matches "
                f"no clause heading in {stem}.md — the loader would silently fall "
                f"back to that document's first clause"))


def validate_path(target: Path) -> list[Finding]:
    if target.is_dir():
        findings: list[Finding] = []
        docs = iter_pack_documents(target)
        if not docs:
            findings.append(Finding(
                target, 1, VIOLATION, "no-documents",
                "pack directory contains no policy documents (*.md)"))
        for doc in docs:
            findings.extend(validate_document(doc))
        validate_config(target, findings)
        return findings
    if target.is_file():
        return validate_document(target)
    return [Finding(target, 1, VIOLATION, "not-found", "no such file or directory")]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("path", help="pack directory or single policy markdown file")
    parser.add_argument("--warnings-as-errors", action="store_true",
                        help="fail the run on warnings too")
    args = parser.parse_args(argv)

    target = Path(args.path)
    findings = validate_path(target)
    for finding in findings:
        print(finding.render())

    violations = [f for f in findings if f.level == VIOLATION]
    warnings = [f for f in findings if f.level == WARNING]
    print()
    print(f"SUMMARY: {len(violations)} violations, {len(warnings)} warnings "
          f"({target})")
    if violations:
        return 1
    if warnings and args.warnings_as_errors:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
