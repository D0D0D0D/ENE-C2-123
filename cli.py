"""AGENTIC STAR Marketplace entry adapter for ENE-C2-123."""

from __future__ import annotations

import json
import os
from collections.abc import Iterator, Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any, cast
from urllib.parse import quote, urlsplit
from urllib.request import Request, urlopen

from framework.schemas.invocation_context import InvocationContext
from framework.schemas.trust_level import TrustLevel
from framework.secrets.base import SecretProvider
from framework.secrets.context import current_secrets
from shared.services.llm.base_llm import BaseLLM

try:
    from framework.utils.config_loader import load_agent_config
except ImportError:  # AgentCore 1.0.0 / 1.0.1 compatibility
    from framework.utils.config_loader import load_config

    def load_agent_config(agent_dir: Path) -> dict[str, Any]:
        config_path = agent_dir / "config" / "config.yaml"
        return load_config(str(config_path)) if config_path.exists() else {}


try:
    from shared.bootstrap import run_agent_marketplace
except ModuleNotFoundError as exc:  # AgentCore 1.0.0 / 1.0.1 compatibility
    if exc.name != "shared.bootstrap":
        raise
    _BOOTSTRAP_IMPORT_ERROR: ModuleNotFoundError | None = exc

    def run_agent_marketplace(*args: Any, **kwargs: Any) -> None:
        del args, kwargs
        raise RuntimeError(
            "The installed AgentCore wheel does not provide Marketplace bootstrap support"
        ) from _BOOTSTRAP_IMPORT_ERROR
else:
    _BOOTSTRAP_IMPORT_ERROR = None


try:
    from shared.secrets.env_provider import EnvProvider
except ModuleNotFoundError as exc:  # AgentCore 1.0.0 / 1.0.1 compatibility
    if exc.name != "shared.secrets.env_provider":
        raise

    class EnvProvider(SecretProvider):  # type: ignore[no-redef]
        """Minimal process-environment provider for AgentCore 1.0.1."""

        def __init__(self, namespace: str = "", agent_name: str = "") -> None:
            self._namespace = namespace
            self._agent_name = agent_name

        def get(self, key: str, default: str | None = None) -> str | None:
            return os.environ.get(key, default)


from src.entry_adapter import SCOPE_KEYS, STG_DEFAULT_SCOPE_ENVS
from src.graph.graph import SupplyDataQualityPrecedentGraph
from src.services.runtime import RuntimeServices

_NAMESPACE = "ene"
_AGENT_NAME = "ENE-C2-123"
_PROVIDER_AGENT_NAME = "ene_c2_123"

_AZURE_OPENAI_SECRET_KEYS = (
    "AZURE_OPENAI_API_KEY",
    "AZURE_OPENAI_ENDPOINT",
    "AZURE_OPENAI_DEPLOYMENT",
)
_INVOKE_KEYS = frozenset({"mode", "exceptions", "revision_notices", "session_id"})
_EXCEPTION_KEYS = frozenset(
    {
        "exception_id",
        "exception_category",
        "series_id",
        "detected_at",
        "metrics",
        "operational_note",
    }
)
_REVISION_NOTICE_KEYS = frozenset({"notice_id", "series_id", "publisher", "effective_date", "body"})


def _validate_scope_declarations() -> None:
    """Validate the reusable entry-adapter declaration before env lookup."""

    if not isinstance(SCOPE_KEYS, tuple):
        raise RuntimeError("Marketplace SCOPE_KEYS must be a tuple")
    if len(set(SCOPE_KEYS)) != len(SCOPE_KEYS):
        raise RuntimeError("Marketplace SCOPE_KEYS must not contain duplicates")
    if any(not isinstance(key, str) or not key or any(char.isspace() for char in key) for key in SCOPE_KEYS):
        raise RuntimeError("Marketplace SCOPE_KEYS must contain clean non-empty strings")
    if not isinstance(STG_DEFAULT_SCOPE_ENVS, Mapping) or set(STG_DEFAULT_SCOPE_ENVS) != set(SCOPE_KEYS):
        raise RuntimeError("Marketplace STG_DEFAULT_SCOPE_ENVS must exactly match SCOPE_KEYS")
    if any(
        not isinstance(env_name, str) or not env_name or any(char.isspace() for char in env_name)
        for env_name in STG_DEFAULT_SCOPE_ENVS.values()
    ):
        raise RuntimeError("Marketplace scope environment names must be clean non-empty strings")


