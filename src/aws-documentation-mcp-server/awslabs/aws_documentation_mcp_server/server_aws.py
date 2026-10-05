# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""awslabs AWS Documentation MCP Server implementation."""

import httpx
import json
import re
import uuid
from awslabs.aws_documentation_mcp_server import fact_store

# Import models
from awslabs.aws_documentation_mcp_server.models import (
    RecommendationResult,
    ResponseMetadata,
    SearchResponse,
    SearchResult,
    SearchResultMetadata,
    SearchTableResponse,
)
from awslabs.aws_documentation_mcp_server.server_utils import (
    DEFAULT_USER_AGENT,
    add_search_result_cache_item,
    read_documentation_impl,
    read_sections_impl,
    search_table_impl,
)

# Import utility functions
from awslabs.aws_documentation_mcp_server.util import (
    add_search_intent_to_search_request,
    parse_recommendation_results,
)
from loguru import logger
from mcp.server.mcpserver import Context, MCPServer
from pydantic import BaseModel, Field, ValidationError
from typing import Any, Dict, List, Optional, Type, TypeVar


SEARCH_API_URL = 'https://proxy.search.docs.aws.com/search'
RECOMMENDATIONS_API_URL = 'https://api.contentrecs.docs.aws.com/v1/recommendations'
SESSION_UUID = str(uuid.uuid4())
SUPPORTED_METADATA_KEYS = ('discovered_services', 'related_tasks', 'relationships')
SUPPORTED_RESULT_METADATA_KEYS = ('additional_urls',)

_ModelT = TypeVar('_ModelT', bound=BaseModel)


def _safe_model_validate(model: Type[_ModelT], data: Dict[str, Any]) -> Optional[_ModelT]:
    """Validate optional metadata, dropping it (returning None) on any error.

    Metadata is best-effort enrichment: a malformed block from the search backend
    must never fail an otherwise-successful search. Log and drop instead of raising.
    """
    if not data:
        return None
    try:
        return model.model_validate(data)
    except (ValidationError, TypeError, KeyError, AttributeError) as e:
        logger.warning(f'Dropping malformed {model.__name__} metadata: {e}')
        return None


# Dict for domain modifiers for search if search terms contain any of the terms
SEARCH_TERM_DOMAIN_MODIFIERS = [
    {
        'terms': ['neuron', 'neuron sdk'],
        'domains': [{'key': 'domain', 'value': 'awsdocs-neuron.readthedocs-hosted.com'}],
        'regex': r'^https?://awsdocs-neuron\.readthedocs-hosted\.com/',
    }
]


mcp = MCPServer(
    'awslabs.aws-documentation-mcp-server',
    instructions="""
    # AWS Documentation MCP Server

    This server provides tools to access public AWS documentation, search for content, and get recommendations.

    ## Best Practices

    - For long documentation pages, make multiple calls to `read_documentation` with different `start_index` values for pagination
    - By default, use read_sections when the answer could be within a specific section(s), given the table of contents. Otherwise, use read_documentation to scan the entire page.
    - For very long documents (>30,000 characters), stop reading if you've found the needed information
    - When searching, use specific technical terms rather than general phrases
    - Use `recommend` tool to discover related content that might not appear in search results
    - For recent updates to a service, get an URL for any page in that service, then check the **New** section of the `recommend` tool output on that URL
    - If multiple searches with similar terms yield insufficient results, pivot to using `recommend` to find related pages.
    - Always cite the documentation URL when providing information to users
    - For an enumerable fact (a regional endpoint, which regions a service is in, a default quota value, an operation's required parameters, what must exist before an operation succeeds), call `query_aws_facts` before searching or recalling. These values are arbitrary and change, so a remembered one is often wrong.
    - When `query_aws_facts` returns `status: "evidence"`, pass its `evidence` URLs to `read_documentation`. The fact store found no typed fact, so the prose is the answer.
    - When `query_aws_facts` returns `status: "abstain"` with `reason_code: "missing_slot"`, fill the named slot and call again. Do not rephrase the question.
    - When `query_aws_facts` returns `status: "ok"`, the fact is authoritative. Stop; do not search to confirm it.
    - When `query_aws_facts` returns `status: "candidates"`, the rows only share words with the question. Check each `quota_name` against what was asked, and fall back to search if none fits. Never report a candidate as the answer without that check.

    ## Tool Selection Guide

    - Use `search_documentation` when: You need to find documentation about a specific AWS service or feature
    - Use `read_documentation` when: You have a specific documentation URL and need its content
    - Use `read_sections` when: You have a specific documentation URL and specific section title(s) and only need content from those specific section(s)
    - Use `search_table` when: You need specific rows from a large table (e.g., service quotas, pricing, supported models). If read_sections or read_documentation shows a truncated table, use this tool with a query to find the rows you need.
    - Use `recommend` when: You want to find related content to a documentation page you're already viewing or need to find newly released information
    - Use `recommend` as a fallback when: Multiple searches have not yielded the specific information needed
    - Use `resolve_aws_entity` when: You need the canonical code for a service, operation, resource type, or resource property. Service codes rarely match the marketing name (Cognito user pools are `cognito-idp`, Elastic Load Balancing is `elb`), so resolve before guessing.
    - Use `query_aws_facts` when: The question has one correct enumerable answer: endpoints, region availability, quotas, operation parameters and IAM actions, headline pricing, operation prerequisites. Give the slots when known, because that path is both faster and more accurate than the natural-language one.
    - Do NOT use `query_aws_facts` for: troubleshooting, best practices, how-to, comparisons between services, or feature-level questions. It abstains on these by design. Search instead.

    ## Facts versus prose

    The fact store and the documentation tools answer different questions, and they compose. A fact
    terminates the task. A citation continues it. So resolve the entity, ask for the fact, and fall back
    to search and read only when the store abstains or returns evidence rather than a fact.
    """,
    dependencies=[
        'pydantic',
        'httpx',
        'beautifulsoup4',
        'boto3',
    ],
)


