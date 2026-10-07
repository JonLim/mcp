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
"""Tests for the fact store client and its two tools.

These are not live tests. The fact store endpoint is IAM-authorized and not publicly reachable, so the
signed call is stubbed and the assertions cover the parts that can break independently of it: the
request payload built from the tool arguments, the evidence-URL rewrite, the failure shape, and the
resolve cache.
"""

import pytest
from awslabs.aws_documentation_mcp_server import fact_store
from unittest.mock import AsyncMock, patch


@pytest.fixture(autouse=True)
def clear_resolve_cache():
    """The cache is module-level, so one test must not see another test's entry."""
    fact_store._RESOLVE_CACHE.clear()
    yield
    fact_store._RESOLVE_CACHE.clear()


class TestEvidenceUrls:
    """read_documentation rejects any URL not ending in .html, and every cited URL has a fragment."""

    def test_fragment_moves_to_its_own_field(self):
        """Fragment moves to its own field."""
        envelope = {
            'evidence': [
                {
                    'url': 'https://docs.aws.amazon.com/lambda/latest/dg/troubleshooting.html#ts-throttling',
                    'title': 'Troubleshooting',
                }
            ]
        }
        result = fact_store._prepare_evidence(envelope)
        item = result['evidence'][0]
        assert item['url'] == 'https://docs.aws.amazon.com/lambda/latest/dg/troubleshooting.html'
        assert item['section_anchor'] == 'ts-throttling'
        assert item['title'] == 'Troubleshooting', 'other fields must survive'

    def test_url_without_a_fragment_is_untouched(self):
        """Url without a fragment is untouched."""
        url = 'https://docs.aws.amazon.com/lambda/latest/dg/welcome.html'
        result = fact_store._prepare_evidence({'evidence': [{'url': url}]})
        assert result['evidence'][0]['url'] == url
        assert 'section_anchor' not in result['evidence'][0]

    def test_every_rewritten_url_is_acceptable_to_read_documentation(self):
        """Every rewritten url is acceptable to read documentation."""
        envelope = {
            'evidence': [
                {'url': 'https://docs.aws.amazon.com/a/b/one.html#x'},
                {'url': 'https://docs.aws.amazon.com/a/b/two.html'},
                {'url': 'https://docs.aws.amazon.com/a/b/three.html#y-z-1'},
            ]
        }
        result = fact_store._prepare_evidence(envelope)
        assert all(item['url'].endswith('.html') for item in result['evidence'])

    def test_missing_or_malformed_evidence_is_safe(self):
        """Missing or malformed evidence is safe."""
        assert fact_store._prepare_evidence({}) == {}
        assert fact_store._prepare_evidence({'evidence': None}) == {'evidence': None}
        # a non-string url must not raise
        assert (
            fact_store._prepare_evidence({'evidence': [{'url': None}]})['evidence'][0]['url']
            is None
        )


class TestUnavailable:
    """An unreachable store must look like an abstain, not an exception."""

    def test_shape_matches_the_abstain_contract(self):
        """Shape matches the abstain contract."""
        result = fact_store._unavailable('no credentials')
        assert result['answer']['status'] == 'abstain'
        assert result['answer']['reason_code'] == 'store_unavailable'
        assert 'no credentials' in result['answer']['detail']
        assert 'search_documentation' in result['fallback']

    @pytest.mark.asyncio
    async def test_call_never_raises_when_signing_fails(self):
        """Call never raises when signing fails."""
        with patch.object(fact_store, '_get_session', side_effect=RuntimeError('boom')):
            result = await fact_store.call({'resolve': 'lambda'})
        assert result['answer']['reason_code'] == 'store_unavailable'
        assert 'boom' in result['answer']['detail']


