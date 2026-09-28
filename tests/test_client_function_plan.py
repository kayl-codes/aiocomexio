"""ComexioClient function plan writes against the fake Comexio server."""

import base64
import json
import re
from collections.abc import Awaitable, Callable
from typing import Any

import pytest
from aiohttp import web

from aiocomexio import (
    ComexioAuthenticationError,
    ComexioClient,
    ComexioConnectionError,
    ComexioDataError,
    ComexioRequestRejectedError,
    ComexioResponseError,
)
from tests.fake_comexio import FUNCTION_MODULE_PATH, LOAD_ELEMENTS_PATH, LOGIN_PAGE, FakeComexio

UNIQUE_CHECK_PATH = "/admin/_helper/isunique"
PLAN_SAVE_PATH = "/admin/function_function_module/save_fub"
PLAN_DELETE_PATH = "/admin/function_function_module/delete/"
PLAN_RUN_PATH = "/admin/function_function_module/run_fup/"
PLAN_STOP_PATH = "/admin/function_function_module/stop_fup/"
ADD_ELEMENT_PATH = "/admin/function_function_module/add_element/"
SAVE_CONNECTION_PATH = "/admin/function_function_module/saveconnection/"
SAVE_POSITIONS_PATH = "/admin/function_function_module/saveelementspos/"
DELETE_ELEMENTS_PATH = "/admin/function_function_module/deleteelements/"
SAVE_COMMENT_PATH = "/admin/function_function_module/savefupcommentelement/"

_HOME = "/admin/function_function_module/home"
_TIMESTAMP_RE = r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z"

PlanCall = Callable[[ComexioClient], Awaitable[Any]]


def _serve_redirect(comexio: FakeComexio, method: str, path: str, location: str) -> None:
    async def handler(_request: web.Request) -> web.Response:
        return web.Response(status=302, headers={"Location": location})

    comexio.serve(method, path, handler)


def _serve_plans(comexio: FakeComexio, fubs: Any) -> None:
    comexio.serve_text(
        "GET",
        FUNCTION_MODULE_PATH,
        f'<script>var $FubModules = {{"2": {{}}}};\nvar $Fubs = {json.dumps(fubs)};\n</script>',
    )


# --- plan settings -------------------------------------------------------------------------------


async def test_create_function_plan(logged_in: ComexioClient, comexio: FakeComexio) -> None:
    comexio.serve_json("POST", UNIQUE_CHECK_PATH, {"result": True})
    _serve_redirect(comexio, "POST", PLAN_SAVE_PATH, f"{_HOME}?added=1")
    _serve_plans(comexio, {"1": {"Name": "Alt"}, "7": {"Name": "HA - Marker"}})

    fub_id = await logged_in.create_function_plan(
        "HA - Marker", comment="von HA", paper_format="a3", orientation="Portrait", dpi=120
    )

    assert fub_id == 7
    (check,) = comexio.received_at("POST", UNIQUE_CHECK_PATH)
    assert check.form == {"model": "fub", "field": "name", "value": "HA - Marker"}
    (request,) = comexio.received_at("POST", PLAN_SAVE_PATH)
    assert request.form == {
        "fub_type": "1",
        "fub_page_count_x": "1",
        "fub_page_count_y": "1",
        "fub_name": "HA - Marker",
        "fub_comment": "von HA",
        "fub_paper": "2",
        "fub_orientation": "1",
        "fub_resolution": "120",
        "fub_position": "-1",
        "fub_active": "0",
        "fub_reset_on_close": "0",
        "fub_create": "Erzeugen",
    }
    assert request.xhr


async def test_create_function_plan_defaults_to_a4_landscape_90_dpi(
    logged_in: ComexioClient, comexio: FakeComexio
) -> None:
    comexio.serve_json("POST", UNIQUE_CHECK_PATH, {"result": True})
    _serve_redirect(comexio, "POST", PLAN_SAVE_PATH, f"{_HOME}?added=1")
    _serve_plans(comexio, {"3": {"Name": "Neu"}})

    assert await logged_in.create_function_plan("Neu") == 3
    (request,) = comexio.received_at("POST", PLAN_SAVE_PATH)
    assert (request.form["fub_paper"], request.form["fub_orientation"], request.form["fub_resolution"]) == (
        "3",
        "0",
        "90",
    )


