"""Restricted, one-request iTerm2 API bridge; the optional SDK is loaded lazily."""

import asyncio
import contextlib
import importlib
import io
import json
import re
import sys


LAYOUT = "Status Bar Layout"
ENABLED = "Show Status Bar"
PROFILE_KEYS = (LAYOUT, ENABLED)
REQUEST_TIMEOUT = 6.0
_SESSION_ID = re.compile(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}\Z")


class InvalidRequest(ValueError):
    pass


def validate_request(request):
    """Reject implicit targets and unsupported operations before connecting."""
    if not isinstance(request, dict) or set(request) - {"action", "session_id", "properties"}:
        raise InvalidRequest("Expected action, exact session_id, and optional properties")
    action = request.get("action")
    if action not in ("get", "apply", "clear", "identity"):
        raise InvalidRequest("Action must be get, apply, clear, or identity")
    identifier = request.get("session_id")
    if not isinstance(identifier, str) or not _SESSION_ID.fullmatch(identifier):
        raise InvalidRequest("session_id must be an exact UUID")
    properties = request.get("properties", {})
    if not isinstance(properties, dict) or set(properties) - set(PROFILE_KEYS):
        raise InvalidRequest("Only Status Bar Layout and Show Status Bar are allowed")
    if action != "apply" and properties:
        raise InvalidRequest("Properties are only accepted by apply")
    if action == "apply" and not properties:
        raise InvalidRequest("apply requires at least one allowed property")
    if LAYOUT in properties and properties[LAYOUT] is not None and not isinstance(properties[LAYOUT], dict):
        raise InvalidRequest("Status Bar Layout must be an object or null")
    if ENABLED in properties and type(properties[ENABLED]) is not bool:
        raise InvalidRequest("Show Status Bar must be a boolean")
    try:
        json.dumps(properties, allow_nan=False)
    except (TypeError, ValueError):
        raise InvalidRequest("Properties must contain finite JSON values") from None
    return action, identifier, properties


def _check_response(api, message, field, response_type):
    response = getattr(message, field)
    if response.status != getattr(api.api_pb2, response_type).OK:
        raise RuntimeError("iTerm2 rejected the targeted request")
    return response


async def _get_profile(api, connection, identifier):
    message = await api.rpc.async_get_profile(connection, session=identifier, keys=PROFILE_KEYS)
    response = _check_response(api, message, "get_profile_property_response", "GetProfilePropertyResponse")
    values = {}
    for prop in response.properties:
        if prop.key not in PROFILE_KEYS or prop.key in values:
            raise RuntimeError("Unexpected profile response")
        values[prop.key] = json.loads(prop.json_value)
    if set(values) != set(PROFILE_KEYS):
        raise RuntimeError("Incomplete profile response")
    # iTerm2 may serialize the native checkbox NSNumber as JSON 0/1 rather
    # than false/true. Normalize only this top-level checkbox on readback.
    if type(values[ENABLED]) is int and values[ENABLED] in (0, 1):
        values[ENABLED] = bool(values[ENABLED])
    # Apply the same type boundary to readback before exposing or reusing it.
    validate_request({"action": "apply", "session_id": identifier, "properties": values})
    return {"layout": values[LAYOUT], "enabled": values[ENABLED]}


async def _operate(api, connection, action, identifier, properties):
    if action == "identity":
        message = await api.rpc.async_variable(
            connection, session_id=identifier, gets=["session.autoNameFormat"])
        response = _check_response(api, message, "variable_response", "VariableResponse")
        if len(response.values) != 1:
            raise RuntimeError("Unexpected session identity response")
        title = json.loads(response.values[0])
        if title is not None and not isinstance(title, str):
            raise RuntimeError("Unexpected session identity type")
        return {"native_title": title}
    if action == "clear":
        message = await api.rpc.async_variable(
            connection, session_id=identifier, sets=[("user.codexMonitor", json.dumps(""))])
        _check_response(api, message, "variable_response", "VariableResponse")
        return {"cleared": True}
    if action == "apply":
        assignments = [(key, json.dumps(properties[key], allow_nan=False))
                       for key in PROFILE_KEYS if key in properties]
        message = await api.rpc.async_set_profile_properties_json(
            connection, session_id=identifier, assignments=assignments)
        _check_response(api, message, "set_profile_property_response", "SetProfilePropertyResponse")
    return await _get_profile(api, connection, identifier)


async def _close(connection):
    # async_create starts this dispatcher but the SDK has no public close method.
    # Cancel it before closing the websocket so closure cannot print a traceback.
    dispatcher = getattr(connection, "_Connection__dispatch_forever_future", None)
    tasks = list(getattr(connection, "_Connection__tasks", ()))
    if dispatcher is not None:
        tasks.append(dispatcher)
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    websocket = getattr(connection, "websocket", None)
    if websocket is not None:
        await asyncio.wait_for(websocket.close(), timeout=0.5)


async def _run(api, action, identifier, properties, timeout):
    connection = None

    async def work():
        nonlocal connection
        # SDK authentication defaults to launch_if_needed=False. This does not
        # enable the API, launch the app, or obtain an application/session list.
        connection = await api.Connection.async_create()
        return await _operate(api, connection, action, identifier, properties)

    try:
        return await asyncio.wait_for(work(), timeout=timeout)
    finally:
        if connection is not None:
            with contextlib.suppress(Exception, asyncio.CancelledError):
                await _close(connection)


def handle_request(request, api=None, timeout=REQUEST_TIMEOUT):
    """Return one safe JSON-ready result, without leaking SDK diagnostics."""
    try:
        action, identifier, properties = validate_request(request)
    except InvalidRequest as error:
        return {"ok": False, "error": str(error)}
    try:
        # The SDK may print authentication/connection diagnostics. Do not expose
        # those, exception strings, cookies, or keys in this bridge's output.
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            if api is None:
                api = importlib.import_module("iterm2")
            result = asyncio.run(_run(api, action, identifier, properties, timeout))
        return {"ok": True, "result": result}
    except (ImportError, ModuleNotFoundError):
        return {"ok": False, "error": "Optional iterm2 Python package is unavailable"}
    except asyncio.TimeoutError:
        return {"ok": False, "error": "iTerm2 API request timed out"}
    except (Exception, SystemExit):
        return {"ok": False, "error": "iTerm2 API unavailable or targeted request failed"}


def main(stdin=None, stdout=None):
    stdin = sys.stdin if stdin is None else stdin
    stdout = sys.stdout if stdout is None else stdout
    try:
        request = json.load(stdin)
    except (ValueError, TypeError):
        result = {"ok": False, "error": "Expected a JSON request"}
    else:
        result = handle_request(request)
    stdout.write(json.dumps(result, allow_nan=False) + "\n")


if __name__ == "__main__":
    main()
