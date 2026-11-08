#!/usr/bin/env python3
"""Axelrod build guard: a Claude Code PreToolUse hook for this repo.

Claude Code pipes the hook JSON to stdin before every Bash, Read, Edit, Write,
MultiEdit and NotebookEdit call. When a rule matches, the guard prints one of:

    deny   the call does not run, and the reason goes back to Claude
    ask    Gordon confirms the call in the Claude Code UI

When nothing matches it prints nothing, so the normal permission rules apply.
It never answers "allow".

The rules, in short:
    secrets  never print, read or write AWS credentials or private keys
    aws      us-east-1 only, workshop credentials only (no --profile), no IAM
             users or access keys, nothing expensive, deletes need Gordon
    git      push only backend and demo-ok tags, no force pushes or history
             rewrites, stage explicit paths, scan for credentials before commit
    files    on the backend branch, edits stay inside backend/ and .claude/
    shell    no recursive deletes outside scratch paths, no curl | sh

Every deny and ask is appended to .claude/guard.log (gitignored), with
anything that looks like a credential redacted.

Self-test: `echo AXELROD_GUARD_SELFTEST` must come back denied. If it prints
instead, the hook is not running.

New code, written for SCU ACM AWS x A10 Hack 2026.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

PROJECT = Path(os.environ.get("CLAUDE_PROJECT_DIR") or Path(__file__).resolve().parents[2]).resolve()
LOG = PROJECT / ".claude" / "guard.log"
REGION = "us-east-1"
OWN_DIRS = ("backend", ".claude")              # the only top-level dirs this branch changes
PROTECTED_DIRS = {".git", "contract", "mock", "docs", "frontend"}
SCRATCH_NAMES = {"__pycache__", ".pytest_cache", ".ruff_cache", ".mypy_cache", ".venv", "tmp", ".tmp", "node_modules"}
EC2_TYPES = {"t3.micro", "t3.small", "t3.medium", "t3.large"}
HOME = Path.home().resolve()

KEY_ID_RE = re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")
KEY_NAME_RE = re.compile(r"aws_(?:secret_access_key|session_token)\s*[=:]", re.I)
PRIVATE_KEY_RE = re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----")
SECRET_ENV_RE = re.compile(r"AWS_SECRET_ACCESS_KEY|AWS_SESSION_TOKEN|\$\{?AWS_ACCESS_KEY_ID\b")

DENY, ASK = "deny", "ask"
RANK = {None: 0, ASK: 1, DENY: 2}


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def has_secret(text: str) -> bool:
    return bool(text) and bool(KEY_ID_RE.search(text) or KEY_NAME_RE.search(text) or PRIVATE_KEY_RE.search(text))


def redact(text: str) -> str:
    text = KEY_ID_RE.sub("[redacted-key-id]", text or "")
    text = re.sub(r"(?i)(aws_(?:secret_access_key|session_token)\s*[=:]\s*)\S+", r"\1[redacted]", text)
    text = re.sub(r"(?i)\b(AWS_(?:SECRET_ACCESS_KEY|SESSION_TOKEN|ACCESS_KEY_ID)\s*=\s*)\S+", r"\1[redacted]", text)
    return text[:400]


def git(*args: str, timeout: float = 4.0) -> str | None:
    try:
        out = subprocess.run(["git", "-C", str(PROJECT), *args], capture_output=True,
                             text=True, encoding="utf-8", errors="replace", timeout=timeout)
        return out.stdout if out.returncode == 0 else None
    except Exception:
        return None


_branch_cache: list[str | None] = []


def branch() -> str | None:
    if not _branch_cache:
        out = git("rev-parse", "--abbrev-ref", "HEAD")
        _branch_cache.append(out.strip() if out else None)
    return _branch_cache[0]


def strict_branch() -> bool:
    """Path protection applies on backend, and when the branch can't be read."""
    b = branch()
    return b is None or b == "backend"


