"""The gateway: the only path to the network and to Bedrock. It enforces the policy, the kill switch
and the budget, meters every call, emits one gateway.request per decision, and fails closed.

Decision order (CONTRACT.md section 6, first match wins): kill switch, a stopped agent (R-stop-<agent_id>,
v0.2), budget, role scope, explicit deny, allow, default deny. Any exception while deciding or carrying out
a request is a deny. Link-local fetch targets (instance metadata lives there) are denied with the explicit
denies, so no allow rule, not even an analyst-approved source, can reach them.

Document cache (v0.2, CACHE_DOCUMENTS=on): the decision always comes first, so a blocked request never
touches the cache. An allowed fetch is served from the cache on a hit (no network call, cache "hit") and
fetched live and stored on a miss (cache "miss"). Each redirect hop is its own entry and is decided again.
An s3:// URL is an archive copy with no known original URL: it is decided like any URL (the domain is the
bucket) and only ever served from the cache, so a miss fails clearly instead of going live.

Model cache (v0.2, CACHE_MODELS=record|replay): again the decision comes first. record stores every live
answer, replay serves a recorded one (cache "hit", cost 0, the tokens as recorded) and calls live on a miss,
storing that answer too. The key strips run ids, agent ids and timestamps from the prompts (cache.model_key).
"""
from __future__ import annotations

import asyncio
import copy
import ipaddress
import json
import re
import socket
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal
from urllib.parse import urljoin

from . import bedrock, cache, pricing
from .contract import m
from .fetch import http
from .ids import now_iso

if TYPE_CHECKING:
    from .config import Settings
    from .runtime import RunContext

# Re-exported so agent modules register fakes without importing bedrock.py themselves.
register_fake = bedrock.register_fake
register_fake_scan = bedrock.register_fake_scan

SYS_RULE_ID = "R-sys-models"
MAX_REDIRECTS = 5
REDIRECT_CODES = frozenset({301, 302, 303, 307, 308})
KILL_REASON = "Kill switch: run stopped. Every request is denied."
STOP_ORIGIN = "user_agent_stop"
STOP_REASON = "Agent stopped by the analyst"

# EC2 instance metadata: the IPv6 endpoint (not link-local by prefix) and its DNS names.
IMDS_V6 = ipaddress.IPv6Address("fd00:ec2::254")
NAT64 = ipaddress.IPv6Network("64:ff9b::/96")
METADATA_HOSTS = frozenset({"instance-data", "instance-data.ec2.internal"})
# Shorthand IPv4 forms resolvers accept (2852039166, 0xa9fea9fe, 169.254.43518, 0251.0376.0251.0376).
_NUMERIC_V4 = re.compile(r"(?:0x[0-9a-f]+|\d+)(?:\.(?:0x[0-9a-f]+|\d+)){0,3}")

# Which model sizes each role may call. The fetcher is the only role that may fetch.
# The orchestrator (Bobby, v0.3) makes the big calls on the large model and never fetches.
ROLE_SIZES: dict[str, tuple[str, ...]] = {
    "orchestrator": ("large",),
    "planner": ("large",),
    "fetcher": (),
    "extractor": ("small",),
    "drafter": ("large",),
    "verifier": ("small",),
    "repairer": ("small", "large"),
    "summarizer": ("large",),
    "reviser": ("small",),
}

Decision = tuple[str, "str | None", str]       # (decision, rule_id, reason)


class Blocked(Exception):
    """A deny. rule_id is None when the default deny decided it."""

    def __init__(self, request_id: str, rule_id: str | None, reason: str) -> None:
        super().__init__(f"{request_id} blocked ({rule_id or 'default deny'}): {reason}")
        self.request_id = request_id
        self.rule_id = rule_id
        self.reason = reason


@dataclass
class FetchResult:
    url: str
    final_url: str
    http_status: int
    content_type: str
    body: bytes
    request_id: str
    origin: str = "fetched"             # v0.2 Document.origin: fetched (live), cache or archive
    collected_at: str | None = None     # when the bytes were first collected. None means now (a live fetch)
    cache: str | None = None            # v0.2 gateway.request cache: "hit", "miss" or None (cache off)


@dataclass
class ToolSpec:
    name: str
    description: str
    input_schema: dict          # JSON Schema of the one forced tool


@dataclass
class InvokeResult:
    tool_input: dict
    tokens_in: int
    tokens_out: int
    cost_usd: float
    request_id: str
    model_id: str = ""
    stop_reason: str = ""       # "tool_use" normally, "max_tokens" when the output was cut off


# ---------------------------------------------------------------------------
# Policy helpers (pure)
# ---------------------------------------------------------------------------


def role_scopes(settings: Settings) -> list[m.RoleScope]:
    """Role scopes from ROLE_SIZES and the configured model ids."""
    by_size = {"large": settings.model_large, "small": settings.model_small}
    out = []
    for role, sizes in ROLE_SIZES.items():
        actions = ["fetch"] if role == "fetcher" else ["model.invoke"]
        out.append(m.RoleScope(role=role, actions=actions, models=[by_size[s] for s in sizes]))
    return out