@mcp.tool()
async def read_documentation(
    ctx: Context,
    url: str = Field(description='URL of the AWS documentation page to read'),
    max_length: int = Field(
        default=5000,
        description='Maximum number of characters to return.',
        gt=0,
        lt=1000000,
    ),
    start_index: int = Field(
        default=0,
        description='On return output starting at this character index, useful if a previous fetch was truncated and more content is required.',
        ge=0,
    ),
) -> str:
    """Fetch and convert an AWS documentation page to markdown format.

    ## Usage

    This tool retrieves the content of an AWS documentation page and converts it to markdown format.
    For long documents, you can make multiple calls with different start_index values to retrieve
    the entire content in chunks.

    ## URL Requirements

    - Must be from the docs.aws.amazon.com domain
    - Must end with .html

    ## Example URLs

    - https://docs.aws.amazon.com/AmazonS3/latest/userguide/bucketnamingrules.html
    - https://docs.aws.amazon.com/lambda/latest/dg/lambda-invocation.html

    ## Output Format

    The output is formatted as markdown text with:
    - Preserved headings and structure
    - Code blocks for examples
    - Lists and tables converted to markdown format

    ## Large Tables

    Tables with more than 20 rows are automatically truncated to show only the header and 5 sample rows,
    along with a hint to use the `search_table` tool. Use `search_table` to filter and retrieve specific
    rows from large tables (e.g., service quotas, IAM actions).

    ## Handling Long Documents

    If the response indicates the document was truncated, you have several options:

    1. **Continue Reading**: Make another call with start_index set to the end of the previous response
    2. **Stop Early**: For very long documents (>30,000 characters), if you've already found the specific information needed, you can stop reading

    Args:
        ctx: MCP context for logging and error handling
        url: URL of the AWS documentation page to read
        max_length: Maximum number of characters to return
        start_index: On return output starting at this character index

    Returns:
        Markdown content of the AWS documentation
    """
    # Validate that URL is from docs.aws.amazon.com and ends with .html
    url_str = str(url)

    supported_domains_regex = [r'^https?://docs\.aws\.amazon\.com/']
    for modifier in SEARCH_TERM_DOMAIN_MODIFIERS:
        supported_domains_regex.append(modifier['regex'])

    if not any(re.match(domain_regex, url_str) for domain_regex in supported_domains_regex):
        await ctx.error(f'Invalid URL: {url_str}. URL must be from list of supported domains')
        raise ValueError('URL must be from list of supported domains')
    if not url_str.endswith('.html'):
        await ctx.error(f'Invalid URL: {url_str}. URL must end with .html')
        raise ValueError('URL must end with .html')

    return await read_documentation_impl(ctx, url_str, max_length, start_index, SESSION_UUID)


