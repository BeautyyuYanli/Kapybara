"""Authentication identities carried by the local execution proxy."""

from typing import Literal, TypedDict


class SessionProxyAuth(TypedDict):
    kind: Literal["session"]
    session_id: str
    token: str


class UserProxyAuth(TypedDict):
    kind: Literal["user"]
    token: str


type ProxyAuth = SessionProxyAuth | UserProxyAuth
