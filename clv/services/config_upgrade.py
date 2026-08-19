"""Fold an operator's settings into a newer shipped template.

CLV's settings file is two thirds prose: every option is introduced by the
comment block explaining what it does. That prose is only ever written once, by
the first run that creates the file, so an operator who installed an old build
and upgraded ever since is reading documentation from whenever they started.
:func:`config.undocumented_settings` can *name* what they are missing and
``clv --print-default-config`` can print the reference, but neither closes the
gap in the file itself.

This module closes it, and is the one place in CLV that rewrites the operator's
settings file. That is a narrowing of the old rule rather than an abandonment of
it: **the launch path still never writes.** The write happens only when someone
explicitly asks for it -- ``clv --upgrade-config``, or running ``install.sh`` --
and only after the previous file has been copied aside, because the thing being
replaced can contain hours of an operator's tuning and every host they have
configured.

What survives a merge, and what does not:

* every value ``[log_viewer]`` set, written into the new template *in the new
  template's position*, keeping the new prose;
* every ``[ssh:<name>]`` section, copied byte-identical -- including one the
  parser rejects. A host with a bad port never reaches ``LogConfig.hosts``, so
  code that regenerated sections from parsed state would silently delete it;
* options this version no longer documents, kept under a banner rather than
  dropped, which is also what preserves a refused ``password =`` so it keeps
  producing its warning;
* the operator's *own* free-text comments inside ``[log_viewer]`` do **not**
  survive -- the new template is the base, and there is no way to know where a
  comment about an option should land among prose that has been rewritten. The
  backup is the recovery path, and it is why the backup is not optional in
  practice.
"""

from __future__ import annotations

import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .config import (
    CONFIG_SECTION,
    CONFIG_VERSION_OPTION,
    SSH_SECTION_PREFIX,
    _text_names_sources,
    config_version_of,
    default_config_text,
    template_config_version,
    user_config_path,
)
from .settings_file import SectionSpan, SettingsDocument, spans_of

#: Introduces the block of options the shipped template no longer defines.
CARRIED_BANNER = (
    "# --- Carried over from your previous settings file -------------------------",
    "# This version of CLV does not document these options. They were kept rather",
    "# than dropped; delete them if they are no longer wanted.",
)

#: ``settings.conf`` -> ``settings.conf.bak-20260818-142530``. Sortable, and it
#: does not collide with the ``.settings.conf.clv-tmp`` name ``save`` uses.
_BACKUP_TIME_FORMAT = "%Y%m%d-%H%M%S"


@dataclass(frozen=True, slots=True)
class UpgradeResult:
    """What :func:`upgrade_user_settings` did, in a form a CLI can print.

    ``status`` is one of:

    ``absent``
        There is no settings file. Nothing was written -- a first run creates
        one from this same template anyway, so there is nothing to upgrade.
    ``current``
        The file already carries this schema version. Nothing was written, and
        that includes not touching its mtime.
    ``upgraded``
        The file was replaced and ``backup_path`` names the copy of the old one.
    ``failed``
        Nothing was written and ``error`` says why.
    """

    status: str
    path: Path
    from_version: int
    to_version: int
    backup_path: Optional[Path] = None
    carried: tuple[str, ...] = ()
    hosts: tuple[str, ...] = ()
    error: str = ""

    @property
    def ok(self) -> bool:
        return self.status != "failed"


def upgrade_user_settings(
    path: Optional[Path] = None, *, backup: bool = True
) -> UpgradeResult:
    """Bring *path* (default: the user's settings file) up to the shipped schema.

    Idempotent: the merged file carries the new version marker, so a second call
    reports ``current`` and does not write.

    Every filesystem failure comes back as a ``failed`` result rather than an
    ``OSError``. This runs inside an installer, and an unreadable settings file
    is not a reason to abort an install of a program tree that is already
    correctly in place.
    """

    target = path if path is not None else user_config_path()
    latest = template_config_version()

    try:
        present = target.exists()
    except OSError as error:
        return UpgradeResult(
            "failed", target, 0, latest, error=f"could not stat: {error}"
        )

    if not present:
        return UpgradeResult("absent", target, 0, latest)

    current = config_version_of(target)
    if current >= latest:
        return UpgradeResult("current", target, current, latest)

    try:
        existing = SettingsDocument.load(target)
    except OSError as error:
        return UpgradeResult(
            "failed", target, current, latest, error=f"could not read: {error}"
        )

    # A file with two [log_viewer] headers is already reported as an issue by
    # ``config._read_parser``, and configparser's non-strict retry folds both
    # into one section -- so options in the second one are live. The merge only
    # ever reads the first, which would drop them without saying so. Refusing is
    # the only honest answer: this is exactly the silent loss the backup exists
    # to guard against, and here it is avoidable.
    duplicates = [
        span for span in spans_of(existing.lines) if span.name == CONFIG_SECTION
    ]
    if len(duplicates) > 1:
        return UpgradeResult(
            "failed",
            target,
            current,
            latest,
            error=(
                f"the file has {len(duplicates)} [{CONFIG_SECTION}] sections; "
                "combine them into one and run this again"
            ),
        )

    merged, carried, hosts = _merge(existing, latest)

    # The same guard ``ensure_user_settings_file`` applies to the template: a
    # settings file with no log_dirs is a viewer with nothing in it, and that is
    # a worse outcome than an out-of-date file that works.
    if not _text_names_sources(merged.text()):
        return UpgradeResult(
            "failed",
            target,
            current,
            latest,
            error="the merged file would name no log sources",
        )

    backup_path: Optional[Path] = None
    if backup:
        try:
            backup_path = _back_up(target)
        except OSError as error:
            # Never replace what could not be saved first.
            return UpgradeResult(
                "failed", target, current, latest, error=f"could not back up: {error}"
            )

    try:
        merged.save(target)
    except OSError as error:
        return UpgradeResult(
            "failed",
            target,
            current,
            latest,
            backup_path=backup_path,
            error=f"could not write: {error}",
        )

    return UpgradeResult(
        "upgraded",
        target,
        current,
        latest,
        backup_path=backup_path,
        carried=carried,
        hosts=hosts,
    )


