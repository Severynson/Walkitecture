from app.config import Settings
from app.schemas.parse import ParseOptions
from app.services.classify import LayerPatterns


def layer_patterns(settings: Settings, options: ParseOptions | None = None) -> LayerPatterns:
    """Layer-name patterns from settings, with per-request overrides from parse options."""

    def pick(name: str) -> tuple[str, ...]:
        override = getattr(options, f"{name}_layers", None) if options else None
        return tuple(override or getattr(settings, f"{name}_layers"))

    return LayerPatterns(
        wall=pick("wall"),
        window=pick("window"),
        door=pick("door"),
        opening=pick("opening"),
        room=pick("room"),
        furniture=pick("furniture"),
    )
