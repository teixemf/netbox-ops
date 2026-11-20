"""JSON language catalogues used by the interactive menu and CLI messages."""
from __future__ import annotations

import json
from pathlib import Path

LANGUAGES = ('pt', 'en')
CATALOGUE_DIR = Path(__file__).resolve().parent / 'i18n'


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
