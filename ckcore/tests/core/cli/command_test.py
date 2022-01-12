import logging
import os
import re
import shutil
import tempfile
from datetime import timedelta
from pathlib import Path
from typing import List, Dict, Optional, Any, AsyncIterator, Tuple

import pytest
from _pytest.logging import LogCaptureFixture
from aiohttp import ClientTimeout
from aiohttp.hdrs import METH_ANY
from aiohttp.test_utils import TestServer
from aiohttp.web import Request, Response, Application, route
from aiostream import stream
from aiostream.core import Stream
from pytest import fixture

from core.cli import is_node
from core.cli.cli import CLI
from core.cli.command import CLIDependencies, CLIContext, HttpCommand
from core.db.jobdb import JobDb
from core.error import CLIParseError
from core.model.model import predefined_kinds
from core.query.model import Template
from core.task.task_description import TimeTrigger, Workflow
from core.task.task_handler import TaskHandler
from core.types import Json
from core.util import first, exist, AccessJson

# noinspection PyUnresolvedReferences
from tests.core.analytics import event_sender

# noinspection PyUnresolvedReferences
from tests.core.cli.cli_test import cli, cli_deps

# noinspection PyUnresolvedReferences
from tests.core.db.graphdb_test import (
    filled_graph_db,
    graph_db,
    test_db,
    foo_model,
    foo_kinds,
)

# noinspection PyUnresolvedReferences
from tests.core.db.runningtaskdb_test import running_task_db

# noinspection PyUnresolvedReferences
from tests.core.message_bus_test import message_bus

# noinspection PyUnresolvedReferences
from tests.core.query.template_expander_test import expander

# noinspection PyUnresolvedReferences
from tests.core.task.task_handler_test import (
    task_handler,
    job_db,
    subscription_handler,
    test_workflow,
    task_handler_args,
)
from tests.core.util_test import not_in_path

# noinspection PyUnresolvedReferences
from tests.core.worker_task_queue_test import worker, task_queue, performed_by


@fixture
def json_source() -> str:
    nums = ",".join([f'{{ "num": {a}, "inner": {{"num": {a%10}}}}}' for a in range(0, 100)])
    return "json [" + nums + "," + nums + "]"


@fixture
async def echo_http_server() -> AsyncIterator[Tuple[int, List[Tuple[Request, Json]]]]:
    requests = []

    async def add_request(request: Request) -> Response:
        requests.append((request, await request.json()))
        status = 500 if request.path.startswith("/fail") else 200
        return Response(status=status)

    app = Application()
    app.add_routes([route(METH_ANY, "/{tail:.+}", add_request)])
    server = TestServer(app)
    await server.start_server()
    yield server.port, requests  # type: ignore
    await server.close()


@pytest.mark.asyncio
async def test_echo_source(cli: CLI) -> None:
    # no arg passed to json
    result = await cli.execute_cli_command("echo", stream.list)
    assert result[0] == [""]

    # simple string passed to json
    result = await cli.execute_cli_command("echo this is a string", stream.list)
    assert result[0] == ["this is a string"]

    result = await cli.execute_cli_command('echo   "foo bla bar" ', stream.list)
    assert result[0] == ["foo bla bar"]


@pytest.mark.asyncio
async def test_json_source(cli: CLI) -> None:
    # json object passed to json
    result = await cli.execute_cli_command('json {"a": 1}', stream.list)
    assert result[0] == [{"a": 1}]

    # json array passed to json
    result = await cli.execute_cli_command('json [{"a": 1}, {"b":2}]', stream.list)
    assert result[0] == [{"a": 1}, {"b": 2}]

    # json string passed to json
    result = await cli.execute_cli_command('json "foo bla bar"', stream.list)
    assert result[0] == ["foo bla bar"]


@pytest.mark.asyncio
async def test_predecessors(cli: CLI) -> None:
    r1 = await cli.execute_cli_command("query id(4_0) | predecessors", stream.list)
    assert len(r1[0]) == 1
    r2 = await cli.execute_cli_command("query id(4_0) | predecessors --with-origin", stream.list)
    assert len(r2[0]) == 2
    r3 = await cli.execute_cli_command("query id(4_0) | predecessors --with-origin dependency", stream.list)
    assert len(r3[0]) == 2
    r4 = await cli.execute_cli_command("query id(4_0) | predecessors delete", stream.list)
    assert len(r4[0]) == 0