async def test_create_function_plan_taken_name_is_rejected_before_saving(
    logged_in: ComexioClient, comexio: FakeComexio
) -> None:
    comexio.serve_json("POST", UNIQUE_CHECK_PATH, {"result": False})

    with pytest.raises(ComexioRequestRejectedError, match="already in use"):
        await logged_in.create_function_plan("Neu")
    assert comexio.received_at("POST", PLAN_SAVE_PATH) == []


async def test_create_function_plan_name_check_without_verdict_is_a_data_error(
    logged_in: ComexioClient, comexio: FakeComexio
) -> None:
    comexio.serve_json("POST", UNIQUE_CHECK_PATH, {"error": "unknown model"})

    with pytest.raises(ComexioDataError, match="no result"):
        await logged_in.create_function_plan("Neu")
    assert comexio.received_at("POST", PLAN_SAVE_PATH) == []


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"paper_format": "A1"}, "paper_format"),
        ({"orientation": "diagonal"}, "orientation"),
        ({"dpi": 44}, "dpi"),
        ({"dpi": 121}, "dpi"),
        ({"dpi": 90.0}, "dpi"),
        ({"dpi": True}, "dpi"),
    ],
)
async def test_create_function_plan_invalid_settings_raise_before_any_request(
    logged_in: ComexioClient, comexio: FakeComexio, kwargs: dict[str, Any], match: str
) -> None:
    with pytest.raises(ValueError, match=match):
        await logged_in.create_function_plan("Neu", **kwargs)
    assert comexio.received_at("POST", UNIQUE_CHECK_PATH) == []


@pytest.mark.parametrize("kwargs", [{"paper_format": None}, {"orientation": 0}, {"comment": None}])
async def test_create_function_plan_non_string_settings_raise_type_error_before_any_request(
    logged_in: ComexioClient, comexio: FakeComexio, kwargs: dict[str, Any]
) -> None:
    with pytest.raises(TypeError, match="Expected strings"):
        await logged_in.create_function_plan("Neu", **kwargs)
    assert comexio.received_at("POST", UNIQUE_CHECK_PATH) == []


@pytest.mark.parametrize("fubs", [{"1": {"Name": "Alt"}}, {"1": {"Name": "Neu"}, "2": {"Name": "Neu"}}, []])
async def test_create_function_plan_not_found_once_in_fubs_is_a_data_error(
    logged_in: ComexioClient, comexio: FakeComexio, fubs: Any
) -> None:
    comexio.serve_json("POST", UNIQUE_CHECK_PATH, {"result": True})
    _serve_redirect(comexio, "POST", PLAN_SAVE_PATH, f"{_HOME}?added=1")
    _serve_plans(comexio, fubs)

    with pytest.raises(ComexioDataError, match="plans with that name"):
        await logged_in.create_function_plan("Neu")


async def test_create_function_plan_non_numeric_id_is_a_data_error(
    logged_in: ComexioClient, comexio: FakeComexio
) -> None:
    comexio.serve_json("POST", UNIQUE_CHECK_PATH, {"result": True})
    _serve_redirect(comexio, "POST", PLAN_SAVE_PATH, f"{_HOME}?added=1")
    _serve_plans(comexio, {"x": {"Name": "Neu"}})

    with pytest.raises(ComexioDataError, match="non-numeric id"):
        await logged_in.create_function_plan("Neu")


async def test_create_function_plan_unconfirmed_redirect_is_rejected(
    logged_in: ComexioClient, comexio: FakeComexio
) -> None:
    comexio.serve_json("POST", UNIQUE_CHECK_PATH, {"result": True})
    _serve_redirect(comexio, "POST", PLAN_SAVE_PATH, f"{_HOME}?error=1")

    with pytest.raises(ComexioRequestRejectedError, match=r"redirect to '.*error=1'"):
        await logged_in.create_function_plan("Neu")
    assert comexio.received_at("GET", FUNCTION_MODULE_PATH) == []


