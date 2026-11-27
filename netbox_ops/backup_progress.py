"""Explicit backup task plans and observed execution states."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from typing import Callable, Iterable


ProgressCallback = Callable[[str, int, int, str], None]


@dataclass(frozen=True)
class BackupTask:
    phase: str
    detail: str = ''
    applicable: bool = True

    @property
    def key(self):
        return self.phase, self.detail


@dataclass(frozen=True)
class BackupTaskEvent:
    task: BackupTask
    state: str
    done: int
    total: int


TaskCallback = Callable[[BackupTaskEvent], None]


def create_tasks(kind: str, *, quiesce: bool = True) -> tuple[BackupTask, ...]:
    combined = kind == 'combined'
    return (
        BackupTask('preflight', applicable=quiesce),
        BackupTask('stop_services', applicable=combined and quiesce),
        BackupTask('prepare'),
        BackupTask('dump_db', applicable=kind in ('db', 'combined')),
        BackupTask('archive_media', applicable=kind in ('media', 'combined')),
        BackupTask('verify', applicable=kind in ('media', 'combined')),
        BackupTask('finalize'),
        BackupTask('restart_services', applicable=combined and quiesce),
    )


def restore_tasks(kind: str, only: str) -> tuple[BackupTask, ...]:
    combined = kind in ('combined', 'full')
    database = kind == 'db' or (combined and only in ('db', 'all'))
    media = kind == 'media' or (combined and only in ('media', 'all'))
    return (
        BackupTask('prepare'),
        BackupTask('verify'),
        BackupTask('extract_media', applicable=combined or media),
        BackupTask('stage_db', applicable=database),
        BackupTask('verify_application', applicable=database),
        BackupTask('stop_services'),
        BackupTask('safety_backup'),
        BackupTask('replace'),
        BackupTask('restart_services'),
        BackupTask('finalize'),
    )


def batch_tasks(phase: str, names: Iterable[str], *, prepare: bool = False) -> tuple[BackupTask, ...]:
    return ((BackupTask('prepare'),) if prepare else ()) + tuple(
        BackupTask(phase, name) for name in names)


class BackupProgress:
    """Emit observed transitions; legacy callbacks retain four arguments.

    Counts are tasks, excluding non-applicable/skipped tasks. Recovery work
    is added only when attempted. Complete is emitted only on normal exit
    from the operation (including required cleanup and lock release).
    """

    def __init__(self, tasks: Iterable[BackupTask], progress: ProgressCallback | None = None,
                 events: TaskCallback | None = None):
        self.tasks = {task.key: task for task in tasks}
        self.states = {key: 'pending' if task.applicable else 'not_applicable'
                       for key, task in self.tasks.items()}
        self.progress = progress
        self.events = events

    @property
    def counts(self) -> tuple[int, int]:
        return (sum(state == 'done' for state in self.states.values()),
                sum(state not in ('not_applicable', 'skipped') for state in self.states.values()))

    def _emit(self, key):
        if self.events is None and self.progress is None:
            return
        state = self.states[key]
        done, total = self.counts
        if self.events is not None:
            self.events(BackupTaskEvent(self.tasks[key], state, done, total))
        if self.progress is not None and state in ('running', 'done', 'skipped'):
            self.progress(key[0], done, total, key[1])

    def start(self, phase: str, detail: str = '') -> None:
        key = (phase, detail)
        if key not in self.tasks:
            self.tasks[key] = BackupTask(phase, detail)
            self.states[key] = 'pending'
        self.states[key] = 'running'
        self._emit(key)

    def done(self, phase: str, detail: str = '', *, skipped: bool = False) -> None:
        key = (phase, detail)
        self.states[key] = 'skipped' if skipped else 'done'
        self._emit(key)

    def fail_active(self) -> None:
        for key, state in tuple(self.states.items()):
            if state == 'running':
                self.states[key] = 'failed'
                self._emit(key)

    @contextmanager
    def step(self, phase: str, detail: str = ''):
        self.start(phase, detail)
        try:
            yield
        except BaseException:
            self.fail_active()
            raise
        else:
            self.done(phase, detail)

    @contextmanager
    def operation(self):
        for key in self.tasks:
            self._emit(key)
        succeeded = False
        try:
            yield
            succeeded = True
        finally:
            self.fail_active()
            for key, state in tuple(self.states.items()):
                if state == 'pending':
                    self.states[key] = 'not_run'
                    self._emit(key)
            if succeeded and self.progress is not None:
                done, total = self.counts
                self.progress('complete', done, total, '')
