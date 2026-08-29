# Cloud Control-Plane Alignment Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `agent-mgr` a strongly typed, JSON-native client of Plow's existing cloud-agent API while leaving Plow's cloud infrastructure unchanged.

**Architecture:** Add strict immutable wire models, a dependency-free authenticated HTTP transport, and five JSON-only cloud resource commands to agent-mgr. Plow remains the sole owner of provider and tenant lifecycle mechanics; agent-mgr validates and forwards the public API's request and resource shapes without inventing a parallel cloud abstraction.

**Tech Stack:** Python 3.11+, frozen slotted dataclasses, `typing.Literal` and `Protocol`, standard-library `urllib`, pytest 8.4.2, strict mypy 1.17.1, Ruff 0.12.11, zipapp distribution.

**Spec:** `docs/superpowers/specs/2026-08-29-cloud-control-plane-alignment-design.md`

## Global Constraints

- Make no changes to the Plow repository, cloud-agent providers, image builds, Terraform, or deployment workflows.
- Keep the distributed zipapp dependency-free at runtime; use only the Python 3.11 standard library.
- Require `PLOW_API_BASE` and `PLOW_API_TOKEN`; never provide a production default or persist either value.
- Permit plain HTTP only for `localhost`, `127.0.0.1`, and `::1`; reject redirects rather than forwarding authorization.
- Preserve Plow's lifecycle and failure vocabularies verbatim and reject every unknown value.
- Cloud commands are JSON-only, and create/update input is the API's JSON object rather than a duplicate flag vocabulary.
- Follow red-green-refactor TDD and run strict mypy and Ruff before completion.

---

## File Structure

- `agent_mgr/cloud_models.py` — immutable request/resource types plus strict JSON boundary parsing and serialization.
- `agent_mgr/cloud_http.py` — environment configuration, URL validation, redirect refusal, authenticated JSON request/response transport, and sanitized remote errors.
- `agent_mgr/cloud_client.py` — typed mapping from the five public cloud operations to HTTP methods and paths.
- `agent_mgr/cli.py` — thin JSON-only command dispatch and request-file/stdin loading; no HTTP or schema logic.
- `agent_mgr/errors.py` — stable local error codes for configuration, remote reachability, remote rejection, and invalid upstream data.
- `tests/fixtures/cloud-agent-contract.json` — representative public resources covering every lifecycle and failure value.
- `tests/test_cloud_models.py` — exhaustive request/resource boundary tests.
- `tests/test_cloud_http.py` — configuration, URL, auth, redirect, HTTP, and redaction tests.
- `tests/test_cloud_client.py` — method/path/body mapping and typed response tests over a fake transport.
- `tests/test_cloud_cli.py` — subprocess-level JSON input/output and failure-envelope tests.
- `tests/test_distribution.py` — zipapp packaging smoke added in the cloud-dispatch red-green cycle.
- `README.md` — concise machine-oriented cloud command reference and explicit local/cloud ownership boundary.

### Task 1: Strict cloud wire models

**Files:**
- Create: `agent_mgr/cloud_models.py`
- Create: `tests/fixtures/cloud-agent-contract.json`
- Create: `tests/test_cloud_models.py`

**Interfaces:**
- Consumes: `agent_mgr.models.JsonValue`.
- Produces: `CloudStatus`, `FailureCode`, `CreateCloudAgentRequest.from_json()`, `UpdateCloudAgentChatsRequest.from_json()`, `CloudAgentResource.from_json()`, and `to_json()` on all three dataclasses.

- [ ] **Step 1: Write the failing request-model tests**

Create tests that establish exact API-shaped input and strict rejection:

```python
from agent_mgr.cloud_models import CreateCloudAgentRequest, UpdateCloudAgentChatsRequest
from agent_mgr.errors import AgentMgrError, ErrorCode


def test_create_request_round_trips_the_api_shape() -> None:
    request = CreateCloudAgentRequest.from_json(
        {"name": "Mary", "provider": "exe:hermes", "chat_uids": ["cht_a", "cht_b"]}
    )
    assert request.to_json() == {
        "name": "Mary",
        "provider": "exe:hermes",
        "chat_uids": ["cht_a", "cht_b"],
    }


def test_update_request_deduplicates_chats_in_first_seen_order() -> None:
    request = UpdateCloudAgentChatsRequest.from_json(
        {"chat_uids": ["cht_b", "cht_a", "cht_b"]}
    )
    assert request.to_json() == {"chat_uids": ["cht_b", "cht_a"]}


@pytest.mark.parametrize(
    "value",
    [
        {},
        {"chat_uids": []},
        {"chat_uids": [""]},
        {"chat_uids": [1]},
        {"chat_uids": ["cht_a"], "surprise": True},
        ["cht_a"],
    ],
)
def test_update_request_rejects_every_non_contract_shape(value: object) -> None:
    with pytest.raises(AgentMgrError) as raised:
        UpdateCloudAgentChatsRequest.from_json(value)
    assert raised.value.code is ErrorCode.INVALID_ARGUMENT
```

- [ ] **Step 2: Run the request tests and verify RED**

Run:

```bash
uv run --no-project --python 3.13 --with pytest==8.4.2 pytest -q tests/test_cloud_models.py
```

Expected: collection fails because `agent_mgr.cloud_models` does not exist.

- [ ] **Step 3: Implement immutable request models and shared validators**

Define these exact immutable public shapes in `agent_mgr/cloud_models.py`:

```python
CloudStatus: TypeAlias = Literal["running", "provisioning", "teardown", "failed"]
FailureCode: TypeAlias = Literal[
    "provider_unreachable",
    "image_pull_timeout",
    "setup_failed",
    "validation_failed",
    "unknown",
]


@dataclass(frozen=True, slots=True)
class CreateCloudAgentRequest:
    chat_uids: tuple[str, ...]
    name: str = "cloud agent"
    provider: str | None = None


@dataclass(frozen=True, slots=True)
class UpdateCloudAgentChatsRequest:
    chat_uids: tuple[str, ...]
```

Give each class a `from_json(cls, value: object)` constructor and `to_json(self) -> dict[str, JsonValue]`. Use private helpers `_object(value, allowed, required)`, `_nonempty_string(value, field)`, and `_chat_uids(value)` so both request types enforce the same rules. `_object` rejects non-dictionaries, computes `unknown = value.keys() - allowed` and `missing = required - value.keys()`, and raises `INVALID_ARGUMENT` naming the sorted keys. `_chat_uids` requires a list, validates every member with `_nonempty_string`, deduplicates with `tuple(dict.fromkeys(validated))`, and rejects the empty result. `CreateCloudAgentRequest.from_json()` accepts omitted `name` as `"cloud agent"` and omitted or JSON-null `provider` as `None`; explicit empty strings are invalid. Reject booleans wherever a string is required.

- [ ] **Step 4: Run the request tests and verify GREEN**

Run the Task 1 command again. Expected: all request tests pass.

- [ ] **Step 5: Write the failing resource-model and fixture tests**

Commit `tests/fixtures/cloud-agent-contract.json` as a JSON array containing nine resources: one each for `running`, `provisioning`, `teardown`, and deletion (`status: null`), plus five `failed` resources—one for each failure code. Add tests equivalent to:

```python
@pytest.mark.parametrize(
    "status,failure_code",
    [
        ("running", None),
        ("provisioning", None),
        ("teardown", None),
        (None, None),
        ("failed", "provider_unreachable"),
        ("failed", "image_pull_timeout"),
        ("failed", "setup_failed"),
        ("failed", "validation_failed"),
        ("failed", "unknown"),
    ],
)
def test_resource_accepts_every_public_terminal_shape(status, failure_code) -> None:
    raw = {
        "agent_id": "a" * 32,
        "chat_uids": ["cht_a"],
        "url": "https://agent.example",
        "provider": "exe:hermes",
        "status": status,
        "failure_code": failure_code,
    }
    assert CloudAgentResource.from_json(raw).to_json() == raw


@pytest.mark.parametrize(
    "status,failure_code",
    [
        ("running", "unknown"),
        ("failed", None),
        ("new-state", None),
        (None, "setup_failed"),
        ("failed", "vendor_message"),
    ],
)
def test_resource_rejects_inconsistent_or_open_ended_states(status, failure_code) -> None:
    raw = {
        "agent_id": "a" * 32,
        "chat_uids": ["cht_a"],
        "url": "https://agent.example",
        "provider": "exe:hermes",
        "status": status,
        "failure_code": failure_code,
    }
    with pytest.raises(AgentMgrError) as raised:
        CloudAgentResource.from_json(raw)
    assert raised.value.code is ErrorCode.INVALID_RESPONSE
```

The fixture test must load every object, round-trip it, and assert its observed status set is `{None, "running", "provisioning", "teardown", "failed"}`.

- [ ] **Step 6: Run the resource tests and verify RED**

Run the Task 1 command. Expected: failures report that `CloudAgentResource` and `ErrorCode.INVALID_RESPONSE` are absent.

- [ ] **Step 7: Add the invalid-response code and implement the resource model**

Add `INVALID_RESPONSE = "invalid_response"` to `ErrorCode`, then implement:

```python
@dataclass(frozen=True, slots=True)
class CloudAgentResource:
    agent_id: str
    chat_uids: tuple[str, ...]
    url: str
    provider: str
    status: CloudStatus | None
    failure_code: FailureCode | None
```

Give the class `from_json(cls, value: object) -> CloudAgentResource` and `to_json(self) -> dict[str, JsonValue]`. Require exactly the six public fields. Validate `status` by membership in `{"running", "provisioning", "teardown", "failed", None}` and `failure_code` by membership in the five-value closed set plus `None`, then use `typing.cast` to narrow both values for strict mypy. Accept `failure_code` only when `status == "failed"`, require it for every failed resource, and require it to be null otherwise. Use `ErrorCode.INVALID_RESPONSE` for every resource-side schema failure so malformed upstream data cannot be mistaken for bad operator input.

- [ ] **Step 8: Run Task 1 tests and strict type checking**

Run:

```bash
uv run --no-project --python 3.13 --with pytest==8.4.2 pytest -q tests/test_cloud_models.py
uv run --no-project --python 3.13 --with mypy==1.17.1 mypy agent_mgr
```

Expected: both commands pass.

- [ ] **Step 9: Commit Task 1**

```bash
git add agent_mgr/cloud_models.py agent_mgr/errors.py tests/fixtures/cloud-agent-contract.json tests/test_cloud_models.py
git commit -m "feat: model the Plow cloud agent contract"
```

### Task 2: Credential-safe HTTP transport

**Files:**
- Create: `agent_mgr/cloud_http.py`
- Create: `tests/test_cloud_http.py`
- Modify: `agent_mgr/errors.py`

**Interfaces:**
- Consumes: `JsonValue` and `AgentMgrError`.
- Produces: `CloudTransport` protocol, `HttpCloudTransport.from_environment(environ)`, and `HttpCloudTransport.request(method, path, body) -> object`.

- [ ] **Step 1: Write failing configuration and URL-validation tests**