def resolve(path: str, cwd: str | None) -> Path:
    p = Path(os.path.expanduser(path.replace("$HOME", "~").replace("${HOME}", "~")))
    if not p.is_absolute():
        p = Path(cwd or PROJECT) / p
    try:
        return p.resolve()
    except Exception:
        return p


def rel_to_project(p: Path) -> Path | None:
    try:
        return p.relative_to(PROJECT)
    except ValueError:
        return None


def under(p: Path, root: Path) -> bool:
    try:
        p.relative_to(root)
        return True
    except ValueError:
        return False


class Verdict:
    def __init__(self) -> None:
        self.decision: str | None = None
        self.reasons: list[str] = []
        self.rules: list[str] = []

    def add(self, decision: str, rule: str, reason: str) -> None:
        if RANK[decision] > RANK[self.decision]:
            self.decision = decision
        if reason not in self.reasons:
            self.reasons.append(reason)
            self.rules.append(rule)


# --------------------------------------------------------------------------
# Bash
# --------------------------------------------------------------------------

WRAPPERS = {"sudo", "nohup", "time", "command", "exec", "builtin"}


def split_segments(cmd: str) -> list[list[str]] | None:
    """Tokenize a shell command into simple commands. None if it can't be parsed."""
    try:
        lex = shlex.shlex(cmd.replace("\r", ""), posix=True, punctuation_chars=";&|()")
        lex.whitespace_split = True
        lex.commenters = ""
        toks = list(lex)
    except ValueError:
        return None
    segs: list[list[str]] = [[]]
    for t in toks:
        if t and set(t) <= set(";&|()"):
            segs.append([])
        else:
            segs[-1].append(t)
    # newlines are separators too, but shlex treats them as whitespace: split
    # again on lines for multi-line commands without heredocs
    return [s for s in segs if s]


def strip_prefix(tokens: list[str]) -> tuple[dict[str, str], list[str]]:
    env: dict[str, str] = {}
    i = 0
    while i < len(tokens):
        t = tokens[i]
        if re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", t):
            k, v = t.split("=", 1)
            env[k] = v
            i += 1
        elif t in WRAPPERS:
            i += 1
        elif t == "env":
            i += 1
        elif t == "timeout" and i + 1 < len(tokens):
            i += 2
        elif t in ("uv", "poetry") and i + 1 < len(tokens) and tokens[i + 1] == "run":
            i += 2
        else:
            break
    return env, tokens[i:]


def check_raw(cmd: str, v: Verdict) -> None:
    if "AXELROD_GUARD_SELFTEST" in cmd:
        v.add(DENY, "selftest", "Guard is live (self-test). This is the expected result.")
        return
    if SECRET_ENV_RE.search(cmd):
        v.add(DENY, "secret-env", "Never read, print or set AWS credential variables. Gordon manages credentials outside Claude.")
    if re.search(r"\.aws[/\\](credentials|sso|cli)", cmd) or re.search(r"\.ssh[/\\]", cmd):
        v.add(DENY, "secret-file", "Never read credential files (~/.aws/credentials, ~/.ssh).")
    if re.search(r"(^|[;&|(]\s*)(printenv|env|set|export\s+-p|declare\s+-[px]+)\s*($|[;&|)])", cmd, re.M):
        v.add(DENY, "env-dump", "That prints every environment variable, including the AWS credentials. Print one named variable instead.")
    if re.search(r"os\.environ(\.items\(\)|\.copy\(\)|\s*\))|get_frozen_credentials|\.get_credentials\(", cmd):
        v.add(DENY, "env-dump-py", "That would print the AWS credentials. Read one named variable instead.")
    if has_secret(cmd):
        v.add(DENY, "secret-literal", "The command contains what looks like an AWS credential or private key.")
    if re.search(r"\b(curl|wget)\b[^\n]*\|\s*(sudo\s+)?(sh|bash|zsh)\b", cmd) or \
       re.search(r"\b(curl|wget)\b[^\n]*\|\s*(python3?|py)\s*(-\s*)?($|[;&|])", cmd, re.M):
        v.add(DENY, "pipe-to-shell", "Don't pipe a download into a shell. Download the file, read it, then run it.")
    if re.search(r"Remove-Item\b[^\n]*-Recurse|\brmdir\s+/s\b|\bdel\s+/s\b", cmd, re.I):
        v.add(ASK, "win-recursive-delete", "Recursive delete through PowerShell or cmd. Gordon confirms.")
    if re.search(r"\bfind\b[^\n]*\s-(delete|exec\s+rm)\b", cmd):
        v.add(ASK, "find-delete", "find with -delete or -exec rm. Gordon confirms.")