class TestConfiguration:
    """Endpoint, region, and the kill switch are environment-driven."""

    def test_endpoint_and_region_are_overridable(self, monkeypatch):
        """Endpoint and region are overridable."""
        monkeypatch.setenv('AWS_FACT_STORE_ENDPOINT', 'https://example.test/query')
        monkeypatch.setenv('AWS_FACT_STORE_REGION', 'eu-west-1')
        assert fact_store.endpoint() == 'https://example.test/query'
        assert fact_store.region() == 'eu-west-1'

    def test_defaults_apply_when_unset(self, monkeypatch):
        """Defaults apply when unset."""
        monkeypatch.delenv('AWS_FACT_STORE_ENDPOINT', raising=False)
        monkeypatch.delenv('AWS_FACT_STORE_REGION', raising=False)
        assert fact_store.endpoint() == fact_store.DEFAULT_ENDPOINT
        assert fact_store.region() == fact_store.DEFAULT_REGION

    @pytest.mark.parametrize(
        'value,expected',
        [
            ('false', False),
            ('FALSE', False),
            ('0', False),
            ('no', False),
            ('true', True),
            ('yes', True),
        ],
    )
    def test_kill_switch(self, monkeypatch, value, expected):
        """Kill switch."""
        monkeypatch.setenv('AWS_FACT_STORE_ENABLED', value)
        assert fact_store.is_configured() is expected

    def test_enabled_by_default(self, monkeypatch):
        """Enabled by default."""
        monkeypatch.delenv('AWS_FACT_STORE_ENABLED', raising=False)
        assert fact_store.is_configured() is True


class TestResolveCache:
    """Resolution is deterministic, so a repeat lookup must not cost a second round trip."""

    @pytest.mark.asyncio
    async def test_second_identical_call_is_served_from_cache(self):
        """Second identical call is served from cache."""
        answer = {
            'resolve': 'Amazon Aurora',
            'count': 1,
            'candidates': [{'canonical_id': 'svc:aurora'}],
        }
        with patch.object(fact_store, 'call', new=AsyncMock(return_value=answer)) as mock_call:
            first = await fact_store.resolve('Amazon Aurora')
            second = await fact_store.resolve('Amazon Aurora')
        assert first == second == answer
        assert mock_call.call_count == 1

    @pytest.mark.asyncio
    async def test_a_failure_is_not_cached(self):
        """A failure is not cached."""
        failure = fact_store._unavailable('transient')
        with patch.object(fact_store, 'call', new=AsyncMock(return_value=failure)) as mock_call:
            await fact_store.resolve('Amazon Aurora')
            await fact_store.resolve('Amazon Aurora')
        assert mock_call.call_count == 2, 'the store may recover within a session'

    @pytest.mark.asyncio
    async def test_type_filter_is_part_of_the_key(self):
        """Type filter is part of the key."""
        answer = {'count': 0, 'candidates': []}
        with patch.object(fact_store, 'call', new=AsyncMock(return_value=answer)) as mock_call:
            await fact_store.resolve('CreateFunction')
            await fact_store.resolve('CreateFunction', entity_type='operation')
        assert mock_call.call_count == 2

    @pytest.mark.asyncio
    async def test_entity_type_reaches_the_payload(self):
        """Entity type reaches the payload."""
        with patch.object(
            fact_store, 'call', new=AsyncMock(return_value={'candidates': []})
        ) as mock_call:
            await fact_store.resolve('CreateFunction', entity_type='operation', limit=3)
        assert mock_call.call_args[0][0] == {
            'resolve': 'CreateFunction',
            'limit': 3,
            'type': 'operation',
        }