@pytest.mark.parametrize(
    ("body", "status", "error"),
    [
        (LOGIN_PAGE, 200, ComexioAuthenticationError),
        ("<form>Fehler</form>", 200, ComexioRequestRejectedError),
        ("", 500, ComexioResponseError),
    ],
)
async def test_plan_settings_form_without_redirect_is_not_success(
    logged_in: ComexioClient, comexio: FakeComexio, body: str, status: int, error: type[Exception]
) -> None:
    comexio.serve_json("POST", UNIQUE_CHECK_PATH, {"result": True})
    comexio.serve_text("POST", PLAN_SAVE_PATH, body, status=status)

    with pytest.raises(error):
        await logged_in.create_function_plan("Neu")


async def test_plan_settings_form_connection_error(logged_in: ComexioClient, comexio: FakeComexio) -> None:
    async def drop(request: web.Request) -> web.StreamResponse:
        assert request.transport is not None
        request.transport.close()
        return web.Response()

    comexio.serve("POST", PLAN_SAVE_PATH, drop)

    with pytest.raises(ComexioConnectionError):
        await logged_in.update_function_plan(
            4, name="P", comment="", position=0, active=False, paper_format="A4", orientation="landscape", dpi=90
        )


async def test_update_function_plan(logged_in: ComexioClient, comexio: FakeComexio) -> None:
    _serve_redirect(comexio, "POST", PLAN_SAVE_PATH, f"{_HOME}?saved=1")

    await logged_in.update_function_plan(
        4,
        name="Umbenannt",
        comment="Kommentar",
        position=2,
        active=True,
        paper_format="A5",
        orientation="landscape",
        dpi=45,
        reset_outputs_on_stop=True,
    )

    (request,) = comexio.received_at("POST", PLAN_SAVE_PATH)
    assert request.form == {
        "fub_id": "4",
        "fub_type": "1",
        "fub_page_count_x": "1",
        "fub_page_count_y": "1",
        "fub_name": "Umbenannt",
        "fub_comment": "Kommentar",
        "fub_paper": "4",
        "fub_orientation": "0",
        "fub_resolution": "45",
        "fub_position": "2",
        "fub_active": "1",
        "fub_reset_on_close": "1",
        "fub_save": "Speichern",
    }
    assert comexio.received_at("POST", UNIQUE_CHECK_PATH) == []


async def test_create_function_plan_read_back_failure_says_the_plan_exists(
    logged_in: ComexioClient, comexio: FakeComexio
) -> None:
    comexio.serve_json("POST", UNIQUE_CHECK_PATH, {"result": True})
    _serve_redirect(comexio, "POST", PLAN_SAVE_PATH, f"{_HOME}?added=1")
    comexio.serve_text("GET", FUNCTION_MODULE_PATH, "", status=500)

    with pytest.raises(ComexioResponseError) as caught:
        await logged_in.create_function_plan("Neu")
    assert any("only reading back its id failed" in note for note in caught.value.__notes__)


@pytest.mark.parametrize("location", ["/board/home/login/", "http://comexio/admin/home/Login?next=saved=1"])
async def test_redirect_to_the_login_page_is_an_authentication_error(
    logged_in: ComexioClient, comexio: FakeComexio, location: str
) -> None:
    _serve_redirect(comexio, "POST", PLAN_SAVE_PATH, location)
    _serve_redirect(comexio, "GET", PLAN_DELETE_PATH, location)

    with pytest.raises(ComexioAuthenticationError):
        await logged_in.update_function_plan(
            4, name="P", comment="", position=0, active=False, paper_format="A4", orientation="landscape", dpi=90
        )
    with pytest.raises(ComexioAuthenticationError):
        await logged_in.delete_function_plan(9)


async def test_update_function_plan_unconfirmed_is_rejected(logged_in: ComexioClient, comexio: FakeComexio) -> None:
    _serve_redirect(comexio, "POST", PLAN_SAVE_PATH, f"{_HOME}?added=1")

    with pytest.raises(ComexioRequestRejectedError):
        await logged_in.update_function_plan(
            4, name="P", comment="", position=0, active=False, paper_format="A4", orientation="landscape", dpi=90
        )


async def test_delete_function_plan(logged_in: ComexioClient, comexio: FakeComexio) -> None:
    _serve_redirect(comexio, "GET", PLAN_DELETE_PATH, f"{_HOME}?delete=ok")

    await logged_in.delete_function_plan(9)

    (request,) = comexio.received_at("GET", PLAN_DELETE_PATH)
    assert request.query == {"id": "9"}
    assert request.xhr