def opt_value(tokens: list[str], name: str) -> str | None:
    for i, t in enumerate(tokens):
        if t == name and i + 1 < len(tokens):
            return tokens[i + 1]
        if t.startswith(name + "="):
            return t.split("=", 1)[1]
    return None


AWS_VALUE_OPTS = {"--region", "--profile", "--output", "--endpoint-url", "--query", "--color",
                  "--ca-bundle", "--cli-read-timeout", "--cli-connect-timeout", "--cli-binary-format"}


def check_aws(env: dict[str, str], toks: list[str], cmd: str, v: Verdict) -> None:
    args = toks[1:]
    words: list[str] = []
    skip = False
    for t in args:
        if skip:
            skip = False
            continue
        if t.startswith("--"):
            if t in AWS_VALUE_OPTS:
                skip = True
            continue
        words.append(t)
    service = words[0] if words else ""
    op = words[1] if len(words) > 1 else ""

    if "--profile" in args or any(t.startswith("--profile=") for t in args) or "AWS_PROFILE" in env:
        v.add(DENY, "aws-profile", "This build uses the workshop credentials only. --profile could reach another account (Gordon's personal one).")
    region = opt_value(args, "--region") or env.get("AWS_REGION") or env.get("AWS_DEFAULT_REGION")
    if region and region != REGION:
        v.add(DENY, "aws-region", f"Hackathon resources live in {REGION} only (organizer rule). Got region {region}.")

    if service == "configure":
        if op in ("get", "export-credentials", "import"):
            v.add(DENY, "aws-configure-read", "That prints or imports credentials. Use `aws sts get-caller-identity` to check who you are.")
        elif op == "set" and len(words) > 2 and not words[2].startswith(("region", "output", "default.region", "cli_pager")):
            v.add(DENY, "aws-configure-set", "Only region and output may be set. Credentials stay with Gordon.")
    if service == "iam" and op in {"create-access-key", "create-user", "create-login-profile", "update-login-profile",
                                   "attach-user-policy", "put-user-policy", "add-user-to-group",
                                   "create-service-specific-credential", "update-access-key", "create-virtual-mfa-device"}:
        v.add(DENY, "aws-iam-user", "No IAM users or access keys (organizer rule). Use the IAM role.")
    if service == "iam" and "AdministratorAccess" in cmd:
        v.add(ASK, "aws-iam-admin", "Attaches AdministratorAccess. Least privilege first. Gordon confirms.")
    if service == "sts" and op in {"get-session-token", "assume-role", "get-federation-token",
                                   "assume-role-with-web-identity", "assume-role-with-saml"}:
        v.add(DENY, "aws-sts-creds", "That prints temporary credentials.")
    if service == "bedrock" and op in {"create-provisioned-model-throughput", "create-model-customization-job",
                                       "create-model-import-job", "create-model-copy-job"}:
        v.add(DENY, "aws-cost", "Provisioned throughput and custom models cost far more than the plan allows.")
    if service in {"rds", "redshift", "eks", "elasticache", "opensearch", "es", "kafka", "emr", "sagemaker", "memorydb",
                   "neptune", "docdb", "fsx", "workspaces"} and op.startswith(("create", "run", "start")):
        v.add(DENY, "aws-not-in-plan", f"{service} is not in the plan (BACKEND.md section 8). Ask Gordon first.")
    if service == "ec2":
        if op in {"create-nat-gateway", "allocate-hosts"} or op.startswith(("purchase-", "request-spot")):
            v.add(DENY, "aws-cost", f"ec2 {op} is not in the plan and costs money by the hour.")
        if op == "run-instances":
            itype = opt_value(args, "--instance-type")
            count = opt_value(args, "--count") or opt_value(args, "--max-count") or "1"
            if itype and itype not in EC2_TYPES:
                v.add(DENY, "aws-ec2-type", f"Instance type {itype} is outside the plan ({', '.join(sorted(EC2_TYPES))}).")
            if count.split(":")[-1] not in ("1",):
                v.add(DENY, "aws-ec2-count", "One instance only.")
        if op in {"create-key-pair", "allocate-address"}:
            v.add(ASK, "aws-ec2-extra", f"ec2 {op} is not needed with Instance Connect or SSM. Gordon confirms.")
        if op == "authorize-security-group-ingress" and ("0.0.0.0/0" in cmd or "::/0" in cmd):
            v.add(ASK, "aws-open-ingress", "Opens a port to the whole internet. Fine for 8000 during the demo. Gordon confirms.")
    destructive = bool(re.match(r"^(delete|terminate|remove|deregister|disable|detach|revoke|release|cancel|stop|reset|purge)-", op))
    if service == "s3" and (op in {"rm", "rb", "mv"} or (op == "sync" and "--delete" in args)):
        destructive = True
    if destructive:
        v.add(ASK, "aws-destructive", f"aws {service} {op} deletes or stops something. Gordon confirms each one (and it must be in AWS_RESOURCES.md).")