def host_ip(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    """The IP address a host names literally, or None for a DNS name.

    Brackets, a trailing dot and an IPv6 zone id are dropped. Numeric IPv4 shorthands count as addresses,
    because the OS resolver would connect to them.
    """
    h = host.strip().lower().strip("[]").rstrip(".").split("%", 1)[0]
    try:
        return ipaddress.ip_address(h)
    except ValueError:
        pass
    if _NUMERIC_V4.fullmatch(h):
        try:
            return ipaddress.IPv4Address(socket.inet_aton(h))
        except OSError:
            return None
    return None


def is_link_local(host: str) -> bool:
    """169.254.0.0/16, fe80::/10, fd00:ec2::254, an IPv4 one inside IPv6 (mapped or NAT64), or a metadata name."""
    if host.strip().lower().rstrip(".") in METADATA_HOSTS:
        return True
    ip = host_ip(host)
    if isinstance(ip, ipaddress.IPv6Address):
        if ip == IMDS_V6 or ip.is_link_local:
            return True
        inner = ip.ipv4_mapped or (ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF) if ip in NAT64 else None)
        return inner is not None and inner.is_link_local
    return ip is not None and ip.is_link_local


def target_parts(url: str) -> tuple[str, str, str] | None:
    """(scheme, host, path) the policy decides on: an http(s) URL, or an s3:// archive copy whose host is
    the bucket. None for anything else (default deny)."""
    return http.split_url(url) or cache.split_s3_url(url)


def stop_rule_id(agent_id: str) -> str:
    return f"R-stop-{agent_id}"


def stop_rule(policy: m.Policy | None, agent_id: str) -> m.PolicyRule | None:
    """The analyst's stop rule for this one agent, if it has one (v0.2)."""
    if policy is None:
        return None
    return next((r for r in policy.rules if r.origin == STOP_ORIGIN and r.effect == "deny"
                 and r.agent_id == agent_id), None)


def _norm_host(host: str) -> str:
    host = host.lower().rstrip(".")
    return host[4:] if host.startswith("www.") else host


def host_matches(host: str, domain: str) -> bool:
    """Same host or a subdomain of it, with a leading www. ignored on both sides."""
    h, d = _norm_host(host), _norm_host(domain)
    return bool(d) and (h == d or h.endswith("." + d))


def rule_matches(rule: m.PolicyRule, role: str, action: str, target: str,
                 parts: tuple[str, str, str] | None, *, agent_id: str | None = None) -> bool:
    """Does this rule apply to (role, action, target) for this agent? Missing scope fields fail closed:
    a fetch allow rule with no domain matches nothing, a fetch deny rule with no domain matches everything.
    A rule with agent_id set (v0.2, R-stop-<agent_id>) applies to that one agent only."""
    if rule.action != action or (rule.roles and role not in rule.roles):
        return False
    if rule.agent_id is not None and rule.agent_id != agent_id:
        return False
    if action == "model.invoke":
        return rule.model_id is None or rule.model_id == target
    if parts is None:
        return False
    if rule.domain is None:
        return rule.effect == "deny"
    _, host, path = parts
    if not host_matches(host, rule.domain):
        return False
    return rule.path_prefix is None or path.startswith(rule.path_prefix)


def _sys_rule(at: str) -> m.PolicyRule:
    return m.PolicyRule(rule_id=SYS_RULE_ID, effect="allow", action="model.invoke", roles=[], origin="system",
                        note="Model calls allowed per role scope", created_at=at)


def base_policy(run_id: str, settings: Settings, *, budget_usd: float | None, at: str, as_of_seq: int = 0,
                kill_switch: bool = False) -> m.Policy:
    """Version 0, what the gateway enforces before approval: R-sys-models and the role scopes, every fetch
    default deny. RunState.policy_view shows the same thing, so the panel never hides a rule in force."""
    return m.Policy(run_id=run_id, version=0, kill_switch=kill_switch, rules=[_sys_rule(at)],
                    role_scopes=role_scopes(settings), budget_usd=budget_usd, updated_at=at, as_of_seq=as_of_seq)


def source_rule(src: m.Source, revoked: dict[str, str | None], at: str) -> m.PolicyRule | None:
    """R-revoke-, R-allow- or R-reject- for one source, or None while it is still pending.
    An s3:// archive source gets the bucket as its domain and the object key as its path prefix."""
    parts = target_parts(src.url)
    domain, prefix = (parts[1], parts[2]) if parts else (src.domain.lower(), None)
    sid = src.source_id
    common = {"action": "fetch", "domain": domain, "path_prefix": prefix, "source_id": sid, "created_at": at}
    if sid in revoked:
        return m.PolicyRule(rule_id=f"R-revoke-{sid}", effect="deny", roles=[], origin="user_revoke",
                            note=revoked[sid] or "Revoked by user", **common)
    if src.approval in ("approved", "auto_approved"):
        if parts is None:       # not an http(s) or s3:// URL: never allow it
            return None
        origin = "user_approval" if src.approval == "approved" else "auto_tier"
        return m.PolicyRule(rule_id=f"R-allow-{sid}", effect="allow", roles=["fetcher"], origin=origin, **common)
    if src.approval == "rejected":
        return m.PolicyRule(rule_id=f"R-reject-{sid}", effect="deny", roles=[], origin="user_rejection",
                            note="Rejected at approval", **common)
    return None


def _deny_reason(rule: m.PolicyRule) -> str:
    sid = rule.source_id or "this target"
    if rule.origin == "user_revoke":
        return f"{sid} was revoked mid-run ({rule.note}). Subsequent requests are denied."
    if rule.origin == "user_rejection":
        return f"{sid} was rejected at approval. Denied by {rule.rule_id}."
    return rule.note or f"Denied by {rule.rule_id}."


