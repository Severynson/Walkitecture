from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel


class CamelModel(BaseModel):
    """Base model: snake_case in Python, camelCase on the wire (both accepted on input)."""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)