def _allowed_environment_keys() -> frozenset[str]:
    return frozenset((*_AZURE_OPENAI_SECRET_KEYS, *STG_DEFAULT_SCOPE_ENVS.values()))


class _AllowlistedEnvironmentProvider(EnvProvider):  # type: ignore[misc]
    """Expose only this single-agent container's declared environment values."""

    def __init__(self) -> None:
        super().__init__(namespace=_NAMESPACE, agent_name=_PROVIDER_AGENT_NAME)

    def get(self, key: str, default: str | None = None) -> str | None:
        if key not in _allowed_environment_keys():
            return default
        value = super().get(key)
        return value if value is not None else default


def _require_clean(provider: SecretProvider, key: str) -> str:
    """Resolve a required deployment value without trimming or repairing it."""

    value = provider.require(key)
    if not value or any(char.isspace() for char in value):
        raise ValueError(f"Marketplace environment variable {key!r} must be a clean non-empty string")
    return value


def _require_azure_endpoint(provider: SecretProvider) -> str:
    """Reject ``AZURE_OPENAI_ENDPOINT`` values that are not a bare Azure resource endpoint.

    ``AzureOpenAIClient`` appends its own ``/openai/v1/`` path, so anything
    beyond a scheme and host silently produces a broken request URL, and the
    failure surfaces inside ``complete()``. A node that swallows that exception
    degrades quietly while the invocation still reports ``status: success``,
    which is why this has to be a startup check. The value is reported here,
    never repaired.
    """

    endpoint = _require_clean(provider, "AZURE_OPENAI_ENDPOINT")
    parts = urlsplit(endpoint)
    try:
        port = parts.port
    except ValueError:
        port = -1
    if (
        parts.scheme not in ("http", "https")
        or not parts.hostname
        or parts.username is not None
        or parts.password is not None
        or port == -1
        or parts.path.strip("/")
        or parts.query
        or parts.fragment
        or any(character.isspace() for character in endpoint)
    ):
        raise ValueError(
            "Marketplace environment variable 'AZURE_OPENAI_ENDPOINT' must be a bare Azure "
            "resource endpoint -- scheme and host only, with no path (such as "
            "'/openai'), query or fragment"
        )
    return endpoint


def _scope_from_environment(provider: SecretProvider) -> dict[str, str]:
    """Build exactly the scope declared by ``src.entry_adapter``."""

    _validate_scope_declarations()
    return {scope_key: _require_clean(provider, STG_DEFAULT_SCOPE_ENVS[scope_key]) for scope_key in SCOPE_KEYS}


class _AzureOpenAIClientCompat(BaseLLM):
    """Small Azure chat-completion client for AgentCore 1.0.1."""

    def __init__(self, config: dict[str, str]) -> None:
        self._config = dict(config)

    def complete(self, messages: list[Any]) -> dict[str, Any]:
        endpoint = self._config["azure_endpoint"].rstrip("/")
        deployment = quote(self._config["azure_deployment"], safe="")
        request = Request(
            f"{endpoint}/openai/deployments/{deployment}/chat/completions?api-version=2024-10-21",
            data=json.dumps({"messages": messages}).encode("utf-8"),
            headers={"api-key": self._config["api_key"], "content-type": "application/json"},
            method="POST",
        )
        with urlopen(request, timeout=30) as response:  # noqa: S310 - deployment-owned endpoint
            payload = json.loads(response.read().decode("utf-8"))
        choices = payload.get("choices", []) if isinstance(payload, dict) else []
        message = choices[0].get("message", {}) if choices else {}
        content = message.get("content", "") if isinstance(message, dict) else ""
        return {
            "content": content,
            "tool_calls": message.get("tool_calls", []) if isinstance(message, dict) else [],
            "model": payload.get("model", self._config["azure_deployment"]),
            "usage": payload.get("usage", {}),
        }

    def stream(self, messages: list[Any]) -> Iterator[str]:
        content = self.complete(messages).get("content", "")
        if content:
            yield str(content)

    def bind_tools(self, tools: list[Any]) -> BaseLLM:
        if tools:
            raise NotImplementedError("Azure compatibility client does not support tool binding")
        return self


