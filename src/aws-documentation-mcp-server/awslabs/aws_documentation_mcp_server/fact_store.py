# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License").
# You may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Client for the AWS Fact Store query API.

The fact store answers enumerable AWS facts (endpoints, region availability, quotas, operation
parameters, prerequisites) from authoritative sources, and abstains instead of guessing. It is reached
over an IAM-authorized HTTP API, so every request is SigV4-signed with the caller's own credentials.

The endpoint is not public. A caller without credentials for it gets a structured unavailable result
naming the documentation tools as the fallback, because a missing internal endpoint must not break a
server whose other tools work.
"""

import json
import os
from loguru import logger
from typing import Any, Dict, Optional


DEFAULT_ENDPOINT = 'https://sn0y6e78y4.execute-api.us-west-2.amazonaws.com/query'
DEFAULT_REGION = 'us-west-2'
SIGNING_SERVICE = 'execute-api'
TIMEOUT_SECONDS = 30.0

# Resolution is deterministic and depends only on the store's vocabulary, so a repeat lookup inside one
# session cannot change answer. Bounded because an agent can resolve many distinct names in one session.
_RESOLVE_CACHE: Dict[str, Dict[str, Any]] = {}
_RESOLVE_CACHE_MAX = 256

_session = None


def endpoint() -> str:
    """The query endpoint, overridable so a teammate can point at their own deployment."""
    return os.getenv('AWS_FACT_STORE_ENDPOINT', DEFAULT_ENDPOINT).strip()


def region() -> str:
    """Signing region. Must match the region the API is deployed in, not the caller's default."""
    return os.getenv('AWS_FACT_STORE_REGION', DEFAULT_REGION).strip()


def is_configured() -> bool:
    """Whether the fact store tools should be registered at all."""
    return os.getenv('AWS_FACT_STORE_ENABLED', 'true').strip().lower() not in ('false', '0', 'no')


def _unavailable(reason: str) -> Dict[str, Any]:
    """A failure shaped like an abstain, so an agent treats it as a routing signal.

    The store's own abstain contract already teaches an agent to branch on `status`, so an unreachable
    store reuses it rather than introducing a second error shape.
    """
    return {
        'answer': {
            'status': 'abstain',
            'reason_code': 'store_unavailable',
            'detail': reason,
        },
        'fallback': (
            'The fact store did not answer. Use search_documentation and read_documentation for this '
            'question instead. Do not retry this tool.'
        ),
    }


def role_arn() -> str:
    """Role to assume before signing. Empty means sign with the ambient credentials.

    An API Gateway HTTP API cannot carry a resource policy, so a caller outside the store's own account
    cannot be granted execute-api:Invoke directly and has to assume a role there instead. Not defaulted,
    because a role ARN names an account.
    """
    return os.getenv('AWS_FACT_STORE_ROLE_ARN', '').strip()


def _get_session():
    """Reuse one boto3 Session. Credentials are re-read per call so a refresh is picked up.

    With AWS_FACT_STORE_ROLE_ARN set, the session carries refreshable assume-role credentials, so a
    long-lived server keeps working past the one-hour session limit without the caller noticing.
    """
    global _session
    if _session is None:
        import boto3

        profile = os.getenv('AWS_FACT_STORE_PROFILE') or os.getenv('AWS_PROFILE')
        base = boto3.Session(profile_name=profile) if profile else boto3.Session()
        arn = role_arn()
        if not arn:
            _session = base
            return _session
        from botocore.credentials import AssumeRoleCredentialFetcher, DeferredRefreshableCredentials

        def _regional(*args, **kwargs):
            # The fetcher builds its own STS client, which otherwise resolves to the legacy global
            # endpoint. Where an account disables that endpoint, AssumeRole fails with AccessDenied.
            kwargs.setdefault('region_name', region())
            return base.client(*args, **kwargs)

        fetcher = AssumeRoleCredentialFetcher(
            client_creator=_regional,
            source_credentials=base.get_credentials(),
            role_arn=arn,
            extra_args={'RoleSessionName': 'aws-docs-facts'},
        )
        botocore_session = base._session
        botocore_session._credentials = DeferredRefreshableCredentials(
            fetcher.fetch_credentials, 'assume-role'
        )
        _session = boto3.Session(botocore_session=botocore_session)
    return _session


def _prepare_evidence(envelope: Dict[str, Any]) -> Dict[str, Any]:
    """Split the fragment off each evidence URL so read_documentation accepts it.

    The store cites a specific section, so its URLs end in `...troubleshooting.html#some-anchor`. But
    read_documentation rejects any URL that does not end in `.html`, which is every cited URL. The
    fragment is still worth keeping, because it names the section the match came from, so it moves to its
    own field rather than being discarded.
    """
    for item in envelope.get('evidence') or []:
        url = item.get('url')
        if isinstance(url, str) and '#' in url:
            base, _, fragment = url.partition('#')
            item['url'] = base
            item['section_anchor'] = fragment
    return envelope


async def call(payload: Dict[str, Any]) -> Dict[str, Any]:
    """POST a signed request to the fact store and return its envelope.

    Never raises. Every failure path returns an abstain-shaped result, because an agent mid-task needs a
    next action rather than an exception.
    """
    try:
        import httpx
        from botocore.auth import SigV4Auth
        from botocore.awsrequest import AWSRequest
    except (
        ImportError
    ) as e:  # pragma: no cover - dependency is declared, so this is a broken install
        return _unavailable(f'missing dependency: {e}')

    try:
        credentials = _get_session().get_credentials()
        if credentials is None:
            return _unavailable(
                'no AWS credentials found; set AWS_FACT_STORE_PROFILE to a profile that can reach the '
                'fact store, and AWS_FACT_STORE_ROLE_ARN when the store is in another account'
            )
        body = json.dumps(payload)
        request = AWSRequest(
            method='POST',
            url=endpoint(),
            data=body,
            headers={'Content-Type': 'application/json'},
        )
        SigV4Auth(credentials.get_frozen_credentials(), SIGNING_SERVICE, region()).add_auth(
            request
        )

        async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS) as client:
            response = await client.post(endpoint(), content=body, headers=dict(request.headers))

        if response.status_code == 403:
            return _unavailable(
                'access denied (403); the credentials in use lack execute-api:Invoke on the fact '
                'store API'
            )
        if response.status_code != 200:
            return _unavailable(f'HTTP {response.status_code}: {response.text[:200]}')
        return _prepare_evidence(response.json())
    except Exception as e:
        logger.warning(f'Fact store call failed: {e}')
        return _unavailable(f'{type(e).__name__}: {str(e)[:200]}')


async def resolve(name: str, entity_type: Optional[str] = None, limit: int = 5) -> Dict[str, Any]:
    """Resolve a free-text AWS name to canonical ids, with an in-session cache."""
    key = f'{name}|{entity_type or ""}|{limit}'
    cached = _RESOLVE_CACHE.get(key)
    if cached is not None:
        return cached

    payload: Dict[str, Any] = {'resolve': name, 'limit': limit}
    if entity_type:
        payload['type'] = entity_type
    result = await call(payload)

    # Only a real answer is cached. An unavailable store may become available within the session.
    if 'candidates' in result:
        if len(_RESOLVE_CACHE) >= _RESOLVE_CACHE_MAX:
            _RESOLVE_CACHE.clear()
        _RESOLVE_CACHE[key] = result
    return result