@pytest.mark.asyncio
async def test_ancestors(cli: CLI) -> None:
    r1 = await cli.execute_cli_command("query id(4_0) | ancestors", stream.list)
    assert len(r1[0]) == 4
    r2 = await cli.execute_cli_command("query id(4_0) | ancestors --with-origin", stream.list)
    assert len(r2[0]) == 5
    r3 = await cli.execute_cli_command("query id(4_0) | ancestors --with-origin dependency", stream.list)
    assert len(r3[0]) == 5
    r4 = await cli.execute_cli_command("query id(4_0) | ancestors delete", stream.list)
    assert len(r4[0]) == 0


@pytest.mark.asyncio
async def test_successors(cli: CLI) -> None:
    r1 = await cli.execute_cli_command("query id(4) | successors", stream.list)
    assert len(r1[0]) == 10
    r2 = await cli.execute_cli_command("query id(4) | successors --with-origin", stream.list)
    assert len(r2[0]) == 11
    r3 = await cli.execute_cli_command("query id(4) | successors --with-origin dependency", stream.list)
    assert len(r3[0]) == 11
    r4 = await cli.execute_cli_command("query id(4) | successors delete", stream.list)
    assert len(r4[0]) == 0


@pytest.mark.asyncio
async def test_descendants(cli: CLI) -> None:
    r1 = await cli.execute_cli_command("query id(4) | descendants", stream.list)
    assert len(r1[0]) == 10
    r2 = await cli.execute_cli_command("query id(4) | descendants --with-origin", stream.list)
    assert len(r2[0]) == 11
    r3 = await cli.execute_cli_command("query id(4) | descendants --with-origin dependency", stream.list)
    assert len(r3[0]) == 11
    r4 = await cli.execute_cli_command("query id(4) | descendants delete", stream.list)
    assert len(r4[0]) == 0


@pytest.mark.asyncio
async def test_query_source(cli: CLI) -> None:
    result = await cli.execute_cli_command(
        'query is("foo") and reported.some_int==0 --> reported.identifier=~"9_"', stream.list
    )
    assert len(result[0]) == 10
    await cli.dependencies.template_expander.put_template(
        Template(
            "test",
            'is(foo) and reported.some_int==0 --> reported.identifier=~"{{fid}}"',
        )
    )
    result2 = await cli.execute_cli_command('query expand(test, fid="9_")', stream.list)
    assert len(result2[0]) == 10

    result3 = await cli.execute_cli_command("query --include-edges is(graph_root) -[0:1]->", stream.list)
    # node: graph_root
    # node: collector
    # edge: graph_root -> collector
    # -----------------------------
    # = 3 elements
    assert len(result3[0]) == 3

    result4 = await cli.execute_cli_command("query --explain --include-edges is(graph_root) -[0:1]->", stream.list)
    assert result4[0][0]["rating"] == "simple"


@pytest.mark.asyncio
async def test_sleep_source(cli: CLI) -> None:
    with pytest.raises(CLIParseError):
        await cli.evaluate_cli_command("sleep forever")
    result = await cli.execute_cli_command("sleep 0.001; echo hello", stream.list)
    assert result == [[""], ["hello"]]


@pytest.mark.asyncio
async def test_count_command(cli: CLI, json_source: str) -> None:
    # count instances
    result = await cli.execute_cli_command(f"{json_source} | count", stream.list)
    assert len(result[0]) == 2
    assert result[0] == ["total matched: 200", "total unmatched: 0"]

    # count attributes
    result = await cli.execute_cli_command(f"{json_source} | count num", stream.list)
    assert len(result[0]) == 102
    assert result[0][-2] == "total matched: 200"
    assert result[0][-1] == "total unmatched: 0"

    # count attributes with path
    result = await cli.execute_cli_command(f"{json_source} | count inner.num", stream.list)
    assert len(result[0]) == 12
    assert result[0][-2] == "total matched: 200"
    assert result[0][-1] == "total unmatched: 0"

    # count unknown attributes
    result = await cli.execute_cli_command(f"{json_source} | count does_not_exist", stream.list)
    assert len(result[0]) == 2
    assert result[0] == ["total matched: 0", "total unmatched: 200"]


@pytest.mark.asyncio
async def test_head_command(cli: CLI) -> None:
    assert await cli.execute_cli_command("json [1,2,3,4,5] | head 2", stream.list) == [[1, 2]]
    assert await cli.execute_cli_command("json [1,2,3,4,5] | head -2", stream.list) == [[1, 2]]
    assert await cli.execute_cli_command("json [1,2,3,4,5] | head", stream.list) == [[1, 2, 3, 4, 5]]