def check_git(toks: list[str], cwd: str | None, v: Verdict) -> None:
    args = toks[1:]
    i = 0
    while i < len(args) and args[i].startswith("-"):
        i += 2 if args[i] in ("-C", "-c") else 1
    if i >= len(args):
        return
    sub, rest = args[i], args[i + 1:]
    flags = [a for a in rest if a.startswith("-")]
    pos = [a for a in rest if not a.startswith("-")]
    short = "".join(f[1:] for f in flags if f.startswith("-") and not f.startswith("--"))

    if sub in {"rebase", "filter-branch", "filter-repo", "replace"}:
        v.add(DENY, "git-rewrite", f"git {sub} rewrites history. Not tonight.")
    elif sub == "push":
        if {"--force", "-f", "--force-with-lease", "--force-if-includes", "--mirror", "--delete", "-d", "--prune", "--all"} & set(flags) \
                or "f" in short or any(p.startswith(("+", ":")) for p in pos[1:]):
            v.add(DENY, "git-force", "No force pushes, deletes or mirrors.")
        refspecs = pos[1:]
        if not refspecs:
            if branch() not in (None, "backend") and "--tags" not in flags:
                v.add(ASK, "git-push-branch", f"Pushes branch {branch()}. Gordon confirms.")
        for r in refspecs:
            dst = r.split(":", 1)[-1]
            dst = dst.removeprefix("refs/heads/")
            if dst in ("backend",) or dst.startswith(("demo-ok", "refs/tags/")):
                continue
            if dst == "HEAD" and branch() in (None, "backend"):
                continue
            if dst == "frontend":
                v.add(DENY, "git-push-frontend", "frontend is Tong's branch.")
            elif dst == "main":
                v.add(ASK, "git-push-main", "Pushing main is the 5:30 merge, done together with Tong (CLAUDE.md). Gordon confirms.")
            else:
                v.add(ASK, "git-push-other", f"Pushes {dst}. Gordon confirms.")
    elif sub == "reset" and "--hard" in flags:
        v.add(DENY, "git-reset-hard", "reset --hard throws away work. Builders share this folder.")
    elif sub == "clean" and ("--force" in flags or "f" in short):
        v.add(DENY, "git-clean", "git clean -f deletes untracked files, including other builders' new files.")
    elif sub in {"checkout", "restore"}:
        if "." in pos or "*" in pos:
            v.add(DENY, "git-discard-all", "That discards every uncommitted change in the shared folder.")
        elif sub == "checkout" and ({"-b", "-B", "--orphan"} & set(flags) or
                                    (pos and "--" not in rest and (pos[0] in {"main", "frontend", "backend"} or pos[0].startswith("origin/")))):
            v.add(ASK, "git-switch", "Switching branches in the shared folder breaks running builders. Only at a gate. Gordon confirms.")
    elif sub == "switch":
        v.add(ASK, "git-switch", "Switching branches in the shared folder breaks running builders. Only at a gate. Gordon confirms.")
    elif sub == "branch" and ({"-D", "-d", "--delete", "-m", "-M"} & set(flags)):
        v.add(ASK, "git-branch-delete", "Deletes or renames a branch. Gordon confirms.")
    elif sub == "stash" and pos[:1] and pos[0] in {"drop", "clear"}:
        v.add(ASK, "git-stash-drop", "Drops stashed work. Gordon confirms.")
    elif sub in {"gc", "reflog", "prune"} and ("--prune=now" in flags or "expire" in pos or sub == "prune"):
        v.add(DENY, "git-prune", "Pruning removes the safety net.")
    elif sub == "merge":
        target = " ".join(pos)
        if "frontend" in target:
            v.add(DENY, "git-merge-frontend", "frontend merges into main at 5:30, not into backend.")
        elif pos and not any(p in {"origin/main", "main"} for p in pos) and "--abort" not in flags:
            v.add(ASK, "git-merge-other", f"Merges {target}. Only origin/main merges into backend (CLAUDE.md). Gordon confirms.")
    elif sub == "add":
        if {"-A", "--all"} & set(flags) or "A" in short or any(p in {".", "*", ":/"} for p in pos):
            v.add(DENY, "git-add-all", "Stage explicit paths, for example `git add backend .claude`.")
        elif strict_branch():
            for p in pos:
                if p == "--":
                    continue
                rel = rel_to_project(resolve(p, cwd))
                if rel is None or not rel.parts or rel.parts[0] not in OWN_DIRS:
                    v.add(DENY, "git-add-outside",
                          f"On backend only backend/ and .claude/ change. '{p}' is outside them. "
                          "contract/, mock/, docs/ change on main (RUNBOOK section 7). frontend/ is Tong's.")
    elif sub == "commit":
        if {"-a", "--all"} & set(flags) or "a" in short:
            v.add(DENY, "git-commit-all", "Stage explicit paths first (`git add backend .claude`), then commit without -a.")
        if "--amend" in flags:
            v.add(ASK, "git-amend", "Amending a pushed commit needs a force push, which the guard blocks. Gordon confirms.")
        check_commit_contents(v)