```python
@pytest.mark.parametrize("missing", ["PLOW_API_BASE", "PLOW_API_TOKEN"])
def test_environment_requires_both_cloud_values(missing: str) -> None:
    environ = {
        "PLOW_API_BASE": "https://api.example",
        "PLOW_API_TOKEN": "secret-token",
    }
    del environ[missing]
    with pytest.raises(AgentMgrError) as raised:
        HttpCloudTransport.from_environment(environ)
    assert raised.value.code is ErrorCode.CONFIGURATION_ERROR
    assert "secret-token" not in str(raised.value)


@pytest.mark.parametrize(
    "url",
    [
        "http://api.example",
        "ftp://api.example",
        "https://user:password@api.example",
        "https://api.example/path?query=yes",
        "https://api.example/path#fragment",
    ],
)
def test_base_url_rejects_unsafe_or_ambiguous_origins(url: str) -> None:
    with pytest.raises(AgentMgrError):
        HttpCloudTransport.from_environment(
            {"PLOW_API_BASE": url, "PLOW_API_TOKEN": "secret-token"}
        )


@pytest.mark.parametrize("url", ["http://localhost:8000", "http://127.0.0.1:8000"])
def test_loopback_http_is_allowed_for_development(url: str) -> None:
    assert HttpCloudTransport.from_environment(
        {"PLOW_API_BASE": url, "PLOW_API_TOKEN": "secret-token"}
    ).base_url == url
```

- [ ] **Step 2: Run the focused tests and verify RED**

Run:

```bash
uv run --no-project --python 3.13 --with pytest==8.4.2 pytest -q tests/test_cloud_http.py
```

Expected: collection fails because `agent_mgr.cloud_http` does not exist.

- [ ] **Step 3: Implement configuration and the transport protocol**

Add these error codes:

```python
CONFIGURATION_ERROR = "configuration_error"
REMOTE_UNREACHABLE = "remote_unreachable"
REMOTE_REJECTED = "remote_rejected"
```

Implement a `CloudTransport` protocol with `request(self, method: str, path: str, body: dict[str, JsonValue] | None = None) -> object`, and define the concrete transport fields exactly as follows:

```python
@dataclass(frozen=True, slots=True)
class HttpCloudTransport:
    base_url: str
    token: str = field(repr=False)
    timeout_seconds: float = 30.0
```

Add `from_environment(cls, environ: Mapping[str, str]) -> HttpCloudTransport`; add the concrete `request()` method only in Step 7, after its failing tests exist. `from_environment` reads both exact keys with `environ.get(key, "").strip()`, raises `CONFIGURATION_ERROR` naming only a missing key, and never interpolates either value. Import `field` from `dataclasses`; the `repr=False` token field is required so assertion failures and debug representations cannot print it. Normalize one trailing slash from the validated base. Require a root-only URL with no userinfo, query, or fragment. Accept `http` only when `urlsplit(url).hostname` is one of the three loopback names in the spec.

- [ ] **Step 4: Run configuration tests and verify GREEN**

Run the Task 2 command. Expected: configuration tests pass; request tests have not been added yet.

- [ ] **Step 5: Write failing request, redirect, and redaction tests**

Patch `urllib.request.build_opener` with a recording fake. Assert a POST carries `Content-Type: application/json`, `Accept: application/json`, and exactly `Authorization: Bearer secret-token`; its body is compact UTF-8 JSON. Add rows proving:

```python
def test_transport_decodes_a_json_success(recording_opener) -> None:
    recording_opener.respond(200, b'{"agent_id":"abc"}')
    transport = configured_transport(recording_opener)
    assert transport.request("GET", "/v1/agents/cloud/abc") == {"agent_id": "abc"}


def test_transport_reports_only_recognized_remote_detail(recording_opener) -> None:
    recording_opener.raise_http_error(
        400,
        b'{"detail":"provider is not available","token":"secret-token"}',
    )
    with pytest.raises(AgentMgrError) as raised:
        configured_transport(recording_opener).request("GET", "/v1/agents/cloud")
    assert raised.value.code is ErrorCode.REMOTE_REJECTED
    assert str(raised.value) == "Plow API rejected the request (400): provider is not available"
    assert "secret-token" not in str(raised.value)
```

Also cover malformed success JSON as `INVALID_RESPONSE`, `URLError` as `REMOTE_UNREACHABLE`, and 301/302/307/308 as `REMOTE_REJECTED` without a second request. Implement a custom `HTTPRedirectHandler` whose `redirect_request()` returns `None` so the opener never forwards the Authorization header.
Assert `"secret-token" not in repr(transport)` in the configuration tests.

- [ ] **Step 6: Run request tests and verify RED**