@pytest.mark.asyncio
async def test_tail_command(cli: CLI) -> None:
    assert await cli.execute_cli_command("json [1,2,3,4,5] | tail 2", stream.list) == [[4, 5]]
    assert await cli.execute_cli_command("json [1,2,3,4,5] | tail -2", stream.list) == [[4, 5]]
    assert await cli.execute_cli_command("json [1,2,3,4,5] | tail", stream.list) == [[1, 2, 3, 4, 5]]


@pytest.mark.asyncio
async def test_chunk_command(cli: CLI, json_source: str) -> None:
    result: List[List[str]] = await cli.execute_cli_command(f"{json_source} | chunk 50", stream.list)
    assert len(result[0]) == 4  # 200 in chunks of 50
    for a in result[0]:
        assert len(a) == 50


@pytest.mark.asyncio
async def test_flatten_command(cli: CLI, json_source: str) -> None:
    result = await cli.execute_cli_command(f"{json_source} | chunk 50 | flatten", stream.list)
    assert len(result[0]) == 200


@pytest.mark.asyncio
async def test_uniq_command(cli: CLI, json_source: str) -> None:
    result = await cli.execute_cli_command(f"{json_source} | uniq", stream.list)
    assert len(result[0]) == 100


@pytest.mark.asyncio
async def test_set_desired_command(cli: CLI) -> None:
    result = await cli.execute_cli_command('query is("foo") | set_desired a="test" b=1 c=true', stream.list)
    assert len(result[0]) == 11
    for elem in result[0]:
        assert {"a": "test", "b": 1, "c": True}.items() <= elem["desired"].items()


@pytest.mark.asyncio
async def test_set_metadata_command(cli: CLI) -> None:
    result = await cli.execute_cli_command('query is("foo") | set_metadata a="test" b=1 c=true', stream.list)
    assert len(result[0]) == 11
    for elem in result[0]:
        assert {"a": "test", "b": 1, "c": True}.items() <= elem["metadata"].items()


@pytest.mark.asyncio
async def test_clean_command(cli: CLI) -> None:
    result = await cli.execute_cli_command('query is("foo") | clean', stream.list)
    assert len(result[0]) == 11
    for elem in result[0]:
        assert {"clean": True}.items() <= elem["desired"].items()


@pytest.mark.asyncio
async def test_protect_command(cli: CLI) -> None:
    result = await cli.execute_cli_command('query is("foo") | protect', stream.list)
    assert len(result[0]) == 11
    for elem in result[0]:
        assert {"protected": True}.items() <= elem["metadata"].items()


@pytest.mark.asyncio
async def test_list_sink(cli: CLI, cli_deps: CLIDependencies) -> None:
    result = await cli.execute_cli_command("json [1,2,3]", stream.list)
    assert result == [[1, 2, 3]]


@pytest.mark.asyncio
async def test_flat_sink(cli: CLI) -> None:
    parsed = await cli.evaluate_cli_command("json [1,2,3]; json [4,5,6]; json [7,8,9]")
    result = await stream.list(stream.concat(stream.iterate((await p.execute())[1] for p in parsed)))
    assert result == [1, 2, 3, 4, 5, 6, 7, 8, 9]


@pytest.mark.asyncio
async def test_format(cli: CLI) -> None:
    # access properties by name and path
    result = await cli.execute_cli_command(
        'json {"a":"b", "b": {"c":"d"}} | format a:{a} b:{b.c} na:{fuerty}', stream.list
    )
    assert result[0] == ["a:b b:d na:null"]
    # access deeply nested properties with dict and array
    result = await cli.execute_cli_command(
        'json {"a":{"b":{"c":{"d":[0,1,2, {"e":"f"}]}}}} | format will be an >{a.b.c.d[3].e}<',
        stream.list,
    )
    assert result[0] == ["will be an >f<"]
    # make sure any path that is not available leads to the null value
    result = await cli.execute_cli_command("json {} | format {a}:{b.c.d}:{foo.bla[23].test}", stream.list)
    assert result[0] == ["null:null:null"]