async def test_delete_function_plan_unconfirmed_is_rejected(logged_in: ComexioClient, comexio: FakeComexio) -> None:
    _serve_redirect(comexio, "GET", PLAN_DELETE_PATH, f"{_HOME}?delete=error")

    with pytest.raises(ComexioRequestRejectedError, match="delete=error"):
        await logged_in.delete_function_plan(9)


# --- run / stop ----------------------------------------------------------------------------------

_PLAN = {
    "elements": {"10": {"id": 10, "type": 2}},
    "connections": {"3": {"id": 3, "input": {"FubElementId": 10}, "output": [{"FubElementId": 11}]}},
}


async def test_run_function_plan_sends_outputs_as_indexed_object(
    logged_in: ComexioClient, comexio: FakeComexio
) -> None:
    comexio.serve_json("POST", PLAN_RUN_PATH, {"result": True, "state": 1})

    await logged_in.run_function_plan(4, _PLAN)

    (request,) = comexio.received_at("POST", PLAN_RUN_PATH)
    assert request.form["id"] == "4"
    assert json.loads(request.form["data"]) == {
        "elements": _PLAN["elements"],
        "connections": {"3": {"id": 3, "input": {"FubElementId": 10}, "output": {"0": {"FubElementId": 11}}}},
    }
    assert comexio.received_at("GET", LOAD_ELEMENTS_PATH) == []


async def test_run_function_plan_without_plan_runs_the_current_state(
    logged_in: ComexioClient, comexio: FakeComexio
) -> None:
    comexio.serve_json("GET", LOAD_ELEMENTS_PATH, {"elements": [{"id": 10}], "connections": []})
    comexio.serve_json("POST", PLAN_RUN_PATH, {"result": True})

    await logged_in.run_function_plan(4)

    (request,) = comexio.received_at("POST", PLAN_RUN_PATH)
    assert json.loads(request.form["data"]) == {"elements": {"10": {"id": 10}}, "connections": {}}


async def test_run_function_plan_never_runs_a_plan_that_failed_to_load(
    logged_in: ComexioClient, comexio: FakeComexio
) -> None:
    comexio.serve_json("GET", LOAD_ELEMENTS_PATH, {"error": "invalid fub"})

    with pytest.raises(ComexioDataError):
        await logged_in.run_function_plan(4)
    assert comexio.received_at("POST", PLAN_RUN_PATH) == []


@pytest.mark.parametrize(
    "answer",
    [
        {"elements": {"10": {"id": 10}}},
        {"elements": {"10": {"id": 10}}, "connections": None},
        {"connections": []},
        {"elements": {}, "connections": {"3": {"id": 3, "output": None}}},
        {"elements": {}, "connections": {"3": None}},
    ],
)
async def test_run_function_plan_without_plan_never_runs_an_incomplete_answer(
    logged_in: ComexioClient, comexio: FakeComexio, answer: dict[str, Any]
) -> None:
    # A missing collection would be normalized to {} and wipe the live plan's elements or wires.
    comexio.serve_json("GET", LOAD_ELEMENTS_PATH, answer)

    with pytest.raises(ComexioDataError):
        await logged_in.run_function_plan(4)
    assert comexio.received_at("POST", PLAN_RUN_PATH) == []


async def test_run_function_plan_refused(logged_in: ComexioClient, comexio: FakeComexio) -> None:
    comexio.serve_json("POST", PLAN_RUN_PATH, {"result": False, "message": "duplicate_outputs"})

    with pytest.raises(ComexioRequestRejectedError, match="duplicate_outputs"):
        await logged_in.run_function_plan(4, _PLAN)


async def test_stop_function_plan(logged_in: ComexioClient, comexio: FakeComexio) -> None:
    comexio.serve_json("POST", PLAN_STOP_PATH, {"result": True, "state": 0})

    await logged_in.stop_function_plan(4)

    (request,) = comexio.received_at("POST", PLAN_STOP_PATH)
    assert request.form == {"id": "4"}
    assert request.xhr