class _AzureMarketplaceLLM(BaseLLM):
    """Resolve Azure credentials and create a fresh client for every operation."""

    @staticmethod
    def _new_client() -> BaseLLM:
        provider = current_secrets()
        values = {key: _require_clean(provider, key) for key in _AZURE_OPENAI_SECRET_KEYS}

        # Azure is the sole invocation-time import. Framework, graph, security,
        # and State imports remain eager for Marketplace's worker-thread path.
        try:
            from shared.services.llm.azure_openai_client import AzureOpenAIClient
        except ModuleNotFoundError as exc:  # AgentCore 1.0.0 / 1.0.1 compatibility
            if exc.name != "shared.services.llm.azure_openai_client":
                raise
            AzureOpenAIClient = _AzureOpenAIClientCompat  # type: ignore[misc,assignment]

        return cast(
            BaseLLM,
            AzureOpenAIClient(
                {
                    "api_key": values["AZURE_OPENAI_API_KEY"],
                    "azure_endpoint": values["AZURE_OPENAI_ENDPOINT"],
                    "azure_deployment": values["AZURE_OPENAI_DEPLOYMENT"],
                }
            ),
        )

    def complete(self, messages: list[Any]) -> dict[str, Any]:
        return cast(dict[str, Any], self._new_client().complete(messages))

    def stream(self, messages: list[Any]) -> Iterator[str]:
        yield from self._new_client().stream(messages)

    def bind_tools(self, tools: list[Any]) -> BaseLLM:
        return self._new_client().bind_tools(tools)


def _services_with_azure(config: Mapping[str, Any], llm: BaseLLM) -> RuntimeServices:
    """Keep all configured precedent stores while replacing only the LLM seam."""

    existing = config.get("services")
    values = dict(config)
    values.pop("services", None)
    values["llm"] = llm
    if isinstance(existing, RuntimeServices):
        values.update(
            {
                "ledger": existing.ledger,
                "ledger_backend": existing.ledger_backend,
                "ledger_batch_writer": existing._ledger_batch_writer,
                "payload_store": existing.payload_store,
                "receipt_store": existing.receipt_store,
            }
        )
    return RuntimeServices(values)


def _marketplace_request(user_input: str) -> dict[str, Any]:
    """Parse the one Marketplace string as the public read-only invoke body."""

    if not isinstance(user_input, str) or not user_input.strip():
        raise ValueError("Marketplace input must be one non-empty JSON string")
    try:
        request = json.loads(user_input)
    except json.JSONDecodeError as exc:
        raise ValueError("Marketplace input must be a JSON invoke request") from exc
    if not isinstance(request, dict):
        raise ValueError("Marketplace input must be a JSON object")
    if request.get("mode", "invoke") != "invoke":
        raise ValueError("Marketplace supports read-only invoke mode")
    if set(request) - _INVOKE_KEYS:
        raise ValueError("Marketplace invoke request contains unsupported fields")
    exceptions = request.get("exceptions")
    notices = request.get("revision_notices", [])
    if not isinstance(exceptions, list) or len(exceptions) != 1:
        raise ValueError("Marketplace invoke requires exactly one exception")
    if not isinstance(notices, list):
        raise ValueError("Marketplace revision_notices must be a list")
    return {"exceptions": exceptions, "revision_notices": notices}