@pytest.mark.asyncio
async def test_add_job_command(cli: CLI, task_handler: TaskHandler, job_db: JobDb) -> None:
    ctx = CLIContext(cli.cli_env)
    result = await cli.execute_cli_command("add_job 23 1 * * * echo Hello World @NOW@", stream.list, ctx)
    assert result == [["Job 06bf8ab1 added."]]
    job = await job_db.get("06bf8ab1")
    assert job is not None
    assert job.command.command == "echo Hello World @NOW@"
    assert job.trigger == TimeTrigger("23 1 * * *")
    assert job.wait is None
    assert job in task_handler.task_descriptions
    assert job.environment == {"graph": "ns"}
    with_event = await cli.execute_cli_command("add_job 23 1 * * * foo : echo Hello World", stream.list, ctx)
    assert with_event == [["Job 2522f6d8 added."]]
    job_with_event: Job = await job_db.get("2522f6d8")  # type: ignore
    assert job_with_event.wait is not None
    event_trigger, timeout = job_with_event.wait
    assert event_trigger.message_type == "foo"
    assert timeout == timedelta(hours=24)
    assert job_with_event.environment == {"graph": "ns"}
    assert job_with_event in task_handler.task_descriptions
    only_event = await cli.execute_cli_command("add_job foo : echo Hello World", stream.list, ctx)
    assert only_event == [["Job d11a8f65 added."]]
    job_only_event: Job = await job_db.get("d11a8f65")  # type: ignore
    assert job_only_event.wait is None
    assert job_only_event.environment == {"graph": "ns"}
    assert job_only_event in task_handler.task_descriptions


@pytest.mark.asyncio
async def test_delete_job_command(cli: CLI, task_handler: TaskHandler, job_db: JobDb) -> None:
    await cli.execute_cli_command("add_job 23 1 * * * echo Hello World", stream.list)
    assert await job_db.get("0814c88f") is not None
    result = await cli.execute_cli_command("delete_job 0814c88f", stream.list)
    assert result == [["Job 0814c88f deleted."]]
    assert await job_db.get("0814c88f") is None
    assert not exist(lambda x: x.id == "0814c88f", task_handler.task_descriptions)  # type: ignore # pypy


@pytest.mark.asyncio
async def test_jobs_command(cli: CLI, task_handler: TaskHandler, job_db: JobDb) -> None:
    await cli.execute_cli_command("add_job 23 1 * * * echo Hello World", stream.list)
    result: List[Json] = (await cli.execute_cli_command("jobs", stream.list))[0]
    job = first(lambda x: x.get("id") == "0814c88f", result)  # type: ignore # pypy
    assert job is not None
    assert job["trigger"] == {"cron_expression": "23 1 * * *"}
    assert job["command"] == "echo Hello World"


@pytest.mark.asyncio
async def test_tag_command(cli: CLI, performed_by: Dict[str, List[str]], caplog: LogCaptureFixture) -> None:
    counter = 0

    def nr_of_performed() -> int:
        nonlocal counter
        performed = len(performed_by)
        increase = performed - counter
        counter = performed
        return increase

    nr_of_performed()  # reset to 0

    assert await cli.execute_cli_command("echo id_does_not_exist | tag update foo bla", stream.list) == [[]]
    assert nr_of_performed() == 0
    res1 = await cli.execute_cli_command('json ["root", "collector"] | tag update foo bla', stream.list)
    assert nr_of_performed() == 2
    assert {a["id"] for a in res1[0]} == {"root", "collector"}
    res2 = await cli.execute_cli_command('query is("foo") | tag update foo bla', stream.list)
    assert nr_of_performed() == 11
    assert len(res2[0]) == 11
    res3 = await cli.execute_cli_command('query is("foo") | tag delete foo', stream.list)
    assert nr_of_performed() == 11
    assert len(res3[0]) == 11
    with caplog.at_level(logging.WARNING):
        caplog.clear()
        res4 = await cli.execute_cli_command('query is("bla") limit 2 | tag delete foo', stream.list)
        assert nr_of_performed() == 2
        assert len(res4[0]) == 2
        # make sure that 2 warnings are emitted
        assert len(caplog.records) == 2
        for res in caplog.records:
            assert res.message.startswith("Tag update not reflected in db. Wait until next collector run.")
    # tag updates can be put into background
    res6 = await cli.execute_cli_command('json ["root", "collector"] | tag update --nowait foo bla', stream.list)
    assert cli.dependencies.forked_tasks.qsize() == 2
    for res in res6[0]:
        # in this case a message with the task id is emitted
        assert res.startswith("Spawned WorkerTask tag:")  # type:ignore
        # and the real result is found when the forked task is awaited, which happens by the CLI reaper
        awaitable, info = await cli.dependencies.forked_tasks.get()
        assert (await awaitable)["id"] in ["root", "collector"]  # type:ignore