Run the Task 2 command. Expected: failures identify missing request behavior.

- [ ] **Step 7: Implement authenticated JSON requests and sanitized failures**

Serialize only when `body is not None`; use no body for GET and DELETE. Validate `path.startswith("/v1/agents/cloud")` and reject any absolute URL. Decode every success as JSON. On `HTTPError`, parse only an object-valued body whose `detail` is a string; never interpolate other fields or raw bytes. On `URLError`, report `Plow API is unreachable` without the exception's potentially credential-bearing text.

- [ ] **Step 8: Run Task 2 tests, mypy, and Ruff**

```bash
uv run --no-project --python 3.13 --with pytest==8.4.2 pytest -q tests/test_cloud_http.py
uv run --no-project --python 3.13 --with mypy==1.17.1 mypy agent_mgr
uv run --no-project --python 3.13 --with ruff==0.12.11 ruff check agent_mgr tests/test_cloud_http.py
```

Expected: all commands pass.

- [ ] **Step 9: Commit Task 2**

```bash
git add agent_mgr/cloud_http.py agent_mgr/errors.py tests/test_cloud_http.py
git commit -m "feat: add a credential-safe Plow API transport"
```

### Task 3: Typed cloud resource client

**Files:**
- Create: `agent_mgr/cloud_client.py`
- Create: `tests/test_cloud_client.py`

**Interfaces:**
- Consumes: `CloudTransport`, both request models, and `CloudAgentResource`.
- Produces: `CloudClient.create()`, `list()`, `get()`, `update_chats()`, and `delete()` with fully typed parameters and return values.

- [ ] **Step 1: Write the failing method/path/body mapping tests**

Use this fake boundary:

```python
@dataclass
class FakeTransport:
    response: object
    calls: list[tuple[str, str, dict[str, JsonValue] | None]] = field(default_factory=list)

    def request(
        self, method: str, path: str, body: dict[str, JsonValue] | None = None
    ) -> object:
        self.calls.append((method, path, body))
        return self.response
```

Test these exact mappings:

```python
client.create(request)       # POST   /v1/agents/cloud              request.to_json()
client.list()                # GET    /v1/agents/cloud              None
client.get("agent-id")       # GET    /v1/agents/cloud/agent-id     None
client.update_chats(id, req) # PUT    /v1/agents/cloud/agent-id/chats req.to_json()
client.delete("agent-id")    # DELETE /v1/agents/cloud/agent-id     None
```

Assert single-resource methods return `CloudAgentResource`, list returns `tuple[CloudAgentResource, ...]`, an object in the list is rejected, and an empty or slash-containing agent id raises `INVALID_ARGUMENT` before transport is called.

- [ ] **Step 2: Run client tests and verify RED**

```bash
uv run --no-project --python 3.13 --with pytest==8.4.2 pytest -q tests/test_cloud_client.py
```

Expected: collection fails because `agent_mgr.cloud_client` does not exist.

- [ ] **Step 3: Implement the thin typed client**

```python
CLOUD_PATH = "/v1/agents/cloud"


def _agent_id(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_-]+", value):
        raise AgentMgrError(ErrorCode.INVALID_ARGUMENT, f"invalid cloud agent id: {value}")
    return value


@dataclass(frozen=True, slots=True)
class CloudClient:
    transport: CloudTransport

    def create(self, request: CreateCloudAgentRequest) -> CloudAgentResource:
        value = self.transport.request("POST", CLOUD_PATH, request.to_json())
        return CloudAgentResource.from_json(value)

    def list(self) -> tuple[CloudAgentResource, ...]:
        value = self.transport.request("GET", CLOUD_PATH)
        if not isinstance(value, list):
            raise AgentMgrError(ErrorCode.INVALID_RESPONSE, "cloud agent list is not an array")
        return tuple(CloudAgentResource.from_json(item) for item in value)

    def get(self, agent_id: str) -> CloudAgentResource:
        value = self.transport.request("GET", f"{CLOUD_PATH}/{_agent_id(agent_id)}")
        return CloudAgentResource.from_json(value)

    def update_chats(
        self, agent_id: str, request: UpdateCloudAgentChatsRequest
    ) -> CloudAgentResource:
        path = f"{CLOUD_PATH}/{_agent_id(agent_id)}/chats"
        value = self.transport.request("PUT", path, request.to_json())
        return CloudAgentResource.from_json(value)

    def delete(self, agent_id: str) -> CloudAgentResource:
        value = self.transport.request("DELETE", f"{CLOUD_PATH}/{_agent_id(agent_id)}")
        return CloudAgentResource.from_json(value)
```

