from uuid import uuid4

import pytest

from kapy.state import (
    CheckpointWrite,
    InvalidArgument,
    MessageWrite,
    QueryLimitExceeded,
    RunContext,
    RunResult,
    UnsafeQuery,
)
from kapy.state.encoding import PAGE_BYTES, encode

from .conftest import Database, spec
from .test_service import simple

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


async def test_scoped_sql_join_subquery_and_functions(database: Database) -> None:
    service = await database.start(simple)
    first = await service.create_session(spec("a"), request_id=uuid4(), input="public apple")
    second = await service.create_session(spec("b"), request_id=uuid4(), input="secret orange")
    for created in (first, second):
        await database.completed(created.submission.request_id)
    answer = await service.query_history(
        first.session.id,
        "SELECT h.text, b.text FROM history h LEFT JOIN "
        "(SELECT seq,text FROM history WHERE kind = :kind) b ON h.seq=b.seq "
        "WHERE h.kind=:kind AND EXISTS (SELECT seq FROM history c WHERE c.seq=h.seq) "
        "ORDER BY h.seq",
        params={"kind": "input"},
    )
    assert answer.rows == (("public apple", "public apple"),)
    summary = await service.query_history(
        first.session.id,
        "SELECT count(*), min(seq), max(seq) FROM history WHERE kind=:kind",
        params={"kind": "input"},
    )
    assert summary.rows == ((1, 1, 1),)
    text = await service.query_history(
        first.session.id,
        "SELECT lower(text), length(text), coalesce(text, '') FROM history WHERE kind='input'",
    )
    assert text.rows == (("public apple", 12, "public apple"),)
    scalar = await service.query_history(
        first.session.id,
        "SELECT (SELECT max(seq) FROM history) AS last FROM history WHERE seq IN (1,2) LIMIT 1",
    )
    assert scalar.rows[0][0] == 3
    malicious_value = "public apple' OR true; SELECT * FROM pg_authid; --"
    safe = await service.query_history(
        first.session.id,
        "SELECT text FROM history WHERE text=:needle",
        params={"needle": malicious_value},
    )
    assert safe.rows == ()


@pytest.mark.parametrize(
    "attack",
    [
        "SELECT * FROM records",
        "SELECT * FROM pg_catalog.pg_authid",
        "SELECT * FROM history h JOIN pg_catalog.pg_class p ON true",
        "SELECT (SELECT rolpassword FROM pg_authid LIMIT 1) FROM history",
        "SELECT pg_read_file('/etc/passwd') FROM history",
        "SELECT pg_sleep(30) FROM history",
        "SELECT set_config('search_path','public',false) FROM history",
        "SELECT * FROM history; SELECT 1",
        "WITH history AS (SELECT * FROM records) SELECT * FROM history",
        "SELECT * INTO leak FROM history",
        "SELECT * FROM history FOR UPDATE",
        "SELECT * FROM history UNION SELECT * FROM records",
        "DELETE FROM history",
        "SELECT 'pg_authid'::regclass FROM history",
        "SELECT current_setting('data_directory') FROM history",
        "SELECT * FROM history h CROSS JOIN LATERAL pg_ls_dir('/') x",
        "SELECT * FROM history TABLESAMPLE SYSTEM (10)",
        "SELECT count(*) FILTER (WHERE true) FROM history",
        "SELECT row_number() OVER () FROM history",
        "SELECT * FROM history AS _kapy_history",
        "SELECT _kapy_history.* FROM history",
        'SELECT text COLLATE "C" FROM history',
        "SELECT * FROM history h JOIN history g USING(seq)",
    ],
)
async def test_malicious_sql_is_rejected(database: Database, attack: str) -> None:
    service = await database.start(simple)
    created = await service.create_session(spec(), request_id=uuid4())
    with pytest.raises(UnsafeQuery):
        await service.query_history(created.session.id, attack)


