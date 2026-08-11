"""Generic web page extraction for open web URLs."""

from .collector import collect_url
from .dynamic import collect_dynamic_url
from .models import GenericWebRequest, GenericWebResponse

__all__ = ["GenericWebRequest", "GenericWebResponse", "collect_url", "collect_dynamic_url"]
