"""Errors that the command-line interface can present without a traceback."""


class ExportError(Exception):
    """The requested extraction could not be completed."""
