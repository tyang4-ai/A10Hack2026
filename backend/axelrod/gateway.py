"""The gateway: the only path to the network and to Bedrock. It enforces the policy, the kill switch
and the budget, meters every call, emits one gateway.request per decision, and fails closed.

Decision order (CONTRACT.md section 6, first match wins): kill switch, budget, role scope, explicit
deny, allow, default deny. Any exception while deciding or carrying out a request is a deny. Link-local
fetch targets (instance metadata lives there) are denied with the explicit denies, so no allow rule,
not even an analyst-approved source, can reach them.
"""
from __future__ import annotations

import asyncio
import ipaddress
import json
import re
import socket
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal
from urllib.parse import urljoin

from . import bedrock, pricing
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
    """R-revoke-, R-allow- or R-reject- for one source, or None while it is still pending."""
    parts = http.split_url(src.url)
    domain, prefix = (parts[1], parts[2]) if parts else (src.domain.lower(), None)
    sid = src.source_id
    common = {"action": "fetch", "domain": domain, "path_prefix": prefix, "source_id": sid, "created_at": at}
    if sid in revoked:
        return m.PolicyRule(rule_id=f"R-revoke-{sid}", effect="deny", roles=[], origin="user_revoke",
                            note=revoked[sid] or "Revoked by user", **common)
    if src.approval in ("approved", "auto_approved"):
        if parts is None:       # not an http(s) URL: never allow it
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
        st = self.ctx.state
        at = now_iso()
        old = {r.rule_id: r for r in st.policy.rules} if st.policy else {}
        rules = [_sys_rule(at)]
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
        parts = http.split_url(target) if action == "fetch" else None
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
            return "blocked", None, "Not a plain http(s) URL. Default deny."
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
            detail = f" ({tokens_in} in / {tokens_out} out)" if tokens_in is not None else f": {reason}"
            message = f"{agent.agent_id} -> {target} {decision}{detail}"
        else:
            detail = f" (HTTP {http_status}, {nbytes} bytes)" if decision == "allowed" else f": {reason}"
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

    async def _fetch_hop(self, agent: m.Agent, url: str, target: str, source_id: str | None,
                         why: str) -> FetchResult | str:
        """One decided GET. Returns the FetchResult, or the next URL when the answer is a redirect."""
        rid, t0 = self.ctx.next_id("REQ"), time.perf_counter()
        decision, rule_id, reason = self.decide(agent, "fetch", target)
        if decision != "allowed":
            self._emit_request(agent, rid, "fetch", target, "blocked", rule_id, f"{why}: {reason}", t0,
                               source_id=source_id)
            raise Blocked(rid, rule_id, reason)
        try:
            status, headers, body = await http.get_once(target, user_agent=self.ctx.settings.http_user_agent)
        except Exception as e:  # fail closed: timeouts, size cap, network errors
            reason = f"gateway error: {type(e).__name__}: {e}"
            self._emit_request(agent, rid, "fetch", target, "blocked", None, reason, t0, source_id=source_id)
            raise Blocked(rid, None, reason) from e
        # STOP or a revoke may have landed while the request was in flight: decide again, discard if denied.
        late, late_rule, late_reason = self.decide(agent, "fetch", target)
        if late != "allowed":
            reason = f"{late_reason} Result discarded: it landed after the policy changed."
            self._emit_request(agent, rid, "fetch", target, "blocked", late_rule, reason, t0, source_id=source_id,
                               http_status=status, nbytes=len(body))
            raise Blocked(rid, late_rule, reason)
        location = headers.get("location") if status in REDIRECT_CODES else None
        self._emit_request(agent, rid, "fetch", target, "allowed", rule_id, f"{why}: {reason}", t0,
                           source_id=source_id, http_status=status, nbytes=len(body))
        if location:
            return urljoin(target, location)
        return FetchResult(url=url, final_url=target, http_status=status,
                           content_type=headers.get("content-type", ""), body=body, request_id=rid)

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

    def _late_block(self) -> str | None:
        """Why a finished model call must be discarded, if it must."""
        if self.kill_switch_on:
            return "Kill switch: run stopped while the call was in flight. Result discarded."
        budget = self.budget_usd()
        spent = self.ctx.state.run.usage.cost_usd
        if budget is not None and spent > budget:
            return f"Budget exceeded after the call: ${spent:.4f} of ${budget:.2f}. Result discarded."
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
                self._emit_request(agent, rid, "model.invoke", model_id, "blocked", rule_id, reason, t0)
                raise Blocked(rid, rule_id, reason)
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
            late = self._late_block()
            decision = "blocked" if late else "allowed"
            self._emit_request(agent, rid, "model.invoke", model_id, decision, None if late else rule_id,
                               late or reason, t0, http_status=None if late else 200,
                               tokens_in=reply.tokens_in, tokens_out=reply.tokens_out, cost_usd=cost)
            usage = self.ctx.state.run.usage
            self.ctx.emit("usage.updated", m.UsageUpdatedData(usage=usage.model_copy()),
                          message=f"Spent ${usage.cost_usd:.4f} so far", agent_id=agent.agent_id)
            if late:
                raise Blocked(rid, None, late)
            return InvokeResult(tool_input=reply.tool_input, tokens_in=reply.tokens_in, tokens_out=reply.tokens_out,
                                cost_usd=cost, request_id=rid, model_id=model_id, stop_reason=reply.stop_reason)

    # -- scan -----------------------------------------------------------------

    def _allow_rule_for(self, url: str) -> str | None:
        parts = http.split_url(url)
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
