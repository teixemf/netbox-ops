"""JSON language catalogues used by the interactive menu and CLI messages."""
from __future__ import annotations

import json
import os
from pathlib import Path

LANGUAGES = ('pt', 'en')
CATALOGUE_DIR = Path(__file__).resolve().parent / 'i18n'


def detect_language() -> str:
    """Use POSIX message locale precedence; unsupported locales use English."""
    for key in ('LC_ALL', 'LC_MESSAGES', 'LANG'):
        value = os.environ.get(key, '').strip()
        if value:
            language = value.split('.', 1)[0].split('@', 1)[0]
            language = language.replace('-', '_').split('_', 1)[0].lower()
            return language if language in LANGUAGES else 'en'
    return 'en'


class Translator:
    def __init__(self, language: str = 'pt'):
        if language not in LANGUAGES:
            raise ValueError(f'Unsupported language: {language}')
        self.language = language
        path = CATALOGUE_DIR / f'{language}.json'
        self.messages = json.loads(path.read_text(encoding='utf-8'))
        self.fallback = (json.loads((CATALOGUE_DIR / 'en.json').read_text(encoding='utf-8'))
                         if language != 'en' else self.messages)

    def __call__(self, key: str, **params: object) -> str:
        template = self.messages.get(key, self.fallback.get(key, f'<{key}>'))
        return template.format(**params)