@pytest.mark.parametrize("answer", [{"result": False}, {"result": 1}, {}])
async def test_stop_function_plan_unconfirmed_is_rejected(
    logged_in: ComexioClient, comexio: FakeComexio, answer: dict[str, Any]
) -> None:
    comexio.serve_json("POST", PLAN_STOP_PATH, answer)

    with pytest.raises(ComexioRequestRejectedError):
        await logged_in.stop_function_plan(4)


# --- elements ------------------------------------------------------------------------------------


async def test_add_function_plan_element(logged_in: ComexioClient, comexio: FakeComexio) -> None:
    comexio.serve_json("POST", ADD_ELEMENT_PATH, {"id": "55"})

    assert await logged_in.add_function_plan_element(4, 271, 2, x=105.0, y=30) == 55

    (request,) = comexio.received_at("POST", ADD_ELEMENT_PATH)
    form = dict(request.form)
    assert re.fullmatch(_TIMESTAMP_RE, form.pop("timestamp"))
    assert form == {"fubid": "4", "name": "", "ref_id": "271", "type": "2", "id": "undefined", "x": "105.0", "y": "30"}
    assert request.xhr


async def test_add_function_plan_element_with_connection(logged_in: ComexioClient, comexio: FakeComexio) -> None:
    comexio.serve_json("POST", ADD_ELEMENT_PATH, {"id": 56})
    connection = {"0": {"id": "new", "fub_id": 4, "type": "binary", "output": {"0": {"element": "new"}}}}

    assert await logged_in.add_function_plan_element(4, 12, 10, x=0, y=0, connection=connection) == 56

    (request,) = comexio.received_at("POST", ADD_ELEMENT_PATH)
    assert request.form["connection"] == json.dumps(connection, separators=(",", ":"))


@pytest.mark.parametrize(
    ("answer", "error"),
    [
        ({"error": "data faulty"}, ComexioRequestRejectedError),
        ({}, ComexioDataError),
        ({"id": None}, ComexioDataError),
        ({"id": "neu"}, ComexioDataError),
        ({"id": True}, ComexioDataError),
        ({"id": 1.9}, ComexioDataError),
        ({"id": "1.0"}, ComexioDataError),
        ({"id": "-3"}, ComexioDataError),
        ({"id": -3}, ComexioDataError),
        ({"id": 0}, ComexioDataError),
        ({"id": "0"}, ComexioDataError),
        ({"id": "²"}, ComexioDataError),
        (["55"], ComexioDataError),
    ],
)
async def test_add_function_plan_element_without_id_raises(
    logged_in: ComexioClient, comexio: FakeComexio, answer: Any, error: type[Exception]
) -> None:
    comexio.serve_json("POST", ADD_ELEMENT_PATH, answer)

    with pytest.raises(error):
        await logged_in.add_function_plan_element(4, 271, 2, x=0, y=0)


async def test_add_function_plan_constant(logged_in: ComexioClient, comexio: FakeComexio) -> None:
    comexio.serve_json("POST", ADD_ELEMENT_PATH, {"id": 57})

    assert await logged_in.add_function_plan_constant(4, "42.5", x=10, y=20) == 57

    (request,) = comexio.received_at("POST", ADD_ELEMENT_PATH)
    assert (request.form["name"], request.form["ref_id"], request.form["type"], request.form["id"]) == (
        "42.5",
        "1",
        "16",
        "undefined",
    )


async def test_add_function_plan_comment(logged_in: ComexioClient, comexio: FakeComexio) -> None:
    comexio.serve_json("POST", ADD_ELEMENT_PATH, {"id": 58})

    assert await logged_in.add_function_plan_comment(4, "Von HA verwaltet", x=100, y=7.5) == 58

    (request,) = comexio.received_at("POST", ADD_ELEMENT_PATH)
    assert (request.form["name"], request.form["ref_id"], request.form["type"], request.form["id"]) == (
        "Von HA verwaltet",
        "3",
        "14",
        "0",
    )
    assert comexio.received_at("POST", SAVE_COMMENT_PATH) == []