def _ms(t0: float) -> int:
    return max(0, int((time.perf_counter() - t0) * 1000))


def _add_usage(u: m.Usage, tokens_in: int, tokens_out: int, cost: float, budget: float | None = None) -> m.Usage:
    """A new Usage (never mutated in place, so events that hold the old one stay as they were)."""
    return m.Usage(model_calls=u.model_calls + 1, tokens_in=u.tokens_in + tokens_in,
                   tokens_out=u.tokens_out + tokens_out, cost_usd=round(u.cost_usd + cost, 6),
                   budget_usd=budget if budget is not None else u.budget_usd)


# ---------------------------------------------------------------------------
# Gateway
# ---------------------------------------------------------------------------


class Gateway:
    def __init__(self, ctx: RunContext) -> None:
        self.ctx = ctx
        self._sems: dict[str, asyncio.Semaphore] = {}
        self._reserved = 0.0          # estimated cost of model calls in flight
        self.doc_cache: cache.DocCache | None = None    # built on first use (tests may set their own)
        self.model_cache: cache.ModelCache | None = None    # CACHE_MODELS record/replay, built on first use
        self._model_seen: dict[str, int] = {}           # model cache key -> calls with it so far in this run

    # -- state ----------------------------------------------------------------

    @property
    def kill_switch_on(self) -> bool:
        """True once STOP was pressed (RunState.kill_switch or the policy's) or the run is already stopped."""
        st = self.ctx.state
        return (bool(getattr(st, "kill_switch", False)) or bool(st.policy is not None and st.policy.kill_switch)
                or st.run.status == "stopped")

    def budget_usd(self) -> float | None:
        """The run's budget: Run.usage.budget_usd when the run set one, else BUDGET_USD."""
        b = self.ctx.state.run.usage.budget_usd
        return b if b is not None else self.ctx.settings.budget_usd

    def _policy(self) -> m.Policy:
        """The stored policy, or the pre-approval base (system rule only, so every fetch is default deny)."""
        st = self.ctx.state
        if st.policy is not None:
            return st.policy
        return base_policy(st.run.run_id, self.ctx.settings, budget_usd=self.budget_usd(), at=st.run.created_at,
                           as_of_seq=st.run.last_seq)

    def _make_policy(self, *, version: int, rules: list[m.PolicyRule], kill_switch: bool) -> m.Policy:
        st = self.ctx.state
        return m.Policy(run_id=st.run.run_id, version=version, kill_switch=kill_switch, rules=rules,
                        role_scopes=role_scopes(self.ctx.settings), budget_usd=self.budget_usd(),
                        updated_at=now_iso(), as_of_seq=st.run.last_seq)

    def _next_version(self) -> int:
        st = self.ctx.state
        return max(st.policy.version if st.policy else 0, st.run.policy_version) + 1

    def _rules_from_sources(self, revoked: dict[str, str | None]) -> list[m.PolicyRule]:
        """The analyst's agent stop rules (kept as they are, first), R-sys-models, then one rule per source."""
        st = self.ctx.state
        at = now_iso()
        old = {r.rule_id: r for r in st.policy.rules} if st.policy else {}
        rules = [r for r in old.values() if r.origin == STOP_ORIGIN]
        rules.append(_sys_rule(at))
        for src in st.sources:
            rule = source_rule(src, revoked, at)
            if rule is not None:
                rules.append(rule)
        # Unchanged rules keep their original created_at.
        return [r.model_copy(update={"created_at": old[r.rule_id].created_at})
                if r.rule_id in old and old[r.rule_id].effect == r.effect else r for r in rules]

    def _revoked(self) -> dict[str, str | None]:
        st = self.ctx.state
        out: dict[str, str | None] = {}
        if st.policy is not None:
            out.update({r.source_id: r.note for r in st.policy.rules if r.origin == "user_revoke" and r.source_id})
        for src in st.sources:
            if src.status == "revoked" and src.source_id not in out:
                out[src.source_id] = src.error
        return out

    def _store_policy(self, policy: m.Policy, *, change: str, rule_ids: list[str], message: str,
                      level: str = "info", emit: bool = True) -> m.Policy:
        st = self.ctx.state
        st.policy = policy
        st.run.policy_version = policy.version
        if emit:
            ev = self.ctx.emit("policy.updated", m.PolicyUpdatedData(version=policy.version, change=change,
                                                                     rule_ids=rule_ids),
                               message=message, level=level)
            seq = getattr(ev, "seq", None)
            if isinstance(seq, int):
                policy.as_of_seq = seq
        return policy

    # -- policy changes -------------------------------------------------------

    def build_policy(self, *, emit: bool = True) -> m.Policy:
        """Policy from the sources' approvals: R-sys-models, R-allow-<S>, R-reject-<S>, default deny.

        Revocations and the kill switch carry over from the current policy. Emits policy.updated
        unless emit=False (for a caller that emits its own).
        """
        old = self.ctx.state.policy
        policy = self._make_policy(version=self._next_version(), rules=self._rules_from_sources(self._revoked()),
                                   kill_switch=bool(old and old.kill_switch))
        n_allow = sum(1 for r in policy.rules if r.effect == "allow" and r.action == "fetch")
        n_deny = sum(1 for r in policy.rules if r.effect == "deny")
        return self._store_policy(
            policy, change="Approval committed", rule_ids=[r.rule_id for r in policy.rules], emit=emit,
            message=f"Gateway policy v{policy.version}: {n_allow} source(s) allowed, {n_deny} denied, default deny")

    def revoke(self, source_id: str, reason: str | None) -> m.Policy:
        """Flip one source to deny, mark it revoked, bump the version. The next request to it is blocked.

        Raises KeyError for an unknown source, ValueError before approval or when already revoked.
        Emits policy.updated and source.status.
        """
        st = self.ctx.state
        src = next((s for s in st.sources if s.source_id == source_id), None)
        if src is None:
            raise KeyError(source_id)
        if st.policy is None:
            raise ValueError("Revoke applies after approval. Before that, reject the source in the approval request.")
        revoked = self._revoked()
        if source_id in revoked or src.status == "revoked":
            raise ValueError(f"{source_id} is already revoked")
        note = reason or "Revoked by user"
        revoked[source_id] = note
        src.status = "revoked"
        src.error = note
        policy = self._make_policy(version=self._next_version(), rules=self._rules_from_sources(revoked),
                                   kill_switch=st.policy.kill_switch)
        self._store_policy(policy, change=f"Revoked {source_id}", rule_ids=[f"R-revoke-{source_id}"], level="warn",
                           message=f"Gateway policy v{policy.version}: {source_id} revoked")
        self.ctx.emit("source.status", m.SourceStatusData(source_id=source_id, status="revoked",
                                                          stats=src.stats.model_copy(), error=note),
                      message=f"{source_id} revoked", level="warn")
        return policy

    def set_kill_switch(self, reason: str | None) -> None:
        """Every later request is denied, and results that land afterwards are discarded. Idempotent."""
        st = self.ctx.state
        if st.policy is not None and st.policy.kill_switch:
            return
        base = self._policy()
        policy = self._make_policy(version=self._next_version(), rules=list(base.rules), kill_switch=True)
        why = reason or "Stopped by user"
        self._store_policy(policy, change=f"Kill switch on: {why}", rule_ids=[], level="warn",
                           message=f"Gateway policy v{policy.version}: kill switch, every request denied")

    def stop_agent(self, agent: m.Agent, reason: str | None) -> m.Policy:
        """Add R-stop-<agent_id> (agent_id set, origin user_agent_stop, the agent's own action): from now on
        decide() denies every request from this one agent right after the kill switch check, and a result
        that lands later is discarded. The run keeps going. Bumps the version and emits policy.updated.
        Idempotent: an agent that already has its rule keeps it. Works before approval too."""
        st = self.ctx.state
        if st.policy is not None and stop_rule(st.policy, agent.agent_id) is not None:
            return st.policy
        base = self._policy()
        rule = m.PolicyRule(rule_id=stop_rule_id(agent.agent_id), effect="deny",
                            action="fetch" if agent.role == "fetcher" else "model.invoke", roles=[],
                            agent_id=agent.agent_id, origin=STOP_ORIGIN, note=reason or STOP_REASON,
                            created_at=now_iso())
        policy = self._make_policy(version=self._next_version(), rules=[rule, *base.rules],
                                   kill_switch=base.kill_switch)
        return self._store_policy(policy, change=f"Stopped agent {agent.agent_id}", rule_ids=[rule.rule_id],
                                  level="warn",
                                  message=f"Gateway policy v{policy.version}: {agent.agent_id} stopped, "
                                          "its requests are denied")

    def agent_stopped(self, agent_id: str) -> m.PolicyRule | None:
        """The stop rule in force for this agent, or None."""
        return stop_rule(self.ctx.state.policy, agent_id)

    def _hold_stopped(self, agent: m.Agent, rule_id: str | None) -> None:
        """A request denied by the agent's own stop rule keeps the agent stopped. AgentRunner.ask marks an
        agent working before it calls, which would otherwise flip a queued agent the analyst stopped back to
        working, then failed. Emits agent.status stopped again when it had to put it back."""
        if rule_id != stop_rule_id(agent.agent_id):
            return
        held = self.ctx.state.agents.get(agent.agent_id) or agent
        rule = self.agent_stopped(agent.agent_id)
        changed = False
        for a in {id(held): held, id(agent): agent}.values():
            if a.status != "stopped":
                a.status, a.finished_at, changed = "stopped", a.finished_at or now_iso(), True
        if changed:
            why = (rule.note if rule else None) or STOP_REASON
            self.ctx.emit("agent.status", m.AgentStatusData(agent_id=held.agent_id, status="stopped", task=held.task,
                                                            usage=held.usage.model_copy(), reason=why),
                          message=f"{held.agent_id} stopped: {why}", agent_id=held.agent_id, level="warn")

    # -- decisions ------------------------------------------------------------

    def decide(self, agent: m.Agent, action: str, target: str, *, est_cost: float = 0.0) -> Decision:
        """(decision, rule_id, reason) in the CONTRACT.md section 6 order. Any exception inside means blocked."""
        try:
            return self._decide(agent, action, target, est_cost)
        except Exception as e:  # fail closed
            return "blocked", None, f"gateway error: {type(e).__name__}: {e}"

    def _decide(self, agent: m.Agent, action: str, target: str, est_cost: float) -> Decision:
        if action not in ("fetch", "model.invoke"):
            raise ValueError(f"unknown action {action!r}")
        policy = self._policy()
        role = agent.role
        # 1. kill switch
        if self.kill_switch_on:
            return "blocked", None, KILL_REASON
        # 1b. a stopped agent (v0.2): every request from it is denied, the rest of the run continues
        stopped = stop_rule(policy, agent.agent_id)
        if stopped is not None:
            return "blocked", stopped.rule_id, f"{STOP_REASON}: {stopped.note or 'no reason given'}"
        # 2. budget
        over = self._budget_block(action, target, est_cost)
        if over:
            return "blocked", None, over
        # 3. role scope
        scope = next((s for s in policy.role_scopes if s.role == role), None)
        if scope is None or action not in scope.actions:
            return "blocked", None, f"Role '{role}' may not {'fetch' if action == 'fetch' else 'call models'}."
        if action == "model.invoke" and target not in scope.models:
            return "blocked", None, f"Role '{role}' may not call {target}."
        parts = target_parts(target) if action == "fetch" else None
        # 4. explicit deny: link-local hosts first (no rule can allow them), then the policy's deny rules
        if parts is not None and is_link_local(parts[1]):
            return "blocked", None, f"{parts[1]}: link-local address, never fetched."
        aid = agent.agent_id
        for rule in policy.rules:
            if rule.effect == "deny" and rule_matches(rule, role, action, target, parts, agent_id=aid):
                return "blocked", rule.rule_id, _deny_reason(rule)
        # 5. allow
        for rule in policy.rules:
            if rule.effect == "allow" and rule_matches(rule, role, action, target, parts, agent_id=aid):
                if action == "model.invoke":
                    return "allowed", rule.rule_id, f"Role '{role}' may call this model."
                return "allowed", rule.rule_id, f"{rule.rule_id} allows {rule.domain}{rule.path_prefix or ''}."
        # 6. default deny
        if action == "fetch" and parts is None:
            return "blocked", None, "Not a plain http(s) or s3:// archive URL. Default deny."
        what = parts[1] + parts[2] if parts else target
        return "blocked", None, f"No rule allows {what}. Default deny."

    def _budget_block(self, action: str, target: str, est_cost: float) -> str | None:
        settings = self.ctx.settings
        if action == "model.invoke" and not settings.fake_models and not pricing.has_price(target, settings.prices):
            return f"No price for {target} in PRICES_JSON, so this call can't be metered."
        budget = self.budget_usd()
        if budget is None:
            return None
        spent = self.ctx.state.run.usage.cost_usd
        if spent >= budget:
            return f"Budget exceeded: ${spent:.4f} spent of ${budget:.2f}."
        if action == "model.invoke" and spent + self._reserved + est_cost > budget:
            return (f"Budget: this call may cost up to ${est_cost:.4f}, with ${spent + self._reserved:.4f} "
                    f"spent or in flight of ${budget:.2f}.")
        return None

    def _emit_request(self, agent: m.Agent, request_id: str, action: str, target: str, decision: str,
                      rule_id: str | None, reason: str, t0: float, *, source_id: str | None = None,
                      http_status: int | None = None, nbytes: int | None = None, tokens_in: int | None = None,
                      tokens_out: int | None = None, cost_usd: float | None = None, cache: str | None = None,
                      cached_at: str | None = None) -> None:
        """One gateway.request. cache and cached_at stay None while the demo cache is off (v0.2)."""
        data = m.GatewayRequestData(
            request_id=request_id, role=agent.role, action=action, target=target, decision=decision,
            rule_id=rule_id, reason=reason, source_id=source_id, latency_ms=_ms(t0), http_status=http_status,
            bytes=nbytes, tokens_in=tokens_in, tokens_out=tokens_out, cost_usd=cost_usd, cache=cache,
            cached_at=cached_at)
        if action == "model.invoke":
            note = {"hit": ", model cache hit, $0", "miss": ", recorded"}.get(cache or "", "")
            detail = f" ({tokens_in} in / {tokens_out} out{note})" if tokens_in is not None else f": {reason}"
            message = f"{agent.agent_id} -> {target} {decision}{detail}"
        else:
            note = {"hit": ", cache hit", "miss": ", cache miss, stored"}.get(cache or "", "")
            detail = f" (HTTP {http_status}, {nbytes} bytes{note})" if decision == "allowed" else f": {reason}"
            message = f"{agent.agent_id} fetch {target} {decision}{detail}"
        self.ctx.emit("gateway.request", data, message=message, agent_id=agent.agent_id,
                      level="info" if decision == "allowed" else "warn")

    # -- fetch ----------------------------------------------------------------

    async def fetch(self, agent: m.Agent, url: str, *, source_id: str | None = None,
                    why: str = "approved source") -> FetchResult:
        """GET url through the policy. Each redirect hop is decided again. Raises Blocked."""
        pacing = self.ctx.settings.demo_pacing
        if pacing > 0:
            await asyncio.sleep(pacing)
        target = url
        for hop in range(MAX_REDIRECTS + 1):
            hop_why = why if hop == 0 else f"redirect {hop} from {url}"
            result = await self._fetch_hop(agent, url, target, source_id, hop_why)
            if isinstance(result, FetchResult):
                return result
            target = result
        rid, t0 = self.ctx.next_id("REQ"), time.perf_counter()
        reason = f"More than {MAX_REDIRECTS} redirects. Blocked."
        self._emit_request(agent, rid, "fetch", target, "blocked", None, reason, t0, source_id=source_id)
        raise Blocked(rid, None, reason)

    def _cache(self) -> cache.DocCache:
        if self.doc_cache is None:
            self.doc_cache = cache.DocCache(self.ctx.settings)
        return self.doc_cache

    def _deny(self, agent: m.Agent, rid: str, target: str, rule_id: str | None, reason: str, t0: float, *,
              source_id: str | None, shown: str | None = None, http_status: int | None = None,
              nbytes: int | None = None) -> Blocked:
        """Emit the blocked gateway.request (reason `shown`, default `reason`) and return the Blocked to raise."""
        self._hold_stopped(agent, rule_id)
        self._emit_request(agent, rid, "fetch", target, "blocked", rule_id, shown or reason, t0, source_id=source_id,
                           http_status=http_status, nbytes=nbytes)
        return Blocked(rid, rule_id, reason)

    def _late_fetch(self, agent: m.Agent, rid: str, target: str, t0: float, source_id: str | None,
                    http_status: int, nbytes: int) -> None:
        """STOP, a revoke or the agent's stop may have landed while the body was on its way (network or cache):
        decide again and discard the result if it is now denied."""
        late, late_rule, late_reason = self.decide(agent, "fetch", target)
        if late != "allowed":
            reason = f"{late_reason} Result discarded: it landed after the policy changed."
            raise self._deny(agent, rid, target, late_rule, reason, t0, source_id=source_id, http_status=http_status,
                             nbytes=nbytes)

    async def _fetch_hop(self, agent: m.Agent, url: str, target: str, source_id: str | None,
                         why: str) -> FetchResult | str:
        """One decided GET. Returns the FetchResult, or the next URL when the answer is a redirect.

        The policy decides first. Only then, with CACHE_DOCUMENTS on (or for an s3:// archive URL), the cache
        is read: a hit is served with no network call, a miss is fetched live and stored."""
        rid, t0 = self.ctx.next_id("REQ"), time.perf_counter()
        decision, rule_id, reason = self.decide(agent, "fetch", target)
        if decision != "allowed":
            raise self._deny(agent, rid, target, rule_id, reason, t0, source_id=source_id, shown=f"{why}: {reason}")
        archive = cache.is_s3_url(target)
        use_cache = self.ctx.settings.cache_documents
        if archive and not use_cache:
            reason = ("gateway error: an s3:// archive copy is served only from the document cache, and "
                      "CACHE_DOCUMENTS is off. Never fetched live.")
            raise self._deny(agent, rid, target, None, reason, t0, source_id=source_id)
        if use_cache:
            hit = await self._cache().aget(target, max_bytes=http.MAX_BYTES)
            if hit is not None:
                return self._serve_hit(agent, rid, url, target, source_id, rule_id, f"{why}: {reason}", t0, hit)
            if archive:
                reason = (f"gateway error: archive copy not in the document cache ({self._cache().describe()}). "
                          "s3:// sources are served only from the cache, never fetched live.")
                raise self._deny(agent, rid, target, None, reason, t0, source_id=source_id)
        try:
            status, headers, body = await http.get_once(target, user_agent=self.ctx.settings.http_user_agent)
        except Exception as e:  # fail closed: timeouts, size cap, network errors
            raise self._deny(agent, rid, target, None, f"gateway error: {type(e).__name__}: {e}", t0,
                             source_id=source_id) from e
        self._late_fetch(agent, rid, target, t0, source_id, status, len(body))
        location = headers.get("location") if status in REDIRECT_CODES else None
        content_type = headers.get("content-type", "")
        stored = use_cache and cache.storable(status, location)
        self._emit_request(agent, rid, "fetch", target, "allowed", rule_id, f"{why}: {reason}", t0,
                           source_id=source_id, http_status=status, nbytes=len(body),
                           cache="miss" if use_cache else None)
        if stored:   # a copy for the next run. A failed write is logged by the cache, never fatal
            await self._cache().aput(target, body, content_type, http_status=status, location=location)
        if location:
            return urljoin(target, location)
        return FetchResult(url=url, final_url=target, http_status=status, content_type=content_type, body=body,
                           request_id=rid, cache="miss" if use_cache else None)

    def _serve_hit(self, agent: m.Agent, rid: str, url: str, target: str, source_id: str | None,
                   rule_id: str | None, reason: str, t0: float, hit: cache.CachedDoc) -> FetchResult | str:
        """A cache hit: no network call, cost 0, the stored bytes. A stored redirect sends the next hop back
        through decide(), like a live one."""
        meta = hit.meta
        self._late_fetch(agent, rid, target, t0, source_id, meta.http_status, len(hit.body))
        kind = "archive copy collected before the event" if meta.origin == "archive" else "copy stored by an earlier run"
        self._emit_request(agent, rid, "fetch", target, "allowed", rule_id,
                           f"{reason} Served from the document cache ({kind}, {meta.collected_at}).", t0,
                           source_id=source_id, http_status=meta.http_status, nbytes=len(hit.body), cost_usd=0.0,
                           cache="hit", cached_at=meta.collected_at)
        if meta.http_status in REDIRECT_CODES and meta.location:
            return urljoin(target, meta.location)
        return FetchResult(url=url, final_url=target, http_status=meta.http_status, content_type=meta.content_type,
                           body=hit.body, request_id=rid, origin=meta.origin, collected_at=meta.collected_at,
                           cache="hit")

    # -- invoke ---------------------------------------------------------------

    def _semaphore(self, size: str) -> asyncio.Semaphore:
        if size not in self._sems:
            s = self.ctx.settings
            self._sems[size] = asyncio.Semaphore(s.max_concurrency_large if size == "large" else s.max_concurrency_small)
        return self._sems[size]

    def _estimate(self, model_id: str, system: str, user: str, tool: ToolSpec, max_tokens: int) -> float:
        """Worst-case cost: prompt characters / 4 in, the full max_tokens out."""
        tokens_in = (len(system) + len(user) + len(json.dumps(tool.input_schema))) // 4 + 64
        return pricing.cost(model_id, tokens_in, max_tokens, prices=self.ctx.settings.prices)

    def _meter(self, agent: m.Agent, tokens_in: int, tokens_out: int, cost: float) -> None:
        """Roll one call's usage into the run and the agent (the registered one and the one passed in)."""
        run = self.ctx.state.run
        run.usage = _add_usage(run.usage, tokens_in, tokens_out, cost, budget=self.budget_usd())
        targets = {id(agent): agent}
        registered = self.ctx.state.agents.get(agent.agent_id)
        if registered is not None:
            targets[id(registered)] = registered
        for a in targets.values():
            a.usage = _add_usage(a.usage, tokens_in, tokens_out, cost)

    def _late_block(self, agent: m.Agent) -> tuple[str | None, str] | None:
        """(rule_id, reason) when a finished model call must be discarded: STOP, the analyst stopped this
        agent, or the budget ran out while it was in flight."""
        if self.kill_switch_on:
            return None, "Kill switch: run stopped while the call was in flight. Result discarded."
        stopped = self.agent_stopped(agent.agent_id)
        if stopped is not None:
            return stopped.rule_id, (f"{STOP_REASON} while the call was in flight "
                                     f"({stopped.note or 'no reason given'}). Result discarded.")
        budget = self.budget_usd()
        spent = self.ctx.state.run.usage.cost_usd
        if budget is not None and spent > budget:
            return None, f"Budget exceeded after the call: ${spent:.4f} of ${budget:.2f}. Result discarded."
        return None

    async def invoke(self, agent: m.Agent, size: Literal["large", "small"], *, system: str, user: str,
                     tool: ToolSpec, max_tokens: int = 4096) -> InvokeResult:
        """One forced-tool Converse call through the policy, metered. Raises Blocked."""
        settings = self.ctx.settings
        if size not in ("large", "small"):
            rid, t0 = self.ctx.next_id("REQ"), time.perf_counter()
            reason = f"gateway error: unknown model size {size!r}"
            self._emit_request(agent, rid, "model.invoke", str(size), "blocked", None, reason, t0)
            raise Blocked(rid, None, reason)
        model_id = settings.model_large if size == "large" else settings.model_small
        async with self._semaphore(size):
            rid, t0 = self.ctx.next_id("REQ"), time.perf_counter()
            try:
                est = self._estimate(model_id, system, user, tool, max_tokens)
            except Exception as e:
                est, decision, rule_id, reason = 0.0, "blocked", None, f"gateway error: {type(e).__name__}: {e}"
            else:
                decision, rule_id, reason = self.decide(agent, "model.invoke", model_id, est_cost=est)
            if decision != "allowed":
                self._hold_stopped(agent, rule_id)
                self._emit_request(agent, rid, "model.invoke", model_id, "blocked", rule_id, reason, t0)
                raise Blocked(rid, rule_id, reason)
            # Model cache (v0.2, V8): only an allowed call gets here, so a blocked one never reads or writes it.
            slot = self._model_slot(model_id, system, user, tool, max_tokens)
            if slot is not None and settings.cache_models == "replay":
                rec = await self._models().aget(*slot)
                if rec is not None:
                    return self._serve_model_hit(agent, rid, model_id, rule_id, reason, t0, rec)
            self._reserved += est
            try:
                reply = await bedrock.converse(settings, model_id, system=system, user=user, tool=tool,
                                               max_tokens=max_tokens)
            except Exception as e:  # fail closed: throttles after retries, access denied, missing fakes
                reason = f"gateway error: {type(e).__name__}: {e}"
                self._emit_request(agent, rid, "model.invoke", model_id, "blocked", None, reason, t0)
                raise Blocked(rid, None, reason) from e
            finally:
                self._reserved -= est
            cost = pricing.cost(model_id, reply.tokens_in, reply.tokens_out, prices=settings.prices)
            self._meter(agent, reply.tokens_in, reply.tokens_out, cost)
            late = self._late_block(agent)
            decision = "blocked" if late else "allowed"
            if late:
                self._hold_stopped(agent, late[0])
            self._emit_request(agent, rid, "model.invoke", model_id, decision, late[0] if late else rule_id,
                               late[1] if late else reason, t0, http_status=None if late else 200,
                               tokens_in=reply.tokens_in, tokens_out=reply.tokens_out, cost_usd=cost,
                               cache="miss" if slot is not None and not late else None)
            usage = self.ctx.state.run.usage
            self.ctx.emit("usage.updated", m.UsageUpdatedData(usage=usage.model_copy()),
                          message=f"Spent ${usage.cost_usd:.4f} so far", agent_id=agent.agent_id)
            if late:
                raise Blocked(rid, late[0], late[1])
            if slot is not None:     # record (or replay's miss): store the live answer for the next run
                await self._models().aput(cache.ModelRecord(
                    key=slot[0], occurrence=slot[1], model_id=model_id, tool_name=tool.name, max_tokens=max_tokens,
                    tool_input=reply.tool_input, tokens_in=reply.tokens_in, tokens_out=reply.tokens_out,
                    stop_reason=reply.stop_reason, recorded_at=now_iso(), run_id=self.ctx.state.run.run_id))
            return InvokeResult(tool_input=reply.tool_input, tokens_in=reply.tokens_in, tokens_out=reply.tokens_out,
                                cost_usd=cost, request_id=rid, model_id=model_id, stop_reason=reply.stop_reason)

    def _models(self) -> cache.ModelCache:
        if self.model_cache is None:
            self.model_cache = cache.ModelCache(self.ctx.settings)
        return self.model_cache

    def _model_slot(self, model_id: str, system: str, user: str, tool: ToolSpec,
                    max_tokens: int) -> tuple[str, int] | None:
        """(key, n) for this allowed call when CACHE_MODELS is record or replay: n counts the calls with the
        same key in this run, so repeated identical prompts (three judges) each get their own recorded answer.
        None when the cache is off, or when the key can't be built (the call then just goes live)."""
        if self.ctx.settings.cache_models not in ("record", "replay"):
            return None
        try:
            key = cache.model_key(model_id, system, user, tool_name=tool.name, tool_description=tool.description,
                                  input_schema=tool.input_schema, max_tokens=max_tokens)
        except Exception:  # noqa: BLE001  the cache never fails a call
            return None
        n = self._model_seen.get(key, 0) + 1
        self._model_seen[key] = n
        return key, n

    def _serve_model_hit(self, agent: m.Agent, rid: str, model_id: str, rule_id: str | None, reason: str,
                         t0: float, rec: cache.ModelRecord) -> InvokeResult:
        """A replayed answer: no model call, cost 0, the tokens as recorded. STOP or the agent's stop that
        landed while the cache was read still discards it."""
        late = self._late_block(agent)
        if late:
            self._hold_stopped(agent, late[0])
            self._emit_request(agent, rid, "model.invoke", model_id, "blocked", late[0], late[1], t0)
            raise Blocked(rid, late[0], late[1])
        self._meter(agent, rec.tokens_in, rec.tokens_out, 0.0)
        self._emit_request(agent, rid, "model.invoke", model_id, "allowed", rule_id,
                           f"{reason} Served from the model cache (answer recorded {rec.recorded_at}).", t0,
                           tokens_in=rec.tokens_in, tokens_out=rec.tokens_out, cost_usd=0.0, cache="hit",
                           cached_at=rec.recorded_at)
        usage = self.ctx.state.run.usage
        self.ctx.emit("usage.updated", m.UsageUpdatedData(usage=usage.model_copy()),
                      message=f"Spent ${usage.cost_usd:.4f} so far (model cache hit)", agent_id=agent.agent_id)
        return InvokeResult(tool_input=copy.deepcopy(rec.tool_input), tokens_in=rec.tokens_in,
                            tokens_out=rec.tokens_out, cost_usd=0.0, request_id=rid, model_id=model_id,
                            stop_reason=rec.stop_reason)

    # -- scan -----------------------------------------------------------------

    def _allow_rule_for(self, url: str) -> str | None:
        parts = target_parts(url)
        for rule in self._policy().rules:
            if rule.effect == "allow" and rule_matches(rule, "fetcher", "fetch", url, parts):
                return rule.rule_id
        return None

    async def scan(self, agent: m.Agent, url: str, source_id: str, text: str) -> m.ScanResult:
        """Guardrails tripwire. When it intervenes it also emits gateway.request with decision "flagged".

        Verdict "error" means the scan didn't run or failed: treat the page as quarantined (fail closed).
        """
        if self.kill_switch_on:
            return m.ScanResult(scanner="none", verdict="error",
                                detail="Kill switch on: scan not run. Treat the page as quarantined.")
        if self.agent_stopped(agent.agent_id) is not None:
            return m.ScanResult(scanner="none", verdict="error",
                                detail=f"{STOP_REASON}: scan not run. Treat the page as quarantined.")
        t0 = time.perf_counter()
        try:
            out = await bedrock.apply_guardrail(self.ctx.settings, text)
        except Exception as e:
            return m.ScanResult(scanner="bedrock_guardrails", verdict="error",
                                detail=f"gateway error: {type(e).__name__}: {e}")
        action = out.get("action")
        if action == "SKIPPED":
            return m.ScanResult(scanner="none", verdict="skipped", detail=out.get("detail"))
        if action != "GUARDRAIL_INTERVENED":
            return m.ScanResult(scanner="bedrock_guardrails", verdict="clean", detail=out.get("detail"))
        rid = self.ctx.next_id("REQ")
        filters = ", ".join(out.get("filters") or []) or "prompt attack"
        reason = (f"Bedrock Guardrails flagged the fetched content ({filters}). "
                  "Stored in the vault, quarantined, never shown to an agent.")
        self._emit_request(agent, rid, "fetch", url, "flagged", self._allow_rule_for(url), reason, t0,
                           source_id=source_id, nbytes=len(text.encode("utf-8")))
        return m.ScanResult(scanner="bedrock_guardrails", verdict="flagged", detail=out.get("detail"))
