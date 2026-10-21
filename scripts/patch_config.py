"""Validated parameters shared by release download, packaging and recovery."""
from dataclasses import asdict, dataclass
import os
import re
import string
import urllib.parse


def validate_tag(value):
    if (not value or value.startswith(('-', '/')) or value.endswith(('/', '.'))
            or any(ord(c) <= 32 or ord(c) == 127 or c in '~^:?*[\\' for c in value)
            or '..' in value or '@{' in value
            or any(not p or p.startswith('.') or p.endswith('.lock') for p in value.split('/'))):
        raise ValueError(f'Invalid release tag: {value!r}')


def validate_relative(value):
    if value and (value.startswith(('/', '\\')) or '\\' in value or ':' in value
                  or any(p in ('', '.', '..') for p in value.split('/'))
                  or any(ord(c) < 32 for c in value)):
        raise ValueError('Entry file must be a relative path using forward slashes')


@dataclass(frozen=True)
class PatchConfig:
    old_tag: str
    new_tag: str
    source_repository: str = 'Kruk2/jasna'
    asset_pattern: str = 'jasna-windows-{version}.7z*'
    release_tag: str = ''
    entry_file: str = 'jasna.exe'
    volume_size_mb: int = 1000

    def __post_init__(self):
        if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', self.source_repository):
            raise ValueError('Source repository must be owner/repository')
        if any(p in ('.', '..') for p in self.source_repository.split('/')):
            raise ValueError('Invalid source repository')
        for tag in (self.old_tag, self.new_tag, self.tag):
            validate_tag(tag)
        if self.old_tag == self.new_tag:
            raise ValueError('Old and new tags must differ')
        validate_relative(self.entry_file)
        if type(self.volume_size_mb) is not int or not 1 <= self.volume_size_mb <= 2000:
            raise ValueError('Volume size must be 1..2000 decimal MB')
        if len(self.archive_stem) > 180:
            raise ValueError('Generated archive name is too long; choose a shorter release_tag')
        for tag in (self.old_tag, self.new_tag):
            self.pattern(tag)

    @property
    def tag(self):
        return self.release_tag or f'{self.new_tag}-{self.old_tag}.patch'

    @property
    def archive_stem(self):
        return re.sub(r'[^A-Za-z0-9_.-]', '_', self.tag)

    @property
    def volume_bytes(self):
        return self.volume_size_mb * 1_000_000

    @property
    def versions(self):
        return [(self.old_tag, 'old'), (self.new_tag, 'new')]

    def pattern(self, tag):
        try:
            for _, field, spec, conversion in string.Formatter().parse(self.asset_pattern):
                if field is not None and (field not in ('tag', 'version') or spec or conversion):
                    raise ValueError('Unsupported asset pattern field')
            result = self.asset_pattern.format(tag=tag, version=tag.removeprefix('v'))
        except (KeyError, IndexError, ValueError, AttributeError) as exc:
            raise ValueError('Asset pattern supports only {tag} and {version}') from exc
        if not result or '/' in result or '\\' in result or any(ord(c) < 32 for c in result):
            raise ValueError('Asset pattern must match filenames, not paths')
        return result

    def url(self, tag):
        return f'https://github.com/{self.source_repository}/releases/tag/{urllib.parse.quote(tag, safe="")}'

    def to_dict(self):
        values = asdict(self)
        values['release_tag'] = self.tag
        return values

    @classmethod
    def from_env(cls):
        values = {}
        for name in cls.__dataclass_fields__:
            raw = os.environ.get('PATCH_' + name.upper())
            if raw is not None:
                values[name] = int(raw) if name == 'volume_size_mb' else raw
        if not values.get('old_tag') or not values.get('new_tag'):
            raise ValueError('PATCH_OLD_TAG and PATCH_NEW_TAG are required')
        return cls(**values)


def config_for(ctx):
    return PatchConfig(**ctx['config'])
