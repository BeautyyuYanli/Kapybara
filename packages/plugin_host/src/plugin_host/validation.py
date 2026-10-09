"""Local schema validation for hooks that replace an Agent's tool arguments."""

from copy import deepcopy
from typing import Annotated, Any

from jsonschema import Draft202012Validator
from pydantic import AfterValidator, ConfigDict, JsonValue, TypeAdapter
from referencing import Registry, Resource
from referencing.exceptions import Unresolvable
from referencing.jsonschema import DRAFT202012

from .rpc import json_value


def args_adapter(schema: dict[str, Any]) -> TypeAdapter[dict[str, JsonValue]]:
    """Validate JSON object arguments against a local Draft 2020-12 schema.

    Arguments must be objects; the schema may describe them through a root ref.
    Pydantic converts JSON Schema failures into ``ValidationError``, allowing
    the SDK's normal validation hooks and retry budget to handle invalid args.
    External references are forbidden so validation never performs network I/O.
    """
    Draft202012Validator.check_schema(schema)
    if "$schema" in schema and schema["$schema"] != Draft202012Validator.META_SCHEMA["$id"]:
        raise ValueError("tool parameter schema must use JSON Schema Draft 2020-12")
    schema = deepcopy(schema)
    resource = Resource.from_contents(schema, default_specification=DRAFT202012)
    registry = Registry().with_resource("", resource).crawl()
    try:
        _check_refs(resource, registry.resolver().in_subresource(resource), set())
    except (Unresolvable, TypeError) as error:
        raise ValueError("unresolvable document-internal schema reference") from error
    validator = Draft202012Validator(schema, registry=registry)

    def validate(value: dict[str, JsonValue]) -> dict[str, JsonValue]:
        # JsonValue's JSON parser accepts nonfinite numeric tokens; check the
        # resulting Python values before the SDK can pass args to execution.
        json_value(value)
        if error := next(validator.iter_errors(value), None):
            raise ValueError(error.message)
        return value

    return TypeAdapter(
        Annotated[dict[str, JsonValue], AfterValidator(validate)],
        config=ConfigDict(strict=True, allow_inf_nan=False),
    )


def _check_refs(resource: Resource[Any], resolver: Any, seen: set[int]) -> None:
    """Check schema resources and reference targets, skipping unreferenced data.

    A reference can turn an annotation's data into an executable schema. Visit
    that target as well, but inspect each document node once to bound cycles.
    """
    identity = id(resource.contents)
    if identity in seen:
        return
    seen.add(identity)
    if isinstance(resource.contents, dict):
        for keyword in ("$ref", "$dynamicRef"):
            if keyword in resource.contents:
                ref = resource.contents[keyword]
                if ref and not ref.startswith("#"):
                    raise ValueError("only document-internal schema references are supported")
                resolved = resolver.lookup(ref)
                Draft202012Validator.check_schema(resolved.contents)
                target = Resource.from_contents(
                    resolved.contents, default_specification=DRAFT202012
                )
                _check_refs(target, resolved.resolver, seen)
    for child in resource.subresources():
        _check_refs(child, resolver.in_subresource(child), seen)
