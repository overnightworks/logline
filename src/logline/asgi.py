"""The ASGI types the middleware modules share."""

from collections.abc import Awaitable, Callable, MutableMapping
from typing import Any

__all__: list[str] = []

type Scope = MutableMapping[str, Any]
type Message = MutableMapping[str, Any]
type Receive = Callable[[], Awaitable[Message]]
type Send = Callable[[Message], Awaitable[None]]
type ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]