@mcp.tool()
async def read_sections(
    ctx: Context,
    url: str = Field(description='URL of the AWS documentation page to read'),
    section_titles: List[str] = Field(
        description='List of section titles to extract from the documentation'
    ),
) -> str:
    """Extract specific sections from AWS documentation pages by title.

    Retrieves a page, converts to markdown, and returns only matching sections.
    Section matching is case-insensitive and handles whitespace differences.

    ## URL Requirements
    - Must end with .html

    ## Large Tables

    Tables with more than 20 rows are automatically truncated to show only the header and 5 sample rows,
    along with a hint to use the `search_table` tool. Use `search_table` to filter and retrieve specific
    rows from large tables (e.g., service quotas, IAM actions).

    ## Read Sections Tips

    - Use exact section titles from search results 'sections' field when available
    - Section matching is case-insensitive and handles whitespace differences
    - Include multiple related sections in one call for comprehensive coverage

    ## Example Usage

    ```
    # If query is about S3 bucket naming rules:
    # Available sections: ['General purpose buckets naming rules', 'Example general purpose bucket names', 'Best practices', 'Creating a bucket that uses a GUID in the bucket name']
    # Read these specific sections:
    read_sections(
        url='https://docs.aws.amazon.com/s3/latest/userguide/bucketnamingrules.html',
        section_titles=['General purpose buckets naming rules', 'Best practices'],
    )

    # If query is about Python Lambda function examples:
    # Available sections: ['Example Python Lambda function code', 'Handler naming conventions', 'Using the Lambda event object', 'Accessing and using the Lambda context object'. 'Valid handler signatures for Python handlers', 'Returning a value', 'Using the AWS SDK for Python (Boto3) in your handler', 'Accessing environment variables, 'Code best practices for Python Lambda functions']
    # Read these specific sections:
    read_sections(
        url='https://docs.aws.amazon.com/lambda/latest/dg/python-handler.html',
        section_titles=[
            'Example Python Lambda function code',
            'Code best practices for Python Lambda functions',
        ],
    )
    ```

    Args:
        ctx: MCP context for logging and error handling
        url: URL of the AWS documentation page to read
        section_titles: List of section titles to extract

    Returns:
        Filtered markdown content containing only the requested sections
    """
    # Validate that URL is from docs.aws.amazon.com and ends with .html
    url_str = str(url)

    supported_domains_regex = [r'^https?://docs\.aws\.amazon\.com/']
    for modifier in SEARCH_TERM_DOMAIN_MODIFIERS:
        supported_domains_regex.append(modifier['regex'])

    if not any(re.match(domain_regex, url_str) for domain_regex in supported_domains_regex):
        await ctx.error(f'Invalid URL: {url_str}. URL must be from list of supported domains')
        raise ValueError('URL must be from list of supported domains')
    if not url_str.endswith('.html'):
        await ctx.error(f'Invalid URL: {url_str}. URL must end with .html')
        raise ValueError('URL must end with .html')

    if not section_titles:
        await ctx.error('section_titles parameter cannot be empty')
        raise ValueError('section_titles parameter cannot be empty')

    return await read_sections_impl(ctx, url_str, section_titles, SESSION_UUID)


@mcp.tool()
async def search_table(
    ctx: Context,
    url: str = Field(description='URL of the AWS documentation page containing the table'),
    section_title: Optional[str] = Field(
        default=None,
        description='The section heading that contains the table. If omitted, searches all tables on the page. Use exact titles from search_documentation results or read_documentation output.',
    ),
    query: str = Field(
        description='Search term to filter rows (case-insensitive, all words must match across any column)'
    ),
    max_rows: int = Field(
        default=20,
        description='Maximum number of matching rows to return per table',
        ge=1,
        le=100,
    ),
) -> SearchTableResponse:
    """Search for specific rows in a large documentation table.

    Use this tool when you need specific rows from a large documentation table
    (e.g., service quotas, pricing tables, supported models lists). Returns
    matching rows as JSON instead of dumping the entire table.

    This is more efficient than read_sections for pages with hundreds of table rows.
    If read_sections or read_documentation indicates a table was truncated, use this
    tool to find the specific rows you need.

    ## Section Title

    - If you know the exact section title, provide it for precision
    - If unsure, omit section_title — the tool will search all tables on the page
    - Use section titles from search_documentation results (TOC) or read_documentation output
    - If the section title is wrong, the hint field will list available sections

    ## URL Requirements

    - Must be from the docs.aws.amazon.com domain
    - Must end with .html

    ## Example Usage

    ```
    # With section title (searches all tables in that section):
    search_table(
        url='https://docs.aws.amazon.com/general/latest/gr/bedrock.html',
        section_title='Amazon Bedrock service quotas',
        query='Titan Text Embeddings V2',
    )

    # Without section title (searches all tables on the page):
    search_table(
        url='https://docs.aws.amazon.com/general/latest/gr/bedrock.html',
        query='Claude 3 Sonnet requests',
    )
    ```

    ## Response Format

    Returns a SearchTableResponse with:
    - tables_searched: Number of tables searched
    - tables_with_matches: Number of tables containing matching rows
    - hint: Guidance message when no matches found, section not found, or no tables on page
    - results: Array of table result objects, each with:
        - table_heading: The sub-heading above the table (if any)
        - columns: Column headers for that table
        - parent_columns: (optional) For rowspan/nested tables, which columns are group headers
        - child_columns: (optional) For rowspan/nested tables, which columns are nested under groups
        - total_rows: Total rows (or groups, for nested tables) in that table
        - matched_rows: Number of rows/groups matching the query per table
        - showing: Number of rows/groups returned per table (capped by max_rows per table)
        - rows: Array of matching row objects. For flat tables: {column → value}.
          For nested tables: {parent_col → value, ..., "rows": [{child_col → value}, ...]}

    Args:
        ctx: MCP context for logging and error handling
        url: AWS documentation page URL (must end with .html)
        section_title: The section heading containing the table (optional — omit to search all tables on the page)
        query: Search term to filter rows (all words must match, case-insensitive)
        max_rows: Maximum matching rows to return per table (default 20)

    Returns:
        SearchTableResponse with matching rows grouped by table
    """
    url_str = str(url)

    supported_domains_regex = [r'^https?://docs\.aws\.amazon\.com/']
    for modifier in SEARCH_TERM_DOMAIN_MODIFIERS:
        supported_domains_regex.append(modifier['regex'])

    if not any(re.match(domain_regex, url_str) for domain_regex in supported_domains_regex):
        await ctx.error(f'Invalid URL: {url_str}. URL must be from list of supported domains')
        raise ValueError('URL must be from list of supported domains')
    if not url_str.endswith('.html'):
        await ctx.error(f'Invalid URL: {url_str}. URL must end with .html')
        raise ValueError('URL must end with .html')

    if not query or not query.strip():
        await ctx.error('query parameter cannot be empty')
        raise ValueError('query parameter cannot be empty')

    return await search_table_impl(ctx, url_str, section_title, query, max_rows, SESSION_UUID)