def _merge(
    existing: SettingsDocument, latest: int
) -> tuple[SettingsDocument, tuple[str, ...], tuple[str, ...]]:
    """The shipped template, wearing the operator's values and hosts."""

    merged = SettingsDocument(default_config_text().splitlines())
    documented = set(merged.options(CONFIG_SECTION))

    carried: list[str] = []
    for option in existing.options(CONFIG_SECTION):
        if option == CONFIG_VERSION_OPTION:
            continue
        if option in documented:
            value = existing.get(CONFIG_SECTION, option)
            merged.set(CONFIG_SECTION, option, value if value is not None else "")
        else:
            carried.append(option)

    if carried:
        merged.insert_into_section(
            CONFIG_SECTION,
            [
                "",
                *CARRIED_BANNER,
                *existing.lines_assigning(CONFIG_SECTION, carried),
            ],
        )

    hosts: list[str] = []
    spans = spans_of(existing.lines)
    for position, span in enumerate(spans):
        if not span.name.startswith(SSH_SECTION_PREFIX):
            continue
        hosts.append(span.name[len(SSH_SECTION_PREFIX) :])
        merged.append_lines(_block(existing.lines, spans, position))

    merged.set(CONFIG_SECTION, CONFIG_VERSION_OPTION, str(latest))
    return merged, tuple(carried), tuple(hosts)


def _block(
    lines: list[str], spans: list[SectionSpan], position: int
) -> list[str]:
    """Everything the operator wrote under one section header, to the next one.

    Deliberately *not* ``SectionSpan.end``, which stops short of a trailing run
    of comments and blank lines. That rule is right for deciding where a new
    option should land -- it keeps an insert from landing under a banner that
    introduces the next section -- but copying a section out with it silently
    drops a comment the operator wrote at the bottom of their host.

    Whether such a comment "belongs" to this section or the next one is
    genuinely ambiguous, and it does not matter here: every section is copied in
    file order, so the comment ends up between the same two headers it started
    between either way. Only the trailing blank lines are dropped, because
    :meth:`SettingsDocument.append_lines` supplies its own separator.
    """

    span = spans[position]
    limit = spans[position + 1].header if position + 1 < len(spans) else len(lines)
    block = lines[span.header : limit]
    while block and not block[-1].strip():
        block.pop()
    return block


def _back_up(target: Path) -> Path:
    """Copy *target* aside, returning where it went.

    ``copy2`` rather than a rename: the original has to stay in place until the
    replacement is written, or a failure between the two leaves the operator
    with no settings file at all.
    """

    stamp = time.strftime(_BACKUP_TIME_FORMAT)
    candidate = target.with_name(f"{target.name}.bak-{stamp}")
    # Two upgrades in the same second is a test doing it, not an operator, but
    # silently overwriting the first backup would still be wrong.
    counter = 2
    while candidate.exists():
        candidate = target.with_name(f"{target.name}.bak-{stamp}-{counter}")
        counter += 1
    shutil.copy2(target, candidate)
    return candidate


def describe(result: UpgradeResult) -> str:
    """One human-readable line (or a short block) for *result*."""

    if result.status == "absent":
        return (
            f"No settings file at {result.path}; CLV will create one on first run."
        )
    if result.status == "current":
        return (
            f"{result.path} is already at settings schema v{result.to_version}; "
            "nothing to do."
        )
    if result.status == "failed":
        return f"Could not upgrade {result.path}: {result.error}"

    lines = [
        f"Upgraded {result.path} from settings schema "
        f"v{result.from_version} to v{result.to_version}.",
    ]
    if result.backup_path is not None:
        lines.append(f"  Previous file saved as {result.backup_path}")
    if result.hosts:
        lines.append(f"  Kept {len(result.hosts)} host(s): {', '.join(result.hosts)}")
    if result.carried:
        lines.append(
            f"  Carried over {len(result.carried)} option(s) this version does not "
            f"document: {', '.join(result.carried)}"
        )
    return "\n".join(lines)