@pytest.mark.asyncio
async def test_start_task_command(cli: CLI, task_handler: TaskHandler, test_workflow: Workflow) -> None:
    result = await cli.execute_cli_command(f"start_task {test_workflow.id}", stream.list)
    assert len(result[0]) == 1
    assert re.match("Task .+ has been started", result[0][0])


@pytest.mark.asyncio
async def test_tasks_command(cli: CLI, task_handler: TaskHandler, test_workflow: Workflow) -> None:
    await task_handler.start_task(test_workflow, "direct")
    result = await cli.execute_cli_command("tasks", stream.list)
    assert len(result[0]) == 1
    task = AccessJson(result[0][0])
    assert task.descriptor.id == "test_workflow"


@pytest.mark.asyncio
async def test_kind_command(cli: CLI) -> None:
    result = await cli.execute_cli_command("kind", stream.list)
    for kind in predefined_kinds:
        assert kind.fqn in result[0]
    result = await cli.execute_cli_command("kind string", stream.list)
    assert result[0][0] == {"name": "string", "runtime_kind": "string"}
    result = await cli.execute_cli_command("kind -p reported.ctime", stream.list)
    assert result[0][0] == {"name": "datetime", "runtime_kind": "datetime"}
    with pytest.raises(Exception):
        await cli.execute_cli_command("kind foo bla bar", stream.list)


@pytest.mark.asyncio
async def test_list_command(cli: CLI) -> None:
    result = await cli.execute_cli_command('reported is (foo) and identifier=="4" | list', stream.list)
    assert len(result[0]) == 1
    assert result[0][0].startswith("kind=foo, identifier=4, age=")
    list_cmd = "list some_int as si, reported.some_string"
    result = await cli.execute_cli_command(f'reported is (foo) and identifier=="4" | {list_cmd}', stream.list)
    assert result[0] == ["si=0, some_string=hello"]


@pytest.mark.asyncio
async def test_jq_command(cli: CLI) -> None:
    result = await cli.execute_cli_command('json {"a":{"b":1}} | jq ".a.b"', stream.list)
    assert len(result[0]) == 1
    assert result[0][0] == 1


@pytest.mark.asyncio
async def test_aggregation_to_count_command(cli: CLI) -> None:
    r = await cli.execute_cli_command("query all | count reported.kind", stream.list)
    assert set(r[0]) == {
        "graph_root: 1",
        "cloud: 1",
        "foo: 11",
        "bla: 100",
        "total matched: 113",
        "total unmatched: 0",
    }
    # exactly the same command as above (above query would be rewritten as this)
    r = await cli.execute_cli_command(
        "execute_query aggregate(reported.kind as name: sum(1) as count):all sort count asc | aggregate_to_count",
        stream.list,
    )
    assert set(r[0]) == {
        "graph_root: 1",
        "cloud: 1",
        "foo: 11",
        "bla: 100",
        "total matched: 113",
        "total unmatched: 0",
    }


@pytest.mark.skipif(not_in_path("arangodump"), reason="requires arangodump to be in path")
@pytest.mark.asyncio
async def test_system_backup_command(cli: CLI) -> None:
    async def check_backup(res: Stream) -> None:
        async with res.stream() as streamer:
            only_one = True
            async for s in streamer:
                assert isinstance(s, str)
                assert os.path.exists(s)
                # backup should have size between 30k and 200k (adjust size if necessary)
                assert 30000 < os.path.getsize(s) < 200000
                assert only_one
                only_one = False

    await cli.execute_cli_command("system backup create", check_backup)


@pytest.mark.skipif(
    not_in_path("arangodump", "arangorestore"),
    reason="requires arangodump and arangorestore",
)
@pytest.mark.asyncio
async def test_system_restore_command(cli: CLI) -> None:
    tmp_dir: Optional[str] = None
    try:
        tmp_dir = tempfile.mkdtemp()
        backup = os.path.join(tmp_dir, "backup")

        async def move_backup(res: Stream) -> None:
            async with res.stream() as streamer:
                async for s in streamer:
                    os.rename(s, backup)

        await cli.execute_cli_command("system backup create", move_backup)
        ctx = CLIContext(uploaded_files={"backup": backup})
        restore = await cli.execute_cli_command(
            f"BACKUP_NO_SYS_EXIT=true system backup restore {backup}", stream.list, ctx
        )
        assert restore == [
            [
                "Database has been restored successfully!",
                "Since all data has changed in the database eventually, this service needs to be restarted!",
            ]
        ]
    finally:
        if tmp_dir:
            shutil.rmtree(tmp_dir)