Keep HTTP paths and decoding here, not in CLI dispatch. `_agent_id()` accepts only a non-empty path segment matching `[A-Za-z0-9_-]+`; this prevents path injection and does not overfit to today's 32-character server-generated identifiers.

- [ ] **Step 4: Run Task 3 tests and quality checks**

```bash
uv run --no-project --python 3.13 --with pytest==8.4.2 pytest -q tests/test_cloud_client.py
uv run --no-project --python 3.13 --with mypy==1.17.1 mypy agent_mgr
uv run --no-project --python 3.13 --with ruff==0.12.11 ruff check agent_mgr tests/test_cloud_client.py
```

Expected: all commands pass.

- [ ] **Step 5: Commit Task 3**

```bash
git add agent_mgr/cloud_client.py tests/test_cloud_client.py
git commit -m "feat: add typed cloud resource operations"
```

### Task 4: JSON-only cloud CLI commands

**Files:**
- Modify: `agent_mgr/cli.py`
- Create: `tests/test_cloud_cli.py`
- Modify: `tests/test_distribution.py`

**Interfaces:**
- Consumes: `CloudClient`, `HttpCloudTransport.from_environment()`, request `from_json()`, and resource `to_json()`.
- Produces: `cloud-create`, `cloud-list`, `cloud-get`, `cloud-update-chats`, and `cloud-delete` operations in the existing schema-version-1 envelope.

- [ ] **Step 1: Write failing subprocess tests for JSON-only dispatch**

Add tests using the existing `run` fixture:

```python
@pytest.mark.parametrize(
    "operation,args",
    [
        ("cloud-create", ("request.json",)),
        ("cloud-list", ()),
        ("cloud-get", ("agent-id",)),
        ("cloud-update-chats", ("agent-id", "request.json")),
        ("cloud-delete", ("agent-id",)),
    ],
)
def test_cloud_commands_require_json(operation: str, args: tuple[str, ...], run) -> None:
    result = run(operation, *args)
    assert result.returncode == 2
    assert "requires --json" in result.stderr


def test_cloud_create_reads_the_api_request_shape_from_a_file(
    run, tmp_path, cloud_server
) -> None:
    request = tmp_path / "request.json"
    request.write_text(
        json.dumps({"name": "Mary", "provider": "exe:hermes", "chat_uids": ["cht_a"]})
    )
    result = run(
        "--json",
        "cloud-create",
        str(request),
        env=cloud_server.environment,
    )
    assert result.returncode == 0
    assert json.loads(result.stdout)["result"]["agent"]["status"] == "provisioning"
    assert cloud_server.requests == [
        ("POST", "/v1/agents/cloud", {"name": "Mary", "provider": "exe:hermes", "chat_uids": ["cht_a"]})
    ]
```

Implement `cloud_server` as an in-process `ThreadingHTTPServer` fixture bound to `127.0.0.1` on an ephemeral port. It records method/path/decoded body, asserts `Authorization: Bearer test-token`, and returns the contract fixture resource selected by the test. This exercises the real standard-library transport without external network access.

Also extend the existing built-zipapp test before implementing dispatch:

```python
cloud = subprocess.run(
    [str(artifact), "--json", "cloud-list"],
    env={"PATH": os.environ["PATH"]},
    text=True,
    capture_output=True,
    check=False,
)
assert cloud.returncode == 1
assert json.loads(cloud.stdout)["error"]["code"] == "configuration_error"
assert cloud.stderr == ""
```