class TestQueryToolPayload:
    """The tool's job is to build a correct payload, so that is what gets asserted."""

    @pytest.mark.asyncio
    async def test_queries_batches_and_caps_at_25(self):
        """A batch goes through as `queries`, capped, with every other slot ignored."""
        from awslabs.aws_documentation_mcp_server.server_aws import query_aws_facts

        ctx = AsyncMock()
        batch = [{'fact_type': 'availability', 'service': f'svc{i}'} for i in range(30)]
        with patch.object(
            fact_store, 'call', new=AsyncMock(return_value={'count': 25, 'results': []})
        ) as mock_call:
            await query_aws_facts(ctx, service='ignored-when-batching', queries=batch)
        sent = mock_call.call_args[0][0]
        assert list(sent) == ['queries'], 'a batch must not carry the single-request slots'
        assert len(sent['queries']) == 25, 'the endpoint caps a batch at 25'
        assert sent['queries'][0] == {'fact_type': 'availability', 'service': 'svc0'}

    @pytest.mark.asyncio
    async def test_slots_map_onto_the_wire_names(self):
        """Slots map onto the wire names."""
        from awslabs.aws_documentation_mcp_server.server_aws import query_aws_facts

        ctx = AsyncMock()
        with patch.object(
            fact_store, 'call', new=AsyncMock(return_value={'answer': {}})
        ) as mock_call:
            await query_aws_facts(
                ctx,
                q=None,
                fact_type='find_related',
                service=None,
                region=None,
                operation=None,
                resource_property='security group',
                depth=None,
                from_handle=None,
                traverse=None,
                relation=None,
                direction=None,
                resource_type=None,
            )
        payload = mock_call.call_args[0][0]
        assert payload == {'fact_type': 'find_related', 'property': 'security group'}, (
            'resource_property must travel as `property`, and empty slots must be omitted'
        )

    @pytest.mark.asyncio
    async def test_from_handle_maps_to_from(self):
        """From handle maps to from."""
        from awslabs.aws_documentation_mcp_server.server_aws import query_aws_facts

        ctx = AsyncMock()
        with patch.object(
            fact_store, 'call', new=AsyncMock(return_value={'answer': {}})
        ) as mock_call:
            await query_aws_facts(
                ctx,
                q=None,
                fact_type=None,
                service=None,
                region=None,
                operation=None,
                resource_property=None,
                depth=None,
                from_handle='concept:abc_def',
                traverse=None,
                relation=None,
                direction=None,
                resource_type=None,
            )
        assert mock_call.call_args[0][0] == {'from': 'concept:abc_def'}

    @pytest.mark.asyncio
    async def test_relation_and_direction_reach_the_payload(self):
        """Relation and direction reach the payload."""
        from awslabs.aws_documentation_mcp_server.server_aws import query_aws_facts

        ctx = AsyncMock()
        with patch.object(
            fact_store, 'call', new=AsyncMock(return_value={'answer': {}})
        ) as mock_call:
            await query_aws_facts(
                ctx,
                q=None,
                fact_type=None,
                service=None,
                region=None,
                operation=None,
                resource_property=None,
                depth=2,
                from_handle='concept:abc_def',
                traverse=None,
                relation='what limits',
                direction='in',
                resource_type=None,
            )
        assert mock_call.call_args[0][0] == {
            'depth': 2,
            'from': 'concept:abc_def',
            'relation': 'what limits',
            'direction': 'in',
        }, 'relation intent must travel as its own slot, unparsed'

    @pytest.mark.asyncio
    async def test_no_arguments_abstains_without_a_round_trip(self):
        """No arguments abstains without a round trip."""
        from awslabs.aws_documentation_mcp_server.server_aws import query_aws_facts

        ctx = AsyncMock()
        with patch.object(fact_store, 'call', new=AsyncMock()) as mock_call:
            result = await query_aws_facts(
                ctx,
                q=None,
                fact_type=None,
                service=None,
                region=None,
                operation=None,
                resource_property=None,
                depth=None,
                from_handle=None,
                traverse=None,
                relation=None,
                direction=None,
                resource_type=None,
            )
        mock_call.assert_not_called()
        assert result['answer']['reason_code'] == 'missing_slot'


class TestResolveTool:
    """Empty input must not reach the network."""

    @pytest.mark.asyncio
    async def test_blank_name_short_circuits(self):
        """Blank name short circuits."""
        from awslabs.aws_documentation_mcp_server.server_aws import resolve_aws_entity

        ctx = AsyncMock()
        with patch.object(fact_store, 'resolve', new=AsyncMock()) as mock_resolve:
            result = await resolve_aws_entity(ctx, name='   ', entity_type=None)
        mock_resolve.assert_not_called()
        assert result['count'] == 0

    @pytest.mark.asyncio
    async def test_unavailable_store_is_reported_to_the_context(self):
        """Unavailable store is reported to the context."""
        from awslabs.aws_documentation_mcp_server.server_aws import resolve_aws_entity

        ctx = AsyncMock()
        with patch.object(
            fact_store, 'resolve', new=AsyncMock(return_value=fact_store._unavailable('403'))
        ):
            await resolve_aws_entity(ctx, name='lambda', entity_type=None)
        ctx.error.assert_awaited_once()
