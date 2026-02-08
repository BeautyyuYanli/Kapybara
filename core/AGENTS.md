## Python Coding Style

- Always use typed python, in latest typing style, e.g. `list[int]` instead of `List[int]`. Use `dataclass(slots=True)` for simple data containers and `pydantic.BaseModel` for data containers that require validation or serialization. Define fields of classes using type annotations before other methods.