@mcp.tool()
async def search_documentation(
    ctx: Context,
    search_phrase: str = Field(description='Search phrase to use'),
    search_intent: str = Field(
        description='For the search_phrase parameter, describe the search intent of the user. CRITICAL: Do not include any PII or customer data, describe only the AWS-related intent for search.',
        default='',
    ),
    limit: int = Field(
        default=10,
        description='Maximum number of results to return',
        ge=1,
        le=50,
    ),
    product_types: Optional[List[str]] = Field(
        default=None,
        description='Filter results by AWS product/service (e.g., ["Amazon Simple Storage Service"])',
    ),
    guide_types: Optional[List[str]] = Field(
        default=None,
        description='Filter results by guide type (e.g., ["User Guide", "API Reference", "Developer Guide"])',
    ),
) -> SearchResponse:
    """Search AWS documentation using the official AWS Documentation Search API.

    ## Usage

    This tool searches across all AWS documentation for pages matching your search phrase.
    Use it to find relevant documentation when you don't have a specific URL.

    ## Search Tips

    - Use specific technical terms rather than general phrases
    - Include service names to narrow results (e.g., "S3 bucket versioning" instead of just "versioning")
    - Use quotes for exact phrase matching (e.g., "AWS Lambda function URLs")
    - Include abbreviations and alternative terms to improve results
    - Use guide_type and product_type filters found from a SearchResponse's "facets" property:
        - Filter only for broad search queries with patterns:
            - "What is [service]?" -> product_types: ["Amazon Simple Storage Service"]
            - "How to use <service 1> with <service 2>?" -> product_types: [<service 1>, <service 2>]
            - "[service] getting started" -> product_types: [<service>] + guide_types: ["User Guide, "Developer Guide"]
            - "API reference for [service]" -> product_types: [<service>] + guide_types: ["API Reference"]

    ## Result Interpretation

    Each SearchResponse includes:
    - search_results: List of documentation pages, each with:
        - rank_order: The relevance ranking (lower is more relevant)
        - url: The documentation page URL
        - title: The page title
        - context: A brief excerpt or summary (if available)
        - recommended_sections: A subset of section titles ranked as most relevant to the query, in rank order (when available) - Pass these directly to read_sections for targeted content extraction
        - sections: All available section titles for this page (when available) - individual titles can be used with the read_sections tool for targeted content extraction
        - metadata: Optional per-result context (when available). May include:
            - additional_urls: Other doc URLs related to the same result `{url, section_title, section_anchor}` - pass `section_title` to read_sections to fetch content; use `url#section_anchor` when citing
    - facets: Available filters (product_types, guide_types) for refining searches
    - query_id: Unique identifier for this search session
    - metadata: Optional response-level context (when available). May include:
        - discovered_services: AWS services inferred from the query that may be relevant beyond the top results
        - related_tasks: Related operations or workflows the user may want to follow up on, each with its own doc URLs
        - relationships: Named connections between information in the search results, useful for understanding how the returned topics relate to each other


    Args:
        ctx: MCP context for logging and error handling
        search_phrase: Search phrase to use
        search_intent: The intent behind the search requested by the user
        limit: Maximum number of results to return
        product_types: Filter by AWS product/service
        guide_types: Filter by guide type

    Returns:
        List of search results with URLs, titles, query ID, context snippets, and facets for filtering
    """
    logger.debug(f'Searching AWS documentation for: {search_phrase}')

    request_body = {
        'textQuery': {
            'input': search_phrase,
        },
        'contextAttributes': [{'key': 'domain', 'value': 'docs.aws.amazon.com'}],
        'acceptSuggestionBody': 'RawText',
        'locales': ['en_us'],
    }
    for modifier in SEARCH_TERM_DOMAIN_MODIFIERS:
        if any(term in search_phrase.lower() for term in modifier['terms']):
            request_body['contextAttributes'].extend(modifier['domains'])

    # Add product and guide filters if provided
    if product_types:
        for product in product_types:
            request_body['contextAttributes'].append(
                {'key': 'aws-docs-search-product', 'value': product}
            )
    if guide_types:
        for guide in guide_types:
            request_body['contextAttributes'].append(
                {'key': 'aws-docs-search-guide', 'value': guide}
            )

    search_url_with_session = f'{SEARCH_API_URL}?session={SESSION_UUID}'
    search_url_with_session = add_search_intent_to_search_request(
        search_url_with_session, search_intent
    )

    async with httpx.AsyncClient() as client:
        try:
            response = await client.post(
                search_url_with_session,
                json=request_body,
                headers={
                    'Content-Type': 'application/json',
                    'User-Agent': DEFAULT_USER_AGENT,
                    'X-MCP-Session-Id': SESSION_UUID,
                },
                timeout=30,
            )
        except httpx.HTTPError as e:
            error_msg = f'Error searching AWS docs: {str(e)}'
            logger.error(error_msg)
            await ctx.error(error_msg)
            return SearchResponse(
                search_results=[SearchResult(rank_order=1, url='', title=error_msg, context=None)],
                facets=None,
                query_id='',
            )

        if response.status_code >= 400:
            error_msg = f'Error searching AWS docs - status code {response.status_code}'
            logger.error(error_msg)
            await ctx.error(error_msg)
            return SearchResponse(
                search_results=[
                    SearchResult(
                        rank_order=1, url='', title=error_msg, context=None, sections=None
                    )
                ],
                facets=None,
                query_id='',
            )

        try:
            data = response.json()
            query_id = data.get('queryId', '')
            raw_facets = data.get('facets', {})

            # Parse facets to rename keys
            facets = {}
            if raw_facets:
                for key, value in raw_facets.items():
                    if key == 'aws-docs-search-product':
                        facets['product_types'] = value
                    elif key == 'aws-docs-search-guide':
                        facets['guide_types'] = value
            raw_metadata = data.get('metadata') or {}
            filtered_metadata = {
                k: raw_metadata[k] for k in SUPPORTED_METADATA_KEYS if raw_metadata.get(k)
            }
            response_metadata = _safe_model_validate(ResponseMetadata, filtered_metadata)

        except json.JSONDecodeError as e:
            error_msg = f'Error parsing search results: {str(e)}'
            logger.error(error_msg)
            await ctx.error(error_msg)
            return SearchResponse(
                search_results=[
                    SearchResult(
                        rank_order=1, url='', title=error_msg, context=None, sections=None
                    )
                ],
                facets=None,
                query_id='',
            )

    results = []
    if 'suggestions' in data:
        for i, suggestion in enumerate(data['suggestions'][:limit]):
            if 'textExcerptSuggestion' in suggestion:
                text_suggestion = suggestion['textExcerptSuggestion']
                context = None

                # Use the authored summary if available, falling back to content body.
                metadata = text_suggestion.get('metadata', {})
                if 'summary' in text_suggestion:
                    context = text_suggestion['summary']
                elif 'suggestionBody' in text_suggestion:
                    context = text_suggestion['suggestionBody']

                sections = []
                title = text_suggestion.get('title', '')
                url = text_suggestion.get('link', '')

                # Log metadata for debugging
                logger.debug(f'Processing result {i + 1}: {title} - {url}')
                logger.debug(f'Available metadata keys: {list(metadata.keys())}')

                if 'sections' in metadata:
                    try:
                        sections_data = metadata.pop('sections')
                        logger.debug(f'Found sections: {sections_data}')
                        logger.debug(f'Raw sections data type: {type(sections_data)}')

                        if isinstance(sections_data, list):
                            logger.debug(f'Processing {len(sections_data)} sections')
                            for idx, section_data in enumerate(sections_data):
                                logger.debug(
                                    f'Section {idx}: {section_data} (type: {type(section_data)})'
                                )

                                if isinstance(section_data, str) and section_data != '':
                                    sections.append(section_data)
                                    logger.debug(f'Added section: {section_data}')
                    except (TypeError, KeyError) as e:
                        logger.error(f'Error processing sections for {title}: {url}, {e}')
                else:
                    logger.debug(f'No sections found in metadata for {title}: {url}')

                if sections:
                    logger.info(
                        f'Found {len(sections)} sections for {title}: {url}, sections: {sections}'
                    )

                # Surface relevant sections distinctly from full table of contents.
                recommended_data = metadata.pop('recommended_sections', None)
                recommended_sections = (
                    [s for s in recommended_data if isinstance(s, str) and s != '']
                    if isinstance(recommended_data, list)
                    else []
                )

                if recommended_sections:
                    logger.info(
                        f'Found {len(recommended_sections)} recommended sections for {title}: {url}'
                    )

                filtered_result_metadata = {
                    k: metadata[k] for k in SUPPORTED_RESULT_METADATA_KEYS if metadata.get(k)
                }
                search_result_metadata = _safe_model_validate(
                    SearchResultMetadata, filtered_result_metadata
                )
                search_result = SearchResult(
                    rank_order=i + 1,
                    url=text_suggestion.get('link', ''),
                    title=text_suggestion.get('title', ''),
                    context=context,
                    sections=sections if sections else None,
                    recommended_sections=recommended_sections if recommended_sections else None,
                    metadata=search_result_metadata,
                )

                results.append(search_result)

    logger.debug(f'Found {len(results)} search results for: {search_phrase}')
    logger.debug(f'Search query ID: {query_id}')
    final_search_response = SearchResponse(
        search_results=results,
        facets=facets if facets else None,
        query_id=query_id,
        metadata=response_metadata,
    )
    add_search_result_cache_item(final_search_response)
    return final_search_response


