"""Native input validation helpers shared by every route that takes a payload.

Routes used to do ``request.get_json(silent=True) or {}`` and then call
``.get`` / ``.strip`` on whatever came back, so a body of ``[1]`` or a field of
``123`` raised ``AttributeError`` and surfaced as a 500. These helpers make the
shape guarantees explicit, so malformed input is a plain "missing/invalid
field" 400 instead of a crash.
"""
from flask import request


def json_object():
    """The request's JSON body if it is an object, else ``{}``.

    A list, string, number or unparseable body all become ``{}`` so callers can
    keep calling ``.get`` and fall through to their normal "field required"
    error.
    """
    body = request.get_json(silent=True)
    return body if isinstance(body, dict) else {}


def str_field(data, key, max_len=None, default=""):
    """``data[key]`` as a stripped string, whatever type the client sent.

    ``None`` and absent keys give ``default``. Non-strings (numbers, lists,
    objects) are rejected to ``default`` rather than stringified, so a client
    cannot smuggle ``["a"]`` in as the name ``"['a']"``.
    """
    value = data.get(key)
    if not isinstance(value, str):
        return default
    value = value.strip()
    return value[:max_len] if max_len else value


def page_args(default_limit=500, max_limit=1000):
    """``(limit, offset)`` from ``?limit=&offset=`` — always bounded.

    List endpoints are capped even when the client sends nothing, so a table
    that grows without anyone noticing cannot turn one request into a
    multi-megabyte response. Malformed or negative values fall back to the
    defaults instead of raising.
    """
    def _int(name, default):
        try:
            return int(request.args.get(name, default))
        except (TypeError, ValueError):
            return default

    limit = min(max(_int("limit", default_limit), 1), max_limit)
    offset = max(_int("offset", 0), 0)
    return limit, offset