async def test_save_function_plan_comment(logged_in: ComexioClient, comexio: FakeComexio) -> None:
    comexio.serve_json("POST", SAVE_COMMENT_PATH, {"result": 1, "data": {}})

    await logged_in.save_function_plan_comment(58, "Grüße", width=5)

    (request,) = comexio.received_at("POST", SAVE_COMMENT_PATH)
    assert request.form == {
        "id": "58",
        "use_base_64": "1",
        "name": base64.b64encode("Grüße".encode()).decode(),
        "width": "5",
    }


@pytest.mark.parametrize("width", [0, 6, True, 2.0])
async def test_save_function_plan_comment_invalid_width(
    logged_in: ComexioClient, comexio: FakeComexio, width: int
) -> None:
    with pytest.raises(ValueError, match="width"):
        await logged_in.save_function_plan_comment(58, "x", width=width)
    assert comexio.received_at("POST", SAVE_COMMENT_PATH) == []


async def test_save_function_plan_comment_unconfirmed_is_rejected(
    logged_in: ComexioClient, comexio: FakeComexio
) -> None:
    comexio.serve_json("POST", SAVE_COMMENT_PATH, {"result": 0})

    with pytest.raises(ComexioRequestRejectedError):
        await logged_in.save_function_plan_comment(58, "x", width=3)


async def test_save_function_plan_connection(logged_in: ComexioClient, comexio: FakeComexio) -> None:
    comexio.serve_json("POST", SAVE_CONNECTION_PATH, {"id": "31"})

    conn_id = await logged_in.save_function_plan_connection(
        4, 55, [(56, 0, False), (57, 1, True)], value_type="analog", source_pos=2, source_inverted=True
    )

    assert conn_id == 31
    (request,) = comexio.received_at("POST", SAVE_CONNECTION_PATH)
    assert re.fullmatch(_TIMESTAMP_RE, request.form["timestamp"])
    assert json.loads(request.form["JSON"]) == {
        "id": "new",
        "fub_id": 4,
        "input": {"element": "55", "pos": "2", "inverted": True},
        "type": "analog",
        "output": {
            "0": {"element": "56", "pos": "0", "inverted": False},
            "1": {"element": "57", "pos": "1", "inverted": True},
        },
    }


async def test_save_function_plan_connection_resaves_an_existing_connection(
    logged_in: ComexioClient, comexio: FakeComexio
) -> None:
    comexio.serve_json("POST", SAVE_CONNECTION_PATH, {"id": 31})

    await logged_in.save_function_plan_connection(4, 55, [(56, 0, False)], value_type="binary", connection_id=31)

    (request,) = comexio.received_at("POST", SAVE_CONNECTION_PATH)
    assert json.loads(request.form["JSON"])["id"] == "31"


@pytest.mark.parametrize(
    ("sinks", "value_type", "match"), [([], "binary", "sink"), ([(56, 0, False)], "digital", "value_type")]
)
async def test_save_function_plan_connection_invalid_arguments(
    logged_in: ComexioClient, comexio: FakeComexio, sinks: list[tuple[int, int, bool]], value_type: str, match: str
) -> None:
    with pytest.raises(ValueError, match=match):
        await logged_in.save_function_plan_connection(4, 55, sinks, value_type=value_type)
    assert comexio.received_at("POST", SAVE_CONNECTION_PATH) == []


async def test_save_function_plan_connection_refused(logged_in: ComexioClient, comexio: FakeComexio) -> None:
    comexio.serve_json("POST", SAVE_CONNECTION_PATH, {"error": "incompatible_types"})

    with pytest.raises(ComexioRequestRejectedError, match="incompatible_types"):
        await logged_in.save_function_plan_connection(4, 55, [(56, 0, False)], value_type="binary")


async def test_move_function_plan_elements(logged_in: ComexioClient, comexio: FakeComexio) -> None:
    comexio.serve_json("POST", SAVE_POSITIONS_PATH, {"result": 1})

    await logged_in.move_function_plan_elements([(55, 15.0, 30.0), (56, 45, 60)])

    (request,) = comexio.received_at("POST", SAVE_POSITIONS_PATH)
    assert json.loads(request.form["Json"]) == {
        "0": {"x": 15.0, "y": 30.0, "id": 55},
        "1": {"x": 45, "y": 60, "id": 56},
    }
    assert re.fullmatch(_TIMESTAMP_RE, request.form["timestamp"])