@mcp.tool()
async def recommend(
    ctx: Context,
    url: str = Field(description='URL of the AWS documentation page to get recommendations for'),
) -> List[RecommendationResult]:
    """Get content recommendations for an AWS documentation page.

    ## Usage

    This tool provides recommendations for related AWS documentation pages based on a given URL.
    Use it to discover additional relevant content that might not appear in search results.

    ## Recommendation Types

    The recommendations include three categories:

    1. **New**: Recently added pages within the same AWS service - useful for finding newly released features
    2. **Similar**: Pages covering similar topics to the current page
    3. **Journey**: Pages commonly viewed next by other users

    ## When to Use

    - After reading a documentation page to find related content
    - When exploring a new AWS service to discover important pages
    - To find alternative explanations of complex concepts
    - To find newly released information by using a service's welcome page URL and checking the **New** recommendations

    ## Finding New Features

    To find newly released information about a service:
    1. Find any page belong to that service, typically you can try the welcome page
    2. Call this tool with that URL
    3. Look specifically at the **New** recommendation type in the results

    ## Result Interpretation

    Each recommendation includes:
    - url: The documentation page URL
    - title: The page title
    - context: A brief description (if available)

    Args:
        ctx: MCP context for logging and error handling
        url: URL of the AWS documentation page to get recommendations for

    Returns:
        List of recommended pages with URLs, titles, and context
    """
    url_str = str(url)
    logger.debug(f'Getting recommendations for: {url_str}')

    recommendation_url = f'{RECOMMENDATIONS_API_URL}?path={url_str}&session={SESSION_UUID}'

    async with httpx.AsyncClient() as client:
        try:
            response = await client.get(
                recommendation_url,
                headers={'User-Agent': DEFAULT_USER_AGENT},
                timeout=30,
            )
        except httpx.HTTPError as e:
            error_msg = f'Error getting recommendations: {str(e)}'
            logger.error(error_msg)
            await ctx.error(error_msg)
            return [RecommendationResult(url='', title=error_msg, context=None)]

        if response.status_code >= 400:
            error_msg = f'Error getting recommendations - status code {response.status_code}'
            logger.error(error_msg)
            await ctx.error(error_msg)
            return [
                RecommendationResult(
                    url='',
                    title=error_msg,
                    context=None,
                )
            ]

        try:
            data = response.json()
        except json.JSONDecodeError as e:
            error_msg = f'Error parsing recommendations: {str(e)}'
            logger.error(error_msg)
            await ctx.error(error_msg)
            return [RecommendationResult(url='', title=error_msg, context=None)]

    results = parse_recommendation_results(data)
    logger.debug(f'Found {len(results)} recommendations for: {url_str}')
    return results