def check_commit_contents(v: Verdict) -> None:
    staged = git("diff", "--cached", "--name-only") or ""
    if strict_branch():
        outside = [f for f in staged.splitlines() if f and f.split("/", 1)[0] not in OWN_DIRS]
        if outside:
            v.add(DENY, "git-commit-outside",
                  "Staged files outside backend/ and .claude/: " + ", ".join(outside[:8]) +
                  ". Unstage them (`git restore --staged <file>`). If this is a merge of origin/main, finish it with `git merge --continue`.")
    texts = [git("diff", "--cached", "-U0", timeout=8) or "", git("diff", "-U0", timeout=8) or ""]
    untracked = (git("ls-files", "--others", "--exclude-standard") or "").splitlines()
    for f in untracked[:400]:
        p = PROJECT / f
        try:
            if p.is_file() and p.stat().st_size < 1_000_000:
                texts.append(p.read_text(encoding="utf-8", errors="ignore"))
        except Exception:
            pass
    for t in texts:
        added = "\n".join(line for line in t.splitlines() if not line.startswith("-")) if t.startswith("diff") else t
        if has_secret(added):
            v.add(DENY, "git-secret", "A change you are about to commit contains what looks like an AWS credential or private key. "
                                       "The repo is public. Find it (git diff) and remove it before committing.")
            break


