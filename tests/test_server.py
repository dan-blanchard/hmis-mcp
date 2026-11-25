import asyncio
import json

from mcp import Client

from hmis_mcp.server import build_server


def with_client(engine, fn):
    async def run():
        async with Client(build_server(engine)) as client:
            return await fn(client)
    return asyncio.run(run())


def call(engine, tool, args=None):
    return with_client(engine, lambda c: c.call_tool(tool, args or {}))


def tools(engine):
    return with_client(engine, lambda c: c.list_tools()).tools


def test_tools_are_listed_and_marked_read_only(engine):
    listed = tools(engine)
    assert {t.name for t in listed} == {
        "describe_dataset", "list_projects", "query_aggregate", "bed_utilization"}
    for t in listed:
        assert t.annotations.read_only_hint is True and t.annotations.open_world_hint is False


def _walk(node):
    if isinstance(node, dict):
        yield node
        for v in node.values():
            yield from _walk(v)
    elif isinstance(node, list):
        for v in node:
            yield from _walk(v)


def test_input_schemas_stay_within_what_every_provider_accepts(engine):
    """No unions, no nullable types, no open-ended maps. Gemini and OpenAI strict
    mode each reject at least one of those."""
    for t in tools(engine):
        schema = t.input_schema
        assert schema["type"] == "object"
        for node in _walk(schema):
            assert not {"anyOf", "oneOf", "allOf", "not"} & set(node), (t.name, node)
            assert node.get("type") != "null" and not isinstance(node.get("type"), list), t.name
            assert node.get("additionalProperties") in (None, False), (t.name, node)
        for name, prop in schema["properties"].items():
            assert prop.get("description"), f"{t.name}.{name} has no description"


def test_query_round_trip_with_filters(engine):
    result = call(engine, "query_aggregate", {
        "measures": ["clients"], "group_by": ["destination"], "cohort": "exited",
        "filters": [{"dimension": "project_type",
                     "values": ["Emergency Shelter - Entry/Exit"]}]})
    assert not result.is_error
    payload = result.structured_content
    assert payload["cohort"] == "exited" and payload["rows"]
    # Clients that ignore structured output still get the same thing as text.
    assert json.loads(result.content[0].text) == payload


def test_defaults_need_only_measures(engine):
    assert call(engine, "query_aggregate", {"measures": ["clients"]}).structured_content["rows"]
    assert call(engine, "bed_utilization").structured_content["rows"]
    assert call(engine, "list_projects").structured_content["projects"]


def test_bad_requests_are_tool_errors_with_guidance(engine):
    result = call(engine, "query_aggregate", {"measures": ["clients"], "group_by": ["ssn"]})
    assert result.is_error and "age_group" in result.content[0].text
    result = call(engine, "query_aggregate", {"measures": ["ssn"]})
    assert result.is_error
    result = call(engine, "query_aggregate", {
        "measures": ["clients"],
        "filters": [{"dimension": "sex", "values": ["Male"], "sql": "1=1"}]})
    assert result.is_error
