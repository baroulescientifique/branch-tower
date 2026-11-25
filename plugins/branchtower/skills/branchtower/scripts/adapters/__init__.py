import os

from common import require


def validate(value, schema, location="response"):
    kind = schema["type"]
    expected = {"object": dict, "array": list, "string": str}[kind]
    require(isinstance(value, expected), f"{location} must be a JSON {kind}.")
    if "enum" in schema:
        require(value in schema["enum"], f"Unexpected value in {location}.")
    if kind == "object":
        keys = set(value)
        require(set(schema["required"]) == keys, f"Unexpected or missing fields in {location}.")
        for key, child in schema["properties"].items():
            validate(value[key], child, f"{location}.{key}")
    if kind == "array":
        for index, item in enumerate(value):
            validate(item, schema["items"], f"{location}[{index}]")


def environment():
    return {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