async def test_move_function_plan_elements_unconfirmed_is_rejected(
    logged_in: ComexioClient, comexio: FakeComexio
) -> None:
    comexio.serve_json("POST", SAVE_POSITIONS_PATH, {"result": 0})

    with pytest.raises(ComexioRequestRejectedError):
        await logged_in.move_function_plan_elements([(55, 0, 0)])


_SETTINGS = {"name": "P", "comment": "", "active": False, "paper_format": "A4", "orientation": "landscape", "dpi": 90}


@pytest.mark.parametrize(
    "call",
    [
        lambda c: c.update_function_plan(4.0, position=0, **_SETTINGS),
        lambda c: c.update_function_plan(4, position=True, **_SETTINGS),
        lambda c: c.update_function_plan(4, position=0, **{**_SETTINGS, "active": "false"}),
        lambda c: c.update_function_plan(4, position=0, reset_outputs_on_stop=1, **_SETTINGS),
        lambda c: c.update_function_plan(4, position=0, **{**_SETTINGS, "name": None}),
        lambda c: c.delete_function_plan("4"),
        lambda c: c.run_function_plan(True),
        lambda c: c.stop_function_plan(None),
        lambda c: c.add_function_plan_element(4, 271.5, 2, x=0, y=0),
        lambda c: c.add_function_plan_element(4, 271, "2", x=0, y=0),
        lambda c: c.add_function_plan_constant(4.7, "1", x=0, y=0),
        lambda c: c.add_function_plan_comment(4, "t", x="10", y=0),
        lambda c: c.add_function_plan_comment(4, "t", x=0, y=True),
        lambda c: c.add_function_plan_comment(4, "t", x=float("nan"), y=0),
        lambda c: c.add_function_plan_comment(4, None, x=0, y=0),
        lambda c: c.add_function_plan_constant(4, True, x=0, y=0),
        lambda c: c.save_function_plan_comment(12, None, width=2),
        lambda c: c.save_function_plan_comment("12", "t", width=2),
        lambda c: c.save_function_plan_connection(4, 4.7, [(56, 0, False)], value_type="binary"),
        lambda c: c.save_function_plan_connection(4, 55, [(56, 0, False)], value_type="binary", source_pos=None),
        lambda c: c.save_function_plan_connection(4, 55, [(56, 0, False)], value_type="binary", connection_id="31"),
        lambda c: c.save_function_plan_connection(4, 55, [(56, 0, False)], value_type="binary", source_inverted=1),
        lambda c: c.save_function_plan_connection(4, 55, ["560"], value_type="binary"),
        lambda c: c.save_function_plan_connection(4, 55, [(56, 0)], value_type="binary"),
        lambda c: c.save_function_plan_connection(4, 55, [(True, 0, False)], value_type="binary"),
        lambda c: c.save_function_plan_connection(4, 55, [(56, 0, 0)], value_type="binary"),
        lambda c: c.move_function_plan_elements([(55, 10, 20), (56.0, 10, 20)]),
        lambda c: c.move_function_plan_elements([(55, "10", 20)]),
        lambda c: c.move_function_plan_elements([[55, 10, 20]]),
        lambda c: c.move_function_plan_elements([(55, 10, float("inf"))]),
        lambda c: c.move_function_plan_elements([(55, 10, float("nan"))]),
        lambda c: c.delete_function_plan_elements([55, 4.7]),
        lambda c: c.delete_function_plan_elements([True]),
    ],
)
async def test_write_primitives_refuse_mistyped_ids_before_any_request(
    logged_in: ComexioClient, comexio: FakeComexio, call: PlanCall
) -> None:
    # A float or bool id would go out as "4.7" / "True" instead of failing on the caller's side.
    sent = len(comexio.received)

    with pytest.raises(TypeError):
        await call(logged_in)
    assert len(comexio.received) == sent