This assertion is red while `cloud-list` is unknown and turns green with the same dispatch implementation as the subprocess tests.

- [ ] **Step 2: Run CLI tests and verify RED**

```bash
uv run --no-project --python 3.13 --with pytest==8.4.2 pytest -q tests/test_cloud_cli.py
```

Expected: commands fail as unknown operations.

- [ ] **Step 3: Add request loading and cloud dispatch**

Add all five operation names to `NATIVE_JSON_OPERATIONS`. Before `_run()` dispatch, reject a cloud operation when `json_output` is false with `INVALID_ARGUMENT`, remediation `rerun with --json`, and exit code 2.

Add the five operation names and their argument shapes to `_usage()` so discovery and invalid-invocation output do not omit supported commands.

Implement:

```python
def _json_input(source: str) -> object:
    if source == "-":
        if sys.stdin.isatty():
            raise AgentMgrError(
                ErrorCode.INVALID_ARGUMENT,
                "refusing to wait for cloud JSON on an interactive terminal",
                "pipe a JSON object or pass a file path",
                2,
            )
        text = sys.stdin.read()
    else:
        try:
            text = Path(source).read_text(encoding="utf-8")
        except OSError as error:
            raise AgentMgrError(ErrorCode.IO_ERROR, f"could not read cloud request: {error}") from error
    try:
        return json.loads(text)
    except json.JSONDecodeError as error:
        raise AgentMgrError(ErrorCode.INVALID_ARGUMENT, f"invalid cloud request JSON: {error.msg}") from error
```

Construct one client per command with `CloudClient(HttpCloudTransport.from_environment(os.environ))`. Emit these exact result keys:

```python
cloud-create       -> {"agent": resource.to_json()}
cloud-list         -> {"agents": [resource.to_json() for resource in resources]}
cloud-get          -> {"agent": resource.to_json()}
cloud-update-chats -> {"agent": resource.to_json()}
cloud-delete       -> {"agent": resource.to_json()}
```

- [ ] **Step 4: Add complete success and failure coverage**

Add success rows for all five operations and failure rows for malformed input JSON, request schema errors, missing environment, 401 detail, connection refusal, malformed success JSON, `-` with piped input, and `-` with terminal stdin. Every `--json` row must assert exactly one JSON document on stdout, empty stderr, the requested operation name, and the stable error code where applicable. Assert the token never appears in either stream.

- [ ] **Step 5: Run CLI and existing JSON contract tests**

```bash
uv run --no-project --python 3.13 --with pytest==8.4.2 pytest -q tests/test_cloud_cli.py tests/test_json_contract.py
```

Expected: all tests pass.

- [ ] **Step 6: Run strict quality checks**

```bash
uv run --no-project --python 3.13 --with mypy==1.17.1 mypy agent_mgr
uv run --no-project --python 3.13 --with ruff==0.12.11 ruff check agent_mgr tests/test_cloud_cli.py
uv run --no-project --python 3.13 --with ruff==0.12.11 ruff format --check agent_mgr tests/test_cloud_cli.py
```

Expected: all commands pass.

- [ ] **Step 7: Commit Task 4**

```bash
git add agent_mgr/cli.py tests/test_cloud_cli.py tests/test_distribution.py
git commit -m "feat: expose cloud resources through JSON commands"
```

### Task 5: Operator documentation and full validation

**Files:**
- Modify: `README.md`

**Interfaces:**
- Consumes: the five cloud commands completed in Task 4.
- Produces: the supported cloud-control usage contract and full-repository validation evidence.

- [ ] **Step 1: Document the structured cloud contract**

Add a `Cloud agents` README section containing:

```bash
export PLOW_API_BASE=https://api.plow.co
read -rsp 'Plow API token: ' PLOW_API_TOKEN; export PLOW_API_TOKEN

printf '%s\n' '{"name":"Mary","provider":"exe:hermes","chat_uids":["cht_example"]}' \
  | agent-mgr --json cloud-create -
agent-mgr --json cloud-list
agent-mgr --json cloud-get AGENT_ID
printf '%s\n' '{"chat_uids":["cht_example","cht_second"]}' \
  | agent-mgr --json cloud-update-chats AGENT_ID -
agent-mgr --json cloud-delete AGENT_ID
```

State that create normally returns `status: "provisioning"`; callers poll `cloud-get` until `running`, `failed`, or `teardown`. Explain the remediation distinction: retry creation after `failed`; repeat deletion after `teardown`. State explicitly that agent-mgr never contacts exe.dev or handles tenant credentials and that local Compose commands remain separate.

- [ ] **Step 2: Run the complete repository validation**

```bash
uv run --no-project --python 3.13 --with pytest==8.4.2 --with pyyaml==6.0.2 pytest -q
uv run --no-project --python 3.13 --with mypy==1.17.1 mypy agent_mgr scripts/build_zipapp.py __main__.py
uv run --no-project --python 3.13 --with mypy==1.17.1 mypy lib/fetch-tree
uv run --no-project --python 3.13 --with ruff==0.12.11 ruff check agent_mgr scripts/build_zipapp.py __main__.py lib/fetch-tree
uv run --no-project --python 3.13 --with ruff==0.12.11 ruff format --check agent_mgr scripts/build_zipapp.py __main__.py lib/fetch-tree
python3 scripts/build_zipapp.py /tmp/agent-mgr.pyz
tmpdir="$(mktemp -d)"; HOME="$tmpdir" AGENT_MGR_REGISTRY="$tmpdir/agents" /tmp/agent-mgr.pyz --json ls
```

Expected: all tests and quality gates pass; the zipapp smoke returns one successful schema-version-1 JSON document.

- [ ] **Step 3: Commit Task 5**

```bash
git add README.md
git commit -m "docs: define the cloud control-plane contract"
```

### Task 6: Final contract audit

**Files:**
- Review: `docs/superpowers/specs/2026-08-29-cloud-control-plane-alignment-design.md`
- Review: `agent_mgr/cloud_models.py`
- Review: `agent_mgr/cloud_http.py`
- Review: `agent_mgr/cloud_client.py`
- Review: `agent_mgr/cli.py`
- Review: `README.md`

**Interfaces:**
- Consumes: all completed tasks.
- Produces: evidence that the implementation stayed on the client side and matches the public Plow resource without cloud-infrastructure changes.

- [ ] **Step 1: Prove no Plow or provider implementation entered the change**

Run:

```bash
git diff --name-only origin/main...HEAD
rg -n 'exe\.dev|terraform|blessed-release|class .*Provider|def deploy\(' agent_mgr tests README.md
```

Expected: every changed path is in agent-mgr; matches for provider implementation or direct exe.dev/Terraform operations are absent. Documentation may mention that agent-mgr does not contact exe.dev, but production code must not name it.

- [ ] **Step 2: Prove the wire vocabularies are closed and covered**

Run:

```bash
uv run --no-project --python 3.13 --with pytest==8.4.2 pytest -q tests/test_cloud_models.py tests/test_cloud_client.py tests/test_cloud_cli.py
```

Expected: tests cover all four non-null statuses, deletion null, and all five failure codes; the command passes.

- [ ] **Step 3: Review the final diff for credential exposure**

Run:

```bash
git diff origin/main...HEAD -- agent_mgr tests README.md
```

Confirm no token value is serialized, persisted, placed in exception text, or included in a test failure representation. Confirm redirects are refused and arbitrary upstream response bodies are not emitted.

- [ ] **Step 4: Run the final full validation once more**

Run every command from Task 5 Step 2. Expected: all pass on the final tree.

- [ ] **Step 5: Commit audit corrections only when the audit changed files**

If the audit required a correction, stage only those files and commit:

```bash
git add agent_mgr tests README.md
git commit -m "fix: close cloud client contract gaps"
```

If no correction was required, do not create an empty commit.