async def test_multilingual_fulltext_and_literal_substring(database: Database) -> None:
    service = await database.start(simple)
    texts = [
        "你好世界，欢迎中文搜索",
        "CAFÉ mañana Über Straße",
        "مرحبا بالعالم",
        "Привет мир",
        "emoji 👋 and 100%_literal",
        "日本語の検索",
        "한국어 검색",
    ]
    created = await service.create_session(spec(), request_id=uuid4())
    for text in texts:
        submission = await service.submit_input(created.session.id, text, request_id=uuid4())
        await database.completed(submission.request_id)
    for query, expected in [
        ("世界", texts[0]),
        ("café", texts[1]),
        ("STRASSE", texts[1]),
        ("مرحبا", texts[2]),
        ("мир", texts[3]),
        ("日本語", texts[5]),
        ("검색", texts[6]),
    ]:
        result = await service.search_history(created.session.id, query)
        assert any(record.data == expected for record in result.items), (query, result)
    assert [
        record.data
        for record in (
            await service.search_history(created.session.id, "%_literal", mode="substring")
        ).items
    ] == [texts[4]]
    assert not (await service.search_history(created.session.id, "世欢")).items
    assert not (
        await service.search_history(created.session.id, "' OR true --", mode="substring")
    ).items


async def test_encoded_page_cap_and_query_limits(database: Database) -> None:
    payload = '\x01😀"\\' * 2500  # many more JSON bytes than Unicode code points

    async def runner(ctx: RunContext) -> RunResult:
        messages = tuple(
            MessageWrite(uuid4(), "model_response", "", {"escaped": payload}) for _ in range(12)
        )
        return RunResult(
            "done", (), CheckpointWrite(1, ctx.state, messages, tuple(i.id for i in ctx.inputs))
        )

    service = await database.start(runner)
    created = await service.create_session(spec(), request_id=uuid4(), input="start")
    assert (await database.completed(created.submission.request_id))["outcome"] == "completed"
    page = await service.read_output(created.session.id)
    records = list(page.items)
    assert page.has_more
    assert len(encode(page)) <= PAGE_BYTES
    while page.has_more:
        page = await service.read_output(created.session.id, after=page.next_cursor)
        assert len(encode(page)) <= PAGE_BYTES
        records.extend(page.items)
    assert sum(item.kind == "model_response" for item in records) == 12
    assert len({record.cursor for record in records}) == len(records)
    with pytest.raises(QueryLimitExceeded):
        await service.query_history(created.session.id, "SELECT data FROM history")
    limited = await service.query_history(created.session.id, "SELECT kind FROM history", limit=2)
    assert len(limited.rows) == 2 and limited.truncated
    with pytest.raises(InvalidArgument):
        await service.submit_input(created.session.id, "\x00" * 50_000, request_id=uuid4())
    with pytest.raises(InvalidArgument):
        await service.read_output(created.session.id, limit=201)


async def test_backlog_payload_rejected_before_it_can_poison_future_delivery(
    database: Database,
) -> None:
    service = await database.start(simple)
    request_id = uuid4()
    # Fits the single JSON payload cap; its duplicated searchable text/envelope cannot fit a page.
    payload = '"' * (128 * 1024 - 1)
    with pytest.raises(InvalidArgument):
        await service.publish_event(
            uuid4(), payload, request_id=request_id, producer_session_id=None
        )
    assert not await database.rows("SELECT id FROM requests WHERE id=%s", (request_id,))
    assert not await database.rows("SELECT id FROM events")


async def test_sql_decimal_and_complexity_limits(database: Database) -> None:
    service = await database.start(simple)
    created = await service.create_session(spec(), request_id=uuid4())
    assert (await service.query_history(created.session.id, "SELECT 1.5 FROM history")).rows == (
        (1.5,),
    )
    with pytest.raises(InvalidArgument):
        await service.query_history(created.session.id, "SELECT " + "9" * 5000 + " FROM history")
    with pytest.raises(UnsafeQuery):
        await service.query_history(
            created.session.id, "SELECT " + ",".join("seq" for _ in range(200)) + " FROM history"
        )