async def test_generator_arguments_are_not_used_up_by_the_checks(
    logged_in: ComexioClient, comexio: FakeComexio
) -> None:
    # Validating a generator consumes it; an empty output would strip every wire of the pin.
    comexio.serve_json("POST", SAVE_CONNECTION_PATH, {"id": "31"})
    comexio.serve_json("POST", DELETE_ELEMENTS_PATH, {"delete": True})

    sinks = ((sink, 0, False) for sink in (56, 57))
    await logged_in.save_function_plan_connection(4, 55, sinks, value_type="binary")  # type: ignore[arg-type]
    await logged_in.delete_function_plan_elements(element_id for element_id in (55, 56))  # type: ignore[arg-type]

    (connection,) = comexio.received_at("POST", SAVE_CONNECTION_PATH)
    assert list(json.loads(connection.form["JSON"])["output"]) == ["0", "1"]
    (deletion,) = comexio.received_at("POST", DELETE_ELEMENTS_PATH)
    assert json.loads(deletion.form["Json"]) == ["55", "56"]


@pytest.mark.parametrize("ids", ["42", b"42", bytearray(b"42"), memoryview(b"42"), {55: None}])
async def test_delete_function_plan_elements_refuses_a_string(
    logged_in: ComexioClient, comexio: FakeComexio, ids: Any
) -> None:
    # A string is a Sequence too: "42" would delete elements 4 and 2.
    with pytest.raises(TypeError):
        await logged_in.delete_function_plan_elements(ids)
    assert comexio.received_at("POST", DELETE_ELEMENTS_PATH) == []


async def test_delete_function_plan_elements(logged_in: ComexioClient, comexio: FakeComexio) -> None:
    comexio.serve_json("POST", DELETE_ELEMENTS_PATH, {"delete": True})

    await logged_in.delete_function_plan_elements([55, 56])

    (request,) = comexio.received_at("POST", DELETE_ELEMENTS_PATH)
    assert json.loads(request.form["Json"]) == ["55", "56"]


@pytest.mark.parametrize("answer", [{"delete": False}, {"delete": 1}, {"error": "no_delete_active_connection"}])
async def test_delete_function_plan_elements_unconfirmed_is_rejected(
    logged_in: ComexioClient, comexio: FakeComexio, answer: dict[str, Any]
) -> None:
    comexio.serve_json("POST", DELETE_ELEMENTS_PATH, answer)

    with pytest.raises(ComexioRequestRejectedError):
        await logged_in.delete_function_plan_elements([55])


@pytest.mark.parametrize(
    "call",
    [
        lambda client: client.move_function_plan_elements([]),
        lambda client: client.delete_function_plan_elements([]),
    ],
)
async def test_empty_element_lists_are_refused_before_sending(
    logged_in: ComexioClient, comexio: FakeComexio, call: PlanCall
) -> None:
    with pytest.raises(ValueError, match="No element"):
        await call(logged_in)
    assert comexio.received_at("POST", SAVE_POSITIONS_PATH) == comexio.received_at("POST", DELETE_ELEMENTS_PATH) == []


# --- lapsed admin session ------------------------------------------------------------------------

_PLAN_JSON_CALLS: list[tuple[str, PlanCall]] = [
    (PLAN_RUN_PATH, lambda client: client.run_function_plan(4, _PLAN)),
    (PLAN_STOP_PATH, lambda client: client.stop_function_plan(4)),
    (ADD_ELEMENT_PATH, lambda client: client.add_function_plan_element(4, 1, 2, x=0, y=0)),
    (ADD_ELEMENT_PATH, lambda client: client.add_function_plan_constant(4, "1", x=0, y=0)),
    (ADD_ELEMENT_PATH, lambda client: client.add_function_plan_comment(4, "x", x=0, y=0)),
    (SAVE_COMMENT_PATH, lambda client: client.save_function_plan_comment(5, "x", width=5)),
    (
        SAVE_CONNECTION_PATH,
        lambda client: client.save_function_plan_connection(4, 5, [(6, 0, False)], value_type="binary"),
    ),
    (SAVE_POSITIONS_PATH, lambda client: client.move_function_plan_elements([(5, 0, 0)])),
    (DELETE_ELEMENTS_PATH, lambda client: client.delete_function_plan_elements([5])),
]


@pytest.mark.parametrize(("path", "call"), _PLAN_JSON_CALLS)
async def test_login_form_instead_of_json_raises_authentication_error(
    logged_in: ComexioClient, comexio: FakeComexio, path: str, call: PlanCall
) -> None:
    comexio.serve_text("POST", path, LOGIN_PAGE)

    with pytest.raises(ComexioAuthenticationError):
        await call(logged_in)