def _canonical_envelope(
    user_input: str,
    *,
    scope: dict[str, str],
    session_id: str,
    services: RuntimeServices,
) -> tuple[str, list[str]]:
    """Mint payload refs and build the graph's canonical invoke envelope."""

    request = _marketplace_request(user_input)
    refs: list[str] = []
    exception = request["exceptions"][0]
    if not isinstance(exception, dict) or set(exception) - _EXCEPTION_KEYS:
        raise ValueError("Marketplace exception fields are invalid")
    projected_exception = dict(exception)
    note = projected_exception.pop("operational_note", None)
    if note is not None:
        if not isinstance(note, str):
            raise ValueError("Marketplace operational_note must be a string")
        note_ref = services.payload_store.put(note, scope=scope, session_id=session_id)
        refs.append(note_ref)
        projected_exception["operational_note_ref"] = note_ref

    projected_notices: list[dict[str, Any]] = []
    try:
        for notice in request["revision_notices"]:
            if not isinstance(notice, dict) or set(notice) != _REVISION_NOTICE_KEYS:
                raise ValueError("Marketplace revision notice fields are invalid")
            projected = {key: value for key, value in notice.items() if key != "body"}
            if not isinstance(notice["body"], str):
                raise ValueError("Marketplace revision notice body must be a string")
            body_ref = services.payload_store.put(notice["body"], scope=scope, session_id=session_id)
            refs.append(body_ref)
            projected["body_ref"] = body_ref
            projected_notices.append(projected)
    except Exception:
        _delete_payload_refs(services, refs, scope=scope, session_id=session_id)
        raise

    envelope = {
        "schema": "ene-c2-123/v1",
        "mode": "invoke",
        **scope,
        "customer_ref": "a-star-marketplace",
        "payload": {
            "exceptions": [projected_exception],
            "revision_notices": projected_notices,
        },
    }
    return json.dumps(envelope, ensure_ascii=False, sort_keys=True), refs


def _delete_payload_refs(
    services: RuntimeServices,
    refs: list[str],
    *,
    scope: dict[str, str],
    session_id: str,
) -> None:
    for ref in refs:
        try:
            services.payload_store.delete(ref, scope=scope, session_id=session_id)
        except ValueError:
            continue


class MarketplaceGraph(SupplyDataQualityPrecedentGraph):
    """Marketplace-only read boundary, deployment scope, and Azure wiring."""

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        # The runner compiles before provision_secrets(). Scope is non-secret
        # deployment identity, so reject a broken Pod during construction.
        _scope_from_environment(_AllowlistedEnvironmentProvider())
        marketplace_config = dict(config or {})
        llm = _AzureMarketplaceLLM()
        marketplace_config.update(
            {
                "llm": llm,
                "services": _services_with_azure(marketplace_config, llm),
                "memory_enabled": False,
                "hitl": {"enabled": False},
            }
        )
        super().__init__(config=marketplace_config)

    def invoke(
        self,
        user_input: str,
        session_id: str = "",
        ctx: InvocationContext | None = None,
        input_context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        # conversation_history and all future runner-owned keys are untrusted.
        # Rebuild context from adapter-owned deployment scope only.
        del input_context
        scope = _scope_from_environment(_AllowlistedEnvironmentProvider())
        base_ctx = (
            ctx
            if ctx is not None
            else (InvocationContext(session_id=session_id) if session_id else InvocationContext())
        )
        marketplace_ctx = replace(
            base_ctx,
            caller_trust_level=TrustLevel.VERIFIED_EXTERNAL,
            hitl_allowed=False,
        )
        services = self.config.get("services")
        if not isinstance(services, RuntimeServices):
            raise RuntimeError("Marketplace RuntimeServices are not configured")
        canonical, refs = _canonical_envelope(
            user_input,
            scope=scope,
            session_id=marketplace_ctx.session_id,
            services=services,
        )
        try:
            return cast(
                dict[str, Any],
                super().invoke(
                    canonical,
                    session_id=marketplace_ctx.session_id,
                    ctx=marketplace_ctx,
                    input_context=scope,
                ),
            )
        finally:
            _delete_payload_refs(
                services,
                refs,
                scope=scope,
                session_id=marketplace_ctx.session_id,
            )

    def provision_secrets(self, provider: SecretProvider) -> None:
        # Ignore the runner's potentially broader/dotenv-capable provider. The
        # graph is already compiled; startup validates this adapter's env only.
        del provider
        environment_provider = _AllowlistedEnvironmentProvider()
        for key in _AZURE_OPENAI_SECRET_KEYS:
            _require_clean(environment_provider, key)
        _scope_from_environment(environment_provider)
        _require_azure_endpoint(environment_provider)
        super().provision_secrets(environment_provider)


def main() -> None:
    run_agent_marketplace(
        MarketplaceGraph,
        agent_name=_AGENT_NAME,
        namespace=_NAMESPACE,
        config=load_agent_config(Path(__file__).resolve().parent),
    )


if __name__ == "__main__":
    main()