def check_rm(toks: list[str], cwd: str | None, v: Verdict) -> None:
    args = toks[1:]
    flags = [a for a in args if a.startswith("-")]
    recursive = any(f in ("-r", "-R", "--recursive") or (not f.startswith("--") and ("r" in f[1:] or "R" in f[1:])) for f in flags)
    if not recursive:
        return
    targets = [a for a in args if not a.startswith("-")]
    tmp_roots = [Path(tempfile.gettempdir()).resolve(), Path("/tmp")]
    for t in targets:
        if t in {"/", "~", ".", "..", "*", "./", "../", "~/", "$HOME", "${HOME}", "/*", "./*", "~/*"}:
            v.add(DENY, "rm-broad", f"rm -r {t} is far too broad.")
            continue
        p = resolve(t.split("*", 1)[0] or ".", cwd)
        rel = rel_to_project(p)
        if rel is not None:
            parts = rel.parts
            if not parts or parts[0] in PROTECTED_DIRS or parts == ("backend",) or parts == (".claude",):
                v.add(DENY, "rm-protected", f"rm -r {t} would delete a protected folder.")
            elif parts[0] == "backend" or any(n in SCRATCH_NAMES for n in parts):
                continue
            else:
                v.add(ASK, "rm-outside", f"rm -r {t} is outside backend/. Gordon confirms.")
        elif any(under(p, r) for r in tmp_roots):
            continue
        elif p == HOME or under(PROJECT, p):
            v.add(DENY, "rm-broad", f"rm -r {t} contains the repo or the home folder.")
        else:
            v.add(ASK, "rm-outside", f"rm -r {t} is outside the repo. Gordon confirms.")


def check_bash(cmd: str, cwd: str | None, v: Verdict) -> None:
    check_raw(cmd, v)
    if v.rules[:1] == ["selftest"]:
        return
    segs = split_segments(cmd.replace("\n", " ; "))
    if segs is None:
        # Unparseable (heredoc, odd quoting): fall back to raw patterns for the risky tools.
        if re.search(r"\bgit\s+(push\s+[^\n]*(-f|--force)|reset\s+--hard|clean\s+-\w*f|rebase|filter-branch)", cmd):
            v.add(DENY, "git-unparsed", "Risky git command inside a command the guard could not parse. Run it on its own line.")
        if re.search(r"\baws\s+[^\n]*--(profile|region)\b", cmd) and not re.search(r"--region[ =]us-east-1\b", cmd):
            v.add(DENY, "aws-unparsed", "AWS command with --profile or a non us-east-1 region inside a command the guard could not parse.")
        return
    for seg in segs:
        env, toks = strip_prefix(seg)
        if toks[:1] == ["cd"] and len(toks) > 1:
            cwd = str(resolve(toks[1], cwd))   # track `cd backend && git add ...`
            continue
        if not toks:
            if any(k in env for k in ("AWS_REGION", "AWS_DEFAULT_REGION")) and \
                    any(env.get(k) not in (None, REGION) for k in ("AWS_REGION", "AWS_DEFAULT_REGION")):
                v.add(DENY, "aws-region", f"Hackathon resources live in {REGION} only.")
            continue
        exe = os.path.basename(toks[0]).lower().removesuffix(".exe")
        if exe == "export":
            for t in toks[1:]:
                if "=" in t:
                    k, val = t.split("=", 1)
                    if k in ("AWS_REGION", "AWS_DEFAULT_REGION") and val != REGION:
                        v.add(DENY, "aws-region", f"Hackathon resources live in {REGION} only.")
                    if k == "AWS_PROFILE":
                        v.add(DENY, "aws-profile", "This build uses the workshop credentials only.")
        elif exe == "aws":
            check_aws(env, toks, cmd, v)
        elif exe == "git":
            check_git(toks, cwd, v)
        elif exe == "rm":
            check_rm(toks, cwd, v)


