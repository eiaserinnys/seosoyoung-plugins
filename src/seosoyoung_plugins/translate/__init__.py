"""Translate plugin package.

Re-exports from the translator modules for backward compatibility.
"""

from seosoyoung_plugins.translate.detector import detect_language, Language
from seosoyoung_plugins.translate.translator import translate
from seosoyoung_plugins.translate.glossary import GlossaryMatchResult

__all__ = ["detect_language", "Language", "translate", "GlossaryMatchResult"]
