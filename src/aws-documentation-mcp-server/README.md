# AWS Documentation MCP Server

Model Context Protocol (MCP) server for AWS Documentation

This MCP server provides tools to access AWS documentation, search for content, and get recommendations.

## Features

- **Read Documentation**: Fetch and convert AWS documentation pages to markdown format
- **Search Documentation**: Search AWS documentation using the official search API (global only)
- **Read Sections**: Fetches sections of AWS documentation page and converts it to markdown format.
- **Search Table**: Filter and search rows in large documentation tables (e.g., service quotas, pricing) without reading the full page (global only)
- **Recommendations**: Get content recommendations for AWS documentation pages (global only)
- **Get Available Services List**: Get a list of available AWS services in China regions (China only)
- **Resolve AWS Entity**: Turn a service, operation, resource type, or resource property name written in prose into a canonical id (fact store, global only)
- **Query AWS Facts**: Look up enumerable AWS facts (endpoints, region availability, quotas, operation parameters, prerequisites) from authoritative sources, with an honest abstain instead of a guess (fact store, global only)

## Prerequisites

### Installation Requirements

1. Install `uv` from [Astral](https://docs.astral.sh/uv/getting-started/installation/) or the [GitHub README](https://github.com/astral-sh/uv#installation)
2. Install Python 3.10 or newer using `uv python install 3.10` (or a more recent version)

## Installation

| Kiro | Cursor | VS Code |
|:----:|:------:|:-------:|
| [![Add to Kiro](https://kiro.dev/images/add-to-kiro.svg)](https://kiro.dev/launch/mcp/add?name=awslabs.aws-documentation-mcp-server&config=%7B%22command%22%3A%22uvx%22%2C%22args%22%3A%5B%22awslabs.aws-documentation-mcp-server%40latest%22%5D%2C%22env%22%3A%7B%22FASTMCP_LOG_LEVEL%22%3A%22ERROR%22%2C%22AWS_DOCUMENTATION_PARTITION%22%3A%22aws%22%7D%7D) | [![Install MCP Server](https://cursor.com/deeplink/mcp-install-light.svg)](https://cursor.com/en/install-mcp?name=awslabs.aws-documentation-mcp-server&config=eyJjb21tYW5kIjoidXZ4IGF3c2xhYnMuYXdzLWRvY3VtZW50YXRpb24tbWNwLXNlcnZlckBsYXRlc3QiLCJlbnYiOnsiRkFTVE1DUF9MT0dfTEVWRUwiOiJFUlJPUiIsIkFXU19ET0NVTUVOVEFUSU9OX1BBUlRJVElPTiI6ImF3cyJ9LCJkaXNhYmxlZCI6ZmFsc2UsImF1dG9BcHByb3ZlIjpbXX0%3D) | [![Install on VS Code](https://img.shields.io/badge/Install_on-VS_Code-FF9900?style=flat-square&logo=visualstudiocode&logoColor=white)](https://insiders.vscode.dev/redirect/mcp/install?name=AWS%20Documentation%20MCP%20Server&config=%7B%22command%22%3A%22uvx%22%2C%22args%22%3A%5B%22awslabs.aws-documentation-mcp-server%40latest%22%5D%2C%22env%22%3A%7B%22FASTMCP_LOG_LEVEL%22%3A%22ERROR%22%2C%22AWS_DOCUMENTATION_PARTITION%22%3A%22aws%22%7D%2C%22disabled%22%3Afalse%2C%22autoApprove%22%3A%5B%5D%7D) |

Configure the MCP server in your MCP client configuration:

```json
{
  "mcpServers": {
    "awslabs.aws-documentation-mcp-server": {
      "command": "uvx",
      "args": ["awslabs.aws-documentation-mcp-server@latest"],
      "env": {
        "FASTMCP_LOG_LEVEL": "ERROR",
        "AWS_DOCUMENTATION_PARTITION": "aws",
        "MCP_USER_AGENT": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
      },
      "disabled": false,
      "autoApprove": []
    }
  }
}
```

For Kiro MCP configuration, see the [Kiro IDE documentation](https://kiro.dev/docs/mcp/configuration/) or the [Kiro CLI documentation](https://kiro.dev/docs/cli/mcp/configuration/) for details.

For global configuration, edit `~/.kiro/settings/mcp.json`. For project-specific configuration, edit `.kiro/settings/mcp.json` in your project directory.

### Windows Installation

For Windows users, the MCP server configuration format is slightly different:

```json
{
  "mcpServers": {
    "awslabs.aws-documentation-mcp-server": {
      "disabled": false,
      "timeout": 60,
      "type": "stdio",
      "command": "uv",
      "args": [
        "tool",
        "run",
        "--from",
        "awslabs.aws-documentation-mcp-server@latest",
        "awslabs.aws-documentation-mcp-server.exe"
      ],
      "env": {
        "FASTMCP_LOG_LEVEL": "ERROR",
        "AWS_DOCUMENTATION_PARTITION": "aws"
      }
    }
  }
}
```


> **Note**: Set `AWS_DOCUMENTATION_PARTITION` to `aws-cn` to query AWS China documentation instead of global AWS documentation.
>
> **Corporate Networks**: If you're behind a corporate proxy or firewall that blocks certain User-Agent strings, set `MCP_USER_AGENT` to match your browser's User-Agent to an allowable string.

or docker after a successful `docker build -t mcp/aws-documentation .`:

```json
{
  "mcpServers": {
    "awslabs.aws-documentation-mcp-server": {
      "command": "docker",
      "args": [
        "run",
        "--rm",
        "--interactive",
        "--env",
        "FASTMCP_LOG_LEVEL=ERROR",
        "--env",
        "AWS_DOCUMENTATION_PARTITION=aws",
        "mcp/aws-documentation:latest"
      ],
      "env": {},
      "disabled": false,
      "autoApprove": []
    }
  }
}
```

## Environment Variables

| Variable | Description | Default |
|----------|-------------|----------|
| `FASTMCP_LOG_LEVEL` | Logging level (DEBUG, INFO, WARNING, ERROR, CRITICAL) | `WARNING` |
| `AWS_DOCUMENTATION_PARTITION` | AWS partition (`aws` or `aws-cn`) | `aws` |
| `MCP_USER_AGENT` | Custom User-Agent string for HTTP requests | Chrome-based default |
| `AWS_FACT_STORE_ENABLED` | Register the fact store tools (`false` to hide them) | `true` |
| `AWS_FACT_STORE_ENDPOINT` | Fact store query endpoint | the shared POC endpoint |
| `AWS_FACT_STORE_REGION` | Signing region for the fact store API | `us-west-2` |
| `AWS_FACT_STORE_PROFILE` | AWS profile used to sign fact store requests (falls back to `AWS_PROFILE`, then the default chain) | unset |
| `AWS_FACT_STORE_ROLE_ARN` | Role to assume before signing. Needed when the store runs in a different AWS account from your credentials | unset |

### Corporate Network Support

For corporate environments with proxy servers or firewalls that block certain User-Agent strings:

```json
{
  "env": {
    "MCP_USER_AGENT": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
  }
}
```

## Basic Usage

Example:

- "look up documentation on S3 bucket naming rule. cite your sources"
- "recommend content for page https://docs.aws.amazon.com/AmazonS3/latest/userguide/bucketnamingrules.html"

![AWS Documentation MCP Demo](https://github.com/awslabs/mcp/blob/main/src/aws-documentation-mcp-server/basic-usage.gif?raw=true)

## Tools

### read_documentation

Fetches an AWS documentation page and converts it to markdown format.

```python
read_documentation(url: str) -> str
```

### search_documentation (global only)

Searches AWS documentation using the official AWS Documentation Search API.

```python
search_documentation(ctx: Context, search_phrase: str, limit: int, product_types: Optional[List[str]], guide_types: Optional[List[str]]) -> SearchResponse
```

### read_sections (global only)

Fetches sections of AWS documentation page and converts it to markdown format.

```python
read_sections(url: str, section: list[str]) -> list[dict]
```

### search_table (global only)

Searches for specific rows in large documentation tables (e.g., service quotas, pricing tables, supported models). More efficient than `read_sections` for pages with hundreds of table rows — returns matching rows as structured JSON instead of the full table.

```python
search_table(url: str, section_title: Optional[str], query: str, max_rows: int = 20) -> SearchTableResponse
```

### recommend (global only)

Gets content recommendations for an AWS documentation page.

```python
recommend(url: str) -> list[dict]
```

### get_available_services (China only)

Gets a list of available AWS services in China regions.

```python
get_available_services() -> str
```

### resolve_aws_entity (global only)

Resolves an AWS name written in prose to the canonical id that `query_aws_facts` needs. Service codes
rarely match the marketing name, so resolve before guessing.

```python
resolve_aws_entity(name: str, entity_type: str | None = None) -> dict
```

Returns candidates carrying `canonical_id`, `type`, `score`, and `resolved_via`. Services also carry
`display_name` and `has_facts`. Several candidates means the name is genuinely ambiguous, and the store
will not pick for you. Zero candidates is expected for features, third-party products, and things that
are not services.

### query_aws_facts (global only)

Looks up an enumerable AWS fact, or abstains.

```python
query_aws_facts(
    q: str | None = None,
    fact_type: str | None = None,       # endpoint, availability, quota, op_attribute,
                                        # list_operations, pricing, find_related, prerequisites
    service: str | None = None,
    region: str | None = None,
    operation: str | None = None,
    resource_property: str | None = None,
    depth: int | None = None,
    from_handle: str | None = None,
    traverse: str | None = None,
    resource_type: str | None = None,
) -> dict
```

Branch on `answer.status`: `ok` terminates, `evidence` means pass the cited URLs to
`read_documentation`, and `abstain` means read `reason_code` before deciding whether to retry. A
`missing_slot` means fill the slot rather than rephrase.

Give the slots when they are known. That path skips the keyword classifier and is both faster and more
accurate than the natural-language one.

## Fact store access

The fact store endpoint is IAM-authorized, so requests are SigV4-signed with the caller's own
credentials and the caller needs `execute-api:Invoke` on the API. Without access the two tools return an
abstain with `reason_code: store_unavailable` and name the documentation tools as the fallback, so the
rest of the server keeps working. Set `AWS_FACT_STORE_ENABLED=false` to leave them out of the tool list

### Reaching a fact store in another account

The query API is an API Gateway HTTP API with `AWS_IAM` authorization. HTTP APIs cannot carry a resource
policy, so a principal outside the store's own account cannot be granted `execute-api:Invoke` directly. It
has to assume a role in that account instead.

Set both variables. The profile supplies your own credentials; the role ARN names what to assume:

```jsonc
"env": {
  "AWS_FACT_STORE_PROFILE": "<your profile>",
  "AWS_FACT_STORE_ROLE_ARN": "<ask the store owner>"
}
```

Credentials refresh automatically, so a long-running server keeps working past the one-hour session
limit. Leave `AWS_FACT_STORE_ROLE_ARN` unset when your credentials are already in the store's account.

Two failure modes to expect. `AccessDenied` on `AssumeRole` means your role lacks `sts:AssumeRole` on the
target, or the target does not trust your account. A `403` on the API itself means the assume succeeded but
the assumed role lacks `execute-api:Invoke`. Both return an abstain-shaped result rather than an
exception, so the agent gets a next action instead of a stack trace.
entirely.

## Development

For getting started with development on the AWS Documentation MCP server, please refer to the awslabs/mcp DEVELOPER_GUIDE first. Everything below this is specific to AWS Documentation MCP Server development.

### Running tests

Unit tests: `uv run --frozen pytest --cov --cov-branch --cov-report=term-missing`
Unit tests with integration tests: `uv run --frozen pytest --cov --cov-branch --cov-report=term-missing --run-live`