# --------------------------------------------------------------------------
# File tools
# --------------------------------------------------------------------------

def check_file(tool: str, ti: dict, cwd: str | None, v: Verdict) -> None:
    raw = ti.get("file_path") or ti.get("notebook_path") or ti.get("path") or ""
    if not raw:
        return
    p = resolve(raw, cwd)
    if under(p, HOME / ".aws") or under(p, HOME / ".ssh"):
        v.add(DENY, "secret-file", "Never read or write ~/.aws or ~/.ssh.")
        return
    if tool == "Read":
        return
    bodies = [ti.get("content") or "", ti.get("new_string") or "", ti.get("new_source") or ""]
    bodies += [e.get("new_string") or "" for e in ti.get("edits") or [] if isinstance(e, dict)]
    if any(has_secret(b) for b in bodies):
        v.add(DENY, "secret-write", "That text looks like an AWS credential or private key. Never write credentials to files.")
    rel = rel_to_project(p)
    if rel is None:
        if under(p, HOME / ".claude") and p.name.startswith("settings"):
            v.add(ASK, "settings-edit", "Changes Claude Code settings. Gordon confirms.")
        return
    parts = rel.parts
    if parts[:1] == (".claude",) and (parts[1:2] == ("hooks",) or (len(parts) == 2 and parts[1].startswith("settings"))):
        v.add(ASK, "guard-edit", "Changes the guard or Claude Code settings. Gordon confirms.")
    if strict_branch() and (not parts or parts[0] not in OWN_DIRS):
        where = parts[0] if parts else str(rel)
        owner = "Tong's" if where == "frontend" else "shared, changed on main (RUNBOOK section 7)"
        v.add(DENY, "path-outside", f"On the backend branch only backend/ and .claude/ change. {where} is {owner}.")


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

def emit(decision: str, reason: str) -> None:
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                             "permissionDecision": decision,
                                             "permissionDecisionReason": reason}}))


def log(entry: dict) -> None:
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass


def main() -> int:
    tool = "?"
    try:
        payload = json.loads(sys.stdin.read() or "{}")
        tool = payload.get("tool_name", "")
        ti = payload.get("tool_input") or {}
        cwd = payload.get("cwd")
        v = Verdict()
        if tool == "Bash":
            check_bash(ti.get("command", ""), cwd, v)
            summary = ti.get("command", "")
        elif tool in {"Read", "Edit", "Write", "MultiEdit", "NotebookEdit"}:
            check_file(tool, ti, cwd, v)
            summary = ti.get("file_path") or ti.get("notebook_path") or ""
        else:
            return 0
        if v.decision:
            reason = "Axelrod guard: " + " ".join(v.reasons)
            emit(v.decision, reason)
            log({"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), "tool": tool,
                 "decision": v.decision, "rules": v.rules, "branch": branch(),
                 "agent": payload.get("agent_type") or payload.get("agent_id"), "input": redact(summary)})
        return 0
    except Exception as exc:  # fail closed for writes and commands, open for reads
        if tool == "Read":
            return 0
        emit(DENY, f"Axelrod guard error ({type(exc).__name__}: {exc}). Failing closed. "
                   "Tell Gordon. To bypass, he can remove the PreToolUse hook from .claude/settings.json.")
        return 0


if __name__ == "__main__":
    sys.exit(main())