async def resolve_aws_entity(
    ctx: Context,
    name: str = Field(
        description='An AWS service, operation, resource type, or resource property name, as written in prose. Examples: "Amazon Aurora", "Elastic Load Balancing", "CreateFunction", "AWS::SQS::Queue", "security group"'
    ),
    entity_type: Optional[str] = Field(
        default=None,
        description='Restrict to one kind: service, operation, resource_type, or resource_property. Omit to search all four.',
    ),
) -> Dict[str, Any]:
    """Turn an AWS name written in prose into the canonical id that query_aws_facts needs.

    ## When to use

    Call this FIRST whenever the exact code is not already known. Service codes rarely match the
    marketing name: Cognito user pools are `cognito-idp`, Elastic Load Balancing is `elb`, Kinesis Data
    Firehose is now `firehose`. Guessing produces an abstain and wastes a round trip.

    Skip it when the canonical code is already known from a previous call or from the user's own input.

    ## Cost

    Local and deterministic, about 8ms server-side, and cached for the rest of this session. No embedding
    or graph work. Calling it is cheaper than one failed query_aws_facts attempt.

    ## Reading the result

    Each candidate carries `canonical_id`, `type`, `name`, `score`, and `resolved_via`. Services also
    carry `display_name`, `facts`, and sometimes `renamed_from`.

    - **One candidate at 0.9 or above**: use it.
    - **Several candidates**: the name is genuinely ambiguous and the store will not guess. `amazon
      cognito` returns three Cognito services, and `CreateFunction` returns one candidate per service
      that defines it. Pick using the user's context, or ask.
    - **`facts`** lists the fact types that actually have data for that id. Check it before calling
      query_aws_facts. `svc:aurora` returns `["availability"]`, so a quota question against it will
      abstain, and searching the documentation is the faster path.
    - **`renamed_from`** names a different id that holds the data, when the resolved id is a newer name
      for the same service. `Amazon EventBridge` resolves to `svc:eventbridge`, whose quotas live under
      `events`. Use `renamed_from` as the service slot for anything missing from `facts`.
    - **Zero candidates**: expected for three inputs, and retrying will not help. Features such as
      "intelligent tiering" are not modelled, third-party products such as "Palo Alto Networks" are not
      AWS entities, and things that are not services ("aws cli", "amazon linux 2023") have no code. Use
      search_documentation for those.

    ## Example

    `resolve_aws_entity("Amazon Aurora")` returns `svc:aurora`, display name "Amazon Aurora", via alias.
    The service slot for query_aws_facts is then `aurora`.

    Args:
        ctx: MCP context for logging and error handling
        name: The name to resolve
        entity_type: Optional restriction to one entity kind

    Returns:
        Candidates with their canonical ids, or an empty list with a note explaining why
    """
    if not name or not name.strip():
        return {'resolve': name, 'count': 0, 'candidates': [], 'note': 'empty input'}
    logger.debug(f'Resolving AWS entity: {name}')
    result = await fact_store.resolve(name.strip(), entity_type=entity_type)
    if 'candidates' not in result:
        await ctx.error(f'Fact store unavailable while resolving "{name}"')
    return result


