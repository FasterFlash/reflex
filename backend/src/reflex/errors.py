"""Errors the upload screen already has wording for. `code` matches its state names."""


class ReflexError(Exception):
    code = "error"


class NotAZip(ReflexError):
    code = "notzip"


class NoYouTubeData(ReflexError):
    code = "nodata"


class HtmlExport(ReflexError):
    code = "html"


class TooBig(ReflexError):
    code = "big"


class LookupFailed(ReflexError):
    """The video-facts lookup couldn't run (no key, key rejected, API down). Never carries the key."""
    code = "lookup"