@pytest.mark.asyncio
async def test_templates_command(cli: CLI) -> None:
    result = await cli.execute_cli_command("templates test kind=volume is({{kind}})", stream.list)
    assert result == [["is(volume)"]]
    result = await cli.execute_cli_command("templates add filter_kind is({{kind}})", stream.list)
    assert result == [["Template filter_kind added to the query library.\nis({{kind}})"]]
    result = await cli.execute_cli_command("templates", stream.list)
    assert result == [["filter_kind: is({{kind}})"]]
    result = await cli.execute_cli_command("templates filter_kind", stream.list)
    assert result == [["is({{kind}})"]]
    result = await cli.execute_cli_command("templates delete filter_kind", stream.list)
    assert result == [["Template filter_kind deleted from the query library."]]


@pytest.mark.asyncio
async def test_write_command(cli: CLI) -> None:
    async def check_file(res: Stream) -> None:
        async with res.stream() as streamer:
            only_one = True
            async for s in streamer:
                assert isinstance(s, str)
                p = Path(s)
                assert p.exists() and p.is_file()
                assert 1 < p.stat().st_size < 100000
                assert p.name.startswith("write_test")
                assert only_one
                only_one = False

    # result can be read as json
    await cli.execute_cli_command("query all limit 3 | format --json | write write_test.json ", check_file)
    # result can be read as yaml
    await cli.execute_cli_command("query all limit 3 | format --yaml | write write_test.yaml ", check_file)


@pytest.mark.asyncio
async def test_http_command(cli: CLI, echo_http_server: Tuple[int, List[Tuple[Request, Json]]]) -> None:
    port, requests = echo_http_server

    def test_arg(
        arg_str: str,
        method: Optional[str] = None,
        url: Optional[str] = None,
        headers: Optional[Dict[str, str]] = None,
        params: Optional[Dict[str, str]] = None,
        timeout: Optional[ClientTimeout] = None,
        compress: Optional[bool] = None,
    ) -> None:
        def test_if_set(prop: Any, value: Any) -> None:
            if prop is not None:
                assert prop == value, f"{prop} is not {value}"

        arg = HttpCommand.parse_args("https", arg_str)
        test_if_set(method, arg.method)
        test_if_set(url, arg.url)
        test_if_set(headers, arg.headers)
        test_if_set(params, arg.params)
        test_if_set(compress, arg.compress)
        test_if_set(timeout, arg.timeout)

    test_arg(":123", "POST", "https://localhost:123", {}, {}, ClientTimeout(30), False)
    test_arg("GET :123", "GET", "https://localhost:123")
    test_arg("://foo:123", "POST", "https://foo:123")
    test_arg("foo:123/bla", "POST", "https://foo:123/bla")
    test_arg("foo:123/bla", "POST", "https://foo:123/bla")
    test_arg("foo/bla", "POST", "https://foo/bla")
    test_arg(
        '--compress --timeout 24 POST :123 "hdr1: test" qp==123  hdr2:fest "qp2 == 321"',
        headers={"hdr1": "test", "hdr2": "fest"},
        params={"qp": "123", "qp2": "321"},
        compress=True,
        timeout=ClientTimeout(24),
    )

    # take 3 instance of type bla and send it to the echo server
    result = await cli.execute_cli_command(f"query is(bla) limit 3 | http :{port}/test", stream.list)
    # one line is returned to the user with a summary of the response types.
    assert result == [["3 requests with status 200 sent."]]
    # make sure all 3 requests have been received - the body is the complete json node
    assert len(requests) == 3
    for ar in (AccessJson(content) for _, content in requests):
        assert is_node(ar)
        assert ar.kinds == ["bla"]
        assert ar.reported.kind == "bla"

    # failing requests are retried
    requests.clear()
    await cli.execute_cli_command(f"query is(bla) limit 1 | http --backoff-base 0.001 :{port}/fail", stream.list)
    # 1 request + 3 retries => 4 requests
    assert len(requests) == 4