async def query_aws_facts(
    ctx: Context,
    q: Optional[str] = Field(
        default=None,
        description='A natural-language question. Use only when the slots below are unknown, because this path runs a keyword classifier and can reach the graph.',
    ),
    fact_type: Optional[str] = Field(
        default=None,
        description='One of: endpoint, availability, quota, op_attribute, list_operations, pricing, find_related, prerequisites.',
    ),
    service: Optional[str] = Field(
        default=None,
        description='Canonical service code, for example sts, lambda, cognito-idp. Get it from resolve_aws_entity when unsure.',
    ),
    region: Optional[str] = Field(default=None, description='Region code, for example eu-west-1.'),
    operation: Optional[str] = Field(
        default=None, description='API operation name, for example PutObject.'
    ),
    resource_property: Optional[str] = Field(
        default=None,
        description='For fact_type=find_related: a resource property such as vpc or security group.',
    ),
    depth: Optional[int] = Field(
        default=None,
        description='For fact_type=prerequisites: how many levels of the dependency chain to walk, 1 to 4.',
    ),
    from_handle: Optional[str] = Field(
        default=None,
        description="A canonical_id from a previous response's entities list, to continue a walk cheaply.",
    ),
    traverse: Optional[str] = Field(
        default=None,
        description='A named traversal: quotas_for_operation or operations_on_resource.',
    ),
    relation: Optional[str] = Field(
        default=None,
        description='Relationship intent, used with from_handle to walk the graph. Either an edge type '
        '(LIMITS, ACTS_ON, REQUIRES, appliesIn, dependsOn, hasChild, relatesTo, references) '
        'or plain words for one ("what limits", "permissions", "acts on", "children", '
        '"related"). A wrong value returns the full list, so guessing is cheap.',
    ),
    direction: Optional[str] = Field(
        default=None,
        description='For relation: out (default), in, or both. Many questions are the inward form: '
        'quotas LIMITS an operation, so what limits an operation is direction=in.',
    ),
    resource_type: Optional[str] = Field(
        default=None,
        description='For traverse=operations_on_resource, for example AWS::SQS::Queue.',
    ),
) -> Dict[str, Any]:
    """Look up an enumerable AWS fact from authoritative sources, or get an honest abstain.

    ## What this answers well

    Facts that are arbitrary, so no model can infer them, and consequential, so a wrong answer costs
    something: regional endpoints, which regions a service is in, default quota values and whether they
    are adjustable, an operation's required parameters and IAM actions, headline pricing, and which
    resources an operation needs to exist first.

    Prefer this over recalling a quota or a region list from memory. Those values are arbitrary and
    change, and a fabricated one is costly.

    ## Choosing the cheapest call

    Give the slots whenever they are known. `fact_type` plus `service` plus `region` costs about 4ms and
    skips the classifier. A bare `q` costs about 18ms, and up to 13 graph queries and ~630ms when it
    falls through to document retrieval. The slots are not an optimization, they are the accurate path.

    To walk several steps, pass `from_handle` with a `canonical_id` the previous response returned. That
    costs 2 graph queries and about 20ms, against re-asking in natural language.

    ## Walking a specific relationship

    `from_handle` plus `relation` walks one edge type, which is **one** graph query and about 20ms,
    against 13 queries and ~630ms for a natural-language question. State the relationship you want
    rather than letting retrieval infer it.

    The graph carries eight edge types, so `relation` takes either the name or plain words for it:

    - `LIMITS`: a quota limits an operation. Asking what limits an operation is `direction="in"`.
    - `ACTS_ON`: an operation acts on a resource type.
    - `REQUIRES`: an IAM action authorizes an operation.
    - `appliesIn`: a regional override applies in a region.
    - `dependsOn`: a task depends on a task. Recursive, so `depth` up to 3 is useful here.
    - `hasChild`, `relatesTo`, `references`: document and concept structure.

    An unknown `relation` returns the full list rather than an error, so a guess costs one cheap call.

    ## When the slots are the wrong guess

    A structured request can only fail inside the slot you chose. A bare `q` can cross tiers: asked for a
    Step Functions duration quota the structured call abstains, because no such quota exists, while `q`
    reaches documentation retrieval instead. So use the slots first, and fall back to `q` when a
    `no_data` abstain suggests the fact type was wrong rather than the value missing.

    ## Reading `answer.status`, which decides the next move

    - **`ok`** (or `status` absent): a slot resolved the fact. Stop. Do not search for confirmation.
    - **`candidates`**: typed rows that share words with the question, which is weaker than a slot
      lookup, so they may not answer it. Check `match_basis`: `operation` matched the named operation
      but returned several rows, `query_terms` matched words in the question, and `all_service_quotas`
      means nothing matched so the whole set is listed. Read
      `quota_name` on each row and confirm it is the quota asked about. If none fits, say so and search
      the documentation. Do NOT report a candidate as the answer without that check. A question about a
      feature lands here, because features are not modelled.
    - **`abstain`**: nothing to return. Read `reason_code`:
      - `missing_slot`: fill the named slot and call again. Do NOT rephrase the question.
      - `ambiguous`: name the service to disambiguate the operation.
      - `no_data`, `unsupported_type`, `low_confidence`, `store_unavailable`: stop calling this tool and
        use search_documentation instead.

    ## Scope, so retries stop early

    The store does not answer: pricing beyond a curated headline slice, troubleshooting and how-to as
    typed facts, comparisons between two services in one request, feature-level granularity (a feature
    question resolves to the parent service, which is the wrong granularity), and operation-level or
    feature-level region availability. Availability is service-level only.

    ## Examples

    - Endpoint: `fact_type="endpoint", service="sts", region="eu-west-1"`
    - Region list: `fact_type="availability", service="aurora"`
    - Quota: `fact_type="quota", service="lambda", region="eu-west-1"`
    - Parameters: `fact_type="op_attribute", service="s3", operation="PutObject"`
    - Provisioning order: `fact_type="prerequisites", service="lambda", operation="CreateFunction", depth=2`
    - Related resources: `fact_type="find_related", resource_property="security group"`

    Args:
        ctx: MCP context for logging and error handling
        q: Natural-language question, when the slots are unknown
        fact_type: The fact type to look up
        service: Canonical service code
        region: Region code
        operation: API operation name
        resource_property: Resource property, for find_related
        depth: Dependency chain depth for prerequisites, or walk depth for relation
        from_handle: A canonical_id from a previous response, to continue a walk
        traverse: A named traversal
        relation: Relationship intent to walk from from_handle
        direction: Walk direction for relation: out, in, or both
        resource_type: Resource type, for operations_on_resource

    Returns:
        An envelope with `answer` (carrying `status`), plus `entities`, `evidence`, `route` and `timing`
    """
    payload: Dict[str, Any] = {}
    if q:
        payload['q'] = q
    for key, value in (
        ('fact_type', fact_type),
        ('service', service),
        ('region', region),
        ('operation', operation),
        ('property', resource_property),
        ('depth', depth),
        ('from', from_handle),
        ('traverse', traverse),
        ('relation', relation),
        ('direction', direction),
        ('resource_type', resource_type),
    ):
        if value is not None and value != '':
            payload[key] = value

    if not payload:
        return {
            'answer': {
                'status': 'abstain',
                'reason_code': 'missing_slot',
                'detail': 'give either q, or fact_type with its slots, or from_handle, or traverse',
            }
        }

    logger.debug(f'Querying fact store: {payload}')
    result = await fact_store.call(payload)
    answer = result.get('answer') or {}
    if answer.get('reason_code') == 'store_unavailable':
        await ctx.error(f'Fact store unavailable: {answer.get("detail")}')
    return result


# Registered conditionally: the fact store endpoint is IAM-authorized and not public, so a user without
# access can set AWS_FACT_STORE_ENABLED=false and keep these out of the agent's tool list entirely.
if fact_store.is_configured():
    mcp.tool()(resolve_aws_entity)
    mcp.tool()(query_aws_facts)
    logger.info(f'Fact store tools registered against {fact_store.endpoint()}')
else:
    logger.info('Fact store tools disabled (AWS_FACT_STORE_ENABLED=false)')


def main():
    """Run the MCP server with CLI argument support."""
    logger.info('Starting AWS Documentation MCP Server')
    mcp.run()


if __name__ == '__main__':
    main()
